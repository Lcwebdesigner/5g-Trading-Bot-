import csv
import json
import math
import os
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from pathlib import Path
from collections import deque
from datetime import datetime, date

from deriv_connection import (
    Channel, DerivApiError, PUBLIC_WS_URL, check_app_id, get_ws_url,
    read_optional_line, read_token, redact,
    CONNECTED, CONNECTING, DISCONNECTED, FAILED, RETRYING,
)
import tradelog
from indicators import ema, ema_series, macd, rsi, evaluate_signal   # same rule the backtest tests

# ----------------------------------------------------------------------------------------------
# CONFIGURATION
# APP_ID: the ID of the app you registered at https://developers.deriv.com (Personal Access Token
# app). You can also set it without editing this file:  set DERIV_APP_ID=12345   (Windows cmd)
# ----------------------------------------------------------------------------------------------
APP_ID = "PASTE_YOUR_APP_ID_HERE"        # or, better, put the ID in deriv_app_id.txt so updates to main.py never erase it
APP_ID_FILE = Path(__file__).resolve().parent / "deriv_app_id.txt"
APP_ID = os.environ.get("DERIV_APP_ID") or read_optional_line(APP_ID_FILE) or APP_ID
SYMBOL = "R_100"
MARKET_WS_URL = PUBLIC_WS_URL            # public market data: no token, no OTP
AUTO_CONNECT_MARKET = True               # start the public tick feed when the window opens
STALE_TICK_TIMEOUT = 30                  # reconnect the feed if no tick arrives for this many seconds
TRADE_RESPONSE_TIMEOUT = 20              # give up on a proposal/purchase reply after this many seconds
TOKEN_FILE = Path(__file__).resolve().parent / "deriv_token.txt"
ACCOUNT_FILE = Path(__file__).resolve().parent / "deriv_account_id.txt"   # optional: demo account ID
REAL_ACCOUNT_FILE = Path(__file__).resolve().parent / "deriv_real_account_id.txt"   # optional: real account ID
RESULT_FILE = Path(__file__).resolve().parent / "five_tick_results.csv"
TICK_FILE = Path(__file__).resolve().parent / "ticks_R_100.csv"       # every tick received, for backtest.py
TRADE_LOG_FILE = Path(__file__).resolve().parent / "trade_log.csv"   # one row per settled contract
RECORD_TICKS = True
BOT_STATE_FILE = Path(__file__).resolve().parent / "demo_bot_state.json"
REAL_STATE_FILE = Path(__file__).resolve().parent / "real_bot_state.json"
MIN_HISTORY_TICKS = 100
CSV_LOCK = threading.Lock()

prices = deque(maxlen=500)
pending = []
closing = False
last_signal_queued = "WAIT"
last_auto_signal = "WAIT"
total_wins = total_losses = total_ties = 0
# Auto-trading controls: the bot refuses to trade unless the connection is a verified demo session.
auto_bot_running = False
account_currency = "USD"
account_info = {"id": None}
configured_stake = 2.0
active_trade = False
proposal_pending = False
trade_phase = None                 # "proposal" or "buy" while a request is in flight
trade_request_at = 0.0
current_contract_id = None
market_streaming = False
trades_today = 0
consecutive_losses = 0
state_date = date.today().isoformat()
state_lock = threading.Lock()
# Limits per mode. Demo keeps the original limits. REAL mode is deliberately stricter and is never the default.
DEMO_MAX_TRADES_PER_DAY = 10
DEMO_MAX_CONSECUTIVE_LOSSES = 3
REAL_MAX_TRADES_PER_DAY = 5
REAL_MAX_CONSECUTIVE_LOSSES = 2
REAL_MAX_STAKE = 5.00            # largest stake the bot accepts in REAL mode
REAL_MAX_DAILY_LOSS = 10.00      # the bot stops for the day once net losses reach this amount
REAL_CONFIRM_WORD = "REAL"
MAX_TRADES_PER_DAY = DEMO_MAX_TRADES_PER_DAY            # the ACTIVE limits; apply_mode_limits() swaps them
MAX_CONSECUTIVE_LOSSES = DEMO_MAX_CONSECUTIVE_LOSSES
max_stake = None
daily_loss_limit = None
last_recorded_epoch = 0
trade_meta = {}                  # direction and stake of the contract currently in flight
trading_mode = "demo"            # the app ALWAYS starts in demo; real mode is never saved or auto-restored
session_mode = None              # which kind of account the open WebSocket was verified for
pnl_today = 0.0

# Worker threads never touch Tk directly; they queue UI updates and the main thread applies them.
ui_queue = queue.SimpleQueue()


def state_path():
    return REAL_STATE_FILE if trading_mode == "real" else BOT_STATE_FILE


def mode_word():
    return "REAL" if trading_mode == "real" else "demo"


def running_label():
    return "RUNNING — REAL MONEY" if trading_mode == "real" else "RUNNING — DEMO ONLY"


def ready_label():
    return "READY — REAL VERIFIED" if trading_mode == "real" else "READY — DEMO VERIFIED"


def apply_mode_limits():
    global MAX_TRADES_PER_DAY, MAX_CONSECUTIVE_LOSSES, max_stake, daily_loss_limit
    if trading_mode == "real":
        MAX_TRADES_PER_DAY, MAX_CONSECUTIVE_LOSSES = REAL_MAX_TRADES_PER_DAY, REAL_MAX_CONSECUTIVE_LOSSES
        max_stake, daily_loss_limit = REAL_MAX_STAKE, REAL_MAX_DAILY_LOSS
    else:
        MAX_TRADES_PER_DAY, MAX_CONSECUTIVE_LOSSES = DEMO_MAX_TRADES_PER_DAY, DEMO_MAX_CONSECUTIVE_LOSSES
        max_stake, daily_loss_limit = None, None


