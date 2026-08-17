from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import smc_ict
from data_source import get_data_source_for_user
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


@router.get("/positions")
def get_positions(user: User = Depends(auth.get_current_user)):
    data_source = get_data_source_for_user(user)
    try:
        return data_source.get_positions()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/news")
def get_news(user: User = Depends(auth.get_current_user)):
    s = user.settings
    currencies = [c.strip() for c in s.news_currencies.split(",") if c.strip()] if s and s.news_enabled else None
    try:
        events = news_calendar.upcoming(hours_ahead=48, min_impact="Medium", currencies=currencies)
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
