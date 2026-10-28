"""Trade log: one CSV row per settled contract, plus a summary for the screen. No Tk, no network."""
import csv
import threading
from pathlib import Path

FIELDS = ["time_local", "mode", "account_id", "contract_id", "direction", "stake", "buy_price",
          "sell_price", "profit", "result", "consecutive_losses", "trades_today", "net_pnl_today"]
_lock = threading.Lock()


def append_trade(path, row):
    """Append one row, writing the header first if the file is new or empty."""
    path = Path(path)
    with _lock:
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow({key: ("" if row.get(key) is None else row.get(key)) for key in FIELDS})


def summarize(path, mode):
    """Totals for one mode ('demo' or 'real') over every day in the log."""
    totals = {"n": 0, "wins": 0, "losses": 0, "ties": 0, "net": 0.0, "win_rate": None}
    path = Path(path)
    if not path.exists():
        return totals
    with _lock:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return totals
    for row in rows:
        if (row.get("mode") or "").lower() != mode:
            continue
        try:
            profit = float(row.get("profit") or 0.0)
        except ValueError:
            continue
        totals["n"] += 1
        totals["net"] += profit
        if profit > 0:
            totals["wins"] += 1
        elif profit < 0:
            totals["losses"] += 1
        else:
            totals["ties"] += 1
    decided = totals["wins"] + totals["losses"]
    if decided:
        totals["win_rate"] = totals["wins"] / decided
    return totals


def summary_text(totals, mode):
    if not totals["n"]:
        return f"Trade log ({mode}): no settled trades yet"
    rate = "n/a" if totals["win_rate"] is None else f"{totals['win_rate'] * 100:.1f}%"
    return (f"Trade log ({mode}): {totals['n']} trades  •  W {totals['wins']} / L {totals['losses']} / "
            f"T {totals['ties']}  •  Net {totals['net']:+.2f}  •  Win rate {rate}")
