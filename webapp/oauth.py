"""Deriv OAuth 2.0 (Authorization Code + PKCE). Endpoints per https://developers.deriv.com/docs/intro/oauth

  authorize  https://auth.deriv.com/oauth2/auth
  token      https://auth.deriv.com/oauth2/token   (form POST, no client secret)
The access token lasts about an hour and Deriv documents no refresh token, so a login is good for ~1 hour.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import deriv_connection as dc

AUTH_URL = "https://auth.deriv.com/oauth2/auth"
TOKEN_URL = "https://auth.deriv.com/oauth2/token"
PENDING_TTL = 600
_urlopen = urllib.request.urlopen          # replaced in tests


def new_pkce():
    verifier = secrets.token_urlsafe(48)                       # 64 URL-safe characters
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class PendingLogins:
    """state -> PKCE verifier for logins that have left for Deriv and not come back yet."""

    def __init__(self, ttl=PENDING_TTL, clock=time.monotonic, cap=500):
        self._items, self._lock, self._ttl, self._clock, self._cap = {}, threading.Lock(), ttl, clock, cap

    def create(self):
        state = secrets.token_urlsafe(32)
        verifier, challenge = new_pkce()
        now = self._clock()
        with self._lock:
            for key in [k for k, (_, at) in self._items.items() if now - at > self._ttl]:
                del self._items[key]
            if len(self._items) >= self._cap:
                self._items.pop(next(iter(self._items)))
            self._items[state] = (verifier, now)
        return state, challenge, verifier

    def take(self, state):
        with self._lock:
            item = self._items.pop(state or "", None)
        if item is None or self._clock() - item[1] > self._ttl:
            return None
        return item[0]


def authorize_url(settings, state, challenge):
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": settings.app_id, "redirect_uri": settings.redirect_uri,
        "scope": settings.SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return AUTH_URL + "?" + query


def exchange_code(settings, code, verifier, timeout=dc.HTTP_TIMEOUT):
    """Trade the one-time code for an access token. Returns (access_token, expires_in_seconds)."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": settings.app_id, "code": code,
        "code_verifier": verifier, "redirect_uri": settings.redirect_uri}).encode("ascii")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
        "User-Agent": "5GTradingBot/1.0"})
    try:
        with _urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8", "replace")).get("error", ""))[:60]
        except Exception:
            pass
        raise dc.DerivApiError("auth", "Deriv refused the sign-in" + (f" ({detail})" if detail else "")
                               + ". Please try signing in again.", exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        raise dc.DerivApiError("network", "Could not reach Deriv to finish signing in. Please try again.",
                               retryable=True)
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise dc.DerivApiError("protocol", "Deriv's sign-in reply had no access token.")
    try:
        expires = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires = 3600
    dc.register_secret(token)
    return token, max(60, min(expires, 86400))
