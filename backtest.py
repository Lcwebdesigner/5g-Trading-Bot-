"""Backtest the bot's signal rule on saved R_100 ticks and say, honestly, whether it beats break-even.

    python fetch_ticks.py                      # first download some history (public data, no token)
    python backtest.py ticks_R_100.csv         # then test it
    python backtest.py ticks_R_100.csv more_ticks.csv --payout 0.92 --apply-limits

What it does
  1. Replays the EXACT live rule (EMA 9/21 + MACD 12/26/9 + RSI 14, same thresholds) over the ticks.
  2. Tries 54 alternative settings on the first part of the data only (default 60 %).
  3. Tests the three best of those on the LAST part, which they never saw. Only that column is evidence.
  4. Compares every win rate with the break-even rate implied by the payout, with a 95 % confidence interval.

It never talks to Deriv and never places a trade. The result is a statement about past data, not a promise.
"""
import argparse
import csv
import itertools
import math
import sys
from collections import namedtuple
from pathlib import Path

MIN_HISTORY = 100        # the live bot waits for 100 ticks before it evaluates anything
HORIZON = 5              # 5-tick Rise/Fall
DEFAULT_PAYOUT = 0.95    # profit per 1.00 staked on a win. ASSUMPTION: check the payout Deriv shows.
DEFAULT_MAX_GAP = 30     # seconds; a longer hole in the data splits it into separate segments

Variant = namedtuple("Variant", "fast slow rsi_mode use_macd")
LIVE = Variant(9, 21, "live", True)


def label(v):
    macd_text = "MACD on" if v.use_macd else "MACD off"
    return f"EMA {v.fast}/{v.slow}, RSI {v.rsi_mode}, {macd_text}"


# ---------------------------------------------------------------------------------------- data
def load_prices(paths):
    """Read epoch,quote rows from one or more CSV files; sort, de-duplicate, skip headers/bad lines."""
    rows = {}
    for path in paths:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            for row in csv.reader(handle):
                try:
                    rows[int(float(row[0]))] = float(row[1])
                except (ValueError, IndexError):
                    continue
    times = sorted(rows)
    return times, [rows[t] for t in times]


def split_segments(times, prices, max_gap=DEFAULT_MAX_GAP):
    segments, start = [], 0
    for i in range(1, len(times)):
        if times[i] - times[i - 1] > max_gap:
            segments.append((times[start:i], prices[start:i]))
            start = i
    if times:
        segments.append((times[start:], prices[start:]))
    return segments


def cut_segments(segments, fraction):
    """Split segments by tick count into (first part, last part); a segment crossing the cut is divided."""
    total = sum(len(t) for t, _ in segments)
    cut, used, first, second = int(total * fraction), 0, [], []
    for t, p in segments:
        n = len(t)
        if used + n <= cut:
            first.append((t, p))
        elif used >= cut:
            second.append((t, p))
        else:
            k = cut - used
            first.append((t[:k], p[:k]))
            second.append((t[k:], p[k:]))
        used += n
    return first, second


# ---------------------------------------------------------------------------------------- indicators
def ema_arr(vals, period):
    out = [None] * len(vals)
    if period < 1 or len(vals) < period:
        return out
    value = sum(vals[:period]) / period
    out[period - 1] = value
    alpha = 2.0 / (period + 1)
    for i in range(period, len(vals)):
        value = alpha * vals[i] + (1.0 - alpha) * value
        out[i] = value
    return out


def macd_arr(vals, fast=12, slow=26, signal=9):
    n = len(vals)
    f, s = ema_arr(vals, fast), ema_arr(vals, slow)
    line = [None] * n
    for i in range(n):
        if s[i] is not None:
            line[i] = f[i] - s[i]
    start = slow - 1
    sig = [None] * n
    if n > start:
        sig[start:] = ema_arr(line[start:], signal)
    return line, sig


