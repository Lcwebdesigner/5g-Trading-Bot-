"""Offline tests for indicators.py, tradelog.py, fetch_ticks.py and backtest.py (no Tk, no network).

Run:  python -m unittest -v test_tools
"""
import contextlib
import csv
import io
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest as bt
import fetch_ticks
import indicators as ind
import tradelog

START = 1_700_000_000


def walk(n, seed=1, start=1000.0, step=0.5):
    rnd = random.Random(seed)
    price, out = start, []
    for _ in range(n):
        price += rnd.gauss(0, step)
        out.append(round(price, 2))
    return out


def tmp_file(name):
    return Path(tempfile.mkdtemp()) / name


class IncrementalMatchesLive(unittest.TestCase):
    """The backtest's fast indicators must give what the live functions give on the bot's 500-tick window."""

    def test_indicator_values(self):
        prices = walk(1600)
        arrays = bt.prepare(prices)
        for idx in (700, 1100, 1599):
            window = prices[idx + 1 - 500: idx + 1]
            for period in (5, 9, 12, 21, 26, 34):
                self.assertAlmostEqual(ind.ema(window, period), arrays["ema"][period][idx], places=6)
            line, signal = ind.macd(window)
            self.assertAlmostEqual(line, arrays["macd"][idx], places=6)
            self.assertAlmostEqual(signal, arrays["macd_signal"][idx], places=6)
            self.assertAlmostEqual(ind.rsi(window, 14), arrays["rsi"][idx], places=6)

    def test_signals_agree_with_the_live_rule(self):
        prices = walk(3200, seed=7, step=0.8)
        sig = bt.signals_for(bt.prepare(prices), bt.LIVE)
        code = {"HIGHER": 1, "LOWER": -1, "WAIT": 0}
        mismatches = 0
        for idx in range(2400, 3200):
            live, _ = ind.evaluate_signal(prices[idx + 1 - 500: idx + 1], 100)
            mismatches += code[live] != sig[idx]
        self.assertLessEqual(mismatches, 8)                          # only float noise at exact thresholds
        self.assertGreater(sum(1 for s in sig[2400:] if s != 0), 50)  # the test is not vacuous

    def test_warmup_and_flat_data(self):
        self.assertEqual(ind.evaluate_signal([1.0] * 50, 100)[0], "WAIT")
        self.assertEqual(ind.evaluate_signal([5.0] * 300, 100)[0], "WAIT")
        self.assertEqual(set(bt.signals_for(bt.prepare([5.0] * 300), bt.LIVE)), {0})


class Simulation(unittest.TestCase):
    def setUp(self):
        self.n = 160
        self.times = [1_000_000_000 + i for i in range(self.n)]

    def test_outcomes_and_one_trade_at_a_time(self):
        prices, sig = [10.0] * self.n, [0] * self.n
        sig[100], sig[101] = 1, 1                      # fresh at 100 only
        prices[105] = 11.0                             # HIGHER and it rose -> win
        sig[110] = -1
        prices[115] = 11.0                             # LOWER but it rose -> loss
        sig[112] = 1                                   # arrives while the trade from 110 is open
        sig[118], sig[121] = 1, 0
        prices[118], prices[123] = 5.0, 5.0            # flat -> tie
        trades = bt.simulate(self.times, prices, sig, 0.95)
        self.assertEqual([t[1] for t in trades], ["W", "L", "T"])
        self.assertEqual([round(t[2], 2) for t in trades], [0.95, -1.0, 0.0])
        again = bt.simulate(self.times, prices, sig, 0.95, one_at_a_time=False)
        self.assertEqual(len(again), 4)                # the overlapping entry at 112 counts when allowed

    def test_daily_limits(self):
        prices = [float(i) for i in range(self.n)]     # always rising
        sig = [0] * self.n
        entries = (100, 110, 120, 130, 140)
        for i in entries:
            sig[i] = -1                                # LOWER every time -> every trade loses
        self.assertEqual(len(bt.simulate(self.times, prices, sig, 0.95)), 5)
        limited = bt.simulate(self.times, prices, sig, 0.95, apply_limits=True)
        self.assertEqual(len(limited), 3)              # stops after 3 losses in a row
        capped = bt.simulate(self.times, prices, sig, 0.95, apply_limits=True, max_trades=2)
        self.assertEqual(len(capped), 2)               # and at the daily trade cap
        spread = [i * 100_000 for i in range(self.n)]  # every entry on a different day -> counters reset
        self.assertEqual(len(bt.simulate(spread, prices, sig, 0.95, apply_limits=True)), 5)

    def test_win_resets_the_loss_streak(self):
        prices = [float(i) for i in range(self.n)]
        sig = [0] * self.n
        for i, direction in zip((100, 110, 120, 130, 140), (-1, -1, 1, -1, -1)):
            sig[i] = direction                         # L L W L L : never 3 in a row
        trades = bt.simulate(self.times, prices, sig, 0.95, apply_limits=True)
        self.assertEqual([t[1] for t in trades], ["L", "L", "W", "L", "L"])


