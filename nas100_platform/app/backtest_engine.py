"""
Walk-forward backtester for the SMC/ICT engine (smc_ict.py).

Design goals, in order of importance:

1. No lookahead bias — at each simulated "now", the strategy only ever
   sees candles up to and including that bar, exactly like live trading.
   We do this by re-running `smc_ict.evaluate_ict()` on a trailing
   window of candles ending at each bar, the same way `main.py` does
   for the live dashboard.
2. Only one open trade at a time — while a simulated trade is open, we
   stop looking for new entries (matches the "confirm one setup, then
   wait" mentality of a scalper; also keeps the sim simple/deterministic).
3. Conservative fills — if a single bar's range touches both the stop
   and the target, the stop is assumed to fill first. No slippage or
   spread is modeled by default (see `spread_points`); real results will
   be somewhat worse than the backtest, especially around news.

This does NOT replay the news filter historically (see news.py — the
calendar feed only covers the current/next week), so `require_news`
here defaults to off; kill-zone timing IS replayed accurately using
each bar's own timestamp.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from candle_utils import Candle, normalize_candles
import smc_ict

Direction = str  # "BUY" | "SELL"


@dataclass
class Trade:
    direction: Direction
    entry_index: int
    entry_time: int  # epoch ms
    entry_price: float
    stop_loss: float
    target: float
    exit_index: int
    exit_time: int
    exit_price: float
    outcome: str  # "win" | "loss" | "timeout"
    r_multiple: float
    bars_held: int
    trigger_index: int | None = None


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    bars_evaluated: int = 0
    bars_total: int = 0
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _candle_dict(c: Candle) -> dict:
    return {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}


def run_backtest(
    raw_candles: list[dict],
    htf_minutes: int = 15,
    swing_lookback: int = 3,
    sweep_lookback: int = 20,
    risk_reward: float = 2.0,
    require_kill_zone: bool = True,
    window: int = 500,
    max_bars_in_trade: int = 200,
    spread_points: float = 0.0,
    max_bars: int = 6000,
) -> BacktestResult:
    candles = normalize_candles(raw_candles)
    warnings: list[str] = []

    if len(candles) > max_bars:
        warnings.append(
            f"Dataset trimmed to the most recent {max_bars} bars (had {len(candles)}) to keep runtime reasonable."
        )
        candles = candles[-max_bars:]

    min_bars = max(60, swing_lookback * 10)
    start = min_bars if window <= 0 else max(min_bars, min(window, len(candles) - 1))

    trades: list[Trade] = []
    i = start
    bars_evaluated = 0

    while i < len(candles):
        window_slice = candles[max(0, i - window + 1): i + 1] if window > 0 else candles[: i + 1]
        as_of = datetime.fromtimestamp(candles[i].time / 1000, tz=timezone.utc)

        result = smc_ict.evaluate_ict(
            [_candle_dict(c) for c in window_slice],
            htf_minutes=htf_minutes,
            swing_lookback=swing_lookback,
            sweep_lookback=sweep_lookback,
            risk_reward=risk_reward,
            require_kill_zone=require_kill_zone,
            news_ok=True,  # historical news blackout not replayed — see module docstring
            news_note="not replayed in backtest",
            as_of=as_of,
        )
        bars_evaluated += 1

        if result.signal in ("BUY", "SELL") and result.stop_loss and result.target:
            trade = _simulate_trade(candles, i, result, max_bars_in_trade, spread_points)
            trades.append(trade)
            i = trade.exit_index + 1
        else:
            i += 1

    stats = summarize(trades)
    return BacktestResult(
        trades=trades,
        bars_evaluated=bars_evaluated,
        bars_total=len(candles),
        stats=stats,
        warnings=warnings,
    )


def _simulate_trade(
    candles: list[Candle],
    entry_index: int,
    result: "smc_ict.ICTResult",
    max_bars_in_trade: int,
    spread_points: float,
) -> Trade:
    direction = result.signal
    entry_price = result.price + (spread_points if direction == "BUY" else -spread_points)
    stop = result.stop_loss
    target = result.target
    risk = abs(entry_price - stop) or 1e-9

    last_index = min(len(candles) - 1, entry_index + max_bars_in_trade)
    for j in range(entry_index + 1, last_index + 1):
        c = candles[j]
        if direction == "BUY":
            hit_stop = c.low <= stop
            hit_target = c.high >= target
        else:
            hit_stop = c.high >= stop
            hit_target = c.low <= target

        if hit_stop and hit_target:
            # ambiguous — conservative assumption: stop fills first
            return _close_trade(direction, entry_index, entry_price, stop, target, j, stop, "loss", risk, result.trigger_index, candles)
        if hit_stop:
            return _close_trade(direction, entry_index, entry_price, stop, target, j, stop, "loss", risk, result.trigger_index, candles)
        if hit_target:
            return _close_trade(direction, entry_index, entry_price, stop, target, j, target, "win", risk, result.trigger_index, candles)

    # neither hit within max_bars_in_trade — close at last available price ("timeout")
    exit_price = candles[last_index].close
    return _close_trade(direction, entry_index, entry_price, stop, target, last_index, exit_price, "timeout", risk, result.trigger_index, candles)


def _close_trade(direction, entry_index, entry_price, stop, target, exit_index, exit_price, outcome, risk, trigger_index, candles) -> Trade:
    raw_pnl = (exit_price - entry_price) if direction == "BUY" else (entry_price - exit_price)
    r_multiple = raw_pnl / risk
    return Trade(
        direction=direction,
        entry_index=entry_index,
        entry_time=candles[entry_index].time,
        entry_price=round(entry_price, 2),
        stop_loss=round(stop, 2),
        target=round(target, 2),
        exit_index=exit_index,
        exit_time=candles[exit_index].time,
        exit_price=round(exit_price, 2),
        outcome=outcome,
        r_multiple=round(r_multiple, 3),
        bars_held=exit_index - entry_index,
        trigger_index=trigger_index,
    )


def summarize(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "total_trades": 0,
            "win_rate_pct": 0, "wins": 0, "losses": 0, "timeouts": 0,
            "avg_win_r": 0, "avg_loss_r": 0, "profit_factor": 0,
            "expectancy_r": 0, "total_r": 0,
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "max_drawdown_r": 0, "equity_curve": [], "monthly": [],
        }

    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0 and t.outcome != "timeout"]
    timeouts = [t for t in trades if t.outcome == "timeout"]

    win_rate = len(wins) / n * 100
    avg_win_r = statistics.mean(t.r_multiple for t in wins) if wins else 0
    avg_loss_r = statistics.mean(t.r_multiple for t in losses) if losses else 0
    gross_profit = sum(t.r_multiple for t in trades if t.r_multiple > 0)
    gross_loss = abs(sum(t.r_multiple for t in trades if t.r_multiple < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0)
    expectancy_r = statistics.mean(t.r_multiple for t in trades)
    total_r = sum(t.r_multiple for t in trades)

    # equity curve + drawdown, in R
    equity = []
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        running += t.r_multiple
        equity.append(round(running, 3))
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    # consecutive streaks
    max_win_streak = cur_win_streak = 0
    max_loss_streak = cur_loss_streak = 0
    for t in trades:
        if t.r_multiple > 0:
            cur_win_streak += 1
            cur_loss_streak = 0
        else:
            cur_loss_streak += 1
            cur_win_streak = 0
        max_win_streak = max(max_win_streak, cur_win_streak)
        max_loss_streak = max(max_loss_streak, cur_loss_streak)

    # monthly consistency breakdown
    by_month: dict[str, list[Trade]] = {}
    for t in trades:
        key = datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).strftime("%Y-%m")
        by_month.setdefault(key, []).append(t)
    monthly = []
    for key in sorted(by_month):
        mt = by_month[key]
        mwins = [t for t in mt if t.r_multiple > 0]
        monthly.append({
            "month": key,
            "trades": len(mt),
            "win_rate_pct": round(len(mwins) / len(mt) * 100, 1),
            "net_r": round(sum(t.r_multiple for t in mt), 2),
        })
    monthly_net_rs = [m["net_r"] for m in monthly]
    consistency_stdev = round(statistics.pstdev(monthly_net_rs), 2) if len(monthly_net_rs) > 1 else 0

    return {
        "total_trades": n,
        "win_rate_pct": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "timeouts": len(timeouts),
        "avg_win_r": round(avg_win_r, 2),
        "avg_loss_r": round(avg_loss_r, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "expectancy_r": round(expectancy_r, 3),
        "total_r": round(total_r, 2),
        "max_consecutive_wins": max_win_streak,
        "max_consecutive_losses": max_loss_streak,
        "max_drawdown_r": round(max_dd, 2),
        "equity_curve": equity,
        "monthly": monthly,
        "monthly_consistency_stdev_r": consistency_stdev,
    }
