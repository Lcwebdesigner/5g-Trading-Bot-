"""Offline tests: no network, no real Tk window, no real websocket-client.

They drive deriv_connection.py and main.py with test doubles (fake Tk, fake WebSocket, fake REST).
They prove the connection state machine, the demo-only guard, the trade limits and the credential
redaction behave as designed. They do NOT prove that Deriv's live servers accept the requests.

Run:  python -m unittest -v test_offline
"""
import io
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deriv_connection as dc

TOKEN = "pat_SECRETTOKEN123456"
OTP = "otpvalue98765"
DEMO_URL = f"wss://api.derivws.com/trading/v1/options/ws/demo?otp={OTP}"
REAL_URL = f"wss://api.derivws.com/trading/v1/options/ws/real?otp={OTP}"


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ fakes
class FakeResp:
    def __init__(self, obj, status=200):
        self._b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_err(code, obj):
    return urllib.error.HTTPError("https://api.derivws.com/x", code, "err", {}, io.BytesIO(json.dumps(obj).encode()))


class FakeRest:
    def __init__(self):
        self.routes, self.requests = {}, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        path = req.full_url.replace(dc.REST_BASE, "")
        result = self.routes[(req.get_method(), path)]
        if isinstance(result, Exception):
            raise result
        return FakeResp(result)


class FakeWS:
    def __init__(self, net, url, on_open, on_message, on_error, on_close):
        self.net, self.url = net, url
        self.on_open, self.on_message, self.on_error, self.on_close = on_open, on_message, on_error, on_close
        self.sent, self.closed = [], threading.Event()

    def run_forever(self, ping_interval=0, ping_timeout=None):
        if self.net.auto_open:
            self.on_open(self)
        self.closed.wait()
        self.on_close(self, 1000, "bye")

    def close(self):
        self.closed.set()

    def send(self, text):
        self.sent.append(json.loads(text))

    def open(self):
        self.on_open(self)

    def message(self, obj):
        self.on_message(self, json.dumps(obj))

    def error(self, text):
        self.on_error(self, Exception(text))


class FakeNet:
    def __init__(self, auto_open=True):
        self.auto_open, self.sockets = auto_open, []

    def factory(self, url, on_open, on_message, on_error, on_close):
        ws = FakeWS(self, url, on_open, on_message, on_error, on_close)
        self.sockets.append(ws)
        return ws


def fast_timers():
    dc.CONNECT_TIMEOUT, dc.BACKOFF_STEPS = 0.8, (0.1, 0.2)


# ------------------------------------------------------------------ deriv_connection
class TokenAndRedaction(unittest.TestCase):
    def write(self, data):
        p = Path(tempfile.mkdtemp()) / "deriv_token.txt"
        p.write_bytes(data)
        return p

    def test_encodings(self):
        for raw in (TOKEN.encode(), b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n",
                    ("﻿" + TOKEN + "\r\n").encode("utf-16"), f'"{TOKEN}"\n'.encode(),
                    f"Bearer {TOKEN}".encode()):
            self.assertEqual(dc.read_token(self.write(raw)), TOKEN)

    def test_bad_files(self):
        for raw in (b"", b"   \n", b"two words", b"a\nb"):
            with self.assertRaises(dc.DerivApiError) as cm:
                dc.read_token(self.write(raw))
            self.assertEqual(cm.exception.kind, "config")
        with self.assertRaises(dc.DerivApiError):
            dc.read_token(Path(tempfile.mkdtemp()) / "missing.txt")
        with self.assertRaises(dc.DerivApiError):
            dc.read_token(self.write(b"\xff\xfe\xfa"))      # undecodable

    def test_redaction(self):
        dc.register_secret(TOKEN)
        text = dc.redact(f"failed {TOKEN} url wss://x/ws/demo?otp={OTP}&a=1 Authorization: Bearer abcdef123456")
        for secret in (TOKEN, OTP, "abcdef123456"):
            self.assertNotIn(secret, text)

    def test_app_id_check(self):
        for bad in ("", None, "PASTE_YOUR_APP_ID_HERE", "YOUR_APP_ID"):
            with self.assertRaises(dc.DerivApiError):
                dc.check_app_id(bad)
        self.assertEqual(dc.check_app_id(" 12345 "), "12345")


