"""Server-side trading engine for the web app (no Flask, no Tk).

  MarketHub      one shared public R_100 tick feed; computes the signal once per tick for everybody.
  LimitStore     per account + mode daily counters on disk (trades, loss streak, P/L, "halted" latch).
  UserBot        one signed-in user: their own account WebSocket, settings, limits and trade state.
  SessionStore   signed-in users, one live session per Deriv account, housekeeping sweeper.

Safety rules carried over from the desktop app and enforced HERE on the server (never in the browser):
  * fixed stake, never raised after a loss (no martingale); stake capped per mode
  * max 10 trades a day and a stop after 3 losses in a row (risk level can only tighten these)
  * a limit stop is LATCHED for the day: restarting the bot, the server, or changing the risk level cannot undo it
  * one trade at a time; nothing is bought from a late quote; real mode needs explicit confirmation
Access tokens live in memory only. They are never written to disk, logs or API replies.
"""
import csv
import json
import math
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime

import tradelog
from deriv_connection import (
    ACCOUNTS_PATH, CONNECTED, CONNECTING, DISCONNECTED, FAILED, PUBLIC_WS_URL, RETRYING,
    Channel, DerivApiError, get_ws_url, http_json, normalize_accounts, redact)
from indicators import evaluate_signal

TRADE_RESPONSE_TIMEOUT = 20
STALE_TICK_TIMEOUT = 30
MIN_HISTORY_TICKS = 100
IDLE_DROP_SECONDS = 1800          # a signed-in browser that stopped polling and has no running bot is dropped
EXPIRED_KEEP_SECONDS = 900
REAL_CONFIRM_WORD = "REAL"


class SessionConflict(Exception):
    pass


class TooManyUsers(Exception):
    pass


