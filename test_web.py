"""Offline tests for the web app (webapp/). No network, no real Deriv, no real browser.

They drive the engine, the OAuth helpers and the Flask routes with test doubles. They prove the server-side safety
rules (limits, latch, real-mode confirmation, one session per account, credential hygiene). They do NOT prove that
Deriv's live OAuth/REST/WebSocket servers accept these requests.

Run:  python -m unittest -v test_web
"""
import base64
import hashlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deriv_connection as dc
from webapp import engine, oauth
from webapp.engine import LimitStore, MarketHub, SessionConflict, SessionStore, TooManyUsers, UserBot
from webapp.server import create_app
from webapp.settings import Settings

BASE = "https://bot.example.com"
TOKEN = "ory_at_SECRETACCESSTOKEN123"
OTP = "otpvalue98765"
ACCOUNTS = {"data": [{"account_id": "DOT91228972", "account_type": "demo", "currency": "USD"},
                     {"account_id": "ROT90460530", "account_type": "real", "currency": "USD"}]}
DEMO_ONLY = {"data": [ACCOUNTS["data"][0]]}


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class Clock:
    def __init__(self):
        self.wall_t, self.mono_t = 1_800_000_000.0, 1000.0

    def wall(self):
        return self.wall_t

    def mono(self):
        return self.mono_t


class FakeResp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeRest:
    """Stands in for api.derivws.com."""

    def __init__(self, accounts=ACCOUNTS):
        self.requests = []
        self.accounts = accounts
        self.fail_with = None

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.fail_with:
            raise self.fail_with
        path = req.full_url.replace(dc.REST_BASE, "")
        if req.get_method() == "GET" and path == dc.ACCOUNTS_PATH:
            return FakeResp(self.accounts)
        if req.get_method() == "POST" and path.endswith("/otp"):
            acc = path.split("/")[-2]
            kind = "real" if acc.startswith("ROT") else "demo"
            return FakeResp({"data": {"url": f"wss://api.derivws.com/trading/v1/options/ws/{kind}?otp={OTP}"}})
        raise AssertionError("unexpected request " + path)


def http401():
    return urllib.error.HTTPError("https://api.derivws.com/x", 401, "no", {}, io.BytesIO(b'{"errors":[{"code":"x","message":"bad"}]}'))


class FakeWS:
    def __init__(self, url, on_open, on_message, on_error, on_close):
        self.url, self.on_open, self.on_message, self.on_close = url, on_open, on_message, on_close
        self.sent, self.closed = [], threading.Event()

    def run_forever(self, ping_interval=0, ping_timeout=None):
        self.on_open(self)
        self.closed.wait()
        self.on_close(self, 1000, "bye")

    def close(self):
        self.closed.set()

    def send(self, text):
        self.sent.append(json.loads(text))

    def message(self, obj):
        self.on_message(self, json.dumps(obj))


class FakeNet:
    def __init__(self):
        self.sockets = []

    def factory(self, url, on_open, on_message, on_error, on_close):
        ws = FakeWS(url, on_open, on_message, on_error, on_close)
        self.sockets.append(ws)
        return ws


class FakeOAuth:
    def __init__(self, reply=None, error=None):
        self.requests, self.reply, self.error = [], reply or {"access_token": TOKEN, "expires_in": 3600,
                                                                "token_type": "Bearer"}, error

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            raise self.error
        return FakeResp(self.reply)


class World:
    """A tiny server: settings, clock, fake Deriv, market hub, limit store and (optionally) a signed-in bot."""

    def __init__(self, accounts=ACCOUNTS, env=None):
        self.tmp = tempfile.mkdtemp()
        base_env = {"DERIV_APP_ID": "app123", "PUBLIC_BASE_URL": BASE, "BOT_DATA_DIR": self.tmp}
        base_env.update(env or {})
        self.settings = Settings(base_env)
        self.clock = Clock()
        self.rest = FakeRest(accounts)
        self.net, self.market_net = FakeNet(), FakeNet()
        dc._urlopen = self.rest
        dc.CONNECT_TIMEOUT, dc.BACKOFF_STEPS = 0.8, (0.1, 0.2)
        self.hub = MarketHub(self.settings.symbol, ws_factory=self.market_net.factory, mono=self.clock.mono)
        self.store = LimitStore(self.settings.data_dir, self.settings.tz, wall=self.clock.wall)
        self.accounts = dc.normalize_accounts(accounts)
        self.bots = []

    def new_bot(self, connect=True):
        bot = UserBot(self.settings, self.hub, self.store, TOKEN, 3600, self.accounts, ws_factory=self.net.factory,
                      wall=self.clock.wall, mono=self.clock.mono)
        self.bots.append(bot)
        if connect:
            bot.connect()
            assert wait_until(bot.session_ready), "bot never connected"
        return bot

    @property
    def ws(self):
        return self.net.sockets[-1]

    def tick(self, price=100.0):
        self.hub._on_tick({"quote": price, "epoch": int(self.clock.wall_t)})

    def sent(self, key):
        return [m for m in self.ws.sent if key in m]

    def trade(self, bot, profit, signal="HIGHER", contract_id=1):
        """Drive one full contract through proposal -> buy -> settlement."""
        self.tick()
        bot.cooldown_until = 0.0
        before = len(self.sent("proposal"))
        bot._maybe_trade(signal)
        assert len(self.sent("proposal")) == before + 1, "no proposal was sent"
        self.ws.message({"msg_type": "proposal", "proposal": {"id": f"p{contract_id}", "ask_price": bot.stake}})
        self.ws.message({"msg_type": "buy", "buy": {"contract_id": contract_id, "buy_price": bot.stake}})
        self.ws.message({"msg_type": "proposal_open_contract", "subscription": {"id": f"s{contract_id}"},
                         "proposal_open_contract": {"contract_id": contract_id, "is_sold": 1, "profit": profit,
                                                    "buy_price": bot.stake, "sell_price": bot.stake + profit,
                                                    "contract_type": "CALL" if signal == "HIGHER" else "PUT"}})

    def ready(self, bot, mode="demo", confirm_real=True):
        bot.acknowledge()
        if mode == "real":
            ok, msg = bot.set_mode("real", "REAL")
            assert ok, msg
            assert wait_until(bot.session_ready)
        ok, msg = bot.start_bot(confirm_real=confirm_real)
        assert ok, msg

    def close(self):
        for bot in self.bots:
            bot.shutdown()
        self.hub.stop()