class DemoGuard(unittest.TestCase):
    def test_accepts_demo(self):
        dc.assert_demo_ws_url(DEMO_URL)

    def test_refuses_everything_else(self):
        for url in (REAL_URL, "ws://api.derivws.com/trading/v1/options/ws/demo?otp=abc123",
                    "wss://evil.example.com/trading/v1/options/ws/demo?otp=abc123",
                    "wss://api.derivws.com.evil.com/ws/demo?otp=abc123",
                    "wss://api.derivws.com/trading/v1/options/ws/demo",
                    "wss://api.derivws.com/trading/v1/options/ws/public?otp=abc123", ""):
            with self.assertRaises(dc.DerivApiError, msg=url) as cm:
                dc.assert_demo_ws_url(url)
            self.assertNotIn("abc123", str(cm.exception))

    def test_real_guard(self):
        dc.assert_ws_url(REAL_URL, "real")
        for url, mode in ((DEMO_URL, "real"), (REAL_URL, "demo"), (REAL_URL, "paper"),
                          ("ws://api.derivws.com/trading/v1/options/ws/real?otp=abc123", "real"),
                          ("wss://evil.example.com/trading/v1/options/ws/real?otp=abc123", "real"),
                          ("wss://api.derivws.com/trading/v1/options/ws/real", "real")):
            with self.assertRaises(dc.DerivApiError, msg=(url, mode)):
                dc.assert_ws_url(url, mode)

    def test_pick_real_account_fails_closed(self):
        accs = dc.normalize_accounts({"data": [
            {"account_id": "DOT2", "account_type": "demo"}, {"account_id": "ROT1", "account_type": "real"},
            {"account_id": "UNK9", "foo": "bar"}]})
        self.assertEqual(dc.pick_real_account(accs)["id"], "ROT1")
        self.assertEqual(dc.pick_real_account(accs, "ROT1")["id"], "ROT1")
        for override in ("DOT2", "UNK9", "NOPE"):
            with self.assertRaises(dc.DerivApiError, msg=override):
                dc.pick_real_account(accs, override)          # demo, unlabelled, or unknown
        with self.assertRaises(dc.DerivApiError):
            dc.pick_real_account([accs[0], accs[2]])           # no explicitly-real account

    def test_pick_account(self):
        accs = dc.normalize_accounts({"data": [
            {"account_id": "ROT1", "account_type": "real", "currency": "USD"},
            {"account_id": "DOT2", "account_type": "demo", "currency": "USD"}]})
        self.assertEqual(dc.pick_demo_account(accs)["id"], "DOT2")
        self.assertEqual(dc.pick_demo_account(accs, "DOT2")["id"], "DOT2")
        with self.assertRaises(dc.DerivApiError):
            dc.pick_demo_account(accs, "ROT1")            # override naming a real account
        with self.assertRaises(dc.DerivApiError):
            dc.pick_demo_account(accs, "NOPE")
        with self.assertRaises(dc.DerivApiError):
            dc.pick_demo_account([accs[0]])               # no demo at all
        unknown = dc.normalize_accounts({"data": [{"account_id": "X1", "foo": "bar"}]})
        with self.assertRaises(dc.DerivApiError) as cm:
            dc.pick_demo_account(unknown)                 # fail closed + show field names
        self.assertIn("foo", str(cm.exception))
        self.assertEqual(dc.pick_demo_account(unknown, "X1")["id"], "X1")   # still behind the URL guard
        flag = dc.normalize_accounts({"data": [{"loginid": "V1", "is_virtual": 1}]})
        self.assertEqual(dc.pick_demo_account(flag)["id"], "V1")
        with self.assertRaises(dc.DerivApiError):
            dc.normalize_accounts({"nodata": 1})


class Rest(unittest.TestCase):
    def setUp(self):
        self.rest = FakeRest()
        dc._urlopen = self.rest
        dc.register_secret(TOKEN)

    def tearDown(self):
        import urllib.request
        dc._urlopen = urllib.request.urlopen

    def call(self, result):
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = result
        return dc.http_json("GET", dc.ACCOUNTS_PATH, TOKEN, "12345")

    def test_headers(self):
        self.call({"data": []})
        req = self.rest.requests[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(req.get_header("Deriv-app-id"), "12345")
        self.assertIn("DerivDesktopTraderPro", req.get_header("User-agent"))

    def test_status_mapping(self):
        cases = [(401, {"errors": [{"code": "Unauthorized", "message": "Deriv-App-ID header is required for PAT tokens"}]},
                  "auth", False, "APP_ID"),
                 (401, {"errors": [{"code": "Unauthorized", "message": "Invalid or missing authentication credentials"}]},
                  "auth", False, "deriv_token.txt"),
                 (403, {"errors": [{"code": "AccessDenied", "message": "nope"}]}, "forbidden", False, "trade"),
                 (429, {"errors": [{"code": "RateLimit", "message": "slow"}]}, "rate_limit", True, "Rate"),
                 (504, {}, "server", True, "504"), (404, {}, "protocol", False, "404")]
        for status, body, kind, retry, needle in cases:
            with self.assertRaises(dc.DerivApiError) as cm:
                self.call(http_err(status, body))
            e = cm.exception
            self.assertEqual((e.kind, e.retryable), (kind, retry), status)
            self.assertIn(needle, str(e))
            self.assertNotIn(TOKEN, str(e))

    def test_network_failures(self):
        import socket
        for exc, needle in ((urllib.error.URLError(socket.gaierror(11001, "x")), "DNS"),
                            (urllib.error.URLError(socket.timeout()), "timed out"),
                            (urllib.error.URLError(ConnectionRefusedError("refused")), "refused")):
            with self.assertRaises(dc.DerivApiError) as cm:
                self.call(exc)
            self.assertTrue(cm.exception.retryable)
            self.assertEqual(cm.exception.kind, "network")
            self.assertIn(needle, str(cm.exception))

    def test_non_json(self):
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = b"<html>blocked</html>"
        with self.assertRaises(dc.DerivApiError) as cm:
            dc.http_json("GET", dc.ACCOUNTS_PATH, TOKEN, "1")
        self.assertEqual(cm.exception.kind, "protocol")

    def test_real_flow_and_mismatched_urls_refused(self):
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = {"data": [
            {"account_id": "ROT1", "account_type": "real", "currency": "USD"},
            {"account_id": "DOT2", "account_type": "demo", "currency": "USD"}]}
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="ROT1"))] = {"data": {"url": REAL_URL}}
        url, acc = dc.get_ws_url(TOKEN, "12345", "real")
        self.assertEqual((url, acc["id"]), (REAL_URL, "ROT1"))
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="ROT1"))] = {"data": {"url": DEMO_URL}}
        with self.assertRaises(dc.DerivApiError) as cm:
            dc.get_ws_url(TOKEN, "12345", "real")              # real account but demo endpoint: refused
        self.assertIn("not a real endpoint", str(cm.exception))
        with self.assertRaises(dc.DerivApiError):
            dc.get_ws_url(TOKEN, "12345", "paper")

    def test_full_flow_and_real_url_refused(self):
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = {"data": [
            {"account_id": "ROT1", "account_type": "real"}, {"account_id": "DOT2", "account_type": "demo", "currency": "USD"}]}
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="DOT2"))] = {"data": {"url": DEMO_URL}}
        url, acc = dc.get_demo_ws_url(TOKEN, "12345")
        self.assertEqual((url, acc["id"]), (DEMO_URL, "DOT2"))
        self.assertEqual(self.rest.requests[-1].get_method(), "POST")
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="DOT2"))] = {"data": {"url": REAL_URL}}
        with self.assertRaises(dc.DerivApiError) as cm:
            dc.get_demo_ws_url(TOKEN, "12345")
        self.assertIn("not a demo endpoint", str(cm.exception))
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="DOT2"))] = {"data": {}}
        with self.assertRaises(dc.DerivApiError):
            dc.get_demo_ws_url(TOKEN, "12345")