def session_ready():
    """True only while the open socket is verified for the account type currently selected."""
    return account_channel.state == CONNECTED and session_mode == trading_mode


def load_bot_state():
    """Load today's trade/loss limits for the current mode so restarting the app does not reset them."""
    global trades_today, consecutive_losses, state_date, pnl_today
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        today = date.today().isoformat()
        if data.get("date") == today:
            trades_today = max(0, int(data.get("trades_today", 0)))
            consecutive_losses = max(0, int(data.get("consecutive_losses", 0)))
            pnl_today = float(data.get("pnl_today", 0.0))
            state_date = today
        else:
            trades_today, consecutive_losses, pnl_today, state_date = 0, 0, 0.0, today
            save_bot_state()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        trades_today, consecutive_losses, pnl_today = 0, 0, 0.0
        state_date = date.today().isoformat()
        if trading_mode == "real" and path.exists():
            # An unreadable real-money limits file must not silently reset the limits: block for today.
            trades_today = MAX_TRADES_PER_DAY
            set_status("Real-mode limits file is unreadable, so REAL trading is blocked for today. "
                       "Check real_bot_state.json.")
        else:
            save_bot_state()


def save_bot_state():
    try:
        state_path().write_text(json.dumps({
            "date": state_date,
            "trades_today": trades_today,
            "consecutive_losses": consecutive_losses,
            "pnl_today": round(pnl_today, 2),
        }, indent=2), encoding="utf-8")
    except OSError as exc:
        set_status(f"Warning: could not save daily limits: {exc}")


def refresh_trade_limits():
    text = (f"Today's {mode_word()} trades: {trades_today}/{MAX_TRADES_PER_DAY}  •  "
            f"Consecutive losses: {consecutive_losses}/{MAX_CONSECUTIVE_LOSSES}")
    if daily_loss_limit is not None:
        text += f"  •  Net P/L: {pnl_today:+.2f} (stop at -{daily_loss_limit:.2f})"
    ui(trade_limits_var.set, text)


def stop_bot(reason):
    global auto_bot_running
    auto_bot_running = False
    ui(bot_state_var.set, "STOPPED")
    set_status(reason)
    refresh_trade_limits()


def start_auto_bot():
    global auto_bot_running, configured_stake
    try:
        stake = float(stake_var.get().strip())
        if not math.isfinite(stake) or stake <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        messagebox.showerror("Invalid stake", "Enter a valid positive stake, for example 2.00.")
        return
    if max_stake is not None and stake > max_stake:
        messagebox.showerror("Stake too high", f"In REAL mode the largest stake allowed is {max_stake:.2f}. "
                                               "Lower the stake and try again.")
        return
    configured_stake = stake
    if trades_today >= MAX_TRADES_PER_DAY:
        messagebox.showwarning("Daily limit reached", f"The bot has already reached {MAX_TRADES_PER_DAY} "
                                                      f"{mode_word()} trades today.")
        return
    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        messagebox.showwarning("Safety stop active", f"The bot has stopped after {MAX_CONSECUTIVE_LOSSES} consecutive "
                                                     "losses. Review results before restarting another day.")
        return
    if daily_loss_limit is not None and pnl_today <= -daily_loss_limit:
        messagebox.showwarning("Daily loss limit reached", "The REAL-mode daily loss limit has been reached. "
                                                           "The bot will not trade again today.")
        return
    if trading_mode == "real":
        if not messagebox.askyesno(
                "Start REAL-MONEY bot",
                f"The bot will place REAL-MONEY trades automatically.\n\nStake per trade: {stake:.2f}\n"
                f"Limits: {MAX_TRADES_PER_DAY} trades/day, stop after {MAX_CONSECUTIVE_LOSSES} losses in a row, "
                f"stop at {daily_loss_limit:.2f} net loss.\n\nYou can lose this money. Start now?",
                icon="warning"):
            set_status("Real-money bot start cancelled.")
            return
    auto_bot_running = True
    ui(bot_state_var.set, "STARTING — REAL MONEY" if trading_mode == "real" else "STARTING — DEMO ONLY")
    set_status(f"Auto bot requested ({mode_word()} mode). It will trade only after the account connection is "
               "verified and a valid signal appears.")
    start_market()
    connect_account()


def request_trade(signal):
    """Request one 5-tick Rise/Fall contract on the verified account of the current mode."""
    global proposal_pending, trade_phase, trade_request_at
    if not auto_bot_running or not session_ready():
        return
    if signal not in ("HIGHER", "LOWER"):
        return
    with state_lock:
        if active_trade or proposal_pending:
            return
        if trades_today >= MAX_TRADES_PER_DAY:
            stop_bot(f"Daily safety stop: maximum of {MAX_TRADES_PER_DAY} {mode_word()} trades reached.")
            return
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            stop_bot(f"Safety stop: {MAX_CONSECUTIVE_LOSSES} consecutive {mode_word()} losses reached.")
            return
        if daily_loss_limit is not None and pnl_today <= -daily_loss_limit:
            stop_bot(f"Daily loss limit reached ({pnl_today:+.2f}). The bot will not trade again today.")
            return
        if account_channel.state != CONNECTED:
            set_status("Account connection is not ready; no trade sent.")
            return
        stake = configured_stake
        if not isinstance(stake, (int, float)) or not math.isfinite(stake) or stake <= 0:
            stop_bot("Invalid stake. Bot stopped without placing a trade.")
            return
        if max_stake is not None and stake > max_stake:
            stop_bot(f"Stake {stake:g} is above the REAL-mode maximum {max_stake:g}. Bot stopped without trading.")
            return
        contract_type = "CALL" if signal == "HIGHER" else "PUT"
        trade_meta.clear()
        trade_meta.update({"contract_type": contract_type, "stake": stake})   # the stake never changes: no martingale
        proposal_pending, trade_phase, trade_request_at = True, "proposal", time.monotonic()
        sent = account_channel.send({
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": account_currency,
            "duration": 5,
            "duration_unit": "t",
            "underlying_symbol": SYMBOL,      # the current API calls this field underlying_symbol
            "req_id": 301,
        })
        if sent:
            ui(bot_state_var.set, running_label())
            set_status(f"{signal} signal: requesting a {stake:g} {account_currency} {mode_word()} contract for 5 ticks.")
        else:
            proposal_pending, trade_phase = False, None
            set_status("Could not send the proposal (connection busy or closed); no trade placed.")


