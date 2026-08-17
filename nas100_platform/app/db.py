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

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args)
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