# --------------------------------------------------------------------------- market feed
class MarketHub:
    def __init__(self, symbol="R_100", tick_file=None, ws_factory=None, mono=time.monotonic):
        self.symbol = symbol
        self.prices = deque(maxlen=500)
        self.signal, self.explanation = "WAIT", "Waiting for market data..."
        self.streaming = False
        self.last_signal = None
        self.last_tick_mono = None
        self.last_epoch = 0
        self.tick_file = tick_file
        self._lock = threading.RLock()
        self._listeners = []
        self._mono = mono
        self.feed_state, self.feed_detail = DISCONNECTED, "Not started"
        kwargs = {"ws_factory": ws_factory} if ws_factory else {}
        self.channel = Channel("market", lambda: PUBLIC_WS_URL, self._on_message, on_open=self._on_open,
                               on_state=self._on_state, on_disconnect=self._on_disconnect,
                               idle_timeout=STALE_TICK_TIMEOUT, **kwargs)

    def start(self):
        return self.channel.start()

    def stop(self):
        self.channel.stop()

    def add_listener(self, fn):
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def remove_listener(self, fn):
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def _on_state(self, name, state, detail):
        with self._lock:
            self.feed_state, self.feed_detail = state, detail

    def _on_open(self, channel):
        with self._lock:
            self.streaming = False
        channel.send({"ticks": self.symbol, "subscribe": 1, "req_id": 1})

    def _on_disconnect(self):
        """Ticks missed during an outage must never trigger a trade: forget the last signal."""
        with self._lock:
            self.streaming = False
            self.last_signal = None

    def _on_message(self, channel, text):
        data = json.loads(text)
        if data.get("error"):
            return
        tick = data.get("tick")
        if tick:
            self._on_tick(tick)

    def _on_tick(self, tick):
        try:
            price = float(tick["quote"])
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(price):
            return
        with self._lock:
            self.streaming = True
            self.last_tick_mono = self._mono()
            self.prices.append(price)
            signal, explanation = evaluate_signal(list(self.prices), MIN_HISTORY_TICKS)
            fresh = (self.last_signal is not None and signal != self.last_signal
                     and signal in ("HIGHER", "LOWER"))
            self.last_signal, self.signal, self.explanation = signal, signal, explanation
            self._record(tick)
            event = {"price": price, "signal": signal, "fresh": fresh, "explanation": explanation,
                     "symbol": self.symbol}
            listeners = list(self._listeners)
        for fn in listeners:                       # outside the lock: a slow listener cannot stall the feed
            try:
                fn(event)
            except Exception:
                pass

    def _record(self, tick):
        """Keep every tick in a CSV so backtest.py can be run on the server's own data."""
        if not self.tick_file:
            return
        try:
            epoch = int(tick["epoch"])
            if epoch <= self.last_epoch:
                return
            new_file = not os.path.exists(self.tick_file) or os.path.getsize(self.tick_file) == 0
            with open(self.tick_file, "a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(["epoch", "quote"])
                writer.writerow([epoch, float(tick["quote"])])
            self.last_epoch = epoch
        except (KeyError, TypeError, ValueError, OSError):
            pass

    def snapshot(self):
        with self._lock:
            return {"symbol": self.symbol, "streaming": self.streaming, "feed_state": self.feed_state,
                    "price": self.prices[-1] if self.prices else None, "signal": self.signal,
                    "explanation": self.explanation, "ticks": len(self.prices),
                    "min_ticks": MIN_HISTORY_TICKS,
                    "age": None if self.last_tick_mono is None else round(self._mono() - self.last_tick_mono, 1)}


# --------------------------------------------------------------------------- daily limits on disk
class LimitStore:
    def __init__(self, directory, tz, wall=time.time):
        self.dir = os.path.join(str(directory), "limits")
        os.makedirs(self.dir, exist_ok=True)
        self.tz, self._wall = tz, wall
        self._lock = threading.Lock()

    def today(self):
        return datetime.fromtimestamp(self._wall(), self.tz).date().isoformat()

    def _path(self, mode, account_id):
        return os.path.join(self.dir, f"{mode}_{re.sub(r'[^A-Za-z0-9_-]', '_', str(account_id))}.json")

    @staticmethod
    def fresh(today):
        return {"date": today, "trades_today": 0, "consecutive_losses": 0, "pnl_today": 0.0,
                "halted": False, "halt_reason": ""}

    def load(self, mode, account_id, hard_max_trades):
        today, path = self.today(), self._path(mode, account_id)
        with self._lock:
            try:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                if data.get("date") != today:
                    data = self.fresh(today)
                else:
                    data = {"date": today, "trades_today": max(0, int(data.get("trades_today", 0))),
                            "consecutive_losses": max(0, int(data.get("consecutive_losses", 0))),
                            "pnl_today": float(data.get("pnl_today", 0.0)),
                            "halted": bool(data.get("halted", False)),
                            "halt_reason": str(data.get("halt_reason", ""))[:200]}
            except FileNotFoundError:
                data = self.fresh(today)
            except (OSError, ValueError, TypeError):
                data = self.fresh(today)
                if mode == "real":              # an unreadable real-money limits file must not reset the limits
                    data.update(trades_today=hard_max_trades, halted=True,
                                halt_reason="The limits file could not be read, so real trading is blocked today.")
            self._write(path, data)
        return data

    def save(self, mode, account_id, data):
        with self._lock:
            self._write(self._path(mode, account_id), data)

    @staticmethod
    def _write(path, data):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, path)
        except OSError:
            pass