class WorldCase(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.addCleanup(lambda: setattr(dc, "_urlopen", __import__("urllib.request").request.urlopen))


# ============================================================================ settings / oauth
class SettingsTests(unittest.TestCase):
    def test_problems(self):
        self.assertTrue(Settings({}).problems())
        self.assertTrue(Settings({"DERIV_APP_ID": "PASTE_YOUR_APP_ID_HERE", "PUBLIC_BASE_URL": BASE}).problems())
        self.assertTrue(Settings({"DERIV_APP_ID": "1", "PUBLIC_BASE_URL": "http://bot.example.com"}).problems())
        self.assertEqual(Settings({"DERIV_APP_ID": "1", "PUBLIC_BASE_URL": BASE}).problems(), [])
        self.assertEqual(Settings({"DERIV_APP_ID": "1", "PUBLIC_BASE_URL": "http://localhost:8000"}).problems(), [])

    def test_limits_never_exceed_the_hard_rules(self):
        s = Settings({"DERIV_APP_ID": "1", "PUBLIC_BASE_URL": BASE})
        for level in range(1, 6):
            for mode in ("demo", "real"):
                trades, losses, _, _ = s.limits(mode, level)
                self.assertLessEqual(trades, 10)
                self.assertLessEqual(losses, 3)
        self.assertEqual(s.limits("demo", 5)[:2], (10, 3))
        s.RISK_TABLE = {1: (99, 9), 2: (50, 7)}                   # even a mistyped table cannot beat your two rules
        self.assertEqual(s.limits("real", 1)[:2], (10, 3))
        self.assertEqual(s.limits("demo", 2)[:2], (10, 3))
        self.assertEqual(s.limits("demo", 99)[:2], s.limits("demo", 2)[:2])    # junk falls back to the default
        self.assertEqual(s.limits("real", 2)[2:], (5.0, 10.0))
        self.assertIsNone(s.limits("demo", 2)[3])


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings({"DERIV_APP_ID": "app123", "PUBLIC_BASE_URL": BASE})

    def test_pkce_challenge_matches_verifier(self):
        verifier, challenge = oauth.new_pkce()
        digest = hashlib.sha256(verifier.encode()).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        self.assertGreaterEqual(len(verifier), 43)

    def test_authorize_url(self):
        url = oauth.authorize_url(self.s, "st", "ch")
        q = parse_qs(urlparse(url).query)
        self.assertTrue(url.startswith("https://auth.deriv.com/oauth2/auth?"))
        self.assertEqual((q["response_type"], q["client_id"], q["scope"], q["code_challenge_method"]),
                         (["code"], ["app123"], ["trade"], ["S256"]))
        self.assertEqual(q["redirect_uri"], [BASE + "/callback"])

    def test_state_is_single_use_and_expires(self):
        t = [0.0]
        pend = oauth.PendingLogins(ttl=10, clock=lambda: t[0])
        state, _, verifier = pend.create()
        self.assertEqual(pend.take(state), verifier)
        self.assertIsNone(pend.take(state))                       # replay
        state2, _, _ = pend.create()
        t[0] = 11
        self.assertIsNone(pend.take(state2))                      # too old
        self.assertIsNone(pend.take("nonsense"))
        self.assertIsNone(pend.take(None))

    def test_exchange_sends_pkce_form_and_returns_token(self):
        fake = FakeOAuth()
        oauth._urlopen = fake
        self.addCleanup(lambda: setattr(oauth, "_urlopen", __import__("urllib.request").request.urlopen))
        token, expires = oauth.exchange_code(self.s, "thecode", "theverifier")
        self.assertEqual((token, expires), (TOKEN, 3600))
        form = parse_qs(fake.requests[0].data.decode())
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code_verifier"], ["theverifier"])
        self.assertEqual(form["redirect_uri"], [BASE + "/callback"])
        self.assertNotIn("client_secret", form)
        self.assertNotIn(TOKEN, dc.redact(f"oops {TOKEN}"))       # the token was registered as a secret

    def test_exchange_failures(self):
        self.addCleanup(lambda: setattr(oauth, "_urlopen", __import__("urllib.request").request.urlopen))
        err = urllib.error.HTTPError(oauth.TOKEN_URL, 400, "bad", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        oauth._urlopen = FakeOAuth(error=err)
        with self.assertRaises(dc.DerivApiError) as cm:
            oauth.exchange_code(self.s, "c", "v")
        self.assertEqual(cm.exception.kind, "auth")
        oauth._urlopen = FakeOAuth(error=urllib.error.URLError("dns"))
        with self.assertRaises(dc.DerivApiError) as cm:
            oauth.exchange_code(self.s, "c", "v")
        self.assertTrue(cm.exception.retryable)
        oauth._urlopen = FakeOAuth(reply={"token_type": "Bearer"})
        with self.assertRaises(dc.DerivApiError):
            oauth.exchange_code(self.s, "c", "v")


# ============================================================================ market hub
class HubTests(WorldCase):
    def test_fresh_signal_only_on_a_transition_and_never_after_reconnect(self):
        seq = iter(["WAIT", "HIGHER", "HIGHER", "LOWER", "HIGHER", "LOWER"])
        orig = engine.evaluate_signal
        engine.evaluate_signal = lambda vals, n: (next(seq), "x")
        self.addCleanup(lambda: setattr(engine, "evaluate_signal", orig))
        events = []
        self.w.hub.add_listener(events.append)
        for i in range(4):
            self.w.tick(100 + i)
        self.assertEqual([e["fresh"] for e in events], [False, True, False, True])
        self.w.hub._on_disconnect()                               # outage: ticks were missed
        self.w.tick(105)                                          # HIGHER is the first tick after the outage:
        self.w.tick(106)                                          # adopted silently, not acted on; a LATER change is fresh
        self.assertEqual([e["fresh"] for e in events][-2:], [False, True])
        self.w.hub._on_disconnect()
        self.assertFalse(self.w.hub.streaming)

    def test_bad_ticks_ignored_and_tick_file_deduplicated(self):
        path = Path(self.w.tmp) / "t.csv"
        hub = MarketHub("R_100", tick_file=str(path), ws_factory=FakeNet().factory)
        for tick in ({"quote": "abc", "epoch": 1}, {"quote": float("nan"), "epoch": 2}, {}, {"quote": 5.5, "epoch": 3},
                     {"quote": 5.6, "epoch": 3}, {"quote": 5.7, "epoch": 4}):
            hub._on_tick(tick)
        self.assertEqual(path.read_text().split(), ["epoch,quote", "3,5.5", "4,5.7"])

    def test_listener_errors_do_not_stop_the_feed(self):
        self.w.hub.add_listener(lambda e: 1 / 0)
        got = []
        self.w.hub.add_listener(got.append)
        self.w.tick(100)
        self.assertEqual(len(got), 1)


# ============================================================================ the user bot
class ConnectionTests(WorldCase):
    def test_connects_to_demo_by_default_with_bearer_only(self):
        bot = self.w.new_bot()
        self.assertEqual(bot.mode, "demo")
        self.assertIn("/ws/demo?otp=", self.w.ws.url)
        for req in self.w.rest.requests:                           # OAuth tokens need no App-ID header
            names = [k.lower() for k, _ in req.header_items()]
            self.assertIn("authorization", names)
            self.assertNotIn("deriv-app-id", names)
        self.w.ws.message({"msg_type": "balance", "balance": {"balance": 10017.06, "currency": "USD"}})
        self.assertEqual(bot.snapshot()["balance"], 10017.06)

    def test_snapshot_never_contains_secrets(self):
        bot = self.w.new_bot()
        text = json.dumps(bot.snapshot())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(OTP, text)
        self.assertNotIn("otp", text.lower())

    def test_revoked_token_marks_session_expired_without_leaking(self):
        bot = self.w.new_bot(connect=False)
        self.w.rest.fail_with = http401()
        bot.connect()
        self.assertTrue(wait_until(lambda: bot.expired))
        snap = bot.snapshot()
        self.assertEqual(snap["bot"]["state"], "EXPIRED")
        self.assertNotIn(TOKEN, json.dumps(snap))
        self.assertIsNone(bot._token)

    def test_expiry_after_an_hour_stops_the_bot_and_forgets_the_token(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.clock.wall_t += 3601
        bot.housekeeping()
        self.assertTrue(bot.expired)
        self.assertFalse(bot.running)
        self.assertIsNone(bot._token)
        self.assertTrue(wait_until(lambda: bot.channel.state == dc.DISCONNECTED))
        self.assertFalse(bot.start_bot()[0])

    def test_no_real_account_means_real_is_refused(self):
        w = World(DEMO_ONLY)
        self.addCleanup(w.close)
        bot = w.new_bot()
        bot.acknowledge()
        ok, msg = bot.set_mode("real", "REAL")
        self.assertFalse(ok)
        self.assertIn("no real account", msg.lower())
        self.assertEqual(bot.mode, "demo")


class TradingTests(WorldCase):
    def test_start_requires_acknowledgement_and_a_connected_account(self):
        bot = self.w.new_bot()
        self.assertFalse(bot.start_bot()[0])                       # risk notice not accepted
        bot.acknowledge()
        self.assertTrue(bot.start_bot()[0])
        offline = self.w.new_bot(connect=False)
        offline.acknowledge()
        self.assertFalse(offline.start_bot()[0])                   # never connected

    def test_trade_request_uses_current_api_fields_and_fixed_stake(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        bot._maybe_trade("LOWER")
        proposal = self.w.sent("proposal")[0]
        self.assertEqual((proposal["contract_type"], proposal["duration"], proposal["duration_unit"],
                          proposal["underlying_symbol"], proposal["basis"], proposal["amount"]),
                         ("PUT", 5, "t", "R_100", "stake", 1.0))
        self.w.ws.message({"msg_type": "proposal", "proposal": {"id": "p1", "ask_price": 1.0}})
        self.assertEqual(self.w.sent("buy")[0]["price"], 1.0)      # capped at the quote

    def test_one_trade_at_a_time_and_no_trade_without_market_data(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        bot._maybe_trade("HIGHER")                                 # hub has no ticks yet
        self.assertEqual(self.w.sent("proposal"), [])
        self.w.tick()
        bot._maybe_trade("HIGHER")
        bot._maybe_trade("LOWER")
        self.assertEqual(len(self.w.sent("proposal")), 1)

    def test_listener_places_trade_on_fresh_signal_only(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        self.w.hub._listeners[0]({"fresh": False, "signal": "HIGHER"})
        self.assertEqual(self.w.sent("proposal"), [])
        self.w.hub._listeners[0]({"fresh": True, "signal": "WAIT"})
        self.assertEqual(self.w.sent("proposal"), [])
        self.w.hub._listeners[0]({"fresh": True, "signal": "HIGHER"})
        self.assertEqual(len(self.w.sent("proposal")), 1)

    def test_late_quote_after_stop_is_not_bought(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        bot._maybe_trade("HIGHER")
        bot.stop_bot()
        self.w.ws.message({"msg_type": "proposal", "proposal": {"id": "p1", "ask_price": 1.0}})
        self.assertEqual(self.w.sent("buy"), [])

    def test_win_loss_accounting_and_trade_log(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.trade(bot, -1.0)
        self.w.trade(bot, 0.95, contract_id=2)
        snap = bot.snapshot()
        self.assertEqual((snap["limits"]["trades_today"], snap["limits"]["losses"], snap["limits"]["pnl_today"]),
                         (2, 0, -0.05))
        self.assertEqual((snap["summary"]["wins"], snap["summary"]["losses"]), (1, 1))
        self.assertEqual([r["result"] for r in snap["recent"]], ["WIN", "LOSS"])
        rows = (Path(self.w.tmp) / "trade_log.csv").read_text().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertIn("DOT91228972", rows[1])
        self.assertTrue(any(m["text"].startswith("Trade finished") for m in snap["messages"]))

    def test_zero_profit_neither_counts_nor_resets_the_streak(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=5)
        self.w.ready(bot)
        self.w.trade(bot, -1.0)
        self.w.trade(bot, 0.0, contract_id=2)
        self.assertEqual(bot.counters["consecutive_losses"], 1)

    def test_no_martingale_stake_never_changes_after_losses(self):
        bot = self.w.new_bot()
        bot.configure(stake=2.5, risk_level=5)
        self.w.ready(bot)
        self.w.trade(bot, -2.5)
        self.w.trade(bot, -2.5, contract_id=2)
        amounts = [p["amount"] for p in self.w.sent("proposal")]
        self.assertEqual(amounts, [2.5, 2.5])
        self.assertEqual(bot.stake, 2.5)

    def test_three_losses_stop_the_bot_for_the_day_and_latch(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=5)
        self.w.ready(bot)
        for i in range(3):
            self.w.trade(bot, -1.0, contract_id=i + 1)
        self.assertFalse(bot.running)
        self.assertTrue(bot.counters["halted"])
        self.assertEqual(bot.snapshot()["bot"]["state"], "LIMIT")
        self.assertFalse(bot.start_bot()[0])
        bot.configure(risk_level=5)
        self.assertFalse(bot.start_bot()[0])

    def test_ten_trades_a_day_hard_stop(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=5)
        self.w.ready(bot)
        for i in range(10):
            self.w.trade(bot, 0.9 if i % 2 == 0 else -1.0, contract_id=i + 1)
        self.assertFalse(bot.running)
        self.assertEqual(bot.counters["trades_today"], 10)
        self.assertFalse(bot.start_bot()[0])
        self.w.tick()
        bot._maybe_trade("HIGHER")
        self.assertEqual(len(self.w.sent("proposal")), 10)         # an 11th is never even requested

    def test_risk_level_tightens_limits(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=1)                                # 2 trades, stop after 1 loss
        self.w.ready(bot)
        self.w.trade(bot, -1.0)
        self.assertFalse(bot.running)
        self.assertEqual(bot.counters["consecutive_losses"], 1)

    def test_halt_survives_restart_and_a_new_session(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=1)
        self.w.ready(bot)
        self.w.trade(bot, -1.0)
        bot.shutdown()
        fresh = self.w.new_bot()                                   # same Deriv account signs in again
        fresh.acknowledge()
        ok, msg = fresh.start_bot()
        self.assertFalse(ok)
        self.assertEqual(fresh.counters["trades_today"], 1)

    def test_limits_reset_the_next_day(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=1)
        self.w.ready(bot)
        self.w.trade(bot, -1.0)
        self.assertTrue(bot.counters["halted"])
        self.w.clock.wall_t += 86400
        bot.expires_at += 86400                                    # (a real sign-in would have to be renewed)
        bot.housekeeping()
        self.assertEqual(bot.counters["trades_today"], 0)
        self.assertFalse(bot.counters["halted"])
        self.assertTrue(bot.start_bot()[0])

    def test_cooldown_between_trades(self):
        bot = self.w.new_bot()
        bot.configure(frequency=60, risk_level=5)
        self.w.ready(bot)
        self.w.trade(bot, 0.9)
        self.w.tick()
        bot._maybe_trade("HIGHER")                                 # still cooling down
        self.assertEqual(len(self.w.sent("proposal")), 1)
        self.w.clock.mono_t += 61
        bot._maybe_trade("HIGHER")
        self.assertEqual(len(self.w.sent("proposal")), 2)

    def test_rejected_proposal_places_nothing(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        bot._maybe_trade("HIGHER")
        self.w.ws.message({"error": {"code": "InvalidSymbol", "message": "nope"}, "req_id": 301})
        self.assertEqual(self.w.sent("buy"), [])
        self.assertFalse(bot.proposal_pending)
        self.assertEqual(bot.counters["trades_today"], 0)

    def test_silent_server_times_out_without_trading(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        bot._maybe_trade("HIGHER")
        self.w.clock.mono_t += engine.TRADE_RESPONSE_TIMEOUT + 1
        bot.housekeeping()
        self.assertFalse(bot.proposal_pending)
        self.assertTrue(bot.running)                               # quote stage: safe to carry on
        bot._maybe_trade("HIGHER")
        self.w.ws.message({"msg_type": "proposal", "proposal": {"id": "p", "ask_price": 1.0}})
        self.w.clock.mono_t += engine.TRADE_RESPONSE_TIMEOUT + 1
        bot.housekeeping()
        self.assertFalse(bot.running)                              # no reply to a BUY: stop and warn

    def test_stake_validation_and_caps(self):
        bot = self.w.new_bot()
        for bad in (0, -1, 0.1, 101, "abc", float("nan"), float("inf")):
            self.assertFalse(bot.configure(stake=bad)[0], bad)
        self.assertTrue(bot.configure(stake=50)[0])                # demo allows more
        for bad in (5, 301, "x"):
            self.assertFalse(bot.configure(frequency=bad)[0], bad)
        for bad in (0, 6, "x"):
            self.assertFalse(bot.configure(risk_level=bad)[0], bad)

    def test_settings_locked_while_running(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.assertFalse(bot.configure(stake=3)[0])

    def test_connection_loss_while_buying_stops_the_bot(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        bot._maybe_trade("HIGHER")
        self.w.ws.message({"msg_type": "proposal", "proposal": {"id": "p", "ask_price": 1.0}})
        bot._on_disconnect()
        self.assertFalse(bot.running)
        self.assertTrue(any("purchase was in flight" in m[1] for m in bot.messages))


class RealModeTests(WorldCase):
    def test_real_switch_needs_ack_and_the_typed_word(self):
        bot = self.w.new_bot()
        self.assertFalse(bot.set_mode("real", "REAL")[0])          # risk notice not accepted
        bot.acknowledge()
        for wrong in ("", "real", "Real", " REA", "yes"):
            self.assertFalse(bot.set_mode("real", wrong)[0], wrong)
        self.assertEqual(bot.mode, "demo")
        self.assertTrue(bot.set_mode("real", " REAL ")[0])
        self.assertEqual(bot.mode, "real")

    def test_real_uses_the_real_endpoint_and_account(self):
        bot = self.w.new_bot()
        self.w.ready(bot, "real")
        self.assertIn("/ws/real?otp=", self.w.ws.url)
        self.assertEqual(bot.account_id, "ROT90460530")
        self.assertEqual(bot.snapshot()["mode"], "real")

    def test_real_start_needs_explicit_confirmation(self):
        bot = self.w.new_bot()
        bot.acknowledge()
        bot.set_mode("real", "REAL")
        self.assertTrue(wait_until(bot.session_ready))
        ok, msg = bot.start_bot(confirm_real=False)
        self.assertFalse(ok)
        self.assertFalse(bot.running)
        self.assertTrue(bot.start_bot(confirm_real=True)[0])

    def test_real_stake_cap_and_clamp_on_switch(self):
        bot = self.w.new_bot()
        bot.configure(stake=20)
        bot.acknowledge()
        bot.set_mode("real", "REAL")
        self.assertEqual(bot.stake, 5.0)                           # clamped to the real cap
        self.assertFalse(bot.configure(stake=5.5)[0])
        self.assertTrue(bot.configure(stake=5.0)[0])

    def test_real_daily_loss_stop(self):
        w = World(env={"REAL_MAX_STAKE": "5", "REAL_MAX_DAILY_LOSS": "4"})
        self.addCleanup(w.close)
        bot = w.new_bot()
        bot.configure(risk_level=5)
        w.ready(bot, "real")
        w.trade(bot, -3.0)
        w.trade(bot, 0.9, contract_id=2)                           # streak reset, still -2.1
        w.trade(bot, -1.9, contract_id=3)                          # net -4.0 -> stop
        self.assertFalse(bot.running)
        self.assertTrue(bot.counters["halted"])

    def test_switching_back_to_demo_stops_the_bot_and_keeps_counters_separate(self):
        bot = self.w.new_bot()
        bot.configure(risk_level=5)
        self.w.ready(bot, "real")
        self.w.trade(bot, -1.0)
        self.assertTrue(bot.set_mode("demo")[0])
        self.assertFalse(bot.running)
        self.assertTrue(wait_until(bot.session_ready))
        self.assertEqual(bot.counters["trades_today"], 0)          # demo has its own counters
        self.assertIn("/ws/demo?otp=", self.w.ws.url)

    def test_no_switching_during_an_open_trade(self):
        bot = self.w.new_bot()
        self.w.ready(bot)
        self.w.tick()
        bot._maybe_trade("HIGHER")
        self.assertFalse(bot.set_mode("real", "REAL")[0])
        self.assertEqual(bot.mode, "demo")

    def test_real_can_be_switched_off_on_the_server(self):
        w = World(env={"ALLOW_REAL": "0"})
        self.addCleanup(w.close)
        bot = w.new_bot()
        bot.acknowledge()
        self.assertFalse(bot.set_mode("real", "REAL")[0])
        self.assertFalse(bot.snapshot()["allow_real"])

    def test_unreadable_real_limits_file_blocks_real_trading(self):
        path = self.w.store._path("real", "ROT90460530")
        Path(path).write_text("{not json")
        bot = self.w.new_bot()
        bot.acknowledge()
        bot.set_mode("real", "REAL")
        self.assertTrue(wait_until(bot.session_ready))
        ok, msg = bot.start_bot(confirm_real=True)
        self.assertFalse(ok)
        self.assertIn("could not be read", msg)


# ============================================================================ sessions
class SessionTests(WorldCase):
    def make_store(self, **env):
        settings = Settings({"DERIV_APP_ID": "a", "PUBLIC_BASE_URL": BASE, "BOT_DATA_DIR": self.w.tmp, **env})
        store = SessionStore(settings, self.w.hub, self.w.store, bot_factory=lambda t, e, a: UserBot(
            settings, self.w.hub, self.w.store, t, e, a, ws_factory=self.w.net.factory,
            wall=self.w.clock.wall, mono=self.w.clock.mono))
        self.w.hub.channel._factory = self.w.market_net.factory
        self.addCleanup(lambda: [store.remove(s) for s in list(store._sessions)])
        return store

    def test_new_login_replaces_the_old_session_for_the_same_account(self):
        store = self.make_store()
        sid1, bot1 = store.create(TOKEN, 3600, self.w.accounts)
        sid2, bot2 = store.create(TOKEN, 3600, self.w.accounts)
        self.assertNotEqual(sid1, sid2)
        self.assertIsNone(store.get(sid1))
        self.assertTrue(bot1.closed)
        self.assertIs(store.get(sid2), bot2)
        self.assertEqual(store.count(), 1)

    def test_login_refused_while_a_trade_is_open_elsewhere(self):
        store = self.make_store()
        sid, bot = store.create(TOKEN, 3600, self.w.accounts)
        self.assertTrue(wait_until(bot.session_ready))
        bot.acknowledge()
        bot.start_bot()
        self.w.tick()
        bot._maybe_trade("HIGHER")
        with self.assertRaises(SessionConflict):
            store.create(TOKEN, 3600, self.w.accounts)
        self.assertIs(store.get(sid), bot)

    def test_user_limit(self):
        store = self.make_store(MAX_USERS="1")
        store.create(TOKEN, 3600, self.w.accounts)
        other = dc.normalize_accounts({"data": [{"account_id": "DOT111", "account_type": "demo"}]})
        with self.assertRaises(TooManyUsers):
            store.create("othertoken123456", 3600, other)

    def test_idle_sessions_are_dropped_but_running_bots_are_kept(self):
        store = self.make_store()
        sid, bot = store.create(TOKEN, 3600, self.w.accounts)
        self.assertTrue(wait_until(bot.session_ready))
        bot.acknowledge()
        bot.start_bot()
        self.w.clock.mono_t += engine.IDLE_DROP_SECONDS + 5
        store.sweep()
        self.assertIsNotNone(store.get(sid))                       # a running bot keeps its session
        bot.stop_bot()
        self.w.clock.mono_t += engine.IDLE_DROP_SECONDS + 5
        store.sweep()
        self.assertIsNone(store.get(sid))

    def test_shutdown_closes_the_socket_and_forgets_the_token(self):
        store = self.make_store()
        sid, bot = store.create(TOKEN, 3600, self.w.accounts)
        self.assertTrue(wait_until(bot.session_ready))
        store.remove(sid)
        self.assertTrue(bot.closed)
        self.assertIsNone(bot._token)
        self.assertTrue(wait_until(lambda: bot.channel.state == dc.DISCONNECTED))
        self.assertNotIn(bot._on_market, self.w.hub._listeners)


# ============================================================================ HTTP routes
class RouteTests(WorldCase):
    def setUp(self):
        super().setUp()
        self.oa = FakeOAuth()
        oauth._urlopen = self.oa
        self.addCleanup(lambda: setattr(oauth, "_urlopen", __import__("urllib.request").request.urlopen))
        settings = self.w.settings
        self.w.hub.channel._factory = self.w.market_net.factory
        self.sessions = SessionStore(settings, self.w.hub, self.w.store, bot_factory=lambda t, e, a: UserBot(
            settings, self.w.hub, self.w.store, t, e, a, ws_factory=self.w.net.factory,
            wall=self.w.clock.wall, mono=self.w.clock.mono))
        self.addCleanup(lambda: [self.sessions.remove(s) for s in list(self.sessions._sessions)])
        self.app = create_app(settings, self.w.hub, self.sessions, start_background=False)
        self.c = self.app.test_client()

    def get(self, path, **kw):
        return self.c.get(path, base_url=BASE, **kw)

    def post(self, path, data=None, headers=None, **kw):
        h = {"Origin": BASE, "X-Requested-With": "5g"}
        h.update(headers or {})
        return self.c.post(path, json=data if data is not None else {}, headers=h, base_url=BASE, **kw)

    def sign_in(self):
        r = self.get("/login")
        self.assertEqual(r.status_code, 302)
        q = parse_qs(urlparse(r.headers["Location"]).query)
        r = self.get(f"/callback?code=thecode&state={q['state'][0]}")
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True))
        return q

    def test_signed_out_state_and_pages(self):
        s = self.get("/api/state").get_json()
        self.assertFalse(s["authenticated"])
        self.assertTrue(s["configured"])
        for path in ("/", "/static/app.js", "/static/style.css"):
            r = self.get(path)
            self.assertEqual(r.status_code, 200, path)
            r.close()
        self.assertEqual(self.get("/healthz").status_code, 200)
        self.assertEqual(self.get("/static/../server.py").status_code, 404)

    def test_login_refused_when_not_configured(self):
        app = create_app(Settings({"BOT_DATA_DIR": self.w.tmp}), self.w.hub, self.sessions, start_background=False)
        r = app.test_client().get("/login")
        self.assertEqual(r.status_code, 503)

    def test_full_login_flow(self):
        q = self.sign_in()
        self.assertEqual(q["scope"], ["trade"])
        form = parse_qs(self.oa.requests[0].data.decode())
        self.assertEqual(form["code"], ["thecode"])
        # the verifier sent to Deriv really is the one behind the challenge we sent
        digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), q["code_challenge"][0])
        cookie = self.c.get_cookie("s5g", domain="bot.example.com") or self.c.get_cookie("s5g")
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie.http_only)
        self.assertTrue(cookie.secure)
        s = self.get("/api/state").get_json()
        self.assertTrue(s["authenticated"])
        self.assertEqual(s["mode"], "demo")
        self.assertNotIn(TOKEN, json.dumps(s))

    def test_callback_rejects_bad_or_replayed_state(self):
        for qs in ("", "?code=x", "?state=nope&code=x", "?error=access_denied&state=s"):
            self.assertEqual(self.get("/callback" + qs).status_code, 400, qs)
        q = self.sign_in()
        r = self.get(f"/callback?code=x&state={q['state'][0]}")        # replay
        self.assertEqual(r.status_code, 400)

    def test_callback_with_failed_token_exchange(self):
        oauth._urlopen = FakeOAuth(error=urllib.error.URLError("down"))
        r = self.get("/login")
        state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
        resp = self.get(f"/callback?code=x&state={state}")
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(self.get("/api/state").get_json()["authenticated"])

    def test_api_requires_a_session(self):
        for path, data in (("/api/ack", {"accept": True}), ("/api/config", {"stake": 1}), ("/api/mode", {"mode": "real"}),
                           ("/api/bot", {"action": "start"}), ("/api/reconnect", {})):
            self.assertEqual(self.post(path, data).status_code, 401, path)

    def test_csrf_protection(self):
        self.sign_in()
        r = self.c.post("/api/ack", json={"accept": True}, base_url=BASE, headers={"Origin": BASE})
        self.assertEqual(r.status_code, 400)                          # missing custom header
        r = self.post("/api/ack", {"accept": True}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/ack", data="accept=1", base_url=BASE,
                        headers={"Origin": BASE, "X-Requested-With": "5g"})
        self.assertEqual(r.status_code, 400)                          # not JSON
        self.assertEqual(self.post("/api/ack", {"accept": True}).status_code, 200)

    def test_full_bot_cycle_over_http(self):
        self.sign_in()
        bot = next(iter(self.sessions._sessions.values()))
        self.assertTrue(wait_until(bot.session_ready))
        self.assertEqual(self.post("/api/bot", {"action": "start"}).status_code, 400)       # not accepted yet
        self.assertEqual(self.post("/api/ack", {"accept": False}).status_code, 400)
        self.assertEqual(self.post("/api/ack", {"accept": True}).status_code, 200)
        self.assertEqual(self.post("/api/config", {"stake": 2, "frequency": 30, "risk_level": 3}).status_code, 200)
        self.assertEqual(self.post("/api/config", {"stake": 500}).status_code, 400)
        self.assertEqual(self.post("/api/bot", {"action": "start"}).status_code, 200)
        s = self.get("/api/state").get_json()
        self.assertEqual(s["bot"]["state"], "PAUSED")                  # no market data yet: never "RUNNING"
        self.w.tick()
        s = self.get("/api/state").get_json()
        self.assertEqual((s["bot"]["state"], s["bot"]["stake"], s["limits"]["max_trades"]), ("RUNNING", 2.0, 6))
        self.assertEqual(self.post("/api/bot", {"action": "stop"}).status_code, 200)
        self.assertEqual(self.post("/api/bot", {"action": "nonsense"}).status_code, 400)

    def test_real_mode_over_http(self):
        self.sign_in()
        self.post("/api/ack", {"accept": True})
        self.assertEqual(self.post("/api/mode", {"mode": "real", "confirm": "nope"}).status_code, 400)
        self.assertEqual(self.post("/api/mode", {"mode": "real", "confirm": "REAL"}).status_code, 200)
        bot = next(iter(self.sessions._sessions.values()))
        self.assertTrue(wait_until(bot.session_ready))
        self.assertEqual(self.post("/api/bot", {"action": "start"}).status_code, 400)       # no confirm_real
        self.assertEqual(self.post("/api/bot", {"action": "start", "confirm_real": "yes"}).status_code, 400)
        self.assertEqual(self.post("/api/bot", {"action": "start", "confirm_real": True}).status_code, 200)

    def test_logout_stops_everything(self):
        self.sign_in()
        bot = next(iter(self.sessions._sessions.values()))
        self.assertEqual(self.post("/api/logout").status_code, 200)
        self.assertTrue(bot.closed)
        self.assertFalse(self.get("/api/state").get_json()["authenticated"])

    def test_security_headers_and_no_caching_of_api(self):
        r = self.get("/api/state")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])

    def test_error_pages_do_not_leak_secrets(self):
        r = self.get("/login")
        state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
        err = urllib.error.HTTPError(oauth.TOKEN_URL, 400, "bad", {}, io.BytesIO(f'{{"error":"{TOKEN}"}}'.encode()))
        oauth._urlopen = FakeOAuth(error=err)
        resp = self.get(f"/callback?code=x&state={state}")
        self.assertNotIn("SECRETACCESS", resp.get_data(as_text=True))


class StaticFrontEndTests(unittest.TestCase):
    """The browser code must only ever put server text in with textContent."""

    def test_no_innerhtml_with_dynamic_data(self):
        js = (Path(__file__).resolve().parent / "webapp" / "static" / "app.js").read_text()
        uses = [line.strip() for line in js.splitlines() if ".innerHTML" in line]
        self.assertEqual(len(uses), 1)
        self.assertIn("<rect", uses[0])                              # the one use is a fixed SVG snippet

    def test_html_has_no_inline_scripts_or_external_resources(self):
        html = (Path(__file__).resolve().parent / "webapp" / "static" / "index.html").read_text()
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertEqual(html.count("<script"), 1)
        self.assertIn('src="/static/app.js"', html)


if __name__ == "__main__":
    unittest.main()
