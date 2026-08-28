"""
Per-user data source resolution. This is a signal-only dashboard with
no broker connection and no order placement anywhere — every account
just reads the same shared, real market data (OANDA if configured,
else the Yahoo Finance proxy, else a clearly-labeled synthetic
simulator if neither is reachable).
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from datetime import datetime, timezone

import httpx

import config
from candle_utils import CANDLE_MINUTES, normalize_candles, resample
from data_import import fetch_yahoo_history
from models import User

logger = logging.getLogger("data_source")

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


class YahooDataSource:
    """Real market data with zero setup required — sourced from Yahoo
    Finance's free, unauthenticated Nasdaq-100 E-mini futures feed
    (NQ=F) as a proxy for NAS100: genuinely real prices (not fake),
    typically with a short delay, close to but not identical to a
    broker's CFD quote (different instrument — futures vs CFD — so
    expect a small, normal basis difference).

    Unofficial API — Yahoo can rate-limit or change shape without
    notice (see data_import.py). If it's unreachable, calls raise and
    the dashboard surfaces a clear error rather than silently
    substituting fake data."""

    # Direct Yahoo interval support; anything else (2h/4h/w/mo) is
    # derived by resampling the finest base interval that covers it.
    YAHOO_INTERVAL = {"m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "h": "60m", "d": "1d"}
    CACHE_TTL_SECONDS = 30  # Yahoo's own bars don't update faster than ~1min anyway

    def __init__(self, symbol: str = "NQ=F"):
        self.symbol = symbol
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def _fetch_raw(self, candle_type: str) -> list[dict]:
        yahoo_interval = self.YAHOO_INTERVAL.get(candle_type)
        if yahoo_interval:
            return fetch_yahoo_history(self.symbol, interval=yahoo_interval)
        # Coarser timeframe Yahoo doesn't serve directly (e.g. 4h) —
        # fetch hourly and resample up.
        base = normalize_candles(fetch_yahoo_history(self.symbol, interval="60m"))
        target_minutes = CANDLE_MINUTES.get(candle_type, 60)
        resampled = resample(base, target_minutes)
        return [
            {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in resampled
        ]

    def _cached_fetch(self, candle_type: str) -> list[dict]:
        now = time.time()
        cached = self._cache.get(candle_type)
        if cached and (now - cached[0]) < self.CACHE_TTL_SECONDS:
            return cached[1]
        data = self._fetch_raw(candle_type)
        self._cache[candle_type] = (now, data)
        return data

    def get_candles(self, symbol: str, candle_type: str = "5m", count: int = 200) -> list[dict]:
        return self._cached_fetch(candle_type)[-count:]

    def get_quote(self, symbol: str) -> dict:
        # Yahoo has no sub-minute tick feed for free — "live" here means
        # the latest available 1-minute bar's close, refreshed on the
        # same cache TTL as everything else.
        candles = self._cached_fetch("m")
        last = candles[-1]
        return {"price": last["close"], "time": last["time"]}

    def get_correlated_candles(self, yahoo_symbol: str, candle_type: str, count: int) -> list[dict]:
        """Fetch a *different* ticker than this instance's own symbol —
        used for SMT divergence, which needs a second, correlated
        instrument's candles alongside the primary one. Cached the same
        way as the primary symbol, keyed separately so it doesn't
        collide with it."""
        yahoo_interval = self.YAHOO_INTERVAL.get(candle_type)
        cache_key = f"smt:{yahoo_symbol}:{candle_type}"
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self.CACHE_TTL_SECONDS:
            return cached[1][-count:]
        if yahoo_interval:
            data = fetch_yahoo_history(yahoo_symbol, interval=yahoo_interval)
        else:
            base = normalize_candles(fetch_yahoo_history(yahoo_symbol, interval="60m"))
            target_minutes = CANDLE_MINUTES.get(candle_type, 60)
            resampled = resample(base, target_minutes)
            data = [
                {"time": c.time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                for c in resampled
            ]
        self._cache[cache_key] = (now, data)
        return data[-count:]


# ---------------------------------------------------------------------
# Crypto-exchange order-book snapshot, for the liquidity heat map only
# ---------------------------------------------------------------------
# NAS100 itself has no real public order book to read: it's a CFD quoted
# by a market maker (OANDA), not an exchange-traded product, so there's
# no genuine "resting liquidity at each price" data available for it
# from OANDA or any NAS100 CFD broker. MEXC lists a USDT-margined
# perpetual futures contract, NAS100_USDT, that tracks the Nasdaq-100
# index via its funding rate — a real, live, freely-reachable order book
# (public REST endpoint, no API key/account needed) for an instrument
# that is actually NAS100-tracking, not an unrelated asset. Its price
# still won't exactly match OANDA's own NAS100 quote (different venue,
# different funding-rate basis, thinner retail-driven liquidity), so
# smc_ict.py still anchors/rescales the book's shape onto OANDA's
# current price — but the correction needed is small, since both are
# quoting roughly the same underlying index rather than two unrelated
# markets.
_crypto_liquidity_client = httpx.Client(timeout=8.0)
_crypto_liquidity_cache: tuple[float, dict | None] | None = None
CRYPTO_LIQUIDITY_CACHE_TTL_SECONDS = 2.0


def get_crypto_liquidity_snapshot(symbol: str = "NAS100_USDT", depth: int = 100) -> dict | None:
    """Real, live order-book depth from MEXC's free public futures REST
    API, for the NAS100_USDT contract (a NAS100-tracking perpetual, not
    an unrelated crypto pair). Cached process-wide (same public data for
    every user of this dashboard) with a short TTL so the heat map still
    feels live without hammering the upstream endpoint on every single
    1s signal poll from every user. Never raises — returns None on any
    failure (network, rate limit, unexpected response shape), including
    caching that failure briefly, so a heat map hiccup degrades
    gracefully instead of breaking signal generation.

    Response shape confirmed live against MEXC's public endpoint:
    GET https://contract.mexc.com/api/v1/contract/depth/{symbol}
    -> {"success": true, "data": {"asks": [[price, qty, order_count], ...],
                                    "bids": [[price, qty, order_count], ...]}}
    (prices/quantities are already numeric, not strings, unlike Binance's
    response shape — no float() coercion needed, but kept defensively.)"""
    global _crypto_liquidity_cache
    now = time.time()
    if _crypto_liquidity_cache is not None and (now - _crypto_liquidity_cache[0]) < CRYPTO_LIQUIDITY_CACHE_TTL_SECONDS:
        return _crypto_liquidity_cache[1]
    try:
        resp = _crypto_liquidity_client.get(
            f"https://contract.mexc.com/api/v1/contract/depth/{symbol}",
            params={"limit": depth},
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise ValueError(f"MEXC depth response reported failure: {payload}")
        data = payload.get("data") or {}
        # Only price and quantity matter to the heat map — MEXC's third
        # element (resting order count at that level) isn't used.
        bids = [(float(level[0]), float(level[1])) for level in data.get("bids", [])]
        asks = [(float(level[0]), float(level[1])) for level in data.get("asks", [])]
        if not bids or not asks:
            raise ValueError("empty bids/asks in MEXC depth response")
        snapshot = {"bids": bids, "asks": asks, "symbol": symbol}
        _crypto_liquidity_cache = (now, snapshot)
        return snapshot
    except Exception as e:
        logger.warning("Crypto liquidity snapshot fetch failed, heat map will fall back: %s", e)
        _crypto_liquidity_cache = (now, None)
        return None


def _parse_oanda_time(value: str) -> int:
    """OANDA timestamps look like '2016-10-17T15:16:40.000000000Z' —
    RFC3339 with nanosecond fractional seconds. Python's
    datetime.fromisoformat only accepts up to 6 fractional digits
    (microseconds), so the fraction is truncated rather than parsed
    losslessly — fine here since we only need millisecond precision
    for chart timestamps."""
    value = value.rstrip("Z")
    if "." in value:
        base, frac = value.split(".", 1)
        value = f"{base}.{frac[:6]}"
    dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class OandaDataSource:
    """Real-time market data via OANDA's free v20 REST API — no paid
    subscription, no CME data license, just a free OANDA practice
    (demo) account. Practice accounts see genuine live market prices;
    this class never places any OANDA order, and is used purely as a
    read-only price feed.

    Confirmed against OANDA's official v20 API reference
    (developer.oanda.com/rest-live-v20/instrument-ep and pricing-ep):
      - GET /v3/instruments/{instrument}/candles?price=M&granularity=X&count=N
        -> {"candles": [{"complete", "volume", "time", "mid": {"o","h","l","c"}}, ...]}
        This endpoint needs only the token, not an account id.
      - GET /v3/accounts/{accountID}/pricing?instruments=X
        -> {"prices": [{"bids": [{"price"}], "asks": [{"price"}], "time", ...}]}
        This one does need an account id, fetched once via GET /v3/accounts
        and cached (a token has at least one associated account).

    Deliberately uses simple per-request REST polling rather than
    OANDA's persistent streaming endpoint (stream-fxpractice.oanda.com):
    this dashboard already polls /api/quote every few seconds, which
    bounds the effective staleness to about the same as a stream
    connection would, without the added complexity of managing a
    long-lived background connection with reconnect logic.

    The exact instrument code for the Nasdaq 100 CFD is NOT verified
    against your specific account (OANDA's naming, e.g. "NAS100_USD",
    is documented convention but instrument availability can vary by
    account region/type) — if candles/quotes fail with a 400/404
    mentioning the instrument, check GET /v3/accounts/{id}/instruments
    for the exact code your account has and set OANDA_INSTRUMENT."""

    GRANULARITY = {
        "m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
        "h": "H1", "2h": "H2", "4h": "H4", "d": "D", "w": "W", "mo": "M",
    }

    def __init__(self, api_token: str, instrument: str = "NAS100_USD", practice: bool = True):
        self.instrument = instrument
        self.base_url = "https://api-fxpractice.oanda.com" if practice else "https://api-fxtrade.oanda.com"
        self._client = httpx.Client(
            timeout=15.0,
            headers={"Authorization": f"Bearer {api_token}", "Accept-Datetime-Format": "RFC3339"},
        )
        self._account_id: str | None = None
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self.CACHE_TTL_SECONDS = 5  # short TTL: this is meant to be near-real-time

    def _get_account_id(self) -> str:
        if self._account_id is None:
            resp = self._client.get(f"{self.base_url}/v3/accounts")
            resp.raise_for_status()
            accounts = resp.json().get("accounts", [])
            if not accounts:
                raise RuntimeError("OANDA token has no associated accounts")
            self._account_id = accounts[0]["id"]
        return self._account_id

    def _fetch_candles(self, candle_type: str, count: int) -> list[dict]:
        granularity = self.GRANULARITY.get(candle_type, "M5")
        resp = self._client.get(
            f"{self.base_url}/v3/instruments/{self.instrument}/candles",
            params={"price": "M", "granularity": granularity, "count": min(max(count, 1), 5000)},
        )
        resp.raise_for_status()
        data = resp.json()
        out = []
        for c in data.get("candles", []):
            if not c.get("mid"):
                continue
            mid = c["mid"]
            out.append({
                "time": _parse_oanda_time(c["time"]),
                "open": float(mid["o"]), "high": float(mid["h"]),
                "low": float(mid["l"]), "close": float(mid["c"]),
                "volume": float(c.get("volume", 0)),
            })
        out.sort(key=lambda c: c["time"])
        return out

    def get_candles(self, symbol: str, candle_type: str = "5m", count: int = 200) -> list[dict]:
        cache_key = candle_type
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self.CACHE_TTL_SECONDS:
            data = cached[1]
        else:
            data = self._fetch_candles(candle_type, max(count, 200))
            self._cache[cache_key] = (now, data)
        return data[-count:]

    def get_correlated_candles(self, instrument: str, candle_type: str, count: int) -> list[dict]:
        """Fetch a *different* OANDA instrument than this instance's own
        (self.instrument) — used for SMT divergence. Cached separately
        from the primary symbol's cache so the two don't collide."""
        granularity = self.GRANULARITY.get(candle_type, "M5")
        cache_key = f"smt:{instrument}:{candle_type}"
        now = time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self.CACHE_TTL_SECONDS:
            return cached[1][-count:]
        resp = self._client.get(
            f"{self.base_url}/v3/instruments/{instrument}/candles",
            params={"price": "M", "granularity": granularity, "count": min(max(count, 1), 5000)},
        )
        resp.raise_for_status()
        data = resp.json()
        out = []
        for c in data.get("candles", []):
            if not c.get("mid"):
                continue
            mid = c["mid"]
            out.append({
                "time": _parse_oanda_time(c["time"]),
                "open": float(mid["o"]), "high": float(mid["h"]),
                "low": float(mid["l"]), "close": float(mid["c"]),
                "volume": float(c.get("volume", 0)),
            })
        out.sort(key=lambda c: c["time"])
        self._cache[cache_key] = (now, out)
        return out[-count:]

    def get_quote(self, symbol: str) -> dict:
        account_id = self._get_account_id()
        resp = self._client.get(
            f"{self.base_url}/v3/accounts/{account_id}/pricing",
            params={"instruments": self.instrument},
        )
        resp.raise_for_status()
        prices = resp.json().get("prices", [])
        if not prices:
            raise RuntimeError(f"OANDA returned no pricing data for {self.instrument}")
        p = prices[0]
        bid = float(p["bids"][0]["price"]) if p.get("bids") else None
        ask = float(p["asks"][0]["price"]) if p.get("asks") else None
        if bid is not None and ask is not None:
            price = (bid + ask) / 2
        elif bid is not None or ask is not None:
            price = bid if bid is not None else ask
        else:
            raise RuntimeError(f"OANDA pricing response for {self.instrument} has no bid/ask")
        return {"price": round(price, 2), "time": _parse_oanda_time(p["time"])}


# In-process caches so instances stay alive (and their internal fetch
# caches warm) across requests within one server process, instead of
# reconstructing every call. Not durable across restarts/multiple
# instances — acceptable since both are read-through caches over an
# external or synthetic feed, not sources of truth.
_dry_run_cache: dict[str, DryRunDataSource] = {}
# Yahoo and OANDA data are the same for every user (public/shared
# market data) and this app only ever trades one instrument, so a
# single shared instance of each serves everyone instead of each
# user/caller hitting the upstream API independently.
_yahoo_source: YahooDataSource | None = None
_oanda_source: OandaDataSource | None = None
# Cooldown timestamp (epoch seconds), not a sticky forever-flag: a single
# transient OANDA failure (a timeout, a momentary rate limit, a brief
# upstream hiccup) used to permanently pin every user to the Yahoo/dry-run
# fallback for the rest of the server process's life, since the old
# boolean flag was never reset — only a full redeploy brought OANDA back.
# This still avoids hammering OANDA with a reachability probe on every
# single request while it's down, but automatically retries after a
# short cooldown instead of giving up on it forever.
_oanda_unreachable_until = 0.0
_OANDA_RETRY_COOLDOWN_SECONDS = 30


def get_shared_yahoo_source() -> YahooDataSource:
    global _yahoo_source
    if _yahoo_source is None:
        _yahoo_source = YahooDataSource(symbol="NQ=F")
    return _yahoo_source


def get_shared_oanda_source() -> OandaDataSource | None:
    """Returns a shared OandaDataSource if OANDA_API_TOKEN is
    configured, else None. Does NOT verify reachability — callers that
    need that should probe with a cheap get_candles() call, same
    pattern as get_data_source_for_user() already does for Yahoo."""
    global _oanda_source
    if not config.OANDA_API_TOKEN:
        return None
    if _oanda_source is None:
        _oanda_source = OandaDataSource(
            api_token=config.OANDA_API_TOKEN,
            instrument=config.OANDA_INSTRUMENT,
            practice=config.OANDA_PRACTICE,
        )
    return _oanda_source


def get_shared_fallback_source():
    """Best real (non-synthetic) data source actually reachable right
    now — OANDA first (genuinely real-time), Yahoo second (real but
    delayed). Both branches are probed with a cheap get_candles() call
    before being returned, so a caller's try/except can reliably catch
    "nothing is reachable" and fall back to the synthetic simulator —
    returning an unprobed source here would silently defer that failure
    to whatever the caller does with it next, which previously meant a
    fully offline Yahoo fallback wasn't actually detected as down."""
    global _oanda_unreachable_until
    oanda = get_shared_oanda_source()
    if oanda is not None and time.time() >= _oanda_unreachable_until:
        try:
            oanda.get_candles(config.OANDA_INSTRUMENT, "5m", count=5)
            _oanda_unreachable_until = 0.0  # confirmed reachable again — clear any prior cooldown
            return oanda
        except Exception as e:
            logger.warning(
                "OANDA unreachable, falling back to Yahoo for %ss: %s",
                _OANDA_RETRY_COOLDOWN_SECONDS, e,
            )
            _oanda_unreachable_until = time.time() + _OANDA_RETRY_COOLDOWN_SECONDS
    yahoo = get_shared_yahoo_source()
    yahoo.get_candles(yahoo.symbol, "5m", count=5)  # reachability probe — let this raise if Yahoo is also down
    return yahoo


