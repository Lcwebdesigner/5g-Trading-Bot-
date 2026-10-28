"""All server settings come from environment variables (see .env.example). No secrets live in source code."""
import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _float(env, name, default):
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError):
        return float(default)
    return value if value == value and value > 0 else float(default)


def _int(env, name, default):
    try:
        return max(1, int(env.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


class Settings:
    SCOPE = "trade"                       # least privilege: no payments, no account management
    HARD_MAX_TRADES = 10                  # your rule: never more than 10 trades a day
    HARD_MAX_LOSSES = 3                   # your rule: stop after 3 losses in a row
    MIN_STAKE = 0.35
    # Risk level -> (max trades per day, stop after N consecutive losses). Level 5 is the hard ceiling.
    RISK_TABLE = {1: (2, 1), 2: (4, 2), 3: (6, 2), 4: (8, 3), 5: (10, 3)}

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.app_id = (env.get("DERIV_APP_ID") or "").strip()
        self.base_url = (env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
        self.data_dir = Path(env.get("BOT_DATA_DIR") or (ROOT / "data"))
        try:
            self.tz = ZoneInfo(env.get("BOT_TZ") or "Africa/Nairobi")
        except Exception:
            self.tz = ZoneInfo("UTC")
        self.host = env.get("BOT_HOST") or "0.0.0.0"
self.port = _int(env, "PORT", _int(env, "BOT_PORT", 10000))
        self.max_users = _int(env, "MAX_USERS", 20)
        self.allow_real = (env.get("ALLOW_REAL", "1").strip() not in ("0", "false", "no"))
        self.real_max_stake = _float(env, "REAL_MAX_STAKE", 5.00)
        self.real_max_daily_loss = _float(env, "REAL_MAX_DAILY_LOSS", 10.00)
        self.demo_max_stake = _float(env, "DEMO_MAX_STAKE", 100.00)
        self.symbol = "R_100"

    @property
    def redirect_uri(self):
        return self.base_url + "/callback"

    @property
    def cookie_secure(self):
        return self.base_url.startswith("https://")

    def problems(self):
        """Human-readable configuration problems; an empty list means the server can offer sign-in."""
        out = []
        if not self.app_id or "PASTE" in self.app_id.upper():
            out.append("DERIV_APP_ID is not set (the App ID of your OAuth app from developers.deriv.com).")
        parsed = urlparse(self.base_url)
        if not self.base_url or parsed.scheme not in ("http", "https") or not parsed.hostname:
            out.append("PUBLIC_BASE_URL is not set (for example https://bot.yourdomain.com).")
        elif parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            out.append("PUBLIC_BASE_URL must start with https:// (Deriv only accepts https redirect addresses).")
        return out

    def limits(self, mode, risk_level):
        """(max_trades, max_consecutive_losses, stake_cap, daily_loss_stop) for a mode and risk level."""
        trades, losses = self.RISK_TABLE.get(int(risk_level), self.RISK_TABLE[2])
        trades, losses = min(trades, self.HARD_MAX_TRADES), min(losses, self.HARD_MAX_LOSSES)
        if mode == "real":
            return trades, losses, self.real_max_stake, self.real_max_daily_loss
        return trades, losses, self.demo_max_stake, None
