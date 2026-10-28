"""Connection diagnostics for Deriv Desktop Trader Pro.

Run:   python diagnose.py
It checks each link in the chain, prints PASS / FAIL / INFO lines, and saves the same text to
diagnostic_report.txt. The report never contains your token, one-time password or balance, so it is
safe to paste when asking for help. It places NO trades.
"""
import json
import os
import platform
import re
import socket
import sys
import time
from pathlib import Path

import deriv_connection as dc

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / "deriv_token.txt"
ACCOUNT_FILE = HERE / "deriv_account_id.txt"
LEGACY_URL = "wss://ws.derivws.com/websockets/v3?app_id={app_id}"
LINES = []


def out(tag, text):
    line = f"[{tag:4}] {dc.redact(text)}"
    print(line)
    LINES.append(line)


def find_app_id():
    env = os.environ.get("DERIV_APP_ID")
    if env:
        return env.strip(), "environment variable DERIV_APP_ID"
    from_file = dc.read_optional_line(HERE / "deriv_app_id.txt")
    if from_file:
        return from_file, "deriv_app_id.txt"
    try:
        match = re.search(r"^APP_ID\s*=\s*[\"']([^\"']*)[\"']", (HERE / "main.py").read_text(encoding="utf-8"), re.M)
        if match:
            return match.group(1).strip(), "main.py"
    except OSError:
        pass
    return "", "not found"


def mask(value):
    value = str(value or "")
    return value[:3] + "***" if len(value) > 3 else "***"


def ws_roundtrip(url, request, want, timeout=15):
    """Open a socket, send one request, return the first reply whose msg_type/keys match `want`."""
    import websocket
    ws = websocket.create_connection(url, timeout=timeout)
    try:
        ws.send(json.dumps(request))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = json.loads(ws.recv())
            if data.get("error") or want in data or data.get("msg_type") == want:
                return data
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def step_environment():
    out("INFO", f"Python {platform.python_version()} on {platform.system()} {platform.release()}")
    try:
        import websocket
        if hasattr(websocket, "WebSocketApp"):
            out("PASS", f"websocket-client {getattr(websocket, '__version__', '?')} installed")
            return True
        out("FAIL", "The wrong 'websocket' package is installed. Run: python -m pip uninstall websocket "
                    "then python -m pip install websocket-client")
    except ImportError:
        out("FAIL", "websocket-client is not installed. Run: python -m pip install -r requirements.txt")
    return False


def step_config():
    app_id, source = find_app_id()
    ok = True
    try:
        dc.check_app_id(app_id)
        out("PASS", f"APP_ID is set to '{app_id}' (from {source})")
        if app_id == "1089":
            out("WARN", "1089 is Deriv's shared TEST app ID. A Personal Access Token needs the ID of YOUR "
                        "registered app in the Deriv-App-ID header.")
    except dc.DerivApiError as exc:
        out("FAIL", str(exc)); ok = False
    token = None
    if not TOKEN_FILE.exists():
        out("FAIL", "deriv_token.txt not found next to main.py"); return app_id, None, False
    raw = TOKEN_FILE.read_bytes()
    enc = "UTF-16 (PowerShell default)" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else (
        "UTF-8 with BOM" if raw.startswith(b"\xef\xbb\xbf") else "UTF-8/ASCII")
    out("INFO", f"deriv_token.txt: {len(raw)} bytes, encoding {enc} (token value never printed)")
    try:
        token = dc.read_token(TOKEN_FILE)
        out("PASS", f"Token file readable: {len(token)} characters, single line, no spaces")
    except dc.DerivApiError as exc:
        out("FAIL", str(exc)); ok = False
    return app_id, token, ok


def step_dns():
    ok = True
    for host in ("api.derivws.com", "ws.derivws.com"):
        try:
            socket.getaddrinfo(host, 443)
            out("PASS", f"DNS resolves {host}")
        except OSError as exc:
            out("FAIL" if host == "api.derivws.com" else "INFO", f"DNS cannot resolve {host}: {exc}")
            ok = ok and host != "api.derivws.com"
    return ok