def current_signal():
    return evaluate_signal(list(prices), MIN_HISTORY_TICKS)


def ui(func, *args):
    """Queue a UI update from any thread; pump_ui() applies it on the Tk main thread."""
    if not closing:
        ui_queue.put((func, args))


def pump_ui():
    try:
        while True:
            func, args = ui_queue.get_nowait()
            try:
                func(*args)
            except tk.TclError:
                pass
            except Exception as exc:                       # never let one bad update stop the pump
                print(f"UI update error: {redact(exc)}")
    except queue.Empty:
        pass
    if not closing:
        root.after(50, pump_ui)


def set_status(text, connected=None):
    ui(status_var.set, redact(text))
    if connected is not None:
        ui(market_status_var.set, "CONNECTED" if connected else "NOT CONNECTED")


def append_history(timestamp, signal, start_price, end_price, outcome):
    def add_row():
        history.insert("", 0, values=(timestamp, signal, f"{start_price:.5f}", f"{end_price:.5f}", outcome))
        children = history.get_children()
        if len(children) > 100:
            history.delete(children[-1])
    ui(add_row)


def save_result(timestamp, signal, start_price, end_price, outcome, ticks_after):
    # Lock protects the CSV if saving and exporting happen close together.
    with CSV_LOCK:
        exists_with_data = RESULT_FILE.exists() and RESULT_FILE.stat().st_size > 0
        with RESULT_FILE.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not exists_with_data:
                writer.writerow([
                    "signal_time_local", "symbol", "signal", "entry_price",
                    "price_after_5_ticks", "ticks_after_entry", "directional_outcome"
                ])
            writer.writerow([
                timestamp, SYMBOL, signal, f"{start_price:.8f}",
                f"{end_price:.8f}", ticks_after, outcome
            ])


def process_tick(price):
    global last_signal_queued, total_wins, total_losses, total_ties, last_auto_signal
    try:
        price = float(price)
    except (TypeError, ValueError):
        return
    if not math.isfinite(price):
        return

    prices.append(price)
    for item in pending[:]:
        item["ticks_seen"] += 1
        if item["ticks_seen"] >= 5:
            start, signal = item["price"], item["signal"]
            if math.isclose(price, start, rel_tol=0.0, abs_tol=1e-12):
                outcome = "TIE"
                total_ties += 1
            elif (price > start and signal == "HIGHER") or (price < start and signal == "LOWER"):
                outcome = "DIRECTION MATCH"
                total_wins += 1
            else:
                outcome = "DIRECTION MISS"
                total_losses += 1
            stamp = item["timestamp"]
            try:
                save_result(stamp, signal, start, price, outcome, item["ticks_seen"])
            except Exception as exc:
                ui(status_var.set, f"Could not save CSV: {exc}")
            append_history(stamp, signal, start, price, outcome)
            pending.remove(item)
            ui(update_stats)

    signal, explanation = current_signal()
    # last_* is None right after a feed reconnect: adopt the current signal without acting on it.
    if (last_signal_queued is not None and signal in ("HIGHER", "LOWER")
            and signal != last_signal_queued):
        pending.append({"signal": signal, "price": price, "ticks_seen": 0,
                        "timestamp": datetime.now().isoformat(timespec="seconds")})
    # One trade per fresh transition into a directional signal; no repeated entries every tick.
    trade_signal = (last_auto_signal is not None and signal != last_auto_signal
                    and signal in ("HIGHER", "LOWER"))
    last_auto_signal = signal
    last_signal_queued = signal
    if trade_signal:
        request_trade(signal)
    ui(price_var.set, f"{price:,.5f}".rstrip("0").rstrip("."))
    display_signal = {"HIGHER": "RISE ↑", "LOWER": "FALL ↓", "WAIT": "WAIT"}.get(signal, signal)
    ui(signal_var.set, display_signal)
    ui(explanation_var.set, explanation)
    ui(ticks_var.set, f"{len(prices)} / {MIN_HISTORY_TICKS} minimum ticks")
    ui(updated_var.set, "Last update: " + datetime.now().strftime("%H:%M:%S"))
    ui(style_signal, signal)


# ----------------------------------------------------------------------------------------------
# Connection state -> UI (the labels always show the real state, never a permanent "Connecting")
# ----------------------------------------------------------------------------------------------
MARKET_LABELS = {DISCONNECTED: "NOT CONNECTED", CONNECTING: "CONNECTING...",
                 RETRYING: "RECONNECTING...", FAILED: "FAILED"}
ACCOUNT_LABELS = {DISCONNECTED: "NOT CONNECTED", CONNECTING: "CONNECTING",
                  CONNECTED: "CONNECTED — VERIFYING", RETRYING: "RECONNECTING", FAILED: "FAILED"}


def on_channel_log(name, text):
    set_status(f"{name.capitalize()} connection: {text}")