def _rsi_value(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_arr(vals, period=14):
    n = len(vals)
    out = [None] * n
    if n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = vals[i] - vals[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = vals[i] - vals[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


PERIODS = (5, 9, 12, 21, 26, 34)


def prepare(prices):
    line, sig = macd_arr(prices)
    return {"ema": {p: ema_arr(prices, p) for p in PERIODS}, "macd": line, "macd_signal": sig,
            "rsi": rsi_arr(prices, 14), "n": len(prices)}


def _rsi_long_ok(mode, value):
    return True if mode == "none" else 50 <= value < (70 if mode == "live" else 80)


def _rsi_short_ok(mode, value):
    return True if mode == "none" else (30 if mode == "live" else 20) < value <= 50


def signals_for(prep, variant):
    """+1 = HIGHER, -1 = LOWER, 0 = WAIT for every tick (same thresholds as indicators.evaluate_signal)."""
    n = prep["n"]
    out = [0] * n
    fast, slow = prep["ema"][variant.fast], prep["ema"][variant.slow]
    line, sig, rsi = prep["macd"], prep["macd_signal"], prep["rsi"]
    for i in range(MIN_HISTORY - 1, n):
        f, s, r = fast[i], slow[i], rsi[i]
        if f is None or s is None or r is None:
            continue
        if variant.use_macd and (line[i] is None or sig[i] is None):
            continue
        if f > s and (not variant.use_macd or line[i] > sig[i]) and _rsi_long_ok(variant.rsi_mode, r):
            out[i] = 1
        elif f < s and (not variant.use_macd or line[i] < sig[i]) and _rsi_short_ok(variant.rsi_mode, r):
            out[i] = -1
    return out


# ---------------------------------------------------------------------------------------- simulation
def simulate(times, prices, sig, payout, one_at_a_time=True, apply_limits=False, max_trades=10, max_losses=3):
    """Entry on the tick where the signal turns HIGHER/LOWER; result HORIZON ticks later.
    Returns [(epoch, 'W'|'L'|'T', profit_in_stakes)]."""
    trades, last_exit = [], -1
    day, day_trades, day_losses = None, 0, 0
    for i in range(MIN_HISTORY - 1, len(prices) - HORIZON):
        s = sig[i]
        if s == 0 or s == sig[i - 1]:
            continue                                  # not a fresh signal
        if one_at_a_time and i <= last_exit:
            continue                                  # the bot trades one contract at a time
        if apply_limits:
            current_day = times[i] // 86400
            if current_day != day:
                day, day_trades, day_losses = current_day, 0, 0
            if day_trades >= max_trades or day_losses >= max_losses:
                continue
        diff = prices[i + HORIZON] - prices[i]
        if diff == 0:
            result, profit = "T", 0.0
        elif (diff > 0) == (s > 0):
            result, profit = "W", payout
        else:
            result, profit = "L", -1.0
        last_exit = i + HORIZON
        if apply_limits:
            day_trades += 1
            if result == "L":
                day_losses += 1
            elif result == "W":
                day_losses = 0
        trades.append((times[i], result, profit))
    return trades


def run_variant(segments, preps, variant, payout, apply_limits=False):
    trades = []
    for (times, prices), prep in zip(segments, preps):
        trades.extend(simulate(times, prices, signals_for(prep, variant), payout, apply_limits=apply_limits))
    return trades


# ---------------------------------------------------------------------------------------- statistics
def wilson(wins, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half, centre + half


def p_above(wins, n, p0):
    """One-sided p-value (normal approximation) for 'true win rate is above p0'."""
    if n == 0:
        return 1.0
    sd = math.sqrt(n * p0 * (1 - p0))
    return 0.5 * math.erfc(((wins - 0.5 - n * p0) / sd) / math.sqrt(2))


def stats(trades, payout):
    wins = sum(1 for t in trades if t[1] == "W")
    losses = sum(1 for t in trades if t[1] == "L")
    ties = len(trades) - wins - losses
    decided = wins + losses
    profit = sum(t[2] for t in trades)
    peak = cum = drawdown = 0.0
    for t in trades:
        cum += t[2]
        peak = max(peak, cum)
        drawdown = max(drawdown, peak - cum)
    breakeven = 1.0 / (1.0 + payout)
    return {"n": len(trades), "wins": wins, "losses": losses, "ties": ties, "decided": decided,
            "win_rate": wins / decided if decided else None, "profit": profit,
            "per_trade": profit / len(trades) if trades else 0.0, "max_dd": drawdown,
            "breakeven": breakeven, "ci": wilson(wins, decided), "p_value": p_above(wins, decided, breakeven)}


def verdict(st):
    if st["decided"] < 100:
        return "TOO FEW TRADES to conclude anything"
    low, high = st["ci"]
    if low > st["breakeven"]:
        return "ABOVE break-even (95% interval clears it): encouraging, confirm on newer data"
    if high < st["breakeven"]:
        return "BELOW break-even: this rule loses money on this data"
    return "NO EVIDENCE of an edge: results are consistent with chance"


def variants():
    return [Variant(f, s, mode, m) for f, s in itertools.product((5, 9, 12), (21, 26, 34))
            for mode in ("live", "wide", "none") for m in (True, False)]


# ---------------------------------------------------------------------------------------- analysis
def analyze(segments, payout=DEFAULT_PAYOUT, train_fraction=0.6, min_trades=100, apply_limits=False):
    train, test = cut_segments(segments, train_fraction)
    prep = {"all": [prepare(p) for _, p in segments], "train": [prepare(p) for _, p in train],
            "test": [prepare(p) for _, p in test]}
    sets = {"all": segments, "train": train, "test": test}

    def run(name, variant):
        return stats(run_variant(sets[name], prep[name], variant, payout, apply_limits), payout)

    result = {"payout": payout, "train_fraction": train_fraction, "apply_limits": apply_limits,
              "ticks": sum(len(t) for t, _ in segments), "segments": len(segments),
              "seconds": sum(t[-1] - t[0] for t, _ in segments if len(t) > 1),
              "live": {name: run(name, LIVE) for name in ("all", "train", "test")}}
    ranked = []
    for variant in variants():
        st = run("train", variant)
        if st["decided"] >= min_trades:
            ranked.append((st["per_trade"], variant, st))
    ranked.sort(key=lambda item: item[0], reverse=True)
    result["tried"] = len(variants())
    result["eligible"] = len(ranked)
    result["top"] = [{"variant": v, "train": st, "test": run("test", v)} for _, v, st in ranked[:3]]
    return result


def _line(name, st):
    if st["decided"] == 0:
        return f"   {name:<10} no trades"
    lo, hi = st["ci"]
    return (f"   {name:<10} {st['n']:>5} trades  W{st['wins']}/L{st['losses']}/T{st['ties']}  "
            f"win rate {st['win_rate'] * 100:5.1f}% (95% range {lo * 100:.1f}-{hi * 100:.1f}%)  "
            f"profit/trade {st['per_trade']:+.3f} stake  max drawdown {st['max_dd']:.1f} stakes")


def report(r):
    be = 1.0 / (1.0 + r["payout"])
    hours = r["seconds"] / 3600
    out = ["BACKTEST OF THE BOT'S SIGNAL RULE ON R_100 TICKS", "=" * 52,
           f"Data: {r['ticks']} ticks, about {hours:.1f} hours, {r['segments']} segment(s) "
           f"(a gap over {DEFAULT_MAX_GAP}s starts a new one).",
           f"Assumed payout {r['payout']:.2f} per 1.00 staked -> break-even win rate {be * 100:.1f}%.",
           "  (The payout is an assumption: use the one Deriv shows for a 5-tick Rise/Fall.)",
           "Entry = the tick where the signal appears, result 5 ticks later, one trade at a time"
           + (", with the 10 trades/day and 3-losses-in-a-row stops." if r["apply_limits"] else "."),
           "Real contracts start on the tick AFTER purchase, so live results will differ somewhat.", "",
           "1) THE BOT'S CURRENT RULE (EMA 9/21 + MACD + RSI)"]
    pct = int(r["train_fraction"] * 100)
    out += [_line("all data", r["live"]["all"]), _line(f"first {pct}%", r["live"]["train"]),
            _line(f"last {100 - pct}%", r["live"]["test"]),
            f"   Verdict (all data): {verdict(r['live']['all'])}", "",
            f"2) VARIANT SEARCH: {r['tried']} settings tried on the first {pct}% only "
            f"({r['eligible']} had enough trades)"]
    if not r["top"]:
        out.append("   Not enough trades in the training part to rank settings. Download more ticks.")
    for rank, item in enumerate(r["top"], 1):
        out += [f"   #{rank} {label(item['variant'])}", "     " + _line("train", item["train"]).strip(),
                "     " + _line("UNSEEN", item["test"]).strip(), f"     Verdict on unseen data: {verdict(item['test'])}"]
    out += ["", "   Only the UNSEEN lines count. Trying many settings makes the best one look good by luck.", "",
            "BOTTOM LINE"]
    cleared = [i for i in r["top"] if i["test"]["decided"] >= 100 and i["test"]["ci"][0] > be]
    live_all = r["live"]["all"]
    if live_all["decided"] >= 100 and live_all["ci"][0] > be:
        out.append("- The current rule clears break-even on this data. Re-test on a fresh download before trusting it.")
    elif cleared:
        out.append(f"- {len(cleared)} variant(s) cleared break-even on unseen data. With 3 candidates a lucky one is "
                   "possible by chance (about 7%): download newer ticks and run this again before relying on it.")
    else:
        out.append("- Nothing here beat break-even on unseen data. That is the expected result if these indicators "
                   "carry no real edge on a random synthetic index.")
        out.append("- Do not move to real money on this evidence. Past ticks are not a promise either way.")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="CSV files with epoch,quote rows")
    parser.add_argument("--payout", type=float, default=DEFAULT_PAYOUT)
    parser.add_argument("--train", type=float, default=0.6, help="fraction of the data used to pick settings")
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--max-gap", type=int, default=DEFAULT_MAX_GAP)
    parser.add_argument("--apply-limits", action="store_true", help="also apply 10 trades/day and 3-loss stop")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "backtest_report.txt"))
    args = parser.parse_args(argv)
    if not 0.2 <= args.train <= 0.9 or args.payout <= 0:
        print("--train must be between 0.2 and 0.9 and --payout must be positive.")
        return 2
    try:
        times, prices = load_prices(args.files)
    except OSError as exc:
        print(f"Could not read the data: {exc.strerror or exc}")
        return 1
    if len(times) < 2000:
        print(f"Only {len(times)} ticks found. Run  python fetch_ticks.py  first (50,000 ticks is a good start).")
        return 1
    text = report(analyze(split_segments(times, prices, args.max_gap), args.payout, args.train,
                          args.min_trades, args.apply_limits))
    print(text)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(f"\nSaved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
