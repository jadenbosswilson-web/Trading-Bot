"""Pydantic request/response models, grouped by feature area."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------- auth
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10)
    accept_tos: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10)


class UserOut(BaseModel):
    id: str
    email: str


# ------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    symbol: str = "NAS100"
    candle_type: str = "5m"
    default_quantity: float = 1.0
    htf_minutes: int = 15
    swing_lookback: int = 3
    sweep_lookback: int = 20
    risk_reward: float = 2.0
    require_kill_zone: bool = True
    require_htf_bias: bool = True
    require_structure_shift: bool = True
    require_liquidity_sweep: bool = True
    require_entry_zone: bool = True
    require_smt_divergence: bool = False
    smt_symbol: str = "SPX500_USD"
    news_enabled: bool = True
    news_min_impact: str = "High"
    news_currencies: str = "USD"
    news_buffer_before_min: int = 15
    news_buffer_after_min: int = 15
    news_fail_open: bool = False


class SettingsOut(SettingsIn):
    pass


# ------------------------------------------------------------ backtest
class BacktestRequest(BaseModel):
    source: str  # "csv" | "yahoo" | "dry_run"
    yahoo_symbol: str = "NQ=F"
    yahoo_interval: str = "5m"
    yahoo_range: str = ""
    lc_symbol: str = ""
    lc_candle_type: str = ""
    htf_minutes: int | None = None
    swing_lookback: int | None = None
    sweep_lookback: int | None = None
    risk_reward: float | None = None
    require_kill_zone: bool | None = None
    window: int = 500
    max_bars_in_trade: int = 200