def on_channel_state(name, state, detail):
    if name == "market":
        if state == CONNECTED:
            label = "CONNECTED — STREAMING" if market_streaming else "CONNECTED — WAITING FOR TICKS"
        else:
            label = MARKET_LABELS[state]
            ui(price_var.set, "Waiting..." if state == CONNECTING else "Offline")
        ui(market_status_var.set, label)
        set_status(f"Market feed: {detail}")
    else:
        handle_account_state(state, detail)


def handle_account_state(state, detail):
    global session_mode
    if state != CONNECTED:
        session_mode = None
    ui(account_status_var.set, ACCOUNT_LABELS[state])
    set_status(f"{trading_mode.capitalize()} account: {detail}")
    if state == CONNECTING:
        ui(balance_var.set, "Connecting...")
    elif state == RETRYING:
        ui(balance_var.set, "Reconnecting...")
        if auto_bot_running:
            ui(bot_state_var.set, "PAUSED — ACCOUNT OFFLINE")
    elif state == FAILED:
        ui(balance_var.set, "Unavailable")
        ui(account_var.set, "Account: not connected")
        if auto_bot_running:
            stop_bot(f"Auto trading stopped: account connection failed. {detail}")
    elif state == DISCONNECTED:
        ui(balance_var.set, "—")
        ui(account_var.set, "Account: not connected")


# ----------------------------------------------------------------------------------------------
# Market feed (public endpoint)
# ----------------------------------------------------------------------------------------------
def on_market_open(channel):
    global market_streaming
    market_streaming = False
    ui(market_status_var.set, "CONNECTED — WAITING FOR TICKS")
    if not channel.send({"ticks": SYMBOL, "subscribe": 1, "req_id": 1}):
        set_status("Market feed connected but the tick subscription could not be sent.")
    else:
        set_status(f"Market feed connected. Subscribed to {SYMBOL} ticks; waiting for the first tick...")


def on_market_message(channel, message):
    global market_streaming
    data = json.loads(message)
    if data.get("error"):
        err = data["error"]
        set_status(f"Market feed error ({err.get('code', '?')}): {err.get('message', 'Unknown error')}")
        return
    tick = data.get("tick")
    if tick:
        if not market_streaming:
            market_streaming = True
            ui(market_status_var.set, "CONNECTED — STREAMING")
            set_status(f"Receiving {SYMBOL} ticks.")
        record_tick(tick)
        process_tick(tick["quote"])


def on_market_disconnect():
    """Ticks missed during an outage must not complete a five-tick observation or trigger a trade."""
    global market_streaming, last_signal_queued, last_auto_signal
    market_streaming = False
    pending.clear()
    last_signal_queued = None
    last_auto_signal = None


market_channel = Channel("market", lambda: MARKET_WS_URL, on_market_message,
                         on_open=on_market_open, on_state=on_channel_state,
                         on_disconnect=on_market_disconnect, on_log=on_channel_log,
                         idle_timeout=STALE_TICK_TIMEOUT)


def start_market():
    if market_channel.start():
        set_status("Connecting to public market feed...")
    else:
        set_status(f"Market feed is already running (state: {market_channel.state}).")


# ----------------------------------------------------------------------------------------------
# Demo account (authenticated via a one-time password from the REST API; demo endpoint only)
# ----------------------------------------------------------------------------------------------
def account_url_provider():
    global account_currency
    mode = trading_mode                            # decided once per attempt
    token = read_token(TOKEN_FILE)                 # re-read each attempt: fix the file without restarting
    app_id = check_app_id(APP_ID)
    override = read_optional_line(REAL_ACCOUNT_FILE if mode == "real" else ACCOUNT_FILE)
    url, account = get_ws_url(token, app_id, mode, override)   # raises unless account AND endpoint match the mode
    account_info["id"] = account["id"]
    account_info["mode"] = mode
    if account.get("currency"):
        account_currency = account["currency"]
    return url


def on_account_open(channel):
    """The OTP in the URL authenticated this socket, and the URL was checked against the selected mode."""
    global session_mode
    mode = account_info.get("mode")
    if mode != trading_mode:                       # the user switched modes mid-connect: do not trust this socket
        return
    session_mode = mode
    real = mode == "real"
    ui(account_var.set, f"Account: {account_info['id'] or 'Account'} ({'REAL MONEY' if real else 'DEMO'})")
    ui(account_status_var.set, "REAL ACCOUNT VERIFIED" if real else "DEMO ACCOUNT VERIFIED")
    ui(balance_var.set, "Loading...")
    ui(bot_state_var.set, running_label() if auto_bot_running else ready_label())
    set_status("REAL account verified. Trades will use REAL money." if real else
               "Demo account verified (demo endpoint). Auto trading can use virtual funds only.")
    channel.send({"balance": 1, "subscribe": 1, "req_id": 2})
    if active_trade and current_contract_id:       # reconnect while a contract is open: resume tracking
        channel.send({"proposal_open_contract": 1, "contract_id": current_contract_id,
                      "subscribe": 1, "req_id": 303})


def on_account_disconnect():
    global proposal_pending, trade_phase
    with state_lock:
        was_buying = proposal_pending and trade_phase == "buy"
        proposal_pending, trade_phase = False, None
    if was_buying:
        stop_bot(f"Connection lost while a purchase was in flight. A {mode_word()} trade may exist: check your "
                 "account history, then restart the bot.")


