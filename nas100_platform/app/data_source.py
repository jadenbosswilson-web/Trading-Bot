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

from candle_utils import CANDLE_MINUTES
import crypto
from liquidcharts_client import LiquidChartsClient
from models import User


class DryRunDataSource:
    """Same synthetic random-walk generator as the single-user app,
    seeded per-user so different accounts see different (but each
    internally consistent) simulated price action."""

    def __init__(self, symbol: str, candle_type: str, seed: int, start_price: float = 19500.0):
        self.symbol = symbol
        self._rng = random.Random(seed)
        self._price = start_price
        self._bar_ms = CANDLE_MINUTES.get(candle_type, 5) * 60_000
        self._candles = self._generate(1500)

    def _generate(self, n: int) -> list[dict]:
        candles = []
        price = self._price
        t = int(time.time() * 1000) - n * self._bar_ms
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
            t += self._bar_ms
        self._price = price
        return candles

    def get_candles(self, symbol: str, candle_type: str = "5m", count: int = 200) -> list[dict]:
        last = self._candles[-1]
        i = len(self._candles)
        drift = math.sin(i / 40) * 0.8 + math.sin(i / 11) * 0.3
        noise = self._rng.uniform(-3, 3)
        open_ = last["close"]
        close = max(1.0, open_ + drift + noise)
        high = max(open_, close) + self._rng.uniform(0, 2.5)
        low = min(open_, close) - self._rng.uniform(0, 2.5)
        new_candle = {
            "time": last["time"] + self._bar_ms, "open": round(open_, 2), "high": round(high, 2),
            "low": round(low, 2), "close": round(close, 2), "volume": round(self._rng.uniform(80, 400), 2),
        }
        self._candles.append(new_candle)
        self._candles = self._candles[-max(count, 1500):]
        return self._candles[-count:]

    def get_history(self, count: int) -> list[dict]:
        return self._generate(count)

    def get_positions(self) -> list[dict]:
        return []

    def place_order(self, **kwargs) -> dict:
        return {
            "orderCode": kwargs.get("client_order_id") or "dry-run-order",
            "status": "SIMULATED_FILLED",
            "note": "Dry-run mode — no real order was sent.",
            **kwargs,
        }


# Small in-process cache so a user's dry-run price walk stays continuous
# across requests within one server process, instead of resetting every
# call. Not durable across restarts/multiple instances — acceptable for
# a simulated feed (see README for the multi-instance caveat).
_dry_run_cache: dict[str, DryRunDataSource] = {}


def get_data_source_for_user(user: User):
    settings = user.settings
    symbol = settings.symbol if settings else "NAS100"
    candle_type = settings.candle_type if settings else "5m"

    if settings is None or settings.dry_run or user.broker_credential is None:
        cache_key = f"{user.id}:{symbol}:{candle_type}"
        if cache_key not in _dry_run_cache:
            seed = int(hashlib.sha256(user.id.encode()).hexdigest()[:8], 16)
            _dry_run_cache[cache_key] = DryRunDataSource(symbol, candle_type, seed)
        return _dry_run_cache[cache_key]

    cred = user.broker_credential
    return LiquidChartsClient(
        username=crypto.decrypt(cred.username_enc),
        password=crypto.decrypt(cred.password_enc),
        domain=crypto.decrypt(cred.domain_enc),
        account_code=crypto.decrypt(cred.account_code_enc),
    )