def _dry_run_fallback_for(user: User, symbol: str) -> DryRunDataSource:
    cache_key = f"{user.id}:{symbol}"
    if cache_key not in _dry_run_cache:
        seed = int(hashlib.sha256(user.id.encode()).hexdigest()[:8], 16)
        _dry_run_cache[cache_key] = DryRunDataSource(symbol, seed)
    return _dry_run_cache[cache_key]


def is_simulated_source(data_source) -> bool:
    """True only when price data itself is fabricated (the synthetic
    random-walk fallback, used only if neither OANDA nor Yahoo is
    reachable) — false for OandaDataSource/YahooDataSource (real market
    data). Used to show a clear "this data isn't real" banner on the
    dashboard instead of leaving it ambiguous why the chart looks off."""
    return isinstance(data_source, DryRunDataSource)


def data_source_label(data_source) -> str:
    """One of 'simulated' | 'yahoo_proxy' | 'oanda' — lets the frontend
    show the right explanation for why the data looks the way it does,
    rather than a single real/fake boolean that can't distinguish
    "fake data" from "real data, just delayed"."""
    if isinstance(data_source, DryRunDataSource):
        return "simulated"
    if isinstance(data_source, OandaDataSource):
        return "oanda"
    if isinstance(data_source, YahooDataSource):
        return "yahoo_proxy"
    return "unknown"