def on_account_message(channel, message):
    global proposal_pending, active_trade, current_contract_id, trade_phase, trade_request_at
    global trades_today, consecutive_losses, state_date, account_currency, pnl_today
    data = json.loads(message)
    if data.get("error"):
        err = data["error"]
        text = f"{err.get('message', 'Unknown error')} ({err.get('code', 'no code')})"
        req_id = data.get("req_id")
        if req_id in (301, 302):
            with state_lock:
                proposal_pending, trade_phase = False, None
            set_status(f"{mode_word().capitalize()} trade request rejected: {text}. No new trade sent.")
        elif req_id == 2:
            ui(balance_var.set, "Unavailable")
            set_status(f"Balance request failed: {text}. Check the token's scopes.")
        else:
            set_status(f"Account API error: {text}")
        return

    msg_type = data.get("msg_type")
    if msg_type == "balance" or "balance" in data:
        b = data.get("balance", {})
        if b.get("currency"):
            account_currency = b["currency"]
        ui(balance_var.set, f"{b.get('balance', '—')} {b.get('currency', '')}".strip())

    elif msg_type == "proposal" or "proposal" in data:
        proposal = data.get("proposal", {})
        with state_lock:
            if not proposal_pending or not auto_bot_running or not session_ready():
                proposal_pending, trade_phase = False, None     # late or unwanted quote: do not buy it
                return
            proposal_id = proposal.get("id")
            ask_price = proposal.get("ask_price")
            if not proposal_id or ask_price is None:
                proposal_pending, trade_phase = False, None
                set_status("Proposal response was incomplete; no trade was placed.")
                return
            trade_phase, trade_request_at = "buy", time.monotonic()
            # Buy price is capped at the proposal's quoted ask price.
            if channel.send({"buy": proposal_id, "price": float(ask_price), "req_id": 302}):
                set_status("Demo proposal received; sending capped purchase request.")
            else:
                proposal_pending, trade_phase = False, None
                set_status("Connection closed before the purchase was sent; no trade placed.")

    elif msg_type == "buy" or "buy" in data:
        buy = data.get("buy", {})
        contract_id = buy.get("contract_id")
        if not contract_id:
            with state_lock:
                proposal_pending, trade_phase = False, None
            set_status("Purchase response had no contract ID; check demo account history.")
            return
        with state_lock:
            proposal_pending, trade_phase = False, None
            active_trade = True
            current_contract_id = contract_id
            trades_today += 1
            state_date = date.today().isoformat()
            save_bot_state()
        refresh_trade_limits()
        set_status(f"{mode_word().capitalize()} trade opened: contract #{contract_id}. Waiting for settlement.")
        channel.send({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1, "req_id": 303})

    elif msg_type == "proposal_open_contract" or "proposal_open_contract" in data:
        contract = data.get("proposal_open_contract", {})
        if int(contract.get("is_sold", 0)) != 1:
            return
        # Ignore repeat "sold" updates and updates for any contract we are not tracking.
        if not active_trade or str(contract.get("contract_id")) != str(current_contract_id):
            return
        profit = float(contract.get("profit", 0.0) or 0.0)
        with state_lock:
            active_trade = False
            proposal_pending = False
            current_contract_id = None
            if profit < 0:
                consecutive_losses += 1
            elif profit > 0:
                consecutive_losses = 0
            # A zero-profit settlement is not counted as a loss and does not erase a loss streak.
            pnl_today += profit
            save_bot_state()
        refresh_trade_limits()
        ui(balance_var.set, "Updating balance…")
        outcome = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "TIE / ZERO")
        set_status(f"{mode_word().capitalize()} trade settled: {outcome}; profit {profit:.2f} {account_currency}.")
        log_settled_trade(contract, profit, outcome)
        channel.send({"balance": 1, "subscribe": 1, "req_id": 2})
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            stop_bot(f"Automatic safety stop: {MAX_CONSECUTIVE_LOSSES} consecutive losing {mode_word()} trades. "
                     "Review results before restarting.")
        elif daily_loss_limit is not None and pnl_today <= -daily_loss_limit:
            stop_bot(f"Daily loss limit reached ({pnl_today:+.2f} {account_currency}). No more REAL trades today.")
        elif trades_today >= MAX_TRADES_PER_DAY:
            stop_bot(f"Automatic daily stop: {MAX_TRADES_PER_DAY} {mode_word()} trades completed.")
        # Cancel the open-contract subscription after settlement.
        sub_id = (data.get("subscription") or {}).get("id")
        if sub_id:
            channel.send({"forget": sub_id})


account_channel = Channel("account", account_url_provider, on_account_message,
                          on_open=on_account_open, on_state=on_channel_state,
                          on_disconnect=on_account_disconnect, on_log=on_channel_log)


def connect_account():
    if not account_channel.start():
        set_status(f"Demo account connection is already running (state: {account_channel.state}).")


def record_tick(tick):
    """Append every new tick to ticks_R_100.csv so backtest.py has data. Never allowed to break the feed."""
    global last_recorded_epoch
    if not RECORD_TICKS:
        return
    try:
        epoch, quote = int(tick["epoch"]), float(tick["quote"])
    except (KeyError, TypeError, ValueError):
        return
    if epoch <= last_recorded_epoch:
        return
    try:
        new_file = not TICK_FILE.exists() or TICK_FILE.stat().st_size == 0
        with TICK_FILE.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["epoch", "quote"])
            writer.writerow([epoch, quote])
        last_recorded_epoch = epoch
    except OSError:
        pass


def refresh_summary():
    ui(summary_var.set, tradelog.summary_text(tradelog.summarize(TRADE_LOG_FILE, trading_mode), mode_word()))


