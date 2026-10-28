"""Connection layer for Deriv Desktop Trader Pro (no Tk code lives here).

What this module does
  * Loads the API token from a local file (BOM / UTF-16 safe) and never prints it.
  * Talks to the current Deriv REST API to list accounts and obtain a one-time
    password (OTP) WebSocket URL for the DEMO account.
  * Enforces demo-only: any WebSocket URL that is not a ".../ws/demo" endpoint on a
    derivws.com host is refused before a connection is attempted.
  * Channel: a self-reconnecting WebSocket worker with connect / idle watchdogs that
    reports its *real* state (CONNECTING, CONNECTED, RETRYING, FAILED, DISCONNECTED).

Endpoints / auth follow https://developers.deriv.com (Options API):
  public ticks  wss://api.derivws.com/trading/v1/options/ws/public       (no auth)
  accounts      GET  https://api.derivws.com/trading/v1/options/accounts
  OTP           POST https://api.derivws.com/trading/v1/options/accounts/{id}/otp
  demo socket   wss://api.derivws.com/trading/v1/options/ws/demo?otp=...  (from OTP reply)
REST calls send  Authorization: Bearer <PAT>  and  Deriv-App-ID: <your app id>.
"""
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

REST_BASE = "https://api.derivws.com"
ACCOUNTS_PATH = "/trading/v1/options/accounts"
OTP_PATH = ACCOUNTS_PATH + "/{account_id}/otp"
PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
USER_AGENT = "DerivDesktopTraderPro/2.0"

CONNECT_TIMEOUT = 15.0   # seconds to wait for a WebSocket handshake
HTTP_TIMEOUT = 15.0      # seconds for each REST call
PING_INTERVAL = 20       # WebSocket-level keep-alive
PING_TIMEOUT = 10
BACKOFF_STEPS = (2, 4, 8, 15, 30)   # reconnect delays in seconds

DISCONNECTED, CONNECTING, CONNECTED, RETRYING, FAILED = (
    "DISCONNECTED", "CONNECTING", "CONNECTED", "RETRYING", "FAILED")

_urlopen = urllib.request.urlopen   # replaced in offline tests

# --------------------------------------------------------------------------- secrets
_SECRETS = set()
_secret_lock = threading.Lock()
_OTP_RE = re.compile(r"(otp=)[^&\s'\"]+", re.I)
_BEARER_RE = re.compile(r"(Bearer\s+)[^\s'\"]+", re.I)


def register_secret(value):
    """Remember a credential so redact() can scrub it from any text."""
    if value and len(value) >= 6:
        with _secret_lock:
            _SECRETS.add(value)


def redact(text):
    out = str(text)
    with _secret_lock:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        out = out.replace(secret, "***")
    out = _OTP_RE.sub(r"\1***", out)
    out = _BEARER_RE.sub(r"\1***", out)
    return out


class DerivApiError(Exception):
    """kind: config | network | auth | forbidden | rate_limit | server | protocol"""

    def __init__(self, kind, message, status=None, retryable=False):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retryable = retryable


