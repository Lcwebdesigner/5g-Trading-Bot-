"""Flask server for the 5G Trading Bot web app.

Run (from the project folder):   python -m webapp.server
Behind a reverse proxy that provides https (see deploy/DEPLOY.md). ONE process only: sign-in sessions and
running bots live in memory, so never start several workers.
"""
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, make_response, redirect, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deriv_connection as dc                                   # noqa: E402
from webapp import oauth                                        # noqa: E402
from webapp.engine import (LimitStore, MarketHub, SessionConflict, SessionStore, TooManyUsers,   # noqa: E402
                           fetch_accounts)
from webapp.settings import Settings                            # noqa: E402

COOKIE = "s5g"
STATIC_DIR = Path(__file__).resolve().parent / "static"
log = logging.getLogger("5g")

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>5G Trading Bot</title>
<style>body{{font-family:system-ui,sans-serif;background:#eceafd;color:#1d1b3a;margin:0;display:grid;
place-items:center;min-height:100vh}}main{{max-width:420px;margin:16px;padding:24px;background:#fff;
border-radius:16px;box-shadow:0 4px 24px #0002}}a{{color:#5b3fd6}}</style></head><body><main>
<h2>{title}</h2><p>{body}</p><p><a href="/">Back to 5G Trading Bot</a></p></main></body></html>"""


def message_page(title, body, status=200):
    return Response(PAGE.format(title=title, body=body), status=status, mimetype="text/html")


def create_app(settings=None, hub=None, sessions=None, start_background=True):
    settings = settings or Settings()
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if hub is None:
        hub = MarketHub(settings.symbol, tick_file=str(settings.data_dir / f"ticks_{settings.symbol}.csv"))
    if sessions is None:
        sessions = SessionStore(settings, hub, LimitStore(settings.data_dir, settings.tz))
    pending = oauth.PendingLogins()
    app.extensions["bot"] = {"settings": settings, "hub": hub, "sessions": sessions, "pending": pending}
    if start_background:
        sessions.start_sweeper()
    own_origin = urlparse(settings.base_url)
    own_origin = f"{own_origin.scheme}://{own_origin.netloc}" if settings.base_url else None

    # ---------------------------------------------------------------- helpers
    def current_bot():
        return sessions.get(request.cookies.get(COOKIE))

    def require_bot():
        bot = current_bot()
        if bot is None:
            abort(make_response(jsonify(error="signed_out", message="Please sign in."), 401))
        return bot

    def check_csrf():
        """State-changing calls must be JSON, carry our custom header and come from our own origin."""
        if request.headers.get("X-Requested-With") != "5g" or not request.is_json:
            abort(make_response(jsonify(error="bad_request", message="Request refused."), 400))
        origin = request.headers.get("Origin")
        if origin and own_origin and origin != own_origin:
            abort(make_response(jsonify(error="bad_origin", message="Request refused."), 403))

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if request.path.startswith(("/api/", "/login", "/callback")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC_DIR, name)

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, users=sessions.count(), market=hub.snapshot()["feed_state"])

    @app.get("/login")
    def login():
        problems = settings.problems()
        if problems:
            return message_page("Not set up yet", "The server owner still needs to finish setup: "
                                + " ".join(problems), 503)
        state, challenge, _verifier = pending.create()      # the verifier stays on the server, keyed by state
        return redirect(oauth.authorize_url(settings, state, challenge), code=302)

    @app.get("/callback")
    def callback():
        if request.args.get("error"):
            return message_page("Sign-in cancelled", "Deriv did not complete the sign-in. You can try again.", 400)
        verifier = pending.take(request.args.get("state"))
        code = request.args.get("code")
        if verifier is None or not code:
            return message_page("Sign-in expired", "That sign-in link is no longer valid. Please start again.", 400)
        try:
            token, expires_in = oauth.exchange_code(settings, code, verifier)
            accounts = fetch_accounts(token)
            if not any(a.get("id") for a in accounts):
                return message_page("No Deriv account", "Your Deriv login has no trading account yet. "
                                    "Open one at deriv.com, then sign in again.", 400)
            sid, _ = sessions.create(token, expires_in, accounts)
        except dc.DerivApiError as exc:
            return message_page("Sign-in failed", dc.redact(str(exc)), 502 if exc.retryable else 400)
        except SessionConflict as exc:
            return message_page("Trade in progress", str(exc), 409)
        except TooManyUsers as exc:
            return message_page("Bot is full", str(exc), 503)
        resp = redirect("/", code=302)
        resp.set_cookie(COOKIE, sid, httponly=True, secure=settings.cookie_secure, samesite="Lax",
                        max_age=int(expires_in) + 900, path="/")
        return resp

    @app.post("/api/logout")
    def logout():
        check_csrf()
        sessions.remove(request.cookies.get(COOKIE))
        resp = jsonify(ok=True)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---------------------------------------------------------------- API
    @app.get("/api/state")
    def state():
        bot = current_bot()
        if bot is None:
            return jsonify(authenticated=False, configured=not settings.problems(),
                           market=hub.snapshot(), allow_real=settings.allow_real)
        return jsonify(bot.snapshot())

    def reply(result):
        ok, message = result
        return jsonify(ok=ok, message=message), (200 if ok else 400)

    @app.post("/api/ack")
    def ack():
        check_csrf()
        bot = require_bot()
        if body().get("accept") is not True:
            return reply((False, "Please tick the box to accept the risk notice."))
        bot.acknowledge()
        return reply((True, "Thanks."))

    @app.post("/api/config")
    def config():
        check_csrf()
        data = body()
        return reply(require_bot().configure(data.get("stake"), data.get("frequency"), data.get("risk_level")))

    @app.post("/api/mode")
    def mode():
        check_csrf()
        data = body()
        return reply(require_bot().set_mode(data.get("mode"), str(data.get("confirm") or "")))

    @app.post("/api/bot")
    def bot_action():
        check_csrf()
        data = body()
        bot = require_bot()
        action = data.get("action")
        if action == "start":
            return reply(bot.start_bot(confirm_real=data.get("confirm_real") is True))
        if action == "stop":
            return reply(bot.stop_bot())
        return reply((False, "Unknown action."))

    @app.post("/api/reconnect")
    def reconnect():
        check_csrf()
        ok = require_bot().reconnect()
        return reply((ok, "Reconnecting..." if ok else "Already connected or connecting."))

    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    for problem in settings.problems():
        print("SETUP PROBLEM:", problem, file=sys.stderr)
    app = create_app(settings)
    app.extensions["bot"]["hub"].start()
    try:
        from waitress import serve
    except ImportError:
        print("waitress is not installed. Run: python -m pip install -r webapp/requirements-web.txt",
              file=sys.stderr)
        return 1
    print(f"5G Trading Bot listening on http://{settings.host}:{settings.port} "
          f"(public address: {settings.base_url or 'NOT SET'})")
    serve(app, host=settings.host, port=settings.port, threads=16, ident="5g")
    return 0


if __name__ == "__main__":
    sys.exit(main())
