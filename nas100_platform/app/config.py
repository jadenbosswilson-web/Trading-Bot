"""
Server-level configuration (as opposed to per-user settings, which live
in the database — see models.py). Loaded from environment variables.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or "sqlite:///./app.db"
JWT_SECRET = os.getenv("JWT_SECRET", "")
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "168"))
COOKIE_SECURE = _bool("COOKIE_SECURE", False)

# OANDA v20 REST API — a free, no-broker-connection real-time market
# data source (see data_source.OandaDataSource), shared by every user.
# Optional: if unset, the app falls back to the Yahoo Finance proxy,
# then to the synthetic simulator — see get_data_source_for_user().
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "").strip()
# True = practice/demo environment (api-fxpractice.oanda.com) — free,
# real live prices, fake money. False = live trading environment. This
# app only ever reads prices through this token; it never places
# OANDA orders, so practice vs live only affects which price feed you
# see, not any real trading risk.
OANDA_PRACTICE = _bool("OANDA_PRACTICE", True)
OANDA_INSTRUMENT = os.getenv("OANDA_INSTRUMENT", "NAS100_USD").strip()

_IS_TEST = "pytest" in sys.modules

if not JWT_SECRET:
    if _IS_TEST:
        JWT_SECRET = "test-secret-not-for-production"
    else:
        raise RuntimeError(
            "JWT_SECRET is not set. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            "and put it in your .env (see .env.example)."
        )