# --------------------------------------------------------------------------- local files
def read_secret_text(path):
    """Read a small text file; handles UTF-8, UTF-8 with BOM and UTF-16 (PowerShell)."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    return text.lstrip("\ufeff")


def read_token(path):
    path = Path(path)
    try:
        text = read_secret_text(path)
    except FileNotFoundError:
        raise DerivApiError("config", f"{path.name} not found. Create it next to main.py and paste your "
                                      "Personal Access Token on a single line.")
    except UnicodeDecodeError:
        raise DerivApiError("config", f"{path.name} could not be decoded. Re-save it as plain UTF-8 text.")
    except OSError as exc:
        raise DerivApiError("config", f"Could not read {path.name}: {exc.strerror or 'access error'}")
    token = text.strip().strip("\"'")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise DerivApiError("config", f"{path.name} is empty. Paste your Personal Access Token into it.")
    if any(ch.isspace() for ch in token):
        raise DerivApiError("config", f"{path.name} contains spaces or several lines. "
                                      "It must contain only the token, on one line.")
    register_secret(token)
    return token


def read_optional_line(path):
    """Return the first non-empty line of a file, or None if it does not exist/is empty."""
    try:
        for line in read_secret_text(path).splitlines():
            if line.strip():
                return line.strip()
    except (OSError, UnicodeDecodeError):
        pass
    return None


def check_app_id(app_id):
    app_id = str(app_id or "").strip()
    if not app_id or "PASTE" in app_id.upper() or "YOUR_APP_ID" in app_id.upper():
        raise DerivApiError("config", "APP_ID is not set. Put the ID of the app you registered at "
                                      "developers.deriv.com into APP_ID in main.py.")
    return app_id


# --------------------------------------------------------------------------- REST
def _http_error(status, body):
    detail = ""
    try:
        first = json.loads(body.decode("utf-8", "replace"))["errors"][0]
        detail = f"{first.get('code', '')}: {first.get('message', '')}".strip(": ")
    except Exception:
        pass
    low = detail.lower()
    suffix = f" Server said: {detail}" if detail else ""
    if status == 401:
        if "app-id" in low or "app id" in low:
            hint = " Check APP_ID in main.py (the ID of the app you registered, used with a Personal Access Token)."
        else:
            hint = " Check deriv_token.txt: it must hold a current Personal Access Token with the 'trade' scope."
        return DerivApiError("auth", "Token or App ID rejected (HTTP 401)." + hint + suffix, 401)
    if status == 403:
        return DerivApiError("forbidden", "Access denied (HTTP 403). The token may lack the 'trade' scope, "
                                          "or a firewall/proxy blocked the request." + suffix, 403)
    if status == 429 or "ratelimit" in low:
        return DerivApiError("rate_limit", "Rate limited by Deriv (HTTP 429)." + suffix, status, retryable=True)
    if status >= 500:
        return DerivApiError("server", f"Deriv server error (HTTP {status})." + suffix, status, retryable=True)
    return DerivApiError("protocol", f"Unexpected response (HTTP {status})." + suffix, status)


def http_json(method, path, token=None, app_id=None, timeout=None):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = "Bearer " + token
    if app_id:
        headers["Deriv-App-ID"] = str(app_id)
    req = urllib.request.Request(REST_BASE + path, method=method, headers=headers,
                                 data=b"" if method == "POST" else None)
    try:
        with _urlopen(req, timeout=HTTP_TIMEOUT if timeout is None else timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        raise _http_error(exc.code, body)
    except (urllib.error.URLError, OSError) as exc:     # DNS, refused, timeout, TLS...
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            text = "TLS/certificate error (check antivirus, VPN or corporate proxy)"
        elif isinstance(reason, (socket.timeout, TimeoutError)):
            text = f"timed out after {HTTP_TIMEOUT:.0f}s"
        elif isinstance(reason, socket.gaierror):
            text = "DNS lookup failed (no internet or DNS blocked)"
        else:
            text = redact(reason)
        raise DerivApiError("network", f"Cannot reach api.derivws.com: {text}.", retryable=True)
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise DerivApiError("protocol", "Deriv returned a reply that is not valid JSON.")


def normalize_accounts(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise DerivApiError("protocol", "Unexpected accounts reply (no 'data' list).")
    accounts = []
    for item in data:
        if not isinstance(item, dict):
            continue
        acc_id = next((str(item[k]) for k in ("account_id", "loginid", "id") if item.get(k)), None)
        acc_type = next((str(item[k]).lower() for k in ("account_type", "type") if item.get(k)), None)
        if acc_type is None:
            for key in ("is_virtual", "is_demo"):
                if key in item:
                    acc_type = "demo" if item[key] in (1, True, "1", "true") else "real"
                    break
        accounts.append({"id": acc_id, "type": acc_type, "currency": item.get("currency"),
                         "fields": sorted(item.keys())})
    return accounts


def pick_demo_account(accounts, override_id=None):
    if override_id:
        for acc in accounts:
            if acc["id"] == override_id:
                if acc["type"] == "real":
                    raise DerivApiError("config", "deriv_account_id.txt names a REAL account. "
                                                  "This app only uses demo accounts.")
                return acc
        raise DerivApiError("config", "The account ID in deriv_account_id.txt is not among the accounts "
                                      "returned for your token.")
    demos = [a for a in accounts if a["type"] == "demo" and a["id"]]
    if demos:
        return demos[0]
    if accounts and all(a["type"] is None for a in accounts):
        fields = sorted({f for a in accounts for f in a["fields"]})
        raise DerivApiError("config", "Cannot tell which account is the demo account (reply fields: "
                                      f"{', '.join(fields)}). Put your demo account ID in deriv_account_id.txt.")
    raise DerivApiError("config", "No demo account found for this token. Open a demo account on Deriv first.")


def assert_ws_url(url, mode):
    """Account-type guard: the socket must be the endpoint of the requested mode ("demo" or "real").
    Never echoes the URL (it contains a one-time password)."""
    if mode not in ("demo", "real"):
        raise DerivApiError("config", "Unknown trading mode. No connection was made.")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    host_ok = host == "derivws.com" or host.endswith(".derivws.com")
    if (parsed.scheme != "wss" or not host_ok
            or not parsed.path.rstrip("/").endswith("/ws/" + mode)
            or "otp" not in parse_qs(parsed.query)):
        raise DerivApiError("config", "Safety check failed: the WebSocket address returned by Deriv is not a "
                                      f"{mode} endpoint. No connection was made.")
    for value in parse_qs(parsed.query).get("otp", []):
        register_secret(value)


def assert_demo_ws_url(url):
    assert_ws_url(url, "demo")


def pick_real_account(accounts, override_id=None):
    """REAL accounts must be explicitly labelled 'real' by Deriv: unknown types fail closed."""
    if override_id:
        for acc in accounts:
            if acc["id"] == override_id:
                if acc["type"] != "real":
                    raise DerivApiError("config", "deriv_real_account_id.txt must name a REAL account, "
                                                  "but that account is not labelled real by Deriv.")
                return acc
        raise DerivApiError("config", "The account ID in deriv_real_account_id.txt is not among the accounts "
                                      "returned for your token.")
    reals = [a for a in accounts if a["type"] == "real" and a["id"]]
    if reals:
        return reals[0]
    raise DerivApiError("config", "No real account found for this token. Open or fund a real account on Deriv first, "
                                  "or switch back to demo.")


def get_ws_url(token, app_id, mode, override_id=None):
    """REST: list accounts -> pick the account of the requested mode -> request an OTP URL -> verify the endpoint."""
    if mode not in ("demo", "real"):
        raise DerivApiError("config", "Unknown trading mode. No connection was made.")
    accounts = normalize_accounts(http_json("GET", ACCOUNTS_PATH, token, app_id))
    account = pick_demo_account(accounts, override_id) if mode == "demo" else pick_real_account(accounts, override_id)
    path = OTP_PATH.format(account_id=quote(account["id"], safe=""))
    reply = http_json("POST", path, token, app_id)
    url = reply.get("data", {}).get("url") if isinstance(reply, dict) and isinstance(reply.get("data"), dict) else None
    if not isinstance(url, str):
        raise DerivApiError("protocol", "The OTP reply did not contain a WebSocket URL.")
    assert_ws_url(url, mode)
    return url, account


def get_demo_ws_url(token, app_id, override_id=None):
    return get_ws_url(token, app_id, "demo", override_id)


# --------------------------------------------------------------------------- WebSocket channel
def default_ws_factory(url, on_open, on_message, on_error, on_close):
    try:
        import websocket
    except ImportError:
        raise DerivApiError("config", "The 'websocket-client' package is not installed. "
                                      "Run: python -m pip install -r requirements.txt")
    if not hasattr(websocket, "WebSocketApp"):
        raise DerivApiError("config", "The wrong 'websocket' package is installed. Run: "
                                      "python -m pip uninstall websocket   then   "
                                      "python -m pip install websocket-client")
    websocket.setdefaulttimeout(CONNECT_TIMEOUT)    # bounds the TCP/TLS/handshake wait
    return websocket.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                  on_error=on_error, on_close=on_close)


class Channel:
    """One WebSocket connection that reconnects on its own and reports its true state.

    url_provider()  -> wss URL (called before EVERY attempt; may raise DerivApiError)
    on_message(channel, text)   on_open(channel)   on_disconnect()
    on_state(name, state, detail)   on_log(name, text)
    idle_timeout    seconds without any message before the socket is recycled (None = off)
    """

    def __init__(self, name, url_provider, on_message, *, on_open=None, on_state=None,
                 on_disconnect=None, on_log=None, idle_timeout=None, ws_factory=None):
        self.name = name
        self._url_provider = url_provider
        self._on_message = on_message
        self._on_open = on_open
        self._on_state = on_state
        self._on_disconnect = on_disconnect
        self._on_log = on_log
        self.idle_timeout = idle_timeout
        self._factory = ws_factory or default_ws_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.state = DISCONNECTED
        self.detail = "Not started"
        self._ws = None
        self._attempt_started = None
        self._last_msg_at = None
        self._last_reason = ""
        self._opened = False
        self._got_traffic = False
        self._closed_by_watchdog = None

    # ---- public API
    def start(self):
        with self._lock:
            if (self._thread is not None and self._thread.is_alive()
                    and not self._stop.is_set()):
                return False                      # a live, un-stopped run already exists
            self._stop = threading.Event()        # a stopped run may still be unwinding; it is fenced off
            stop = self._stop
            self._thread = threading.Thread(target=self._run, args=(stop,), daemon=True,
                                            name=f"{self.name}-channel")
            thread = self._thread
        thread.start()
        threading.Thread(target=self._watch, args=(stop, thread), daemon=True,
                         name=f"{self.name}-watchdog").start()
        return True

    def stop(self):
        with self._lock:
            stop, ws = self._stop, self._ws
        stop.set()
        self._close(ws)
        self._set_state(DISCONNECTED, "Stopped", force=True)

    def send(self, payload):
        with self._lock:
            ws, connected = self._ws, self.state == CONNECTED
        if ws is None or not connected:
            return False
        try:
            ws.send(json.dumps(payload))
            return True
        except Exception as exc:
            self._log(f"send failed: {redact(exc)}")
            return False

    # ---- internals
    def _log(self, text):
        if self._on_log:
            try:
                self._on_log(self.name, text)
            except Exception:
                pass

    def _set_state(self, state, detail, force=False, stop=None):
        # A worker thread from an earlier run (stop() then start()) must never touch the new run.
        if not force:
            mine = stop if stop is not None else self._stop
            if mine.is_set() or mine is not self._stop:
                return
        with self._lock:
            self.state, self.detail = state, detail
        if self._on_state:
            try:
                self._on_state(self.name, state, detail)
            except Exception as exc:
                self._log(f"state handler error: {redact(exc)}")

    @staticmethod
    def _close(ws):
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def _delay(attempt):
        return BACKOFF_STEPS[min(attempt, len(BACKOFF_STEPS) - 1)]

    def _run(self, stop):
        attempt = 0
        while not stop.is_set():
            self._set_state(CONNECTING, "Connecting...", stop=stop)
            try:
                url = self._url_provider()
                traffic = self._run_socket(url, stop)
            except DerivApiError as exc:
                message = redact(exc)
                if not exc.retryable:
                    self._set_state(FAILED, message, stop=stop)
                    self._notify_disconnect(stop)
                    return
                traffic, self._last_reason = False, message
            except Exception as exc:                      # a bug: say so instead of hanging
                self._set_state(FAILED, f"Unexpected internal error: {redact(exc)}", stop=stop)
                self._notify_disconnect(stop)
                return
            if stop.is_set():
                return
            attempt = 0 if traffic else attempt + 1
            delay = self._delay(attempt - 1 if attempt else 0)
            reason = self._last_reason or "connection closed"
            self._set_state(RETRYING, f"{reason} Retrying in {delay:g}s.", stop=stop)
            if stop.wait(delay):
                return

    def _notify_disconnect(self, stop):
        if self._on_disconnect and stop is self._stop:
            try:
                self._on_disconnect()
            except Exception as exc:
                self._log(f"disconnect handler error: {redact(exc)}")

    def _run_socket(self, url, stop):
        self._opened = self._got_traffic = False
        self._last_reason = ""
        ws = self._factory(url, self._h_open, self._h_message, self._h_error, self._h_close)
        with self._lock:
            self._ws = ws
            self._attempt_started = time.monotonic()
            self._last_msg_at = None
        try:
            if not stop.is_set():
                ws.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT)
        except Exception as exc:
            self._last_reason = self._last_reason or redact(exc)
        finally:
            with self._lock:
                if self._ws is ws:
                    self._ws = None
                    self._attempt_started = None
            self._notify_disconnect(stop)
        if not self._last_reason:
            self._last_reason = "Connection closed by the server." if self._opened else "Could not connect."
        return self._got_traffic

    def _h_open(self, ws):
        if ws is not self._ws or self._stop.is_set():
            return
        self._opened = True
        with self._lock:
            self._last_msg_at = time.monotonic()
        self._set_state(CONNECTED, "Connected")
        if self._on_open:
            try:
                self._on_open(self)
            except Exception as exc:
                self._log(f"open handler error: {redact(exc)}")

    def _h_message(self, ws, message):
        if ws is not self._ws:
            return
        self._got_traffic = True
        with self._lock:
            self._last_msg_at = time.monotonic()
        try:
            self._on_message(self, message)
        except Exception as exc:
            self._log(f"message handler error: {redact(exc)}")

    def _h_error(self, ws, error):
        if ws is not self._ws:
            return
        self._last_reason = redact(error)[:220].rstrip(".") + "."

    def _h_close(self, ws, code=None, message=None):
        if ws is not self._ws:
            return
        if not self._last_reason:
            suffix = f" (code {code})" if code else ""
            self._last_reason = f"Connection closed{suffix}."

    def _watch(self, stop, run_thread):
        while not stop.wait(0.5):
            if not run_thread.is_alive():
                return
            with self._lock:
                ws, state = self._ws, self.state
                started, last = self._attempt_started, self._last_msg_at
            if ws is None or ws is self._closed_by_watchdog:
                continue
            now = time.monotonic()
            if state == CONNECTING and started is not None and now - started > CONNECT_TIMEOUT:
                self._last_reason = f"Timed out after {CONNECT_TIMEOUT:g}s waiting for the server."
            elif (state == CONNECTED and self.idle_timeout and last is not None
                  and now - last > self.idle_timeout):
                self._last_reason = f"No data received for {int(now - last)}s."
            else:
                continue
            self._closed_by_watchdog = ws
            self._close(ws)