# --------------------------------------------------------------------------- one signed-in user
class UserBot:
    STATUS_LABELS = {DISCONNECTED: "OFFLINE", CONNECTING: "CONNECTING", CONNECTED: "CONNECTING",
                     RETRYING: "RECONNECTING", FAILED: "FAILED"}

    def __init__(self, settings, hub, store, token, expires_in, accounts, *, ws_factory=None,
                 wall=time.time, mono=time.monotonic):
        self.settings, self.hub, self.store = settings, hub, store
        self._token = token
        self._wall, self._mono = wall, mono
        self.expires_at = wall() + expires_in
        self.accounts = accounts
        self.sid = None
        self.lock = threading.RLock()
        self.mode = "demo"                         # every sign-in starts on demo
        self.session_mode = None
        self.account_id = None
        self.account_mode = None
        self.currency = "USD"
        self.balance = None
        self.acknowledged = False
        self.expired = False
        self.closed = False
        self.running = False
        self.stake, self.frequency, self.risk_level = 1.0, 30, 2
        self.cooldown_until = 0.0
        self.counter_key = None
        self.counters = LimitStore.fresh(store.today())
        self.history = []                          # this account's settled trades for the current mode
        self.active_trade = False
        self.proposal_pending = False
        self.trade_phase = None
        self.trade_request_at = 0.0
        self.contract_id = None
        self.trade_meta = {}
        self.account_state = DISCONNECTED
        self.messages = deque(maxlen=30)
        self.last_seen = mono()
        self.expired_at = None
        kwargs = {"ws_factory": ws_factory} if ws_factory else {}
        self.channel = Channel("account", self._url_provider, self._on_message, on_open=self._on_open,
                               on_state=self._on_state, on_disconnect=self._on_disconnect, **kwargs)
        self.hub.add_listener(self._on_market)

    # ---- helpers
    def say(self, text):
        self.messages.append((datetime.fromtimestamp(self._wall(), self.settings.tz).strftime("%H:%M:%S"),
                              redact(text)))

    def touch(self):
        self.last_seen = self._mono()

    def has_account(self, mode):
        return any(a.get("type") == mode and a.get("id") for a in self.accounts)

    def account_ids(self):
        return {a["id"] for a in self.accounts if a.get("id")}

    def limits(self):
        return self.settings.limits(self.mode, self.risk_level)

    def session_ready(self):
        return (not self.closed and not self.expired and self.channel.state == CONNECTED
                and self.session_mode == self.mode)

    def _save_counters(self):
        if self.counter_key:
            self.store.save(self.counter_key[0], self.counter_key[1], self.counters)

    def _roll_day(self):
        today = self.store.today()
        if self.counters.get("date") != today:
            self.counters = LimitStore.fresh(today)
            self._save_counters()

    def _load_counters(self, mode, account_id):
        self.counters = self.store.load(mode, account_id, self.settings.HARD_MAX_TRADES)
        self.counter_key = (mode, account_id)
        self.history = self._read_history(mode, account_id)
        if self.counters.get("halted"):
            self.say(self.counters.get("halt_reason") or "A safety limit stopped trading for today.")

    def _log_path(self):
        return os.path.join(str(self.settings.data_dir), "trade_log.csv")

    def _read_history(self, mode, account_id):
        rows = []
        try:
            with open(self._log_path(), newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    if row.get("account_id") == str(account_id) and (row.get("mode") or "") == mode:
                        rows.append(row)
        except (OSError, csv.Error):
            pass
        return rows[-200:]

    # ---- connection
    def connect(self):
        with self.lock:
            if self.closed or self.expired:
                return
            self.channel.start()

    def reconnect(self):
        with self.lock:
            if self.closed or self.expired or self.channel.state in (CONNECTED, CONNECTING):
                return False
            self.say(f"Reconnecting to your {self.mode} account...")
            return self.channel.start()

    def _url_provider(self):
        with self.lock:
            mode, token = self.mode, self._token
            if self.closed or self.expired or token is None:
                raise DerivApiError("auth", "Your Deriv sign-in has expired. Please sign in again.", 401)
        try:
            url, account = get_ws_url(token, None, mode, None)
        except DerivApiError as exc:
            if exc.kind == "auth":
                self._expire("Your Deriv sign-in has expired or was revoked. Please sign in again.")
                raise DerivApiError("auth", "Your Deriv sign-in has expired. Please sign in again.", 401)
            raise
        with self.lock:
            if self.mode != mode:
                raise DerivApiError("config", "The account type changed while connecting.", retryable=True)
            self.account_id, self.account_mode = account["id"], mode
            if account.get("currency"):
                self.currency = account["currency"]
            if self.counter_key != (mode, account["id"]):
                self._load_counters(mode, account["id"])
        return url

    def _on_open(self, channel):
        with self.lock:
            if self.account_mode != self.mode or self.closed:
                return
            self.session_mode = self.account_mode
            self.balance = None
            real = self.session_mode == "real"
            self.say(("Real" if real else "Demo") + f" account {self.account_id} connected.")
        channel.send({"balance": 1, "subscribe": 1, "req_id": 2})
        with self.lock:
            resume = self.active_trade and self.contract_id
        if resume:
            channel.send({"proposal_open_contract": 1, "contract_id": self.contract_id, "subscribe": 1,
                          "req_id": 303})

    def _on_state(self, name, state, detail):
        with self.lock:
            self.account_state = state
            if state != CONNECTED:
                self.session_mode = None
            if state in (RETRYING, FAILED, CONNECTING):
                self.say(f"Account connection: {detail}")
            if state == FAILED and self.running:
                self._stop(f"Auto trading stopped: account connection failed. {detail}")

    def _on_disconnect(self):
        with self.lock:
            was_buying = self.proposal_pending and self.trade_phase == "buy"
            self.proposal_pending, self.trade_phase = False, None
            if was_buying:
                self._stop("Connection lost while a purchase was in flight. A trade may exist: check your "
                           "Deriv account history before starting again.")

    # ---- user actions (called from HTTP threads)
    def acknowledge(self):
        with self.lock:
            self.acknowledged = True

    def configure(self, stake=None, frequency=None, risk_level=None):
        with self.lock:
            if self.running:
                return False, "Stop the bot before changing its settings."
            try:
                if stake is not None:
                    value = float(stake)
                    cap = self.limits()[2]
                    if not math.isfinite(value) or value < self.settings.MIN_STAKE or value > cap:
                        return False, (f"Amount must be between {self.settings.MIN_STAKE:g} and {cap:g} "
                                       f"in {self.mode} mode.")
                    self.stake = round(value, 2)
                if frequency is not None:
                    value = int(frequency)
                    if not 10 <= value <= 300:
                        return False, "Trade frequency must be between 10 and 300 seconds."
                    self.frequency = value
                if risk_level is not None:
                    value = int(risk_level)
                    if value not in self.settings.RISK_TABLE:
                        return False, "Risk level must be 1 to 5."
                    self.risk_level = value
            except (TypeError, ValueError):
                return False, "Those settings are not valid numbers."
            return True, "Settings saved."

    def set_mode(self, new_mode, confirm=""):
        with self.lock:
            if new_mode not in ("demo", "real"):
                return False, "Unknown account type."
            if self.closed or self.expired:
                return False, "Your sign-in has expired. Please sign in again."
            if new_mode == self.mode:
                return True, "Already on this account."
            if self.active_trade or self.proposal_pending:
                return False, "Wait for the open trade to settle before switching accounts."
            if not self.has_account(new_mode):
                return False, ("You have no real account on Deriv yet. Open one at deriv.com first."
                               if new_mode == "real" else "You have no demo account on Deriv.")
            if new_mode == "real":
                if not self.settings.allow_real:
                    return False, "Real-money trading is switched off on this server."
                if not self.acknowledged:
                    return False, "Please accept the risk notice first."
                if (confirm or "").strip() != REAL_CONFIRM_WORD:
                    return False, f"Type {REAL_CONFIRM_WORD} in capitals to confirm."
            self.running = False                    # a mode change never keeps a running bot
            self.session_mode = None
            self.channel.stop()
            self.mode = new_mode
            self.balance = None
            self.counter_key = None
            self.history = []
            self.stake = min(self.stake, self.limits()[2])
            self.say("Switching to your REAL account. The bot is stopped." if new_mode == "real"
                     else "Switching to your demo account. The bot is stopped.")
            self.channel.start()
            return True, "Switching account..."

    def start_bot(self, confirm_real=False):
        with self.lock:
            if self.closed or self.expired:
                return False, "Your sign-in has expired. Please sign in again."
            if not self.acknowledged:
                return False, "Please accept the risk notice first."
            if self.running:
                return True, "The bot is already running."
            if not self.session_ready():
                return False, "Your account is not connected yet. Wait for it to show CONNECTED."
            self._roll_day()
            max_trades, max_losses, stake_cap, loss_stop = self.limits()
            if self.mode == "real" and not (confirm_real and self.settings.allow_real):
                return False, "Real-money start needs your confirmation."
            if not math.isfinite(self.stake) or self.stake < self.settings.MIN_STAKE or self.stake > stake_cap:
                return False, f"Amount must be between {self.settings.MIN_STAKE:g} and {stake_cap:g}."
            c = self.counters
            if c.get("halted"):
                return False, c.get("halt_reason") or "A safety limit stopped trading for today."
            if c["trades_today"] >= max_trades:
                return False, f"You have already reached {max_trades} trades today at this risk level."
            if c["consecutive_losses"] >= max_losses:
                return False, f"Stopped after {max_losses} loss(es) in a row. Come back tomorrow."
            if loss_stop is not None and c["pnl_today"] <= -loss_stop:
                return False, "The daily loss limit has been reached. No more trades today."
            self.running = True
            self.cooldown_until = 0.0
            self.say(f"Bot started on your {self.mode} account: stake {self.stake:g} {self.currency}, "
                     f"{max_trades} trades/day max, stop after {max_losses} losses in a row.")
            return True, "Bot started."

    def stop_bot(self):
        with self.lock:
            if not self.running:
                return True, "The bot is already stopped."
            self._stop("Bot stopped by you.")
            return True, "Bot stopped."

    def _stop(self, reason, latch=False):
        self.running = False
        if latch:
            self.counters["halted"], self.counters["halt_reason"] = True, reason
            self._save_counters()
        self.say(reason)

    # ---- trading
    def _on_market(self, event):
        if event.get("fresh") and event.get("signal") in ("HIGHER", "LOWER"):
            self._maybe_trade(event["signal"])

    def _maybe_trade(self, signal):
        with self.lock:
            if not self.running or not self.session_ready() or not self.acknowledged:
                return
            if not self.hub.streaming or self.active_trade or self.proposal_pending:
                return
            if self._mono() < self.cooldown_until:
                return
            self._roll_day()
            max_trades, max_losses, stake_cap, loss_stop = self.limits()
            c = self.counters
            if c["trades_today"] >= max_trades:
                return self._stop(f"Daily safety stop: {max_trades} trades reached.", latch=True)
            if c["consecutive_losses"] >= max_losses:
                return self._stop(f"Safety stop: {max_losses} loss(es) in a row.", latch=True)
            if loss_stop is not None and c["pnl_today"] <= -loss_stop:
                return self._stop(f"Daily loss limit reached ({c['pnl_today']:+.2f}). No more trades today.",
                                  latch=True)
            stake = self.stake                      # fixed: never changed by wins or losses
            if not math.isfinite(stake) or stake < self.settings.MIN_STAKE or stake > stake_cap:
                return self._stop("Invalid amount. Bot stopped without placing a trade.")
            contract_type = "CALL" if signal == "HIGHER" else "PUT"
            self.trade_meta = {"contract_type": contract_type, "stake": stake}
            self.proposal_pending, self.trade_phase, self.trade_request_at = True, "proposal", self._mono()
            sent = self.channel.send({
                "proposal": 1, "amount": stake, "basis": "stake", "contract_type": contract_type,
                "currency": self.currency, "duration": 5, "duration_unit": "t",
                "underlying_symbol": self.hub.symbol, "req_id": 301})
            if sent:
                self.say(f"{'RISE' if contract_type == 'CALL' else 'FALL'} signal: requesting a "
                         f"{stake:g} {self.currency} 5-tick contract.")
            else:
                self.proposal_pending, self.trade_phase = False, None
                self.say("Could not send the request (connection busy); no trade placed.")

    def _on_message(self, channel, text):
        data = json.loads(text)
        with self.lock:
            if self.closed:
                return
            if data.get("error"):
                err = data["error"]
                detail = f"{err.get('message', 'Unknown error')} ({err.get('code', 'no code')})"
                req_id = data.get("req_id")
                if req_id in (301, 302):
                    self.proposal_pending, self.trade_phase = False, None
                    self.say(f"Trade request rejected: {detail}. No new trade sent.")
                elif req_id == 2:
                    self.say(f"Balance request failed: {detail}")
                else:
                    self.say(f"Account error: {detail}")
                return
            msg_type = data.get("msg_type")
            if msg_type == "balance" or "balance" in data:
                info = data.get("balance") or {}
                if info.get("currency"):
                    self.currency = info["currency"]
                try:
                    self.balance = float(info.get("balance"))
                except (TypeError, ValueError):
                    pass
            elif msg_type == "proposal" or "proposal" in data:
                self._on_proposal(channel, data.get("proposal") or {})
            elif msg_type == "buy" or "buy" in data:
                self._on_buy(channel, data.get("buy") or {})
            elif msg_type == "proposal_open_contract" or "proposal_open_contract" in data:
                self._on_contract(channel, data)

    def _on_proposal(self, channel, proposal):
        if not self.proposal_pending or not self.running or not self.session_ready():
            self.proposal_pending, self.trade_phase = False, None      # late or unwanted quote: do not buy it
            return
        proposal_id, ask = proposal.get("id"), proposal.get("ask_price")
        if not proposal_id or ask is None:
            self.proposal_pending, self.trade_phase = False, None
            self.say("The price quote was incomplete; no trade was placed.")
            return
        self.trade_phase, self.trade_request_at = "buy", self._mono()
        if not channel.send({"buy": proposal_id, "price": float(ask), "req_id": 302}):   # price capped at the quote
            self.proposal_pending, self.trade_phase = False, None
            self.say("Connection closed before the purchase was sent; no trade placed.")

    def _on_buy(self, channel, buy):
        contract_id = buy.get("contract_id")
        self.proposal_pending, self.trade_phase = False, None
        if not contract_id:
            self.say("The purchase reply had no contract ID; check your Deriv account history.")
            return
        self.active_trade, self.contract_id = True, contract_id
        self.counters["trades_today"] += 1
        self._save_counters()
        self.say(f"Trade opened (contract {contract_id}). Waiting for the result.")
        channel.send({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1, "req_id": 303})

    def _on_contract(self, channel, data):
        contract = data.get("proposal_open_contract") or {}
        try:
            sold = int(contract.get("is_sold", 0)) == 1
        except (TypeError, ValueError):
            sold = False
        if not sold or not self.active_trade or str(contract.get("contract_id")) != str(self.contract_id):
            return
        try:
            profit = float(contract.get("profit", 0.0) or 0.0)
        except (TypeError, ValueError):
            profit = 0.0
        self.active_trade, self.proposal_pending, self.contract_id = False, False, None
        c = self.counters
        if profit < 0:
            c["consecutive_losses"] += 1
        elif profit > 0:
            c["consecutive_losses"] = 0                 # a zero-profit result neither counts as a loss nor resets the streak
        c["pnl_today"] = round(c["pnl_today"] + profit, 2)
        self._save_counters()
        outcome = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "TIE / ZERO")
        self.say(f"Trade finished: {outcome}, {profit:+.2f} {self.currency}.")
        row = {"time_local": datetime.fromtimestamp(self._wall(), self.settings.tz).isoformat(timespec="seconds"),
               "mode": self.mode, "account_id": self.account_id, "contract_id": contract.get("contract_id"),
               "direction": contract.get("contract_type") or self.trade_meta.get("contract_type"),
               "stake": self.trade_meta.get("stake"), "buy_price": contract.get("buy_price"),
               "sell_price": contract.get("sell_price"), "profit": round(profit, 2), "result": outcome,
               "consecutive_losses": c["consecutive_losses"], "trades_today": c["trades_today"],
               "net_pnl_today": c["pnl_today"]}
        try:
            tradelog.append_trade(self._log_path(), row)
        except OSError:
            self.say("Warning: could not write the trade log.")
        self.history.append({k: ("" if v is None else str(v)) for k, v in row.items()})
        del self.history[:-200]
        self.cooldown_until = self._mono() + self.frequency
        channel.send({"balance": 1, "subscribe": 1, "req_id": 2})
        sub_id = (data.get("subscription") or {}).get("id")
        if sub_id:
            channel.send({"forget": sub_id})
        max_trades, max_losses, _, loss_stop = self.limits()
        if self.running:
            if c["consecutive_losses"] >= max_losses:
                self._stop(f"Safety stop: {max_losses} loss(es) in a row. Trading is off until tomorrow.", latch=True)
            elif loss_stop is not None and c["pnl_today"] <= -loss_stop:
                self._stop("Daily loss limit reached. No more trades today.", latch=True)
            elif c["trades_today"] >= max_trades:
                self._stop(f"Daily stop: {max_trades} trades completed.", latch=True)
        if self.expired:
            self.channel.stop()

    # ---- housekeeping (called about once a second by SessionStore)
    def housekeeping(self):
        with self.lock:
            if self.closed:
                return
            now = self._mono()
            if self.proposal_pending and now - self.trade_request_at > TRADE_RESPONSE_TIMEOUT:
                phase = self.trade_phase
                self.proposal_pending, self.trade_phase = False, None
                if phase == "buy":
                    self._stop("No reply to the purchase request. A trade may exist: check your Deriv account "
                               "history before starting again.")
                else:
                    self.say("No price quote arrived in time; no trade was placed.")
            if not self.expired and self._wall() >= self.expires_at:
                self._expire("Your Deriv sign-in has expired (it lasts about an hour). Sign in again to continue.")
            if self.expired and not self.active_trade and self.channel.state != DISCONNECTED:
                self.channel.stop()
            self._roll_day()

    def _expire(self, reason):
        with self.lock:
            if self.expired:
                return
            self.expired, self.expired_at = True, self._mono()
            self._token = None
            if self.running:
                self.running = False
            self.say(reason)

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed, self.running = True, False
            self._token = None
        self.hub.remove_listener(self._on_market)
        self.channel.stop()

    @property
    def dead(self):
        with self.lock:
            idle = self._mono() - self.last_seen
            if self.closed:
                return True
            if self.expired and self.expired_at is not None and self._mono() - self.expired_at > EXPIRED_KEEP_SECONDS:
                return True
            return not self.running and not self.active_trade and idle > IDLE_DROP_SECONDS

    # ---- what the browser sees (never contains the token or one-time password)
    def snapshot(self):
        with self.lock:
            self._roll_day()
            max_trades, max_losses, stake_cap, loss_stop = self.limits()
            market = self.hub.snapshot()
            c = self.counters
            if self.expired:
                bot_state = "EXPIRED"
            elif c.get("halted"):
                bot_state = "LIMIT"
            elif self.running:
                bot_state = ("PAUSED" if not self.session_ready() or not market["streaming"]
                             else "IN_TRADE" if self.active_trade else "RUNNING")
            else:
                bot_state = "READY" if self.session_ready() else "STOPPED"
            wins = sum(1 for r in self.history if (r.get("result") or "") == "WIN")
            losses = sum(1 for r in self.history if (r.get("result") or "") == "LOSS")
            net = 0.0
            for r in self.history:
                try:
                    net += float(r.get("profit") or 0)
                except ValueError:
                    pass
            recent = [{"time": (r.get("time_local") or "")[11:19], "result": r.get("result"),
                       "profit": r.get("profit"), "stake": r.get("stake"), "direction": r.get("direction")}
                      for r in reversed(self.history[-15:])]
            return {
                "authenticated": True,
                "mode": self.mode, "account_id": self.account_id, "currency": self.currency,
                "has_demo": self.has_account("demo"), "has_real": self.has_account("real"),
                "allow_real": self.settings.allow_real,
                "balance": self.balance, "account_status": ("VERIFIED" if self.session_ready()
                                                            else self.STATUS_LABELS.get(self.account_state, "OFFLINE")),
                "market": market,
                "bot": {"state": bot_state, "running": self.running, "stake": self.stake,
                        "frequency": self.frequency, "risk_level": self.risk_level,
                        "min_stake": self.settings.MIN_STAKE, "stake_cap": stake_cap,
                        "in_trade": self.active_trade, "halted": bool(c.get("halted"))},
                "limits": {"trades_today": c["trades_today"], "max_trades": max_trades,
                           "losses": c["consecutive_losses"], "max_losses": max_losses,
                           "pnl_today": round(c["pnl_today"], 2), "loss_stop": loss_stop},
                "risk_table": {str(k): list(v) for k, v in self.settings.RISK_TABLE.items()},
                "acknowledged": self.acknowledged,
                "expires_in": max(0, int(self.expires_at - self._wall())) if not self.expired else 0,
                "expired": self.expired,
                "messages": [{"t": t, "text": m} for t, m in list(self.messages)[-8:]],
                "recent": recent,
                "summary": {"n": len(self.history), "wins": wins, "losses": losses, "net": round(net, 2),
                            "win_rate": (round(wins / (wins + losses) * 100, 1) if wins + losses else None)},
            }