def log_settled_trade(contract, profit, outcome):
    row = {"time_local": datetime.now().isoformat(timespec="seconds"), "mode": trading_mode,
           "account_id": account_info.get("id"), "contract_id": contract.get("contract_id"),
           "direction": contract.get("contract_type") or trade_meta.get("contract_type"),
           "stake": trade_meta.get("stake"), "buy_price": contract.get("buy_price"),
           "sell_price": contract.get("sell_price"), "profit": round(profit, 2), "result": outcome,
           "consecutive_losses": consecutive_losses, "trades_today": trades_today,
           "net_pnl_today": round(pnl_today, 2)}
    try:
        tradelog.append_trade(TRADE_LOG_FILE, row)
    except OSError as exc:
        set_status(f"Warning: could not write trade_log.csv: {exc}")
    refresh_summary()


def toggle_mode():
    """Demo -> REAL needs two explicit confirmations. REAL -> demo needs none."""
    if active_trade or proposal_pending:
        messagebox.showwarning("Trade in progress", "Wait for the open contract to settle before switching accounts.")
        return
    if trading_mode == "demo":
        if not messagebox.askyesno(
                "Switch to REAL account",
                "REAL mode places automated trades with REAL money from your Deriv account.\n\n"
                "The strategy is experimental and can lose your stake. Limits in REAL mode: "
                f"max stake {REAL_MAX_STAKE:.2f}, {REAL_MAX_TRADES_PER_DAY} trades/day, stop after "
                f"{REAL_MAX_CONSECUTIVE_LOSSES} losses in a row, stop at {REAL_MAX_DAILY_LOSS:.2f} net loss.\n\n"
                "Continue?", icon="warning"):
            set_status("Staying in demo mode.")
            return
        typed = simpledialog.askstring("Confirm REAL mode", f"Type {REAL_CONFIRM_WORD} (capitals) to switch to the "
                                       "REAL account:", parent=root)
        if (typed or "").strip() != REAL_CONFIRM_WORD:
            set_status("Switch to REAL account cancelled. Staying in demo mode.")
            return
        switch_mode("real")
    else:
        switch_mode("demo")


def switch_mode(new_mode):
    global trading_mode, session_mode, auto_bot_running
    auto_bot_running = False                       # a mode change never keeps a running bot
    session_mode = None
    account_channel.stop()                         # drop the old socket before anything else
    trading_mode = new_mode
    apply_mode_limits()
    load_bot_state()
    real = new_mode == "real"
    ui(bot_state_var.set, "STOPPED")
    ui(mode_var.set, "MODE: REAL MONEY — LIVE FUNDS" if real else "MODE: DEMO — virtual funds")
    ui(lambda: mode_label.configure(style="RealMode.TLabel" if real else "Mode.TLabel"))
    ui(mode_button_var.set, "Switch back to DEMO" if real else "Switch to REAL Account")
    ui(balance_title_var.set, "REAL ACCOUNT BALANCE" if real else "DEMO ACCOUNT BALANCE")
    ui(start_button_var.set, "START REAL BOT" if real else "START DEMO BOT")
    ui(stake_label_var.set, "REAL stake:" if real else "Demo stake:")
    refresh_trade_limits()
    refresh_summary()
    set_status(("Switching to the REAL account... " if real else "Switching to the demo account... ")
               + "The bot is stopped; press START to trade.")
    connect_account()


def housekeeping():
    """Runs on the UI thread once a second: bounds how long a trade request may stay unanswered."""
    global proposal_pending, trade_phase
    now = time.monotonic()
    with state_lock:
        stale = proposal_pending and now - trade_request_at > TRADE_RESPONSE_TIMEOUT
        phase = trade_phase
        if stale:
            proposal_pending, trade_phase = False, None
    if stale:
        if phase == "buy":
            stop_bot(f"No reply to the purchase request. A {mode_word()} trade may exist: check your account "
                     "history, then restart the bot.")
        else:
            set_status(f"No price proposal within {TRADE_RESPONSE_TIMEOUT}s; no trade was placed.")
    if not closing:
        root.after(1000, housekeeping)


def update_stats():
    completed = total_wins + total_losses
    accuracy = (total_wins / completed * 100) if completed else 0
    wins_var.set(str(total_wins))
    misses_var.set(str(total_losses))
    ties_var.set(str(total_ties))
    accuracy_var.set(f"{accuracy:.1f}%")
    completed_var.set(str(completed + total_ties))


def export_csv():
    if not RESULT_FILE.exists():
        messagebox.showinfo("No results yet", "Connect the market feed and wait for completed five-tick observations.")
        return
    destination = filedialog.asksaveasfilename(title="Export research results", defaultextension=".csv",
        filetypes=[("CSV files", "*.csv")], initialfile="five_tick_results.csv")
    if destination:
        try:
            with CSV_LOCK:
                shutil.copy2(RESULT_FILE, destination)
            messagebox.showinfo("Export complete", f"Research results exported to:\n{destination}")
        except OSError as exc:
            messagebox.showerror("Export failed", f"Could not export results: {exc}")


def style_signal(signal):
    signal_card.configure(style={"HIGHER": "Higher.TFrame", "LOWER": "Lower.TFrame"}.get(signal, "Card.TFrame"))
    signal_value.configure(style={"HIGHER": "Higher.TLabel", "LOWER": "Lower.TLabel"}.get(signal, "Signal.TLabel"))


def close_app():
    global closing, auto_bot_running
    auto_bot_running = False
    closing = True
    for channel in (market_channel, account_channel):
        try:
            channel.stop()
        except Exception:
            pass
    root.destroy()


