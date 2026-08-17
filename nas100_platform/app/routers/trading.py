from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import smc_ict
from candle_utils import CANDLE_MINUTES
from data_source import get_data_source_for_user, is_simulated_source
from db import get_db
from models import LastSignal, SignalLog, User
from news import get_calendar
from schemas import ConfirmRequest

router = APIRouter(prefix="/api", tags=["trading"])
news_calendar = get_calendar()

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
def get_signal(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    s = user.settings
    if s is None:
        raise HTTPException(status_code=400, detail="Settings not initialized for this account")

    data_source = get_data_source_for_user(user)
    try:
        candles = data_source.get_candles(s.symbol, s.candle_type, count=CANDLE_FETCH_COUNT)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch market data: {e}")

    news_ok, news_note = _news_status(user)

    result = smc_ict.evaluate_ict(
        candles,
        htf_minutes=s.htf_minutes,
        swing_lookback=s.swing_lookback,
        sweep_lookback=s.sweep_lookback,
        risk_reward=s.risk_reward,
        require_kill_zone=s.require_kill_zone,
        news_ok=news_ok,
        news_note=news_note,
    )

    signal_id = str(uuid.uuid4())
    payload = {
        "id": signal_id,
        "symbol": s.symbol,
        "signal": result.signal,
        "price": result.price,
        "htf_bias": result.htf_bias,
        "entry_zone": result.entry_zone,
        "entry_zone_kind": result.entry_zone_kind,
        "stop_loss": result.stop_loss,
        "target": result.target,
        "kill_zone": result.kill_zone,
        "reason": result.reason,
        "confluences": [{"name": c.name, "met": c.met, "detail": c.detail} for c in result.confluences],
        "quantity": s.default_quantity,
        "generated_at": int(time.time()),
        "dry_run": s.dry_run,
        "simulated_data": is_simulated_source(data_source),
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
        "simulated_data": is_simulated_source(data_source),
    }


def _normalize_quote(raw: dict) -> dict:
    """DryRunDataSource / SimulatedExecutionDataSource-over-dry-run
    already return a clean {"price", "time"} shape. A real, live
    LiquidChartsClient.get_quote() returns Liquid Charts' raw dxfeed-style
    marketdata payload, whose exact field names are NOT confirmed against
    a real account yet (see liquidcharts_client.py's module docstring) —
    so this tries the plausible shapes and fails loudly rather than ever
    guessing a price, since a wrong number silently shown as "live price"
    on a trading dashboard is worse than an error."""
    if "price" in raw and "time" in raw:
        return raw
    events = raw.get("events") if isinstance(raw, dict) else None
    quotes = (events or {}).get("Quote") if isinstance(events, dict) else None
    if quotes:
        row = quotes[0]
        if isinstance(row, dict):
            price = row.get("bidPrice") or row.get("askPrice") or row.get("price")
            if price is not None:
                return {"price": price, "time": row.get("time") or int(time.time() * 1000)}
    raise ValueError(
        "Unrecognized quote response shape from Liquid Charts — field names haven't been "
        "confirmed against a real account yet. Raw response: " + str(raw)[:300]
    )


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
        normalized["simulated_data"] = is_simulated_source(data_source)
        return normalized
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch quote: {e}")


@router.get("/positions")
def get_positions(user: User = Depends(auth.get_current_user)):
    data_source = get_data_source_for_user(user)
    try:
        return data_source.get_positions()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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


@router.post("/confirm")
def confirm_trade(body: ConfirmRequest, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    """Explicit, user-initiated order placement — only reachable by a
    direct click from THIS user's own dashboard, scoped to THIS user's
    own last-computed signal and THIS user's own broker credentials.
    There is no code path that places an order without this call, and
    no code path that lets one user's request act on another user's
    account or signal."""
    last = db.get(LastSignal, user.id)
    if last is None or last.signal_id != body.signal_id:
        raise HTTPException(status_code=409, detail="Signal is stale or unknown — refresh and try again.")
    if body.side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")

    s = user.settings
    quantity = body.quantity or s.default_quantity
    data_source = get_data_source_for_user(user)
    try:
        result = data_source.place_order(instrument=s.symbol, side=body.side, quantity=quantity, order_type="MARKET")
    except Exception as e:
        _log(db, user.id, "order_error", {"signal_id": body.signal_id, "error": str(e)})
        raise HTTPException(status_code=502, detail=str(e))

    _log(db, user.id, "order_confirmed", {"signal_id": body.signal_id, "side": body.side, "quantity": quantity, "result": result})
    return result
