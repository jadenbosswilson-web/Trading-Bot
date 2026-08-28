"""
Shared candle data type + helpers used by both the legacy VWAP/EMA
strategy (strategy.py) and the SMC/ICT engine (smc_ict.py).
"""
from __future__ import annotations

from dataclasses import dataclass

# candle_type string -> minutes, used for resampling lower timeframe (LTF)
# candles up into a higher timeframe (HTF) for structure/bias.
CANDLE_MINUTES = {
    "m": 1, "5m": 5, "15m": 15, "30m": 30,
    "h": 60, "2h": 120, "4h": 240,
    "d": 1440, "w": 10080, "mo": 43200,
}


@dataclass
class Candle:
    time: int  # epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


def normalize_candles(raw: list[dict]) -> list[Candle]:
    candles = [
        Candle(
            time=int(c["time"]),
            open=float(c["open"]),
            high=float(c["high"]),
            low=float(c["low"]),
            close=float(c["close"]),
            volume=float(c.get("volume", 0)),
        )
        for c in raw
    ]
    candles.sort(key=lambda c: c.time)
    return candles


def resample(candles: list[Candle], target_minutes: int) -> list[Candle]:
    """Aggregate a chronological list of candles into coarser bars.

    Used to derive a higher-timeframe (HTF) series for structure/bias
    from the same lower-timeframe (LTF) data used for entries, so the
    two are always perfectly in sync (no separate API call needed).
    """
    if not candles:
        return []
    bucket_ms = target_minutes * 60_000
    buckets: dict[int, list[Candle]] = {}
    for c in candles:
        key = (c.time // bucket_ms) * bucket_ms
        buckets.setdefault(key, []).append(c)

    out = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append(Candle(
            time=key,
            open=group[0].open,
            high=max(g.high for g in group),
            low=min(g.low for g in group),
            close=group[-1].close,
            volume=sum(g.volume for g in group),
        ))
    return out