# OANDA instrument code -> Yahoo Finance ticker, for the handful of
# common SMT correlation pairs, so the SMT feature still has *something*
# to compare against on the Yahoo fallback path (which only understands
# Yahoo tickers, not OANDA's instrument codes).
_SMT_OANDA_TO_YAHOO = {
    "SPX500_USD": "ES=F",
    "US30_USD": "YM=F",
    "NAS100_USD": "NQ=F",
    "DE30_EUR": "^GDAXI",
}


def get_smt_candles(data_source, smt_symbol: str, candle_type: str, count: int) -> list[dict] | None:
    """Best-effort fetch of a second, correlated instrument's candles for
    SMT divergence — `smt_symbol` is always an OANDA-style instrument
    code (e.g. "SPX500_USD"), since that's what the Settings UI collects,
    translated to a Yahoo ticker when running on the Yahoo fallback.
    Returns None (never raises) if correlated data isn't available for
    any reason — the synthetic simulator has no real correlated
    instrument to offer, and a fetch failure here should degrade the SMT
    confluence to "unavailable" rather than break signal generation."""
    try:
        if isinstance(data_source, OandaDataSource):
            return data_source.get_correlated_candles(smt_symbol, candle_type, count)
        if isinstance(data_source, YahooDataSource):
            yahoo_symbol = _SMT_OANDA_TO_YAHOO.get(smt_symbol, "ES=F")
            return data_source.get_correlated_candles(yahoo_symbol, candle_type, count)
    except Exception as e:
        logger.warning("SMT correlated candle fetch failed for %s: %s", smt_symbol, e)
    return None


def get_data_source_for_user(user: User):
    """No broker connection exists in this app — every user gets the
    same shared, real market data: OANDA if configured (genuinely
    real-time), else Yahoo's delayed NQ futures proxy, else a clearly
    labeled synthetic simulator if neither is reachable right now."""
    settings = user.settings
    symbol = settings.symbol if settings else "NAS100"
    try:
        return get_shared_fallback_source()
    except Exception:
        # Nothing reachable right now — fall back to the
        # clearly-labeled synthetic simulator rather than erroring the
        # whole dashboard out.
        return _dry_run_fallback_for(user, symbol)
