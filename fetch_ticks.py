"""Download R_100 tick history from Deriv's PUBLIC WebSocket (no token, no account needed).

    python fetch_ticks.py                    # newest 50,000 ticks (about 28 hours of R_100)
    python fetch_ticks.py --count 200000     # about 4.6 days; takes a few minutes
Saves/merges into ticks_R_100.csv with the columns  epoch,quote  (the same file the app's tick recorder
writes), ready for  python backtest.py ticks_R_100.csv

Request format (documented at developers.deriv.com, Ticks history comparison):
  {"ticks_history": "R_100", "end": "latest" | <epoch>, "count": N, "style": "ticks"}
  reply: {"history": {"times": [...], "prices": [...]}, "msg_type": "history"}
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

from deriv_connection import CONNECT_TIMEOUT, PUBLIC_WS_URL

HERE = Path(__file__).resolve().parent


class FetchError(Exception):
    pass


def collect(request_fn, symbol, want, batch=1000, pause=0.4, sleep=time.sleep, log=print):
    """Walk backwards in time in `batch`-sized pages until `want` ticks are collected."""
    got, end, request_id = {}, "latest", 0
    while len(got) < want:
        request_id += 1
        reply = request_fn({"ticks_history": symbol, "end": end, "count": batch, "style": "ticks",
                            "req_id": request_id})
        if reply.get("error"):
            err = reply["error"]
            raise FetchError(f"Deriv refused the request ({err.get('code', '?')}): {err.get('message', '')}")
        history = reply.get("history") or {}
        times, prices = history.get("times"), history.get("prices")
        if not isinstance(times, list) or not isinstance(prices, list) or len(times) != len(prices):
            raise FetchError("Unexpected reply (no history.times / history.prices). Reply keys: "
                             + ", ".join(sorted(reply.keys())))
        if not times:                               # past the start of the available history
            if got:
                break
            raise FetchError(f"Deriv returned no ticks for {symbol}.")
        added = 0
        for stamp, price in zip(times, prices):
            stamp = int(stamp)
            if stamp not in got:
                got[stamp] = float(price)
                added += 1
        log(f"  page {request_id}: +{added} ticks (total {len(got)})")
        if added == 0:
            break                                   # no progress: reached the start of available history
        end = min(int(t) for t in times) - 1
        if len(got) < want:
            sleep(pause)
    return sorted(got.items())


def read_existing(path):
    rows = {}
    path = Path(path)
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.reader(handle):
                try:
                    rows[int(float(row[0]))] = float(row[1])
                except (ValueError, IndexError):
                    continue                        # header or damaged line
    return rows


def write_merged(path, new_ticks):
    rows = read_existing(path)
    before = len(rows)
    rows.update(dict(new_ticks))
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["epoch", "quote"])
        for stamp in sorted(rows):
            writer.writerow([stamp, rows[stamp]])
    return before, len(rows)


def make_ws_request_fn(url=PUBLIC_WS_URL, timeout=CONNECT_TIMEOUT):
    try:
        import websocket
    except ImportError:
        raise FetchError("websocket-client is not installed. Run: python -m pip install -r requirements.txt")
    ws = websocket.create_connection(url, timeout=timeout)

    def request(payload):
        ws.send(json.dumps(payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(ws.recv())
            if message.get("req_id") == payload["req_id"] or message.get("error"):
                return message
        raise FetchError("No reply from Deriv within the timeout.")
    request.close = ws.close
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="R_100")
    parser.add_argument("--count", type=int, default=50000, help="how many of the newest ticks to fetch")
    parser.add_argument("--batch", type=int, default=1000, help="ticks per request (Deriv's default is 1000)")
    parser.add_argument("--out", default=str(HERE / "ticks_R_100.csv"))
    args = parser.parse_args(argv)
    request_fn = None
    try:
        request_fn = make_ws_request_fn()
        print(f"Fetching about {args.count} {args.symbol} ticks from the public Deriv feed...")
        ticks = collect(request_fn, args.symbol, args.count, args.batch)
    except FetchError as exc:
        print(f"FAILED: {exc}")
        return 1
    except Exception as exc:                        # network/TLS problems: say what, never a traceback wall
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if request_fn is not None:
            try:
                request_fn.close()
            except Exception:
                pass
    before, after = write_merged(args.out, ticks)
    hours = (ticks[-1][0] - ticks[0][0]) / 3600 if len(ticks) > 1 else 0
    print(f"Downloaded {len(ticks)} ticks spanning {hours:.1f} hours. {args.out} now holds {after} ticks "
          f"({after - before} new). Next: python backtest.py \"{args.out}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