class Statistics(unittest.TestCase):
    def test_wilson_and_breakeven(self):
        low, high = bt.wilson(50, 100)
        self.assertAlmostEqual(low, 0.404, delta=0.01)
        self.assertAlmostEqual(high, 0.596, delta=0.01)
        self.assertEqual(bt.wilson(0, 0), (0.0, 1.0))
        self.assertAlmostEqual(bt.stats([], 0.95)["breakeven"], 1 / 1.95)

    def test_p_value_behaviour(self):
        self.assertGreater(bt.p_above(50, 100, 0.5128), 0.5)
        self.assertLess(bt.p_above(70, 100, 0.5), 0.001)
        self.assertEqual(bt.p_above(0, 0, 0.5), 1.0)

    def test_verdicts(self):
        def verdict_for(wins, losses):
            trades = [(0, "W", 0.95)] * wins + [(0, "L", -1.0)] * losses
            return bt.verdict(bt.stats(trades, 0.95))
        self.assertIn("TOO FEW", verdict_for(30, 20))
        self.assertIn("NO EVIDENCE", verdict_for(205, 195))          # 51.2%: a coin flip
        self.assertIn("NO EVIDENCE", verdict_for(216, 184))          # 54% of 400 is still within chance
        self.assertIn("ABOVE", verdict_for(260, 140))                # 65%
        self.assertIn("BELOW", verdict_for(150, 250))

    def test_profit_and_drawdown(self):
        trades = [(0, "W", 0.95), (1, "L", -1.0), (2, "L", -1.0), (3, "W", 0.95), (4, "T", 0.0)]
        st = bt.stats(trades, 0.95)
        self.assertEqual((st["wins"], st["losses"], st["ties"], st["decided"]), (2, 2, 1, 4))
        self.assertAlmostEqual(st["profit"], -0.1)
        self.assertAlmostEqual(st["max_dd"], 2.0)
        self.assertAlmostEqual(st["win_rate"], 0.5)


class DataLoading(unittest.TestCase):
    def test_load_prices_is_tolerant(self):
        a, b = tmp_file("a.csv"), tmp_file("b.csv")
        a.write_text("epoch,quote\n100,1.5\n102,1.6\nbad,line\n,\n104,1.7\n", encoding="utf-8")
        b.write_text("﻿102,1.65\n106,1.8\n", encoding="utf-8")        # overlap + BOM
        times, prices = bt.load_prices([a, b])
        self.assertEqual(times, [100, 102, 104, 106])
        self.assertEqual(prices, [1.5, 1.65, 1.7, 1.8])

    def test_segments_and_cut(self):
        times = [0, 2, 4, 6, 100, 102, 104, 300, 302]
        segments = bt.split_segments(times, list(range(9)), 30)
        self.assertEqual([len(t) for t, _ in segments], [4, 3, 2])
        first, second = bt.cut_segments(segments, 0.5)                    # cut after 4 of 9 ticks
        self.assertEqual(sum(len(t) for t, _ in first), 4)
        self.assertEqual(sum(len(t) for t, _ in second), 5)
        first, second = bt.cut_segments(segments, 0.6)                    # cut falls inside segment 2
        self.assertEqual([len(t) for t, _ in first], [4, 1])
        self.assertEqual([len(t) for t, _ in second], [2, 2])
        self.assertEqual(bt.split_segments([], [], 30), [])