def step_rest(app_id, token):
    try:
        reply = dc.http_json("GET", "/v1/health")
        out("PASS", f"REST health check answered: {json.dumps(reply)[:120]}")
    except dc.DerivApiError as exc:
        out("INFO", f"REST health check: {exc}")
    if not token:
        out("SKIP", "Accounts/OTP checks skipped (no usable token or APP_ID)"); return None
    try:
        accounts = dc.normalize_accounts(dc.http_json("GET", dc.ACCOUNTS_PATH, token, app_id))
    except dc.DerivApiError as exc:
        out("FAIL", f"Listing accounts: {exc}"); return None
    out("PASS", f"Accounts endpoint OK: {len(accounts)} account(s)")
    for acc in accounts:
        out("INFO", f"  account {mask(acc['id'])}  type={acc['type']}  currency={acc['currency']}  fields={acc['fields']}")
    try:
        account = dc.pick_demo_account(accounts, dc.read_optional_line(ACCOUNT_FILE))
        out("PASS", f"Demo account selected: {mask(account['id'])}")
    except dc.DerivApiError as exc:
        out("FAIL", str(exc)); return None
    try:
        url, _ = dc.get_demo_ws_url(token, app_id, dc.read_optional_line(ACCOUNT_FILE))
        out("PASS", "OTP issued and the returned address passed the demo-only safety check "
                    "(host *.derivws.com, path .../ws/demo)")
        return url
    except dc.DerivApiError as exc:
        out("FAIL", f"OTP step: {exc}"); return None


def step_demo_ws(url):
    if not url:
        out("SKIP", "Demo WebSocket check skipped (no OTP URL)"); return
    try:
        reply = ws_roundtrip(url, {"balance": 1}, "balance")
    except Exception as exc:
        out("FAIL", f"Demo WebSocket: {type(exc).__name__}: {exc}"); return
    if reply is None:
        out("FAIL", "Demo WebSocket connected but no balance reply within 15s")
    elif reply.get("error"):
        err = reply["error"]
        out("FAIL", f"Demo WebSocket error {err.get('code')}: {err.get('message')}")
    else:
        out("PASS", f"Demo WebSocket connected and balance reply received "
                    f"(currency {reply.get('balance', {}).get('currency', '?')}; amount not shown)")


def step_ticks(label, url):
    try:
        reply = ws_roundtrip(url, {"ticks": "R_100", "subscribe": 1}, "tick")
    except Exception as exc:
        out("FAIL" if label == "public" else "INFO", f"{label} tick feed: {type(exc).__name__}: {exc}"); return
    if reply is None:
        out("FAIL" if label == "public" else "INFO", f"{label} tick feed connected but no tick within 15s")
    elif reply.get("error"):
        err = reply["error"]
        out("FAIL" if label == "public" else "INFO", f"{label} tick feed error {err.get('code')}: {err.get('message')}")
    else:
        out("PASS" if label == "public" else "INFO", f"{label} tick feed works (R_100 quote {reply.get('tick', {}).get('quote')})")


def main():
    out("INFO", "Deriv Desktop Trader Pro diagnostics (no trades are placed)")
    have_ws = step_environment()
    app_id, token, _ = step_config()
    step_dns()
    otp_url = step_rest(app_id, token)
    if have_ws:
        step_ticks("public", dc.PUBLIC_WS_URL)
        step_demo_ws(otp_url)
        if app_id:
            step_ticks("legacy (ws.derivws.com, informational)", LEGACY_URL.format(app_id=app_id))
    else:
        out("SKIP", "WebSocket checks skipped (websocket-client problem above)")
    text = "\n".join(LINES)
    if token and token in text:                         # belt and braces: never write a credential
        print("Internal safety stop: report contained the token and was not saved.")
        return 1
    (HERE / "diagnostic_report.txt").write_text(text + "\n", encoding="utf-8")
    print("\nSaved diagnostic_report.txt. It contains no token, OTP or balance amount; paste it when asking for help.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
