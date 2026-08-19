"""
NAS100 ICT Sniper — multi-tenant platform.

Run with (from inside this app/ directory):
    python -m uvicorn main:app --reload --port 8000

Signal-only: this app has no broker connection and no order placement
anywhere. Every account reads the same shared market data (OANDA if
configured, else a Yahoo Finance proxy, else a clearly-labeled
synthetic simulator) — see data_source.py and routers/trading.py.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from db import init_db
from rate_limit import limiter
from routers import auth as auth_router
from routers import backtest as backtest_router
from routers import settings as settings_router
from routers import trading as trading_router

app = FastAPI(title="NAS100 ICT Sniper")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR.parent / "static"


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.include_router(auth_router.router)
app.include_router(settings_router.router)
app.include_router(trading_router.router)
app.include_router(backtest_router.router)

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