class EndToEnd(unittest.TestCase):
    def make_file(self, n=9000, seed=3):
        path = tmp_file("ticks.csv")
        prices = walk(n, seed=seed, step=0.7)
        with path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["epoch", "quote"])
            for i, price in enumerate(prices):
                writer.writerow([START + 2 * i, price])
        return path

    def test_analyze_and_report_on_random_data(self):
        path = self.make_file()
        times, prices = bt.load_prices([path])
        result = bt.analyze(bt.split_segments(times, prices, 30), 0.95, 0.6, 50)
        self.assertEqual(result["ticks"], 9000)
        self.assertEqual(result["tried"], 54)
        self.assertLessEqual(len(result["top"]), 3)
        self.assertGreater(result["live"]["all"]["n"], 30)
        text = bt.report(result)
        for needle in ("BACKTEST OF THE BOT'S SIGNAL RULE", "break-even win rate 51.3%", "UNSEEN", "BOTTOM LINE"):
            self.assertIn(needle, text)

    def test_cli_writes_report_and_rejects_small_files(self):
        path, out = self.make_file(n=6000), tmp_file("report.txt")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = bt.main([str(path), "--min-trades", "30", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("BOTTOM LINE", out.read_text())
        small = tmp_file("small.csv")
        small.write_text("1,1.0\n2,1.1\n")
        with contextlib.redirect_stdout(io.StringIO()) as capture:
            self.assertEqual(bt.main([str(small), "--out", str(out)]), 1)
        self.assertIn("fetch_ticks.py", capture.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bt.main([str(path), "--train", "0.99"]), 2)
            self.assertEqual(bt.main([str(tmp_file("missing.csv"))]), 1)


class TradeLog(unittest.TestCase):
    def test_append_summarize_and_text(self):
        path = tmp_file("trade_log.csv")
        rows = [("demo", 1.9), ("demo", -2.0), ("demo", 0.0), ("real", -2.0), ("demo", 1.9)]
        for mode, profit in rows:
            tradelog.append_trade(path, {"time_local": "t", "mode": mode, "profit": profit, "extra": "ignored"})
        lines = path.read_text().splitlines()
        self.assertEqual(lines[0], ",".join(tradelog.FIELDS))
        self.assertEqual(len(lines), 6)                                   # one header, five rows
        demo = tradelog.summarize(path, "demo")
        self.assertEqual((demo["n"], demo["wins"], demo["losses"], demo["ties"]), (4, 2, 1, 1))
        self.assertAlmostEqual(demo["net"], 1.8)
        self.assertAlmostEqual(demo["win_rate"], 2 / 3)
        self.assertEqual(tradelog.summarize(path, "real")["losses"], 1)
        self.assertIn("Net +1.80", tradelog.summary_text(demo, "demo"))
        self.assertIn("no settled trades", tradelog.summary_text(tradelog.summarize(tmp_file("none.csv"), "demo"), "demo"))

    def test_damaged_rows_are_skipped(self):
        path = tmp_file("trade_log.csv")
        path.write_text(",".join(tradelog.FIELDS) + "\nt,demo,A,1,CALL,2,2,3.9,notanumber,WIN,0,1,0\n"
                        "t,demo,A,2,CALL,2,2,3.9,1.9,WIN,0,2,1.9\n")
        self.assertEqual(tradelog.summarize(path, "demo")["n"], 1)


class FakeHistory:
    """Behaves like the public feed: returns the newest `count` ticks at or before `end`."""

    def __init__(self, n=4500):
        self.times = [START + 2 * i for i in range(n)]
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        end = req["end"]
        pool = [t for t in self.times if end == "latest" or t <= end][-req["count"]:]
        return {"history": {"times": pool, "prices": [100 + t % 7 for t in pool]}, "msg_type": "history",
                "req_id": req["req_id"]}


class Fetching(unittest.TestCase):
    def test_pages_backwards_without_gaps_or_duplicates(self):
        server = FakeHistory()
        ticks = fetch_ticks.collect(server, "R_100", 2500, batch=1000, pause=0, log=lambda *_: None)
        self.assertGreaterEqual(len(ticks), 2500)
        stamps = [t for t, _ in ticks]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertEqual(stamps[-1], server.times[-1])                           # starts from the newest tick
        self.assertTrue(all(b - a == 2 for a, b in zip(stamps, stamps[1:])))      # contiguous
        first = server.requests[0]
        self.assertEqual((first["ticks_history"], first["end"], first["style"], first["count"]),
                         ("R_100", "latest", "ticks", 1000))
        self.assertIsInstance(server.requests[1]["end"], int)

    def test_stops_when_history_runs_out(self):
        server = FakeHistory(n=1500)
        ticks = fetch_ticks.collect(server, "R_100", 10_000, batch=1000, pause=0, log=lambda *_: None)
        self.assertEqual(len(ticks), 1500)
        self.assertLess(len(server.requests), 6)
        with self.assertRaises(fetch_ticks.FetchError) as cm:                 # nothing at all for the symbol
            fetch_ticks.collect(lambda req: {"history": {"times": [], "prices": []}}, "R_100", 10,
                                log=lambda *_: None)
        self.assertIn("no ticks", str(cm.exception))

    def test_errors_become_readable_messages(self):
        with self.assertRaises(fetch_ticks.FetchError) as cm:
            fetch_ticks.collect(lambda req: {"error": {"code": "InvalidInput", "message": "bad count"}},
                                "R_100", 10, log=lambda *_: None)
        self.assertIn("bad count", str(cm.exception))
        with self.assertRaises(fetch_ticks.FetchError) as cm:
            fetch_ticks.collect(lambda req: {"msg_type": "history"}, "R_100", 10, log=lambda *_: None)
        self.assertIn("Unexpected reply", str(cm.exception))

    def test_merge_keeps_old_rows_and_adds_new(self):
        path = tmp_file("ticks.csv")
        path.write_text("epoch,quote\n10,1.0\n12,1.1\n")
        before, after = fetch_ticks.write_merged(path, [(12, 9.9), (14, 1.2)])
        self.assertEqual((before, after), (2, 3))
        self.assertEqual(path.read_text().splitlines(), ["epoch,quote", "10,1.0", "12,9.9", "14,1.2"])

    def test_cli_success_and_failure(self):
        out, server = tmp_file("ticks.csv"), FakeHistory(1200)
        server.close = lambda: None
        original = fetch_ticks.make_ws_request_fn
        try:
            fetch_ticks.make_ws_request_fn = lambda: server
            with contextlib.redirect_stdout(io.StringIO()) as capture:
                code = fetch_ticks.main(["--count", "1000", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("Downloaded", capture.getvalue())
            self.assertEqual(len(bt.load_prices([out])[0]), 1000)

            def broken():
                raise fetch_ticks.FetchError("websocket-client is not installed.")
            fetch_ticks.make_ws_request_fn = broken
            with contextlib.redirect_stdout(io.StringIO()) as capture:
                self.assertEqual(fetch_ticks.main(["--out", str(out)]), 1)
            self.assertIn("FAILED", capture.getvalue())
        finally:
            fetch_ticks.make_ws_request_fn = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
