from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import smc_ict
from candle_utils import CANDLE_MINUTES
from data_source import data_source_label, get_crypto_liquidity_snapshot, get_data_source_for_user, get_smt_candles
from db import get_db
from models import LastSignal, SignalLog, User
from news import get_calendar

router = APIRouter(prefix="/api", tags=["trading"])
news_calendar = get_calendar()
logger = logging.getLogger("trading")

CANDLE_FETCH_COUNT = 800


def _log(db: Session, user_id: str, kind: str, detail: dict) -> None:
    db.add(SignalLog(user_id=user_id, kind=kind, detail=detail))
    db.commit()


def _news_status(user: User) -> tuple[bool, str]:
    s = user.settings
    if not s.news_enabled:
        return True, "News filter disabled"
    currencies = [c.strip() for c in s.news_currencies.split(",") if c.strip()]
    return news_calendar.blackout_status(
        buffer_before_min=s.news_buffer_before_min,
        buffer_after_min=s.news_buffer_after_min,
        min_impact=s.news_min_impact,
        currencies=currencies,
        fail_open=s.news_fail_open,
    )


@router.get("/signal")
def get_signal(
    # Set by the dashboard's "Reset checklist" button (see smc_ict.py's
    # evaluate_ict docstring for checklist_reset_after) — every LTF
    # structure trigger at or before this timestamp is ignored, so a
    # confluence checklist stuck on one still-active trigger all day
    # goes back to "waiting for a new BOS/CHoCH" until a genuinely fresh
    # one forms. Separate from entry-zone selection, which is already
    # automatic (see ICTResult.callouts) and needs no manual reset.
    checklist_reset_after: int | None = None,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    s = user.settings
    if s is None:
        raise HTTPException(status_code=400, detail="Settings not initialized for this account")

    data_source = get_data_source_for_user(user)
    try:
        candles = data_source.get_candles(s.symbol, s.candle_type, count=CANDLE_FETCH_COUNT)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch market data: {e}")

    news_ok, news_note = _news_status(user)

    smt_candles = None
    smt_primary_candles = None
    if s.require_smt_divergence:
        # SMT is read on its own configured timeframe (independent of
        # candle_type, the fast entry timeframe) — both this instrument's
        # own candles and the correlated instrument's candles need to be
        # fetched at that same timeframe so their swing points are
        # actually comparable (see smc_ict.find_smt_divergences).
        smt_candles = get_smt_candles(data_source, s.smt_symbol, s.smt_timeframe, CANDLE_FETCH_COUNT)
        try:
            smt_primary_candles = data_source.get_candles(s.symbol, s.smt_timeframe, count=CANDLE_FETCH_COUNT)
        except Exception as e:
            # Best-effort, same as get_smt_candles() — SMT just degrades
            # to "unavailable" rather than failing the whole signal.
            logger.warning("SMT primary-timeframe candle fetch failed for %s @ %s: %s", s.symbol, s.smt_timeframe, e)

    # Real, live order-book depth for the liquidity heat map — NAS100
    # itself has no public order book (a market-maker CFD, not an
    # exchange product), so this borrows a genuinely real one for an
    # instrument that actually tracks NAS100 (MEXC's NAS100_USDT
    # perpetual futures contract, free/public) and evaluate_ict reshapes
    # its liquidity distribution onto NAS100's own price range.
    # Best-effort: a fetch hiccup just drops the heat map back to its
    # ICT-concept fallback, never fails the whole signal.
    crypto_liquidity = get_crypto_liquidity_snapshot()

    result = smc_ict.evaluate_ict(
        candles,
        htf_minutes=s.htf_minutes,
        swing_lookback=s.swing_lookback,
        sweep_lookback=s.sweep_lookback,
        risk_reward=s.risk_reward,
        require_kill_zone=s.require_kill_zone,
        require_htf_bias=s.require_htf_bias,
        require_structure_shift=s.require_structure_shift,
        require_liquidity_sweep=s.require_liquidity_sweep,
        require_entry_zone=s.require_entry_zone,
        require_smt_divergence=s.require_smt_divergence,
        smt_raw=smt_candles,
        smt_primary_raw=smt_primary_candles,
        news_required=s.news_enabled,
        news_ok=news_ok,
        news_note=news_note,
        crypto_liquidity_raw=crypto_liquidity,
        checklist_reset_after=checklist_reset_after,
    )

    signal_id = str(uuid.uuid4())
    payload = {
        "id": signal_id,
        "symbol": s.symbol,
        "candle_type": s.candle_type,
        "signal": result.signal,
        "price": result.price,
        "htf_bias": result.htf_bias,
        "entry_zone": result.entry_zone,
        "entry_zone_kind": result.entry_zone_kind,
        "entry_zone_met": result.entry_zone_met,
        "stop_loss": result.stop_loss,
        "target": result.target,
        "kill_zone": result.kill_zone,
        "zones": result.zones,
        "heatmap": result.heatmap,
        "callouts": result.callouts,
        "reason": result.reason,
        "confluences": [{"name": c.name, "met": c.met, "detail": c.detail} for c in result.confluences],
        # A separate, independent top/bottom-tick reversal signal (see
        # ReversalResult/find_reversal in smc_ict.py) — can fire against
        # htf_bias, since that's the whole point of catching a reversal.
        # Always present (never null), same "no null-check needed"
        # convention as the rest of this payload.
        "reversal": {
            "signal": result.reversal.signal,
            "direction": result.reversal.direction,
            "swept_level": result.reversal.swept_level,
            "swept_price": result.reversal.swept_price,
            "entry_zone": result.reversal.entry_zone,
            "entry_zone_kind": result.reversal.entry_zone_kind,
            "entry_zone_met": result.reversal.entry_zone_met,
            "stop_loss": result.reversal.stop_loss,
            "target": result.reversal.target,
            "reason": result.reversal.reason,
            "confluences": [{"name": c.name, "met": c.met, "detail": c.detail} for c in result.reversal.confluences],
            "callouts": result.reversal.callouts,
        },
        "quantity": s.default_quantity,
        "generated_at": int(time.time()),
        "data_source": data_source_label(data_source),
        "candles": candles[-150:],
    }

    existing = db.get(LastSignal, user.id)
    if existing:
        existing.signal_id = signal_id
        existing.payload = payload
    else:
        db.add(LastSignal(user_id=user.id, signal_id=signal_id, payload=payload))
    db.commit()

    _log(db, user.id, "signal", {k: v for k, v in payload.items() if k not in ("candles", "confluences")})
    return payload


@router.get("/candles")
def get_candles(
    candle_type: str = "5m", count: int = 200, user: User = Depends(auth.get_current_user)
):
    """Chart-only candle feed, decoupled from the strategy's own
    candle_type setting — lets the dashboard chart show any timeframe
    the user picks without touching signal generation."""
    if candle_type not in CANDLE_MINUTES:
        raise HTTPException(status_code=400, detail=f"Unknown candle_type. Valid: {sorted(CANDLE_MINUTES)}")
    count = max(50, min(count, 1500))

    s = user.settings
    symbol = s.symbol if s else "NAS100"
    data_source = get_data_source_for_user(user)
    try:
        candles = data_source.get_candles(symbol, candle_type, count=count)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch market data: {e}")
    return {
        "symbol": symbol,
        "candle_type": candle_type,
        "candles": candles,
        "data_source": data_source_label(data_source),
    }


def _normalize_quote(raw: dict) -> dict:
    """Every data source (OANDA, Yahoo, the synthetic simulator) always
    returns a clean {"price", "time"} shape — this just guards against
    a future data source returning something else, failing loudly
    rather than ever guessing a price, since a wrong number silently
    shown as "live price" on a trading dashboard is worse than an
    error."""
    if isinstance(raw, dict) and "price" in raw and "time" in raw:
        return raw
    raise ValueError(f"Unrecognized quote response shape: {str(raw)[:300]}")


@router.get("/quote")
def get_quote(user: User = Depends(auth.get_current_user)):
    """Lightweight live-price poll for animating the currently-forming
    candle between bar closes — deliberately separate from /api/candles
    so the frontend can hit this every couple of seconds without
    re-fetching/re-resampling the whole candle history each time."""
    s = user.settings
    symbol = s.symbol if s else "NAS100"
    data_source = get_data_source_for_user(user)
    try:
        raw = data_source.get_quote(symbol)
        normalized = _normalize_quote(raw)
        normalized["data_source"] = data_source_label(data_source)
        return normalized
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch quote: {e}")


@router.get("/news")
def get_news(user: User = Depends(auth.get_current_user)):
    # All high (red) and medium (orange) impact events for today (in
    # America/New_York), across every currency — this is a general
    # awareness table, not the currency-scoped trading blackout filter
    # (that logic lives separately in _news_status()/blackout_status()).
    try:
        events = news_calendar.events_for_day(min_impact="Medium", currencies=None)
        return [{"title": e.title, "country": e.country, "impact": e.impact, "time": e.time.isoformat()} for e in events]
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