root = tk.Tk()
root.title("Deriv Desktop Trader Pro | Client Test")
root.geometry("1060x800")
root.minsize(900, 700)
root.configure(bg="#101827")
style = ttk.Style(root)
try: style.theme_use("clam")
except tk.TclError: pass
style.configure("TFrame", background="#101827")
style.configure("Card.TFrame", background="#1b2638")
style.configure("Higher.TFrame", background="#12352b")
style.configure("Lower.TFrame", background="#3b1e2b")
style.configure("TLabel", background="#101827", foreground="#e8eef7", font=("Segoe UI", 10))
style.configure("Muted.TLabel", background="#1b2638", foreground="#a8b6cb", font=("Segoe UI", 9))
style.configure("CardValue.TLabel", background="#1b2638", foreground="#ffffff", font=("Segoe UI", 18, "bold"))
style.configure("Header.TLabel", background="#101827", foreground="#ffffff", font=("Segoe UI", 21, "bold"))
style.configure("Subheader.TLabel", background="#101827", foreground="#a8b6cb", font=("Segoe UI", 10))
style.configure("Signal.TLabel", background="#1b2638", foreground="#facc15", font=("Segoe UI", 23, "bold"))
style.configure("Higher.TLabel", background="#12352b", foreground="#4ade80", font=("Segoe UI", 23, "bold"))
style.configure("Lower.TLabel", background="#3b1e2b", foreground="#fb7185", font=("Segoe UI", 23, "bold"))
style.configure("Mode.TLabel", background="#101827", foreground="#4ade80", font=("Segoe UI", 10, "bold"))
style.configure("RealMode.TLabel", background="#7f1d1d", foreground="#ffffff", font=("Segoe UI", 11, "bold"), padding=(8, 3))
style.configure("Metric.TLabel", background="#1b2638", foreground="#ffffff", font=("Segoe UI", 16, "bold"))
style.configure("TButton", font=("Segoe UI", 10, "bold"), padding=(12, 9))
style.configure("Accent.TButton", background="#2563eb", foreground="#ffffff")
style.configure("Secondary.TButton", background="#334155", foreground="#ffffff")

header = ttk.Frame(root, padding=(24, 20, 24, 8)); header.pack(fill="x")
tr = ttk.Frame(header); tr.pack(fill="x")
ttk.Label(tr, text="DERIV DESKTOP TRADER PRO", style="Header.TLabel").pack(side="left")
market_status_var = tk.StringVar(value="NOT CONNECTED")
ttk.Label(tr, textvariable=market_status_var, style="Subheader.TLabel").pack(side="right", pady=8)
mode_var = tk.StringVar(value="MODE: DEMO — virtual funds")
mode_button_var = tk.StringVar(value="Switch to REAL Account")
balance_title_var = tk.StringVar(value="DEMO ACCOUNT BALANCE")
start_button_var = tk.StringVar(value="START DEMO BOT")
stake_label_var = tk.StringVar(value="Demo stake:")
ttk.Label(header, text="5G AUTO-TRADING  •  EMA + MACD + RSI  •  RISE/FALL 5 TICKS  •  DEMO BY DEFAULT", style="Subheader.TLabel").pack(anchor="w", pady=(5,0))
mode_label = ttk.Label(header, textvariable=mode_var, style="Mode.TLabel")
mode_label.pack(anchor="w", pady=(4, 0))

account_var = tk.StringVar(value="Account: not connected"); balance_var = tk.StringVar(value="—")
account_status_var = tk.StringVar(value="NOT CONNECTED"); price_var = tk.StringVar(value="Waiting...")
signal_var = tk.StringVar(value="WAIT"); explanation_var = tk.StringVar(value="Connect to public market data to begin.")
ticks_var = tk.StringVar(value=f"0 / {MIN_HISTORY_TICKS} minimum ticks"); updated_var = tk.StringVar(value="Last update: —")
status_var = tk.StringVar(value="Ready. Connect to market data or demo balance.")
wins_var = tk.StringVar(value="0"); misses_var = tk.StringVar(value="0"); ties_var = tk.StringVar(value="0")
accuracy_var = tk.StringVar(value="0.0%"); completed_var = tk.StringVar(value="0")
bot_state_var = tk.StringVar(value="STOPPED")
summary_var = tk.StringVar(value="Trade log: no settled trades yet")
trade_limits_var = tk.StringVar(value="Today: 0/10 trades  •  Consecutive losses: 0/3")
stake_var = tk.StringVar(value="2.00")

body = ttk.Frame(root, padding=(24, 10, 24, 8)); body.pack(fill="both", expand=True)
row = ttk.Frame(body); row.pack(fill="x", pady=(0, 12))

def make_card(parent, title, variable, sub=None):
    card = ttk.Frame(parent, style="Card.TFrame", padding=15)
    if isinstance(title, tk.StringVar):
        ttk.Label(card, textvariable=title, style="Muted.TLabel").pack(anchor="w")
    else:
        ttk.Label(card, text=title, style="Muted.TLabel").pack(anchor="w")
    ttk.Label(card, textvariable=variable, style="CardValue.TLabel").pack(anchor="w", pady=(9, 3))
    if sub is not None: ttk.Label(card, textvariable=sub, style="Muted.TLabel").pack(anchor="w")
    return card

balance_card = make_card(row, balance_title_var, balance_var, account_var); balance_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
price_card = make_card(row, f"{SYMBOL} LIVE PRICE", price_var, updated_var); price_card.pack(side="left", fill="both", expand=True, padx=6)
count_card = make_card(row, "COMPLETED OBSERVATIONS", completed_var, ticks_var); count_card.pack(side="left", fill="both", expand=True, padx=(6, 0))

signal_card = ttk.Frame(body, style="Card.TFrame", padding=18); signal_card.pack(fill="x", pady=(0, 12))
ttk.Label(signal_card, text="CURRENT RESEARCH SIGNAL", style="Muted.TLabel").pack(anchor="w")
signal_value = ttk.Label(signal_card, textvariable=signal_var, style="Signal.TLabel"); signal_value.pack(anchor="w", pady=(5, 2))
ttk.Label(signal_card, textvariable=explanation_var, style="Muted.TLabel", wraplength=950).pack(anchor="w")

