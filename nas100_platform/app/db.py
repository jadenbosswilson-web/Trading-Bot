"""
Database engine + session setup.

Uses Postgres in production (DATABASE_URL), falls back to a local
SQLite file for development. Tables are created automatically on
startup via create_all() — there's no migration framework wired up yet
(see README "Known gaps"). For a real production deployment with
evolving schema, add Alembic before this has real user data you can't
afford to lose.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

import config

# Railway, Heroku, and some other platforms hand out connection strings
# starting with "postgres://" — SQLAlchemy 1.4+ only accepts
# "postgresql://" for the same database. Normalize it here so a raw
# platform-provided DATABASE_URL doesn't crash the app on startup.
_db_url = config.DATABASE_URL
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    import models  # noqa: F401  (ensure models are registered on Base before create_all)
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
