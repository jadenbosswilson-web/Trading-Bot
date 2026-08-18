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

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

import config

logger = logging.getLogger("db")

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
    _sync_missing_columns()


def _default_sql_literal(column) -> str | None:
    """Best-effort translation of a mapped column's Python-side default
    into a SQL literal, for the ADD COLUMN ... DEFAULT clause below.
    Only handles the plain scalar defaults actually used in this app's
    models (bool/int/float/str) — anything fancier (callables like
    uuid/datetime defaults) is skipped, since those are always on
    primary-key/timestamp columns that existing rows don't need
    backfilled anyway."""
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _sync_missing_columns() -> None:
    """Additive-only schema patch, run once at startup: for every mapped
    column that doesn't yet exist on its live table, issue an ALTER
    TABLE ... ADD COLUMN for it.

    This app has no real migration framework (see the module docstring)
    and Base.metadata.create_all() only creates whole missing TABLES —
    it silently does nothing when a column is added to an *existing*
    table's model. That gap previously caused a real production outage
    (UserSettings.dry_run being removed from the model without a
    migration broke every settings INSERT against the already-deployed
    table, since the live schema still had it as NOT NULL). This closes
    that gap going forward for the common case (new column, has a
    plain default) — it deliberately does NOT handle renames, type
    changes, or drops, which still need a manual, reviewed migration."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # brand new table — create_all() already handled it
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'
                default_sql = _default_sql_literal(column)
                if default_sql is not None:
                    ddl += f" DEFAULT {default_sql}"
                logger.warning("Schema patch: adding missing column %s.%s", table.name, column.name)
                conn.execute(text(ddl))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
