"""
Backtests run in a background thread, not inline in the request, so one
user kicking off a slow backtest can't tie up the web worker for
everyone else. The client starts a job (gets a job id back immediately)
and polls for the result.

This is intentionally simple (a Python thread + a DB row for status) —
no Celery/Redis. That's a fine v1 tradeoff for a single-server
deployment; the "Known gaps" section in the README covers when you'd
want a real task queue instead.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

import auth
import backtest_engine
import crypto
import data_import
from db import SessionLocal, get_db
from liquidcharts_client import LiquidChartsClient
from models import BacktestJob, User

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

MAX_CONCURRENT_JOBS_PER_USER = 1


@router.post("")
async def start_backtest(
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
    source: str = Form(...),
    csv_file: UploadFile | None = File(None),
    yahoo_symbol: str = Form("NQ=F"),
    yahoo_interval: str = Form("5m"),
    yahoo_range: str = Form(""),
    lc_symbol: str = Form(""),
    lc_candle_type: str = Form(""),
    lc_total_candles: int = Form(3000),
    htf_minutes: int | None = Form(None),
    swing_lookback: int | None = Form(None),
    sweep_lookback: int | None = Form(None),
    risk_reward: float | None = Form(None),
    require_kill_zone: bool | None = Form(None),
    window: int = Form(500),
    max_bars_in_trade: int = Form(200),
):
    running = (
        db.query(BacktestJob)
        .filter(BacktestJob.user_id == user.id, BacktestJob.status.in_(["pending", "running"]))
        .count()
    )
    if running >= MAX_CONCURRENT_JOBS_PER_USER:
        raise HTTPException(status_code=429, detail="You already have a backtest running — wait for it to finish first")

    csv_text = None
    csv_filename = None
    if source == "csv":
        if csv_file is None:
            raise HTTPException(status_code=400, detail="No CSV file uploaded")
        csv_text = (await csv_file.read()).decode("utf-8", errors="replace")
        csv_filename = csv_file.filename

    s = user.settings
    params = {
        "source": source,
        "csv_text": csv_text,
        "csv_filename": csv_filename,
        "yahoo_symbol": yahoo_symbol,
        "yahoo_interval": yahoo_interval,
        "yahoo_range": yahoo_range,
        "lc_symbol": lc_symbol or (s.symbol if s else "NAS100"),
        "lc_candle_type": lc_candle_type or (s.candle_type if s else "5m"),
        "lc_total_candles": lc_total_candles,
        "htf_minutes": htf_minutes if htf_minutes is not None else (s.htf_minutes if s else 15),
        "swing_lookback": swing_lookback if swing_lookback is not None else (s.swing_lookback if s else 3),
        "sweep_lookback": sweep_lookback if sweep_lookback is not None else (s.sweep_lookback if s else 20),
        "risk_reward": risk_reward if risk_reward is not None else (s.risk_reward if s else 2.0),
        "require_kill_zone": require_kill_zone if require_kill_zone is not None else (s.require_kill_zone if s else True),
        "window": window,
        "max_bars_in_trade": max_bars_in_trade,
    }

    job = BacktestJob(user_id=user.id, status="pending", params=params)
    db.add(job)
    db.commit()
    db.refresh(job)

    thread = threading.Thread(target=_run_job, args=(job.id, user.id), daemon=True)
    thread.start()

    return {"job_id": job.id, "status": job.status}


@router.get("/{job_id}")
def get_backtest_job(job_id: str, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    job = db.get(BacktestJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status_code=404, detail="Backtest job not found")
    return {
        "job_id": job.id,
        "status": job.status,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@router.get("")
def list_backtest_jobs(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    jobs = (
        db.query(BacktestJob)
        .filter(BacktestJob.user_id == user.id)
        .order_by(BacktestJob.created_at.desc())
        .limit(20)
        .all()
    )
    return [{"job_id": j.id, "status": j.status, "created_at": j.created_at.isoformat()} for j in jobs]


def _run_job(job_id: str, user_id: str) -> None:
    """Runs in a background thread with its own DB session — never
    reuse a request-scoped session across threads."""
    db = SessionLocal()
    try:
        job = db.get(BacktestJob, job_id)
        if job is None:
            return
        job.status = "running"
        db.commit()
        params = job.params

        try:
            candles, data_label = _load_candles(db, user_id, params)
            if len(candles) < 100:
                raise ValueError(f"Only got {len(candles)} candles — need at least 100 to backtest")

            result = backtest_engine.run_backtest(
                candles,
                htf_minutes=params["htf_minutes"],
                swing_lookback=params["swing_lookback"],
                sweep_lookback=params["sweep_lookback"],
                risk_reward=params["risk_reward"],
                require_kill_zone=params["require_kill_zone"],
                window=params["window"],
                max_bars_in_trade=params["max_bars_in_trade"],
            )
            job.result = {
                "data_label": data_label,
                "candle_count": len(candles),
                "date_range": {
                    "from": candles[0]["time"] if isinstance(candles[0], dict) else candles[0].time,
                    "to": candles[-1]["time"] if isinstance(candles[-1], dict) else candles[-1].time,
                },
                "bars_evaluated": result.bars_evaluated,
                "stats": result.stats,
                "warnings": result.warnings,
                "trades": [
                    {
                        "direction": t.direction, "entry_time": t.entry_time, "entry_price": t.entry_price,
                        "stop_loss": t.stop_loss, "target": t.target, "exit_time": t.exit_time,
                        "exit_price": t.exit_price, "outcome": t.outcome, "r_multiple": t.r_multiple,
                        "bars_held": t.bars_held,
                    }
                    for t in result.trades
                ],
            }
            job.status = "completed"
        except Exception as e:
            job.status = "failed"
            job.error = str(e)

        job.completed_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _load_candles(db: Session, user_id: str, params: dict) -> tuple[list[dict], str]:
    source = params["source"]

    if source == "csv":
        if not params.get("csv_text"):
            raise ValueError("No CSV content")
        candles = data_import.load_csv(params["csv_text"])
        return candles, f"CSV: {params.get('csv_filename') or 'uploaded file'}"

    if source == "yahoo":
        candles = data_import.fetch_yahoo_history(
            symbol=params["yahoo_symbol"], interval=params["yahoo_interval"], range_=params["yahoo_range"] or None
        )
        return candles, f"Yahoo Finance {params['yahoo_symbol']} ({params['yahoo_interval']})"

    if source == "liquidcharts":
        user = db.get(User, user_id)
        if user is None or user.broker_credential is None:
            raise ValueError("No Liquid Charts credentials saved — add them in Settings first")
        cred = user.broker_credential
        client = LiquidChartsClient(
            username=crypto.decrypt(cred.username_enc),
            password=crypto.decrypt(cred.password_enc),
            domain=crypto.decrypt(cred.domain_enc),
            account_code=crypto.decrypt(cred.account_code_enc),
        )
        candles = data_import.fetch_liquidcharts_history(
            client, symbol=params["lc_symbol"], candle_type=params["lc_candle_type"],
            total_candles=params["lc_total_candles"],
        )
        return candles, f"Liquid Charts live history: {params['lc_symbol']}"

    if source == "dry_run":
        from data_source import get_data_source_for_user
        user = db.get(User, user_id)
        ds = get_data_source_for_user(user)
        # get_candles() resamples from the synthetic 1-minute base series
        # to whatever timeframe was actually selected for the backtest —
        # previously this used a get_history() shortcut that ignored
        # lc_candle_type and silently returned whatever fixed interval
        # the data source happened to be constructed with.
        candles = ds.get_candles(params["lc_symbol"], params["lc_candle_type"], count=5000)
        return candles, "Synthetic demo data (NOT real market data)"

    raise ValueError(f"Unknown source '{source}'")
