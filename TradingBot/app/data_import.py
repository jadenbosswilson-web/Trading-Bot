"""
Historical candle sources for backtesting real market data.

Three options, in order of how much you should trust them:

1. `load_csv()` — the most reliable path. Export historical NAS100 (or
   whatever you're trading) candles from Liquid Charts, TradingView, or
   any other source as a CSV and point the app at it. No credentials,
   no third-party API reliability risk.
2. `fetch_liquidcharts_history()` — pulls real broker history straight
   from your Liquid Charts account via the same REST client used for
   live trading. Most accurate to what you'd actually have traded, but
   requires confirmed API access (see README).
3. `fetch_yahoo_history()` — a free, no-key "quick start" option using
   Yahoo Finance's Nasdaq-100 E-mini futures data (NQ=F) as a proxy for
   NAS100. This is UNOFFICIAL and Yahoo has been known to rate-limit or
   change behavior without notice — treat it as a convenience for
   trying the backtester out, not a reliable long-term data source.
   Futures prices also won't exactly match your broker's CFD quotes
   (different overnight funding, small basis differences), so treat
   results from this source as directional, not precise.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("data_import")


def _parse_time(value: str) -> int:
    """Best-effort parse of a time column into epoch milliseconds."""
    value = value.strip()
    if value.isdigit():
        n = int(value)
        # heuristic: treat 13-digit as ms, 10-digit as seconds
        return n if len(value) >= 13 else n * 1000
    # try common ISO-ish formats
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    # last resort: fromisoformat handles most remaining ISO variants
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


COLUMN_ALIASES = {
    "time": {"time", "date", "datetime", "timestamp"},
    "open": {"open", "o"},
    "high": {"high", "h"},
    "low": {"low", "l"},
    "close": {"close", "c", "adj close", "adj_close"},
    "volume": {"volume", "vol", "v"},
}


def load_csv(file_content: str) -> list[dict]:
    """Parse a CSV string into normalized candle dicts. Header row
    required; column names are matched case-insensitively against
    common aliases (time/date, open, high, low, close, volume)."""
    reader = csv.DictReader(io.StringIO(file_content))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    colmap: dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for actual in reader.fieldnames:
            if actual.strip().lower() in aliases:
                colmap[field_name] = actual
                break

    missing = [f for f in ("time", "open", "high", "low", "close") if f not in colmap]
    if missing:
        raise ValueError(
            f"CSV is missing required column(s): {missing}. "
            f"Found columns: {reader.fieldnames}"
        )

    out = []
    for row in reader:
        try:
            out.append({
                "time": _parse_time(row[colmap["time"]]),
                "open": float(row[colmap["open"]]),
                "high": float(row[colmap["high"]]),
                "low": float(row[colmap["low"]]),
                "close": float(row[colmap["close"]]),
                "volume": float(row[colmap["volume"]]) if "volume" in colmap and row.get(colmap["volume"]) not in (None, "") else 0.0,
            })
        except (ValueError, KeyError) as e:
            logger.debug("Skipping malformed CSV row %s: %s", row, e)
    out.sort(key=lambda c: c["time"])
    if not out:
        raise ValueError("No valid rows parsed from CSV")
    return out


YAHOO_INTERVAL_MAX_RANGE = {
    "1m": "7d", "2m": "60d", "5m": "60d", "15m": "60d", "30m": "60d",
    "60m": "730d", "1h": "730d", "1d": "max",
}


def fetch_yahoo_history(symbol: str = "NQ=F", interval: str = "5m", range_: str | None = None) -> list[dict]:
    """Best-effort fetch from Yahoo Finance's unofficial chart API.
    See module docstring for caveats."""
    range_ = range_ or YAHOO_INTERVAL_MAX_RANGE.get(interval, "60d")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": interval, "range": range_}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; nas100-backtester/1.0)"}

    with httpx.Client(timeout=20.0, headers=headers) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    try:
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected Yahoo Finance response shape: {e}")

    out = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if None in (o, h, l, c):
            continue
        vol = (quote.get("volume") or [0] * len(timestamps))[i] or 0
        out.append({
            "time": ts * 1000,
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": float(vol),
        })
    if not out:
        raise RuntimeError("Yahoo Finance returned no usable candles for this symbol/interval/range")
    return out


def fetch_liquidcharts_history(
    client,
    symbol: str,
    candle_type: str,
    total_candles: int = 3000,
    page_size: int = 1000,
    max_requests: int = 10,
) -> list[dict]:
    """Page backward through LiquidChartsClient.get_candles() to build a
    longer history than a single request would return. Stops early if
    the API returns fewer candles than requested (start of history) or
    after max_requests pages, whichever comes first."""
    all_candles: dict[int, dict] = {}
    to_time_ms = int(time.time() * 1000)

    for _ in range(max_requests):
        if len(all_candles) >= total_candles:
            break
        batch = client.get_candles(symbol, candle_type, to_time_ms=to_time_ms, count=page_size)
        if not batch:
            break
        for c in batch:
            all_candles[int(c["time"])] = c
        earliest = min(int(c["time"]) for c in batch)
        if earliest >= to_time_ms:
            break  # not making progress
        to_time_ms = earliest - 1
        if len(batch) < page_size:
            break  # likely hit the start of available history

    out = sorted(all_candles.values(), key=lambda c: c["time"])
    return out[-total_candles:]
