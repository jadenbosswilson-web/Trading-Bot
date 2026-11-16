"""
Database models. Every table that holds per-user data is scoped by
user_id with a foreign key — there is no code path anywhere in this
app that queries settings, signals, or trade logs without filtering by
the authenticated user's own id (see auth.py's current_user dependency
and how routers use it).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    accepted_tos_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    settings: Mapped["UserSettings"] = relationship(back_populates="user", uselist=False, cascade="all, delete-orphan")


class UserSettings(Base):
    """Per-user strategy configuration — the multi-tenant equivalent of
    the single-user app's .env file."""
    __tablename__ = "user_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), unique=True, nullable=False)
    user: Mapped["User"] = relationship(back_populates="settings")

    symbol: Mapped[str] = mapped_column(String, default="NAS100")
    candle_type: Mapped[str] = mapped_column(String, default="5m")
    default_quantity: Mapped[float] = mapped_column(Float, default=1.0)

    # Vestigial — no longer read or written anywhere in the app (the
    # broker/dry-run feature was removed). Kept mapped, with a default,
    # purely so INSERTs still populate it: this app has no migration
    # tooling (Base.metadata.create_all() only adds missing tables, it
    # never alters existing ones), and any database that was running
    # before the broker removal still has this column as NOT NULL.
    # Dropping it from the model without a real migration made new-row
    # inserts fail against that old schema. Safe to actually drop this
    # column (and the orphaned broker_credentials table) via a manual
    # `ALTER TABLE` once you're comfortable doing that against
    # production, but it isn't required — leaving it here is harmless.
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)

    htf_minutes: Mapped[int] = mapped_column(Integer, default=15)
    swing_lookback: Mapped[int] = mapped_column(Integer, default=3)
    sweep_lookback: Mapped[int] = mapped_column(Integer, default=20)
    risk_reward: Mapped[float] = mapped_column(Float, default=2.0)
    require_kill_zone: Mapped[bool] = mapped_column(Boolean, default=True)

    # Per-confluence toggles — every confluence the strategy checks can
    # be individually turned off if a user doesn't want it gating their
    # signals. HTF bias / LTF structure shift are structurally load-
    # bearing (evaluate_ict() can't determine a direction or an entry
    # trigger without them, so it stays FLAT regardless of these two
    # toggles) — they're still exposed here for a consistent settings UI
    # and so they can be hidden from the checklist display.
    require_htf_bias: Mapped[bool] = mapped_column(Boolean, default=True)
    require_structure_shift: Mapped[bool] = mapped_column(Boolean, default=True)
    require_liquidity_sweep: Mapped[bool] = mapped_column(Boolean, default=True)
    require_entry_zone: Mapped[bool] = mapped_column(Boolean, default=True)
    # SMT divergence is a brand-new confluence — defaults to off so it
    # doesn't silently change existing accounts' signal behavior the
    # moment this ships; turn it on in Settings to require it.
    require_smt_divergence: Mapped[bool] = mapped_column(Boolean, default=False)
    smt_symbol: Mapped[str] = mapped_column(String, default="SPX500_USD")
    # Timeframe SMT's swing-point comparison is read on — independent of
    # `candle_type` (the main entry timeframe). SMT is a slower, more
    # structural read in ICT terms; forcing it onto a fast scalp
    # timeframe produces noisy, low-conviction swings on both
    # instruments. Defaults to 15m rather than mirroring candle_type.
    smt_timeframe: Mapped[str] = mapped_column(String, default="15m")

    news_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    news_min_impact: Mapped[str] = mapped_column(String, default="High")
    news_currencies: Mapped[str] = mapped_column(String, default="USD")  # comma-separated
    news_buffer_before_min: Mapped[int] = mapped_column(Integer, default=15)
    news_buffer_after_min: Mapped[int] = mapped_column(Integer, default=15)
    news_fail_open: Mapped[bool] = mapped_column(Boolean, default=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class LastSignal(Base):
    """One row per user — the current signal awaiting a possible
    confirm. Stored in the DB (not an in-process dict) so the
    confirm-guard works correctly even if the app is scaled to more
    than one server instance behind a load balancer."""
    __tablename__ = "last_signals"

    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), primary_key=True)
    signal_id: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SignalLog(Base):
    """Append-only audit trail per user — the DB equivalent of the
    single-user app's signal_log.csv."""
    __tablename__ = "signal_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # "signal" | "order_confirmed" | "order_error"
    detail: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class BacktestJob(Base):
    """Backtests run in a background thread rather than blocking a web
    worker (a scalping-strategy backtest over a few thousand candles can
    take up to ~30s — unacceptable to hold a request open for under
    concurrent multi-user load)."""
    __tablename__ = "backtest_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | running | completed | failed
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
