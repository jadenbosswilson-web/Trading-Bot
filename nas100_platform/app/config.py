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
CREDENTIAL_ENCRYPTION_KEY = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "")
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "168"))
COOKIE_SECURE = _bool("COOKIE_SECURE", False)

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

if not CREDENTIAL_ENCRYPTION_KEY:
    if _IS_TEST:
        from cryptography.fernet import Fernet
        CREDENTIAL_ENCRYPTION_KEY = Fernet.generate_key().decode()
    else:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            "and put it in your .env (see .env.example). This key encrypts every "
            "user's stored broker credentials — back it up somewhere safe."
        )