class ChannelBehaviour(unittest.TestCase):
    def setUp(self):
        fast_timers()
        self.states, self.msgs, self.logs, self.disc = [], [], [], []
        self.net = FakeNet()
        self.ch = None

    def tearDown(self):
        if self.ch:
            self.ch.stop()

    def make(self, provider=lambda: "wss://x/y", **kw):
        self.ch = dc.Channel("t", provider, lambda c, m: self.msgs.append(m), ws_factory=self.net.factory,
                             on_state=lambda n, s, d: self.states.append((s, d)),
                             on_log=lambda n, t: self.logs.append(t),
                             on_disconnect=lambda: self.disc.append(1), **kw)
        return self.ch

    def test_connect_message_send_stop(self):
        ch = self.make(on_open=lambda c: c.send({"hello": 1}))
        self.assertTrue(ch.start())
        self.assertFalse(ch.start())                                  # already running
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED))
        ws = self.net.sockets[0]
        self.assertEqual(ws.sent, [{"hello": 1}])
        ws.message({"a": 1})
        self.assertTrue(wait_until(lambda: self.msgs))
        ch.stop()
        self.assertEqual(ch.state, dc.DISCONNECTED)
        self.assertFalse(ch.send({"x": 1}))
        self.assertTrue(wait_until(lambda: not ch._thread.is_alive()))
        self.assertEqual(self.states[0][0], dc.CONNECTING)

    def test_handshake_hang_times_out_then_retries(self):
        self.net.auto_open = False                                   # server never answers
        ch = self.make()
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.RETRYING, 4), self.states)
        self.assertIn("Timed out", ch.detail)
        self.net.auto_open = True
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED, 4), self.states)
        self.assertGreaterEqual(len(self.net.sockets), 2)

    def test_idle_socket_is_recycled(self):
        ch = self.make(idle_timeout=0.7)
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED))
        self.assertTrue(wait_until(lambda: any(s == dc.RETRYING and "No data" in d for s, d in self.states), 4))
        self.assertTrue(wait_until(lambda: len(self.net.sockets) >= 2, 4))

    def test_non_retryable_provider_error_fails_and_stops(self):
        def provider():
            raise dc.DerivApiError("config", "token file empty")
        ch = self.make(provider)
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.FAILED))
        self.assertIn("token file empty", ch.detail)
        self.assertEqual(self.net.sockets, [])
        self.assertTrue(wait_until(lambda: not ch._thread.is_alive()))
        self.assertTrue(ch.start())                                  # can be restarted after fixing the file

    def test_retryable_provider_error_backs_off(self):
        calls = []

        def provider():
            calls.append(1)
            if len(calls) < 3:
                raise dc.DerivApiError("network", "no internet", retryable=True)
            return "wss://x/y"
        ch = self.make(provider)
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED, 4))
        self.assertEqual(len(calls), 3)
        self.assertTrue(any(s == dc.RETRYING and "no internet" in d for s, d in self.states))

    def test_unexpected_exception_is_reported(self):
        def provider():
            raise ZeroDivisionError("boom")
        ch = self.make(provider)
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.FAILED))
        self.assertIn("boom", ch.detail)

    def test_factory_config_error(self):
        def factory(*a):
            raise dc.DerivApiError("config", "install websocket-client")
        ch = self.make()
        ch._factory = factory
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.FAILED))
        self.assertIn("websocket-client", ch.detail)

    def test_error_text_is_redacted_and_reconnect_after_server_close(self):
        ch = self.make()
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED))
        ws = self.net.sockets[0]
        ws.error(f"Handshake status 401 url: wss://api.derivws.com/ws/demo?otp={OTP}")
        ws.closed.set()
        self.assertTrue(wait_until(lambda: ch.state == dc.RETRYING))
        self.assertNotIn(OTP, ch.detail)
        self.assertIn("401", ch.detail)
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED))
        self.assertTrue(self.disc)

    def test_restart_while_old_worker_still_unwinding(self):
        gate, calls = threading.Event(), []

        def provider():
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)                                         # old run stuck in a slow REST call
                raise dc.DerivApiError("config", "stale failure from the old run")
            return "wss://x/y"
        ch = self.make(provider)
        ch.start()
        self.assertTrue(wait_until(lambda: len(calls) == 1))
        ch.stop()
        self.assertEqual(ch.state, dc.DISCONNECTED)
        self.assertTrue(ch.start())                                  # must be allowed immediately
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED))
        gate.set()                                                   # old worker now fails...
        time.sleep(0.3)
        self.assertEqual(ch.state, dc.CONNECTED)                     # ...and must not disturb the new run
        self.assertIsNotNone(ch._ws)
        self.assertFalse(any("stale failure" in d for _, d in self.states))

    def test_stale_socket_callbacks_ignored(self):
        ch = self.make()
        ch.start()
        self.assertTrue(wait_until(lambda: ch.state == dc.CONNECTED))
        old = self.net.sockets[0]
        old.closed.set()
        self.assertTrue(wait_until(lambda: len(self.net.sockets) >= 2 and ch.state == dc.CONNECTED))
        before = len(self.msgs)
        old.message({"late": 1})
        time.sleep(0.1)
        self.assertEqual(len(self.msgs), before)


# ------------------------------------------------------------------ main.py with a fake Tk
def install_fake_tk():
    tk = types.ModuleType("tkinter")

    class TclError(Exception):
        pass

    class Var:
        def __init__(self, value=""):
            self.v = value

        def set(self, v):
            self.v = v

        def get(self):
            return self.v

    class Root:
        def __init__(self):
            self.after_calls = []

        def after(self, ms, fn=None, *a):
            self.after_calls.append((ms, fn))

        def __getattr__(self, name):
            return MagicMock(name=name)

    tk.TclError, tk.StringVar, tk.Tk = TclError, Var, Root
    ttk = types.ModuleType("tkinter.ttk")
    for n in ("Style", "Frame", "Label", "Button", "Entry", "Treeview", "Scrollbar"):
        setattr(ttk, n, MagicMock(name=n))
    tk.ttk, tk.messagebox, tk.filedialog = ttk, MagicMock(name="messagebox"), MagicMock(name="filedialog")
    tk.simpledialog = MagicMock(name="simpledialog")
    sys.modules["tkinter.simpledialog"] = tk.simpledialog
    sys.modules.update({"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": tk.messagebox,
                        "tkinter.filedialog": tk.filedialog})