# --------------------------------------------------------------------------- sessions
class SessionStore:
    def __init__(self, settings, hub, store, bot_factory=None):
        self.settings, self.hub, self.store = settings, hub, store
        self._factory = bot_factory or (lambda token, expires, accounts: UserBot(settings, hub, store, token,
                                                                               expires, accounts))
        self._sessions = {}
        self._lock = threading.RLock()
        self._sweeper = None

    def create(self, token, expires_in, accounts):
        """Open a session. A new sign-in replaces an older one for the same Deriv account, unless that
        older session has a trade in flight (two bots must never trade one account)."""
        ids = {a["id"] for a in accounts if a.get("id")}
        with self._lock:
            self.sweep()
            clashing = [(sid, bot) for sid, bot in self._sessions.items() if bot.account_ids() & ids]
            for _, bot in clashing:
                with bot.lock:
                    if bot.active_trade or bot.proposal_pending:
                        raise SessionConflict("A trade is still running from another sign-in on this account. "
                                              "Wait for it to finish, then sign in again.")
            for sid, bot in clashing:
                bot.shutdown()
                del self._sessions[sid]
            if len(self._sessions) >= self.settings.max_users:
                raise TooManyUsers("The bot is full right now. Please try again later.")
            bot = self._factory(token, expires_in, accounts)
            sid = secrets.token_urlsafe(32)
            bot.sid = sid
            self._sessions[sid] = bot
        self.hub.start()
        bot.connect()
        return sid, bot

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            bot = self._sessions.get(sid)
        if bot is not None and bot.closed:
            return None
        if bot is not None:
            bot.touch()
        return bot

    def remove(self, sid):
        with self._lock:
            bot = self._sessions.pop(sid, None)
        if bot is not None:
            bot.shutdown()
        return bot is not None

    def sweep(self):
        with self._lock:
            items = list(self._sessions.items())
        for sid, bot in items:
            try:
                bot.housekeeping()
            except Exception:
                pass
            if bot.dead:
                self.remove(sid)

    def count(self):
        with self._lock:
            return len(self._sessions)

    def start_sweeper(self, interval=1.0):
        if self._sweeper is not None:
            return

        def loop():
            while True:
                time.sleep(interval)
                self.sweep()
        self._sweeper = threading.Thread(target=loop, daemon=True, name="session-sweeper")
        self._sweeper.start()


def fetch_accounts(token):
    """List the user's Deriv accounts with their fresh OAuth token (no App-ID header is needed for OAuth)."""
    return normalize_accounts(http_json("GET", ACCOUNTS_PATH, token, None))