metrics = ttk.Frame(body); metrics.pack(fill="x", pady=(0, 12))
for title, var in [("DIRECTION MATCHES", wins_var), ("DIRECTION MISSES", misses_var), ("TIES", ties_var), ("DIRECTION ACCURACY*", accuracy_var)]:
    c = ttk.Frame(metrics, style="Card.TFrame", padding=12); c.pack(side="left", fill="both", expand=True, padx=(0, 8))
    ttk.Label(c, text=title, style="Muted.TLabel").pack(anchor="w")
    ttk.Label(c, textvariable=var, style="Metric.TLabel").pack(anchor="w", pady=(6, 0))

auto_controls = ttk.Frame(body, style="Card.TFrame", padding=10); auto_controls.pack(fill="x", pady=(0, 8))
ttk.Label(auto_controls, textvariable=stake_label_var, style="Muted.TLabel").pack(side="left", padx=(0, 5))
stake_entry = ttk.Entry(auto_controls, textvariable=stake_var, width=10)
stake_entry.pack(side="left", padx=(0, 12))
ttk.Label(auto_controls, text="Bot:", style="Muted.TLabel").pack(side="left", padx=(0, 5))
ttk.Label(auto_controls, textvariable=bot_state_var, style="Muted.TLabel").pack(side="left", padx=(0, 12))
ttk.Button(auto_controls, textvariable=start_button_var, style="Accent.TButton", command=start_auto_bot).pack(side="left", padx=(0, 7))
ttk.Button(auto_controls, text="STOP BOT", style="Secondary.TButton", command=lambda: stop_bot("Bot stopped by user. Any already-open demo contract will be allowed to settle.")).pack(side="left", padx=7)
ttk.Label(auto_controls, textvariable=trade_limits_var, style="Muted.TLabel").pack(side="right", padx=(8, 0))

controls = ttk.Frame(body); controls.pack(fill="x", pady=(0, 10))
ttk.Button(controls, text="Connect Market Feed", style="Accent.TButton", command=start_market).pack(side="left", padx=(0, 7))
ttk.Button(controls, text="Connect Account Balance", style="Secondary.TButton", command=connect_account).pack(side="left", padx=7)
ttk.Button(controls, textvariable=mode_button_var, style="Secondary.TButton", command=toggle_mode).pack(side="left", padx=7)
ttk.Button(controls, text="Export CSV Results", style="Secondary.TButton", command=export_csv).pack(side="left", padx=7)
ttk.Button(controls, text="Close App", style="Secondary.TButton", command=close_app).pack(side="right")

hist_head = ttk.Frame(body); hist_head.pack(fill="x", pady=(4, 5))
ttk.Label(hist_head, text="RECENT FIVE-TICK OBSERVATIONS", style="Header.TLabel").pack(side="left")
ttk.Label(hist_head, text="Last 100 shown • all completed rows saved to CSV", style="Subheader.TLabel").pack(side="right", pady=8)
table_frame = ttk.Frame(body); table_frame.pack(fill="both", expand=True)
columns = ("time", "signal", "entry", "exit", "outcome")
history = ttk.Treeview(table_frame, columns=columns, show="headings", height=7)
for col, label, width in [("time", "Signal time", 190), ("signal", "Signal", 100), ("entry", "Entry price", 160), ("exit", "Price after 5 ticks", 180), ("outcome", "Directional outcome", 190)]:
    history.heading(col, text=label); history.column(col, width=width, anchor="center")
scroll = ttk.Scrollbar(table_frame, orient="vertical", command=history.yview); history.configure(yscrollcommand=scroll.set)
history.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")

status_card = ttk.Frame(body, style="Card.TFrame", padding=12); status_card.pack(fill="x", pady=(10, 0))
ttk.Label(status_card, text="SYSTEM STATUS", style="Muted.TLabel").pack(anchor="w")
ttk.Label(status_card, textvariable=status_var, background="#1b2638", foreground="#e8eef7", wraplength=950).pack(anchor="w", pady=(5, 0))
ttk.Label(status_card, textvariable=summary_var, style="Muted.TLabel", wraplength=950).pack(anchor="w", pady=(6, 0))
footer = ttk.Frame(root, padding=(24, 0, 24, 14)); footer.pack(fill="x")
ttk.Label(footer, text=f"Indicators: EMA 9/21 + MACD 12/26/9 + Wilder RSI 14. Warm-up: {MIN_HISTORY_TICKS} ticks. Accuracy excludes ties and is not contract profitability.", style="Subheader.TLabel", wraplength=980).pack(anchor="w")
ttk.Label(footer, text=f"The app always starts in DEMO ({DEMO_MAX_TRADES_PER_DAY} trades/day, stop after {DEMO_MAX_CONSECUTIVE_LOSSES} losses). REAL mode needs two confirmations each time and is stricter: max stake {REAL_MAX_STAKE:g}, {REAL_MAX_TRADES_PER_DAY} trades/day, stop after {REAL_MAX_CONSECUTIVE_LOSSES} losses or {REAL_MAX_DAILY_LOSS:g} net loss. Keep deriv_token.txt private.", style="Subheader.TLabel", wraplength=980).pack(anchor="w", pady=(3, 0))
root.protocol("WM_DELETE_WINDOW", close_app)
apply_mode_limits()
load_bot_state()
refresh_trade_limits()
refresh_summary()
root.after(50, pump_ui)
root.after(1000, housekeeping)
if AUTO_CONNECT_MARKET:
    root.after(300, start_market)
root.mainloop()