install_fake_tk()
import main  # noqa: E402  (imports with the fake Tk installed; builds the UI against mocks)
STARTUP = {"mode": main.trading_mode, "limits": (main.MAX_TRADES_PER_DAY, main.MAX_CONSECUTIVE_LOSSES),
           "stake_cap": main.max_stake, "session": main.session_mode}   # values straight after import


def drain():
    main.pump_ui()


def var(v):
    drain()
    return v.get()


class MainAppTests(unittest.TestCase):
    def setUp(self):
        fast_timers()
        self.tmp = Path(tempfile.mkdtemp())
        main.BOT_STATE_FILE = self.tmp / "state.json"
        main.RESULT_FILE = self.tmp / "results.csv"
        main.TOKEN_FILE = self.tmp / "deriv_token.txt"
        main.ACCOUNT_FILE = self.tmp / "deriv_account_id.txt"
        main.APP_ID = "12345"
        self.rest = FakeRest()
        dc._urlopen = self.rest
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = {"data": [
            {"account_id": "ROT1", "account_type": "real", "currency": "USD"},
            {"account_id": "DOT2", "account_type": "demo", "currency": "USD"}]}
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="DOT2"))] = {"data": {"url": DEMO_URL}}
        self.mnet, self.anet = FakeNet(), FakeNet()
        main.market_channel._factory = self.mnet.factory
        main.account_channel._factory = self.anet.factory
        main.auto_bot_running = main.active_trade = main.proposal_pending = False
        main.session_mode, main.trading_mode, main.pnl_today = None, "demo", 0.0
        main.REAL_STATE_FILE = self.tmp / "real_state.json"
        main.TRADE_LOG_FILE = self.tmp / "trade_log.csv"
        main.TICK_FILE = self.tmp / "ticks.csv"
        main.last_recorded_epoch = 0
        main.RECORD_TICKS = True
        main.trade_meta.clear()
        main.REAL_ACCOUNT_FILE = self.tmp / "deriv_real_account_id.txt"
        main.apply_mode_limits()
        main.messagebox.askyesno.return_value = True
        main.simpledialog.askstring.return_value = "REAL"
        main.trade_phase = main.current_contract_id = None
        main.trades_today = main.consecutive_losses = 0
        main.configured_stake = 2.0
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="ROT1"))] = {"data": {"url": REAL_URL}}
        main.pending.clear(); main.prices.clear()
        main.last_signal_queued = main.last_auto_signal = "WAIT"
        main.closing = False
        main.account_currency = "USD"
        main.stake_var.set("2.00")
        while not main.ui_queue.empty():
            main.ui_queue.get_nowait()

    def tearDown(self):
        main.market_channel.stop()
        main.account_channel.stop()
        import urllib.request
        dc._urlopen = urllib.request.urlopen

    def connect_demo(self):
        main.TOKEN_FILE.write_text(TOKEN, encoding="utf-8")
        main.connect_account()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.CONNECTED), main.account_channel.detail)
        return self.anet.sockets[-1]

    def start_bot_running(self):
        ws = self.connect_demo()
        main.auto_bot_running = True
        return ws

    # ---- connection reporting
    def test_market_feed_states_and_subscription(self):
        main.start_market()
        self.assertTrue(wait_until(lambda: main.market_channel.state == dc.CONNECTED))
        ws = self.mnet.sockets[0]
        self.assertEqual(ws.url, dc.PUBLIC_WS_URL)
        self.assertIn({"ticks": "R_100", "subscribe": 1, "req_id": 1}, ws.sent)
        self.assertEqual(var(main.market_status_var), "CONNECTED — WAITING FOR TICKS")
        ws.message({"msg_type": "tick", "tick": {"quote": 1234.5678, "symbol": "R_100"}})
        self.assertTrue(wait_until(lambda: len(main.prices) == 1))
        self.assertEqual(var(main.market_status_var), "CONNECTED — STREAMING")
        self.assertEqual(var(main.price_var), "1,234.5678")
        ws.message({"error": {"code": "X", "message": "bad"}})
        time.sleep(0.1)
        self.assertIn("bad", var(main.status_var))

    def test_market_outage_is_visible_and_clears_state(self):
        main.start_market()
        self.assertTrue(wait_until(lambda: main.market_channel.state == dc.CONNECTED))
        ws = self.mnet.sockets[0]
        main.pending.append({"signal": "HIGHER", "price": 1.0, "ticks_seen": 2, "timestamp": "t"})
        ws.error("connection reset")
        ws.closed.set()
        self.assertTrue(wait_until(lambda: main.market_channel.state == dc.RETRYING))
        self.assertEqual(var(main.market_status_var), "RECONNECTING...")
        self.assertEqual(var(main.price_var), "Offline")
        self.assertEqual(main.pending, [])
        self.assertIsNone(main.last_auto_signal)

    def test_market_handshake_hang_shows_timeout(self):
        self.mnet.auto_open = False
        main.start_market()
        self.assertTrue(wait_until(lambda: main.market_channel.state == dc.RETRYING, 4))
        self.assertIn("Timed out", var(main.status_var))
        self.assertEqual(var(main.market_status_var), "RECONNECTING...")

    def test_missing_token_never_leaves_balance_on_connecting(self):
        main.connect_account()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.FAILED))
        self.assertEqual(var(main.balance_var), "Unavailable")
        self.assertEqual(var(main.account_status_var), "FAILED")
        self.assertEqual(var(main.account_var), "Account: not connected")
        self.assertIn("deriv_token.txt not found", var(main.status_var))
        self.assertEqual(self.anet.sockets, [])

    def test_rejected_token_shows_reason_without_token(self):
        main.TOKEN_FILE.write_text(TOKEN)
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = http_err(401, {"errors": [{"code": "Unauthorized", "message": "Invalid"}]})
        main.connect_account()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.FAILED))
        text = var(main.status_var)
        self.assertIn("401", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(var(main.balance_var), "Unavailable")

    def test_no_internet_retries_and_reports(self):
        main.TOKEN_FILE.write_text(TOKEN)
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = urllib.error.URLError(ConnectionRefusedError("refused"))
        main.connect_account()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.RETRYING))
        self.assertEqual(var(main.balance_var), "Reconnecting...")
        self.assertIn("api.derivws.com", var(main.status_var))
        self.assertIsNone(main.session_mode)

    def test_demo_session_verified_and_balance_shown(self):
        ws = self.connect_demo()
        self.assertEqual(ws.url, DEMO_URL)
        self.assertEqual(main.session_mode, "demo")
        self.assertIn({"balance": 1, "subscribe": 1, "req_id": 2}, ws.sent)
        self.assertEqual(var(main.account_status_var), "DEMO ACCOUNT VERIFIED")
        self.assertEqual(var(main.account_var), "Account: DOT2 (DEMO)")
        ws.message({"msg_type": "balance", "balance": {"balance": 9999.5, "currency": "USD", "loginid": "DOT2"}})
        self.assertTrue(wait_until(lambda: var(main.balance_var) == "9999.5 USD"))
        self.assertNotIn(OTP, var(main.status_var))
        self.assertNotIn(TOKEN, var(main.status_var))

    def test_real_endpoint_is_refused_and_no_socket_opened(self):
        main.TOKEN_FILE.write_text(TOKEN)
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="DOT2"))] = {"data": {"url": REAL_URL}}
        main.auto_bot_running = True
        main.connect_account()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.FAILED))
        self.assertIsNone(main.session_mode)
        self.assertEqual(self.anet.sockets, [])
        self.assertIn("not a demo endpoint", var(main.status_var))
        self.assertFalse(main.auto_bot_running)                 # bot stopped
        self.assertEqual(var(main.bot_state_var), "STOPPED")

    def test_account_reconnect_drops_verification(self):
        ws = self.connect_demo()
        ws.error("lost")
        ws.closed.set()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.RETRYING))
        self.assertIsNone(main.session_mode)
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.CONNECTED))
        self.assertEqual(main.session_mode, "demo")
        self.assertEqual(len([r for r in self.rest.requests if r.get_method() == "POST"]), 2)   # fresh OTP each time

    # ---- trading flow and safety limits
    def run_trade_to_settlement(self, ws, profit, cid=111):
        main.request_trade("HIGHER")
        proposal = ws.sent[-1]
        ws.message({"msg_type": "proposal", "proposal": {"id": "prop-1", "ask_price": 2.0}})
        self.assertTrue(wait_until(lambda: ws.sent[-1].get("buy") == "prop-1"))
        ws.message({"msg_type": "buy", "buy": {"contract_id": cid}})
        self.assertTrue(wait_until(lambda: main.active_trade))
        ws.message({"msg_type": "proposal_open_contract", "subscription": {"id": "sub-9"},
                    "proposal_open_contract": {"contract_id": cid, "is_sold": 1, "profit": profit,
                                                   "buy_price": 2.0, "sell_price": round(2.0 + profit, 2)}})
        self.assertTrue(wait_until(lambda: not main.active_trade))
        return proposal

    def test_trade_message_shapes(self):
        ws = self.start_bot_running()
        proposal = self.run_trade_to_settlement(ws, 1.5)
        self.assertEqual(proposal, {"proposal": 1, "amount": 2.0, "basis": "stake", "contract_type": "CALL",
                                    "currency": "USD", "duration": 5, "duration_unit": "t",
                                    "underlying_symbol": "R_100", "req_id": 301})
        self.assertNotIn("symbol", proposal)
        self.assertIn({"buy": "prop-1", "price": 2.0, "req_id": 302}, ws.sent)
        self.assertIn({"proposal_open_contract": 1, "contract_id": 111, "subscribe": 1, "req_id": 303}, ws.sent)
        self.assertIn({"forget": "sub-9"}, ws.sent)
        self.assertEqual((main.trades_today, main.consecutive_losses), (1, 0))
        self.assertEqual(json.loads(main.BOT_STATE_FILE.read_text())["trades_today"], 1)
        main.request_trade("LOWER")
        self.assertEqual(ws.sent[-1]["contract_type"] if "contract_type" in ws.sent[-1] else None, "PUT")

    def test_three_losses_stop_bot_and_duplicate_sold_ignored(self):
        ws = self.start_bot_running()
        for i in range(3):
            self.run_trade_to_settlement(ws, -2.0, cid=200 + i)
            ws.message({"msg_type": "proposal_open_contract",
                        "proposal_open_contract": {"contract_id": 200 + i, "is_sold": 1, "profit": -2.0}})   # duplicate
            time.sleep(0.05)
        self.assertEqual(main.consecutive_losses, 3)
        self.assertFalse(main.auto_bot_running)
        self.assertEqual(var(main.bot_state_var), "STOPPED")
        before = len(ws.sent)
        main.auto_bot_running = True                 # even if forced on, the limit blocks new trades
        main.request_trade("HIGHER")
        self.assertEqual(len(ws.sent), before)
        self.assertFalse(main.auto_bot_running)

    def test_win_resets_loss_streak_zero_profit_does_not(self):
        ws = self.start_bot_running()
        self.run_trade_to_settlement(ws, -2.0, cid=1)
        self.assertEqual(main.consecutive_losses, 1)
        self.run_trade_to_settlement(ws, 0.0, cid=2)
        self.assertEqual(main.consecutive_losses, 1)
        self.run_trade_to_settlement(ws, 1.0, cid=3)
        self.assertEqual(main.consecutive_losses, 0)

    def test_daily_limit(self):
        ws = self.start_bot_running()
        main.trades_today = main.MAX_TRADES_PER_DAY
        before = len(ws.sent)
        main.request_trade("HIGHER")
        self.assertEqual(len(ws.sent), before)
        self.assertFalse(main.auto_bot_running)
        self.assertIn("10", var(main.status_var))

    def test_no_trade_when_not_verified_or_bot_off(self):
        ws = self.connect_demo()
        before = len(ws.sent)
        main.request_trade("HIGHER")                      # bot not running
        main.auto_bot_running, main.session_mode = True, None
        main.request_trade("HIGHER")                      # not verified
        main.session_mode = "demo"
        main.request_trade("WAIT")                        # no direction
        self.assertEqual(len(ws.sent), before)

    def test_one_trade_at_a_time(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        n = len(ws.sent)
        main.request_trade("HIGHER")
        self.assertEqual(len(ws.sent), n)

    def test_rejected_proposal_clears_pending(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        ws.message({"msg_type": "proposal", "req_id": 301, "error": {"code": "InvalidSymbol", "message": "bad field"}})
        self.assertTrue(wait_until(lambda: not main.proposal_pending))
        self.assertIn("bad field", var(main.status_var))
        self.assertEqual(main.trades_today, 0)

    def test_proposal_timeout_then_late_quote_is_not_bought(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        main.trade_request_at -= main.TRADE_RESPONSE_TIMEOUT + 1
        main.housekeeping()
        self.assertFalse(main.proposal_pending)
        self.assertTrue(main.auto_bot_running)
        ws.message({"msg_type": "proposal", "proposal": {"id": "late", "ask_price": 2.0}})
        time.sleep(0.1)
        self.assertFalse(any("buy" in m for m in ws.sent))

    def test_purchase_timeout_stops_bot(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        ws.message({"msg_type": "proposal", "proposal": {"id": "p", "ask_price": 2.0}})
        self.assertTrue(wait_until(lambda: main.trade_phase == "buy"))
        main.trade_request_at -= main.TRADE_RESPONSE_TIMEOUT + 1
        main.housekeeping()
        self.assertFalse(main.auto_bot_running)
        self.assertIn("history", var(main.status_var))

    def test_disconnect_during_purchase_stops_bot(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        ws.message({"msg_type": "proposal", "proposal": {"id": "p", "ask_price": 2.0}})
        self.assertTrue(wait_until(lambda: main.trade_phase == "buy"))
        ws.closed.set()
        self.assertTrue(wait_until(lambda: not main.auto_bot_running))
        self.assertFalse(main.proposal_pending)

    def test_reconnect_resumes_open_contract_tracking(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        ws.message({"msg_type": "proposal", "proposal": {"id": "p", "ask_price": 2.0}})
        self.assertTrue(wait_until(lambda: ws.sent[-1].get("buy") == "p"))
        ws.message({"msg_type": "buy", "buy": {"contract_id": 77}})
        self.assertTrue(wait_until(lambda: main.active_trade))
        ws.closed.set()
        self.assertTrue(wait_until(lambda: len(self.anet.sockets) >= 2))
        ws2 = self.anet.sockets[-1]
        self.assertTrue(wait_until(lambda: {"proposal_open_contract": 1, "contract_id": 77, "subscribe": 1, "req_id": 303} in ws2.sent))

    def test_signal_transition_triggers_one_trade_and_reconnect_does_not(self):
        ws = self.start_bot_running()
        orig = main.current_signal
        try:
            main.current_signal = lambda: ("HIGHER", "x")
            main.last_signal_queued = main.last_auto_signal = "WAIT"
            main.process_tick(100.0)
            self.assertTrue(wait_until(lambda: any("proposal" in m for m in ws.sent)))
            n = len(ws.sent)
            main.process_tick(100.1)                                # same signal: no second trade
            self.assertEqual(len(ws.sent), n)
            main.on_market_disconnect()                             # feed outage resets trackers
            main.proposal_pending = False
            main.process_tick(100.2)                                # first tick after reconnect: adopt only
            self.assertEqual(len(ws.sent), n)
            self.assertEqual(main.last_auto_signal, "HIGHER")
        finally:
            main.current_signal = orig

    def test_five_tick_observation_still_recorded(self):
        orig = main.current_signal
        try:
            main.current_signal = lambda: ("HIGHER", "x")
            main.last_signal_queued = main.last_auto_signal = "WAIT"
            for p in (100.0, 100.1, 100.2, 100.3, 100.4, 100.5):
                main.process_tick(p)
        finally:
            main.current_signal = orig
        self.assertEqual((main.total_wins, main.total_losses), (1, 0))
        self.assertIn("DIRECTION MATCH", main.RESULT_FILE.read_text())

    def test_start_bot_validates_stake_and_limits(self):
        main.stake_var.set("abc")
        main.start_auto_bot()
        self.assertFalse(main.auto_bot_running)
        main.stake_var.set("2")
        main.consecutive_losses = 3
        main.start_auto_bot()
        self.assertFalse(main.auto_bot_running)

    def test_ui_updates_never_applied_off_main_thread(self):
        seen = []
        main.ui(lambda: seen.append(threading.current_thread().name))
        t = threading.Thread(target=lambda: main.ui(lambda: seen.append(threading.current_thread().name)))
        t.start(); t.join()
        self.assertEqual(seen, [])
        drain()
        self.assertEqual(seen, [threading.current_thread().name] * 2)


    # ---- REAL mode safeguards
    def switch_real(self):
        main.TOKEN_FILE.write_text(TOKEN, encoding="utf-8")
        main.toggle_mode()
        self.assertTrue(wait_until(lambda: main.session_mode == "real" and main.account_channel.state == dc.CONNECTED),
                        main.account_channel.detail)
        return self.anet.sockets[-1]

    def start_real_bot(self):
        ws = self.switch_real()
        main.stake_var.set("2")
        main.start_auto_bot()
        self.assertTrue(main.auto_bot_running)
        return ws

    def test_app_starts_in_demo(self):
        self.assertEqual(STARTUP, {"mode": "demo", "limits": (10, 3), "stake_cap": None, "session": None})
        self.assertEqual(main.trading_mode, "demo")
        self.assertEqual((main.MAX_TRADES_PER_DAY, main.MAX_CONSECUTIVE_LOSSES), (10, 3))
        self.assertIsNone(main.max_stake)

    def test_real_switch_needs_both_confirmations(self):
        main.messagebox.askyesno.return_value = False
        main.toggle_mode()
        self.assertEqual(main.trading_mode, "demo")
        main.messagebox.askyesno.return_value = True
        for typed in ("yes", "real", "", None, "REALLY"):
            main.simpledialog.askstring.return_value = typed
            main.toggle_mode()
            self.assertEqual(main.trading_mode, "demo", typed)
        self.assertEqual(self.anet.sockets, [])                    # nothing was ever opened

    def test_switch_to_real_uses_real_endpoint_and_stricter_limits(self):
        demo_ws = self.connect_demo()
        ws = self.switch_real()
        self.assertEqual(ws.url, REAL_URL)
        self.assertTrue(demo_ws.closed.is_set())
        self.assertEqual(main.trading_mode, "real")
        self.assertEqual((main.MAX_TRADES_PER_DAY, main.MAX_CONSECUTIVE_LOSSES, main.max_stake, main.daily_loss_limit),
                         (5, 2, 5.0, 10.0))
        self.assertEqual(var(main.account_var), "Account: ROT1 (REAL MONEY)")
        self.assertEqual(var(main.account_status_var), "REAL ACCOUNT VERIFIED")
        self.assertEqual(var(main.balance_title_var), "REAL ACCOUNT BALANCE")
        self.assertEqual((var(main.start_button_var), var(main.stake_label_var)), ("START REAL BOT", "REAL stake:"))
        self.assertIn("REAL MONEY", var(main.mode_var))
        self.assertIn({"balance": 1, "subscribe": 1, "req_id": 2}, ws.sent)
        main.toggle_mode()                                          # back to demo needs no confirmation
        self.assertEqual(main.trading_mode, "demo")
        self.assertTrue(wait_until(lambda: main.session_mode == "demo"))
        self.assertEqual((main.MAX_TRADES_PER_DAY, main.max_stake), (10, None))
        self.assertEqual((var(main.start_button_var), var(main.stake_label_var)), ("START DEMO BOT", "Demo stake:"))
        self.assertEqual(self.anet.sockets[-1].url, DEMO_URL)

    def test_switch_stops_a_running_bot(self):
        self.start_bot_running()
        self.assertTrue(main.auto_bot_running)
        self.switch_real()
        self.assertFalse(main.auto_bot_running)
        self.assertEqual(var(main.bot_state_var), "READY — REAL VERIFIED")

    def test_switch_blocked_while_contract_open(self):
        ws = self.start_bot_running()
        main.request_trade("HIGHER")
        ws.message({"msg_type": "proposal", "proposal": {"id": "p", "ask_price": 2.0}})
        self.assertTrue(wait_until(lambda: ws.sent[-1].get("buy") == "p"))
        ws.message({"msg_type": "buy", "buy": {"contract_id": 5}})
        self.assertTrue(wait_until(lambda: main.active_trade))
        main.toggle_mode()
        self.assertEqual(main.trading_mode, "demo")

    def test_real_stake_cap_and_start_confirmation(self):
        self.switch_real()
        main.stake_var.set("5.01")
        main.start_auto_bot()
        self.assertFalse(main.auto_bot_running)
        main.stake_var.set("2")
        main.messagebox.askyesno.return_value = False
        main.start_auto_bot()
        self.assertFalse(main.auto_bot_running)
        main.messagebox.askyesno.return_value = True
        main.start_auto_bot()
        self.assertTrue(main.auto_bot_running)
        main.configured_stake = 50.0                                # even if bypassed, the trade path refuses
        before = len(self.anet.sockets[-1].sent)
        main.request_trade("HIGHER")
        self.assertEqual(len(self.anet.sockets[-1].sent), before)
        self.assertFalse(main.auto_bot_running)

    def test_real_trade_goes_to_real_socket_with_real_limits(self):
        ws = self.start_real_bot()
        self.assertEqual(ws.url, REAL_URL)
        proposal = self.run_trade_to_settlement(ws, 1.0)
        self.assertEqual((proposal["contract_type"], proposal["underlying_symbol"], proposal["amount"]), ("CALL", "R_100", 2.0))
        saved = json.loads(main.REAL_STATE_FILE.read_text())
        self.assertEqual((saved["trades_today"], saved["pnl_today"]), (1, 1.0))
        self.assertIn("Real trade settled", var(main.status_var))

    def test_real_two_losses_stop(self):
        ws = self.start_real_bot()
        self.run_trade_to_settlement(ws, -2.0, cid=1)
        self.assertTrue(main.auto_bot_running)
        self.run_trade_to_settlement(ws, -2.0, cid=2)
        self.assertTrue(wait_until(lambda: not main.auto_bot_running))
        self.assertEqual(main.consecutive_losses, 2)

    def test_real_daily_loss_limit(self):
        ws = self.start_real_bot()
        main.pnl_today = -9.5
        self.run_trade_to_settlement(ws, -1.0)
        self.assertTrue(wait_until(lambda: not main.auto_bot_running))
        self.assertIn("loss limit", var(main.status_var).lower())
        self.assertEqual(main.consecutive_losses, 1)                # stopped by the P/L limit, not the streak
        main.stake_var.set("2")
        main.start_auto_bot()                                       # cannot be restarted the same day
        self.assertFalse(main.auto_bot_running)

    def test_real_daily_trade_limit(self):
        ws = self.start_real_bot()
        main.trades_today = 5
        before = len(ws.sent)
        main.request_trade("HIGHER")
        self.assertEqual(len(ws.sent), before)
        self.assertFalse(main.auto_bot_running)

    def test_demo_and_real_limits_are_kept_separately(self):
        main.trades_today, main.consecutive_losses = 4, 2
        main.save_bot_state()
        self.switch_real()
        self.assertEqual((main.trades_today, main.consecutive_losses), (0, 0))
        main.trades_today = 3
        main.save_bot_state()
        main.toggle_mode()
        self.assertEqual((main.trades_today, main.consecutive_losses), (4, 2))
        self.assertEqual(json.loads(main.REAL_STATE_FILE.read_text())["trades_today"], 3)

    def test_unreadable_real_limits_file_blocks_real_trading(self):
        main.REAL_STATE_FILE.write_text("{not json")
        self.switch_real()
        self.assertEqual(main.trades_today, main.MAX_TRADES_PER_DAY)
        main.stake_var.set("2")
        main.start_auto_bot()
        self.assertFalse(main.auto_bot_running)

    def test_no_real_account_fails_safely(self):
        self.rest.routes[("GET", dc.ACCOUNTS_PATH)] = {"data": [{"account_id": "DOT2", "account_type": "demo"}]}
        main.TOKEN_FILE.write_text(TOKEN)
        main.toggle_mode()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.FAILED))
        self.assertIn("No real account", var(main.status_var))
        self.assertEqual(self.anet.sockets, [])
        self.assertEqual(var(main.balance_var), "Unavailable")
        self.assertFalse(main.auto_bot_running)

    def test_real_mode_refuses_a_demo_endpoint(self):
        self.rest.routes[("POST", dc.OTP_PATH.format(account_id="ROT1"))] = {"data": {"url": DEMO_URL}}
        main.TOKEN_FILE.write_text(TOKEN)
        main.toggle_mode()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.FAILED))
        self.assertEqual(self.anet.sockets, [])

    def test_real_override_file_must_name_a_real_account(self):
        main.REAL_ACCOUNT_FILE.write_text("DOT2")
        main.TOKEN_FILE.write_text(TOKEN)
        main.toggle_mode()
        self.assertTrue(wait_until(lambda: main.account_channel.state == dc.FAILED))
        self.assertEqual(self.anet.sockets, [])

    def test_session_of_wrong_type_cannot_trade(self):
        ws = self.start_bot_running()                               # verified DEMO session
        main.trading_mode = "real"                                  # simulate any mismatch
        before = len(ws.sent)
        main.request_trade("HIGHER")
        self.assertEqual(len(ws.sent), before)
        main.trading_mode = "demo"


    # ---- trade log, tick recorder, no martingale
    def test_settled_trade_is_logged_and_summarised(self):
        import csv as csvmod
        ws = self.start_bot_running()
        self.run_trade_to_settlement(ws, 1.9, cid=111)
        with main.TRADE_LOG_FILE.open() as handle:
            rows = list(csvmod.DictReader(handle))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["mode"], row["direction"], row["stake"], row["profit"], row["result"]),
                         ("demo", "CALL", "2.0", "1.9", "WIN"))
        self.assertEqual((row["account_id"], row["contract_id"], row["trades_today"]), ("DOT2", "111", "1"))
        self.assertEqual((row["buy_price"], row["sell_price"], row["net_pnl_today"]), ("2.0", "3.9", "1.9"))
        self.assertTrue(wait_until(lambda: "1 trades" in var(main.summary_var)))
        self.assertIn("Net +1.90", var(main.summary_var))

    def test_log_and_summary_are_per_mode(self):
        import csv as csvmod
        ws = self.start_real_bot()
        self.run_trade_to_settlement(ws, -2.0, cid=9)
        with main.TRADE_LOG_FILE.open() as handle:
            self.assertEqual(list(csvmod.DictReader(handle))[0]["mode"], "real")
        main.toggle_mode()                                          # back to demo
        self.assertTrue(wait_until(lambda: "(demo): no settled trades yet" in var(main.summary_var)))

    def test_stake_is_never_increased_after_losses(self):
        ws = self.start_bot_running()
        self.run_trade_to_settlement(ws, -2.0, cid=1)
        self.run_trade_to_settlement(ws, -2.0, cid=2)
        main.request_trade("LOWER")                                 # third trade, after two losses
        amounts = [m["amount"] for m in ws.sent if "proposal" in m]
        self.assertEqual(amounts, [2.0, 2.0, 2.0])
        self.assertEqual(main.configured_stake, 2.0)

    def test_ticks_are_recorded_once_each(self):
        main.start_market()
        self.assertTrue(wait_until(lambda: main.market_channel.state == dc.CONNECTED))
        ws = self.mnet.sockets[0]
        for epoch, quote in ((1700000000, 100.5), (1700000000, 100.5), (1700000002, 100.75)):
            ws.message({"msg_type": "tick", "tick": {"epoch": epoch, "quote": quote, "symbol": "R_100"}})
        self.assertTrue(wait_until(lambda: len(main.prices) == 3))
        self.assertEqual(main.TICK_FILE.read_text().splitlines(), ["epoch,quote", "1700000000,100.5", "1700000002,100.75"])
        main.RECORD_TICKS = False
        ws.message({"msg_type": "tick", "tick": {"epoch": 1700000004, "quote": 101.0}})
        self.assertTrue(wait_until(lambda: len(main.prices) == 4))
        self.assertEqual(len(main.TICK_FILE.read_text().splitlines()), 3)

    def test_unwritable_tick_file_never_breaks_the_feed(self):
        main.TICK_FILE = self.tmp                                    # a directory: opening it for append fails
        main.start_market()
        self.assertTrue(wait_until(lambda: main.market_channel.state == dc.CONNECTED))
        self.mnet.sockets[0].message({"msg_type": "tick", "tick": {"epoch": 1700000000, "quote": 100.5}})
        self.assertTrue(wait_until(lambda: len(main.prices) == 1))
        self.assertEqual(main.market_channel.state, dc.CONNECTED)


class Indicators(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(main.ema([5.0] * 30, 9), 5.0)
        self.assertEqual(main.rsi([float(i) for i in range(1, 40)]), 100.0)
        self.assertEqual(main.rsi([float(i) for i in range(40, 1, -1)]), 0.0)
        self.assertEqual(main.macd([1.0] * 10), (None, None))
        m, s = main.macd([float(i) for i in range(1, 80)])
        self.assertGreater(m, s - 1e9)
        self.assertEqual(main.current_signal()[0], "WAIT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
