"""
Per-user data source resolution — the multi-tenant equivalent of the
single-user app's data_source.get_data_source(). Each user gets their
own DryRunDataSource instance (kept alive for their session via a small
in-process cache) or a real LiquidChartsClient built from their own
decrypted credentials. Nothing here is shared across users.
"""
from __future__ import annotations

import hashlib
import math
import random
import time

from candle_utils import CANDLE_MINUTES, normalize_candles, resample
import crypto
from liquidcharts_client import LiquidChartsClient
from models import User

# Base resolution the synthetic random walk is generated at. Every
# requested timeframe (5m, 1h, 1d, ...) is derived by resampling this
# 1-minute series — previously each DryRunDataSource was locked to a
# single fixed bar interval decided at construction time, so switching
# the chart's timeframe fetched a *relabeled* copy of the same 5-minute
# data instead of actually different bars.
BASE_BAR_MS = 60_000
INITIAL_BASE_BARS = 30_000  # ~20.8 days of 1-minute history to backfill on first load
MAX_BASE_BARS = 60_000  # trim ceiling so a long-running server process doesn't grow unbounded


class DryRunDataSource:
    """Synthetic random-walk generator, seeded per-user so different
    accounts see different (but each internally consistent) simulated
    price action. Generates at 1-minute resolution and resamples up to
    whatever timeframe is actually requested, and stays paced to real
    wall-clock time (a new base bar only appears once a real minute has
    actually elapsed) so the countdown-to-next-candle on the dashboard
    means something."""

    def __init__(self, symbol: str, seed: int, start_price: float = 19500.0):
        self.symbol = symbol
        self._rng = random.Random(seed)
        self._price = start_price
        self._candles = self._generate(INITIAL_BASE_BARS)

    def _generate(self, n: int) -> list[dict]:
        candles = []
        price = self._price
        now_minute = (int(time.time() * 1000) // BASE_BAR_MS) * BASE_BAR_MS
        t = now_minute - n * BASE_BAR_MS
        for i in range(n):
            drift = math.sin(i / 40) * 0.8 + math.sin(i / 11) * 0.3
            noise = self._rng.uniform(-3, 3)
            open_ = price
            close = max(1.0, open_ + drift + noise)
            high = max(open_, close) + self._rng.uniform(0, 2.5)
            low = min(open_, close) - self._rng.uniform(0, 2.5)
            volume = self._rng.uniform(80, 400)
            candles.append({
                "time": t, "open": round(open_, 2), "high": round(high, 2),
                "low": round(low, 2), "close": round(close, 2), "volume": round(volume, 2),
            })
            price = close
            t += BASE_BAR_MS
        self._price = price
        return candles

    def _append_base_bar(self, t: int) -> None:
        i = len(self._candles)
        last_close = self._candles[-1]["close"]
        drift = math.sin(i / 40) * 0.8 + math.sin(i / 11) * 0.3
        noise = self._rng.uniform(-3, 3)
        open_ = last_close
        close = max(1.0, open_ + drift + noise)
        high = max(open_, close) + self._rng.uniform(0, 2.5)
        low = min(open_, close) - self._rng.uniform(0, 2.5)
        self._candles.append({
            "time": t, "open": round(open_, 2), "high": round(high, 2),
            "low": round(low, 2), "close": round(close, 2), "volume": round(self._rng.uniform(80, 400), 2),
        })

    def _catch_up(self) -> None:
        """Advance the base series so its last bar matches the current
        real minute — exactly one new synthetic bar per real minute
        elapsed, not one per request, so timing stays realistic."""
        now_minute = (int(time.time() * 1000) // BASE_BAR_MS) * BASE_BAR_MS
        last_time = self._candles[-1]["time"]
        while last_time + BASE_BAR_MS <= now_minute:
            last_time += BASE_BAR_MS
            self._append_base_bar(last_time)
        if len(self._candles) > MAX_BASE_BARS:
            self._candles = self._candles[-MAX_BASE_BARS:]

    def get_candles(self, symbol: str, candle_type: str = "5m", count: int = 200) -> list[dict]:
        self._catch_up()
        target_minutes = CANDLE_MINUTES.get(candle_type, 5)
        base = normalize_candles(self._candles)
        resampled = base if target_minutes <= 1 else resample(base, target_minutes)
        out = [
            {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in resampled
        ]
        # If the synthetic history isn't deep enough for very coarse
        # timeframes (e.g. asking for thousands of daily bars), this
        # simply returns fewer than `count` rather than fabricating
        # years of fake data — this feed is documented as a demo, not a
        # real historical source.
        return out[-count:]

    def get_quote(self, symbol: str) -> dict:
        """Sub-bar tick for live polling: perturbs the *current forming*
        base bar in place without creating a new one, so the chart's
        last candle visibly moves between bar closes."""
        self._catch_up()
        last = self._candles[-1]
        jitter = self._rng.uniform(-1.5, 1.5)
        price = round(max(1.0, last["close"] + jitter), 2)
        last["close"] = price
        last["high"] = max(last["high"], price)
        last["low"] = min(last["low"], price)
        return {"price": price, "time": int(time.time() * 1000)}

    def get_positions(self) -> list[dict]:
        return []

    def place_order(self, **kwargs) -> dict:
        return {
            "orderCode": kwargs.get("client_order_id") or "dry-run-order",
            "status": "SIMULATED_FILLED",
            "note": "Dry-run mode — no real order was sent.",
            **kwargs,
        }


class SimulatedExecutionDataSource:
    """Wraps a real LiquidChartsClient so market data (candles, quotes,
    positions) comes from the user's actual broker account — real prices,
    not a synthetic random walk — while order placement is still fully
    simulated. This is what a user with saved, dry-run credentials gets:
    accurate charts/signals, but zero real orders until they explicitly
    flip dry_run off in Settings.

    Dry-run mode is ONLY about not sending real orders — it was never
    meant to fake the price feed too, and tying the two together made
    every dry-run account see fabricated prices even with valid,
    verified broker credentials on file."""

    def __init__(self, client: LiquidChartsClient):
        self._client = client

    def get_candles(self, symbol: str, candle_type: str = "5m", count: int = 200) -> list[dict]:
        return self._client.get_candles(symbol, candle_type, count=count)

    def get_quote(self, symbol: str) -> dict:
        return self._client.get_quote(symbol)

    def get_positions(self) -> list[dict]:
        # Informational only — showing the account's real open positions
        # in dry-run is safe (nothing is being acted on). If the account
        # can't be reached, degrade to an empty list rather than break
        # the whole dashboard.
        try:
            return self._client.get_positions()
        except Exception:
            return []

    def place_order(self, **kwargs) -> dict:
        return {
            "orderCode": kwargs.get("client_order_id") or "dry-run-order",
            "status": "SIMULATED_FILLED",
            "note": "Dry-run mode — no real order was sent (price data is live from your broker account).",
            **kwargs,
        }


# Small in-process cache so a user's dry-run price walk stays continuous
# across requests within one server process, instead of resetting every
# call. Not durable across restarts/multiple instances — acceptable for
# a simulated feed (see README for the multi-instance caveat). Only used
# as a last-resort fallback now, for accounts with no broker credentials
# saved at all.
_dry_run_cache: dict[str, DryRunDataSource] = {}


def is_simulated_source(data_source) -> bool:
    """True only when price data itself is synthetic (no broker
    credentials saved at all) — false for SimulatedExecutionDataSource,
    since that one sources real prices and only simulates order
    placement. Used to show a clear "this data isn't real" banner on the
    dashboard instead of leaving it ambiguous why the chart looks off."""
    return isinstance(data_source, DryRunDataSource)


def get_data_source_for_user(user: User):
    settings = user.settings
    symbol = settings.symbol if settings else "NAS100"

    if user.broker_credential is None:
        # No broker credentials saved yet — nothing real to source data
        # from, so fall back to the synthetic simulator. One instance per
        # user+symbol now (not per-timeframe) since it serves any
        # requested timeframe by resampling its own 1-minute base series.
        cache_key = f"{user.id}:{symbol}"
        if cache_key not in _dry_run_cache:
            seed = int(hashlib.sha256(user.id.encode()).hexdigest()[:8], 16)
            _dry_run_cache[cache_key] = DryRunDataSource(symbol, seed)
        return _dry_run_cache[cache_key]

    cred = user.broker_credential
    client = LiquidChartsClient(
        username=crypto.decrypt(cred.username_enc),
        password=crypto.decrypt(cred.password_enc),
        domain=crypto.decrypt(cred.domain_enc),
        account_code=crypto.decrypt(cred.account_code_enc),
    )

    if settings is None or settings.dry_run:
        # Real credentials on file, but still in dry-run: use them for
        # real market data, simulate order placement only.
        return SimulatedExecutionDataSource(client)

    # Fully live: real data, real orders (still only ever placed on an
    # explicit user click — see routers/trading.py confirm_trade()).
    return client
