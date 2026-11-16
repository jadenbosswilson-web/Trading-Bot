"""
Smart Money Concepts / ICT-style confluence engine for sniper scalp entries.

This is a simplified, codified interpretation of commonly-taught ICT/SMC
ideas — not an official ICT product, and not a guarantee of edge. Treat
the confluence checklist as a structured way to see *why* the app does
or doesn't like a setup, and tune/backtest it before trusting it.

Concepts implemented
---------------------
- Market structure: swing highs/lows (fractals), Break of Structure (BOS)
  and Change of Character (CHoCH).
- Higher-timeframe (HTF) bias vs lower-timeframe (LTF) entry trigger —
  classic top-down ICT approach. The HTF series is derived by resampling
  the same LTF candles, so the two are always in sync.
- Liquidity sweeps: a wick through a recent swing high/low that closes
  back inside — the "Judas swing" / stop-hunt that often precedes a real
  move (the "Manipulation" leg of ICT's Accumulation-Manipulation-
  Distribution model).
- Order Blocks (OB): the last opposite-colour candle before the
  displacement move that caused the structural break.
- Breaker blocks: an order block that gets invalidated when its trend
  fails via a CHoCH — that same zone flips polarity and becomes
  support/resistance in the new direction instead.
- Buy-side / sell-side liquidity and grabs: resting liquidity above
  swing highs (buy-side) and below swing lows (sell-side), and every
  wick-through-and-reject "grab" of either across the whole history.
- Fair Value Gaps (FVG): 3-candle imbalance left behind by a displacement
  move, treated as a magnet / re-entry zone.
- Premium/Discount + Optimal Trade Entry (OTE): the 62%-79% Fibonacci
  retracement zone of the impulse leg — buy only from discount, sell
  only from premium.
- Kill zones: London / New York / London-close session windows, in
  America/New_York time (the convention ICT content is usually taught
  in), converted to the server's actual UTC time under the hood.
- SMT (Smart Money Technique) divergence: comparing this instrument's
  swing structure against a correlated instrument's (e.g. NAS100 vs
  ES/S&P 500) — one printing a new high/low the other doesn't confirm.

Every confluence above is individually toggleable (see the require_*
parameters on evaluate_ict()) — a disabled confluence is left out of
the returned checklist entirely and isn't required for a signal to
fire.

Nothing here places an order — this module only classifies candles and
returns a structured result for the caller (main.py / the dashboard) to
display and, if the user clicks confirm, act on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from candle_utils import Candle, normalize_candles, resample

logger = logging.getLogger("smc_ict")

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tzdata isn't installed
    logger.warning("zoneinfo/tzdata unavailable — kill zones will use fixed UTC-5, DST not accounted for")
    NY_TZ = None

Signal = Literal["BUY", "SELL", "FLAT"]
Direction = Literal["bullish", "bearish"]

# (name, start_hour, end_hour) in America/New_York local time, 24h clock.
# Ranges that cross midnight (Asian session) are handled specially.
KILL_ZONES = [
    ("Asian", 20, 24),
    ("Asian", 0, 0),       # placeholder handled via wraparound below
    ("London", 2, 5),
    ("New York AM", 7, 10),
    ("London Close", 10, 12),
]


@dataclass
class Confluence:
    name: str
    met: bool
    detail: str


@dataclass
class ReversalResult:
    """A *separate*, independent signal from the main trend-following one
    above — a top/bottom-tick reversal setup: a significant liquidity
    level (previous day/session high or low, or a strong swing point as
    a fallback) gets swept, then price immediately prints a CHoCH in the
    opposite direction. Deliberately can fire against the current
    htf_bias — that's the whole point of a reversal — so it's reported
    alongside the trend signal rather than folded into it. Entry-zone
    resolution reuses the exact same tested breaker/OB/FVG/OTE +
    recency-based callout machinery as the trend signal (see
    _zone_candidates_for_trigger/_merge_touch_events_to_callouts in
    evaluate_ict), just anchored to the reversal's own CHoCH trigger —
    same consistency and rigor, not a separate, weaker copy."""
    signal: Signal
    direction: Direction | None
    swept_level: str | None  # e.g. "Previous day high", "Previous London session low", "Recent swing high"
    swept_price: float | None
    entry_zone: tuple[float, float] | None
    entry_zone_kind: str | None
    stop_loss: float | None
    target: float | None
    confluences: list[Confluence] = field(default_factory=list)
    reason: str = ""
    entry_zone_met: bool = False
    callouts: list[dict] = field(default_factory=list)


@dataclass
class ICTResult:
    signal: Signal
    price: float
    htf_bias: Direction | None
    entry_zone: tuple[float, float] | None
    entry_zone_kind: str | None
    stop_loss: float | None
    target: float | None
    kill_zone: str | None
    confluences: list[Confluence] = field(default_factory=list)
    reason: str = ""
    # Index (into the LTF candle list passed in) of the structural event
    # this signal is based on. Used by the backtester to avoid re-firing
    # a "new" signal for a setup it has already traded.
    trigger_index: int | None = None
    # Structural price zones the strategy actually evaluated to reach
    # this signal — order block, unmitigated fair value gaps, the OTE
    # retracement band, the structure-break level, and the swept
    # liquidity level (when present). Each is
    # {type, direction, top, bottom, from_time, to_time, label, active}
    # with `from_time`/`to_time` as candle epoch-ms timestamps
    # (`to_time` null means "extends to the right edge") — the exact
    # numbers evaluate_ict() used, not an approximation, so a chart
    # overlay built from this is accurate to the decision rather than a
    # guess at what the strategy "probably" looked at.
    zones: list[dict] = field(default_factory=list)
    # True once price has tapped into a real order block / unmitigated
    # FVG / OTE zone (the "sticky" zone semantics — stays true until
    # invalidated), regardless of whether every other optional
    # confluence (kill zone, news, SMT) is also met. This is a weaker,
    # earlier condition than `signal in ("BUY", "SELL")`: stop_loss and
    # target are populated whenever this is true, so the frontend can
    # offer a "Confirm position" action off of this rather than waiting
    # for the fully-gated strict signal.
    entry_zone_met: bool = False
    # Price-bucketed liquidity density read (see build_liquidity_heatmap)
    # — built from real liquidity pools, confirmed grabs, and breaker
    # blocks (plus a light live-edge layer so it still reacts between
    # candle closes). Each entry is {top, bottom, intensity} with
    # intensity normalized to [0, 1] against the hottest bucket in this
    # same result. Buckets with zero weight are omitted, so this is typically
    # a small, sparse list rather than one entry per bucket.
    heatmap: list[dict] = field(default_factory=list)
    # Chronological feed of every real re-entry/continuation opportunity
    # the strategy found within the recency window — replaces the old
    # manual "reset entry target" button entirely. Built by merging every
    # distinct touch event (see _tap_events) across every trigger's
    # breaker/order block/live FVGs/OTE, then classifying each as
    # "entry" (the very first touch seen), "continuation" (a zone/trigger
    # that had never been touched before) or "re-entry" (a repeat touch
    # of a zone already seen once). Sorted oldest to newest; the LAST
    # entry in this list is always the one currently driving
    # entry_zone/stop_loss/target — so the frontend can just show this as
    # a log and highlight the newest row, no manual reset needed. Each
    # entry is {index, time, kind ("entry"|"continuation"|"re-entry"),
    # zone_kind, top, bottom, trigger_index}.
    callouts: list[dict] = field(default_factory=list)
    # A completely separate, independent top/bottom-tick reversal signal
    # — see ReversalResult's own docstring. Always populated (never
    # None) with at least signal="FLAT", so callers never need a null
    # check, same convention as this outer ICTResult itself.
    reversal: ReversalResult = field(default_factory=lambda: ReversalResult(
        signal="FLAT", direction=None, swept_level=None, swept_price=None,
        entry_zone=None, entry_zone_kind=None, stop_loss=None, target=None,
    ))


# ----------------------------------------------------------------------
# Swing points / market structure
# ----------------------------------------------------------------------
def find_swing_points(candles: list[Candle], lookback: int = 3) -> tuple[set[int], set[int]]:
    highs, lows = set(), set()
    n = len(candles)
    for i in range(lookback, n - lookback):
        window_high = candles[i].high
        window_low = candles[i].low
        if all(window_high > candles[i - j].high for j in range(1, lookback + 1)) and \
           all(window_high > candles[i + j].high for j in range(1, lookback + 1)):
            highs.add(i)
        if all(window_low < candles[i - j].low for j in range(1, lookback + 1)) and \
           all(window_low < candles[i + j].low for j in range(1, lookback + 1)):
            lows.add(i)
    return highs, lows


def find_liquidity_pools(candles: list[Candle], lookback: int = 3) -> tuple[set[int], set[int]]:
    """A looser cousin of find_swing_points(), used only for liquidity
    sweep detection — never for market structure (BOS/CHoCH) or order
    blocks, which still need find_swing_points()'s strict, fully-isolated
    fractal definition to stay reliable.

    Two relaxations here, both aimed at catching real, well-known ICT
    liquidity concepts that the strict version was silently missing:

    1. Inclusive comparison (>=/<=) instead of strict (>/<) — "equal
       highs" / "equal lows" (price tagging the same level two or more
       times) is itself a textbook liquidity pool; requiring every
       neighbor to be strictly lower/higher threw these out entirely.
    2. A one-bar-shorter confirmation window than the main structural
       lookback — a minor local high/low is still real, sweepable
       liquidity well before it would qualify as a full structural
       swing point, and traders react to it sooner than 3 full bars of
       confirmation on each side.
    """
    pool_lookback = max(1, lookback - 1)
    highs, lows = set(), set()
    n = len(candles)
    for i in range(pool_lookback, n - pool_lookback):
        window_high = candles[i].high
        window_low = candles[i].low
        if all(window_high >= candles[i - j].high for j in range(1, pool_lookback + 1)) and \
           all(window_high >= candles[i + j].high for j in range(1, pool_lookback + 1)):
            highs.add(i)
        if all(window_low <= candles[i - j].low for j in range(1, pool_lookback + 1)) and \
           all(window_low <= candles[i + j].low for j in range(1, pool_lookback + 1)):
            lows.add(i)
    return highs, lows


@dataclass
class StructureEvent:
    index: int
    type: Literal["BOS", "CHoCH"]
    direction: Direction
    level: float
    swept_index: int | None = None  # index of the swing point this break invalidated


def _walk_structure(
    candles: list[Candle], lookback: int
) -> tuple[list[StructureEvent], tuple[int, float] | None, tuple[int, float] | None, Direction | None]:
    """Shared walk-forward pass behind both detect_structure() (the
    confirmed BOS/CHoCH history) and find_pending_structure_levels() (the
    still-unbroken swing high/low sitting at the end of that same walk —
    i.e. exactly the level that would fire the *next* event). Keeping
    this as one function means the "what would happen if price broke
    this level" read can never silently drift out of sync with the
    actual BOS/CHoCH detection logic above it."""
    swing_highs, swing_lows = find_swing_points(candles, lookback)
    trend: Direction | None = None
    last_high: tuple[int, float] | None = None
    last_low: tuple[int, float] | None = None
    events: list[StructureEvent] = []

    for i, c in enumerate(candles):
        if i in swing_highs:
            last_high = (i, c.high)
        if i in swing_lows:
            last_low = (i, c.low)

        if last_high is not None and i > last_high[0] and c.close > last_high[1]:
            etype = "BOS" if trend == "bullish" else "CHoCH"
            events.append(StructureEvent(i, etype, "bullish", last_high[1], last_high[0]))
            trend = "bullish"
            last_high = None

        if last_low is not None and i > last_low[0] and c.close < last_low[1]:
            etype = "BOS" if trend == "bearish" else "CHoCH"
            events.append(StructureEvent(i, etype, "bearish", last_low[1], last_low[0]))
            trend = "bearish"
            last_low = None

    return events, last_high, last_low, trend


def detect_structure(candles: list[Candle], lookback: int = 3) -> list[StructureEvent]:
    """Walk forward through candles, tracking the most recent unbroken
    swing high/low, and flag BOS (continuation) or CHoCH (reversal) each
    time price closes through one."""
    events, _, _, _ = _walk_structure(candles, lookback)
    return events


def find_pending_structure_levels(
    candles: list[Candle], lookback: int = 3
) -> tuple[tuple[int, float] | None, tuple[int, float] | None, Direction | None]:
    """The current unbroken swing high and swing low — i.e. the exact
    levels that, if price closes beyond them, would fire the *next*
    structural event (a continuation BOS if it agrees with the current
    trend, a reversal CHoCH if it doesn't). Either can be None if no
    qualifying swing point exists yet on that side. `trend` is the
    current confirmed direction (None if no structure has confirmed at
    all yet), needed by the caller to label the pending break correctly
    as BOS vs CHoCH."""
    _, last_high, last_low, trend = _walk_structure(candles, lookback)
    return last_high, last_low, trend


# ----------------------------------------------------------------------
# Liquidity sweeps
# ----------------------------------------------------------------------
@dataclass
class SweepEvent:
    index: int
    direction: Direction  # 'bullish' = sellside liquidity swept (grab below a low) -> bullish reversal
    level: float
    side: Literal["buyside", "sellside"] = "sellside"  # which resting pool this grabbed


def find_liquidity_sweep(
    candles: list[Candle],
    swing_highs: set[int],
    swing_lows: set[int],
    before_index: int,
    lookback_candles: int = 20,
) -> SweepEvent | None:
    """Look for the most recent wick-through-and-reject of a swing
    high/low in the `lookback_candles` bars immediately before
    `before_index` (typically the index of a structural break)."""
    start = max(0, before_index - lookback_candles)
    recent_highs = [(i, candles[i].high) for i in swing_highs if start <= i < before_index]
    recent_lows = [(i, candles[i].low) for i in swing_lows if start <= i < before_index]

    best: SweepEvent | None = None
    for i in range(start, before_index):
        c = candles[i]
        for hi_idx, hi_price in recent_highs:
            if hi_idx < i and c.high > hi_price and c.close < hi_price:
                best = SweepEvent(i, "bearish", hi_price, "buyside")  # swept buyside liquidity -> favors shorts
        for lo_idx, lo_price in recent_lows:
            if lo_idx < i and c.low < lo_price and c.close > lo_price:
                best = SweepEvent(i, "bullish", lo_price, "sellside")  # swept sellside liquidity -> favors longs
    return best


def find_all_liquidity_grabs(
    candles: list[Candle], swing_highs: set[int], swing_lows: set[int]
) -> list[SweepEvent]:
    """Every wick-through-and-close-back-through of a swing high (a
    buy-side liquidity grab — buy-stops resting above the high get
    triggered, then price rejects back down) or swing low (a sell-side
    liquidity grab — sell-stops resting below the low get triggered,
    then price rejects back up), scanned across the *entire* candle
    history — not just whichever single grab happens to sit in the
    lookback window right before one particular structural trigger (see
    find_liquidity_sweep() for that narrower, trade-relevant search used
    to gate the actual signal).

    Each swing point is only ever counted once, at the first bar that
    genuinely sweeps it, so a level that keeps getting re-tested doesn't
    spam duplicate grabs. Swing points that round to the *same* price
    (equal highs/lows — several fractal points tagging the identical
    level, a real and common ICT pattern) are also only counted once per
    side, at whichever of them gets swept earliest — without this,
    every one of those equal-level points would independently generate
    its own near-identical grab event once the level finally breaks,
    stacking redundant, visually-overlapping markers on the chart for
    what a trader would recognize as a single pool. Same "call out every
    single one" treatment already given to SMT divergences and pending
    structure levels — the chart shows every *distinct* liquidity grab
    that has actually happened, not only the one gating the current
    setup."""
    events: list[SweepEvent] = []
    seen_hi_levels: set[float] = set()
    for hi_idx in sorted(swing_highs):
        hi_price = candles[hi_idx].high
        hi_key = round(hi_price, 2)
        if hi_key in seen_hi_levels:
            continue
        for i in range(hi_idx + 1, len(candles)):
            c = candles[i]
            if c.high > hi_price and c.close < hi_price:
                events.append(SweepEvent(i, "bearish", hi_price, "buyside"))
                seen_hi_levels.add(hi_key)
                break
    seen_lo_levels: set[float] = set()
    for lo_idx in sorted(swing_lows):
        lo_price = candles[lo_idx].low
        lo_key = round(lo_price, 2)
        if lo_key in seen_lo_levels:
            continue
        for i in range(lo_idx + 1, len(candles)):
            c = candles[i]
            if c.low < lo_price and c.close > lo_price:
                events.append(SweepEvent(i, "bullish", lo_price, "sellside"))
                seen_lo_levels.add(lo_key)
                break
    events.sort(key=lambda e: e.index)
    return events


# ----------------------------------------------------------------------
# Order blocks
# ----------------------------------------------------------------------
@dataclass
class OrderBlock:
    index: int
    high: float
    low: float
    direction: Direction


def find_order_block(candles: list[Candle], event_index: int, direction: Direction, search_back: int = 15) -> OrderBlock | None:
    start = max(0, event_index - search_back)
    for i in range(event_index - 1, start - 1, -1):
        c = candles[i]
        if direction == "bullish" and c.bearish:
            return OrderBlock(i, c.high, c.low, "bullish")
        if direction == "bearish" and c.bullish:
            return OrderBlock(i, c.high, c.low, "bearish")
    return None


# ----------------------------------------------------------------------
# Breaker blocks
# ----------------------------------------------------------------------
@dataclass
class Breaker:
    index: int  # index of the order-block candle itself (same candle it was before flipping)
    high: float
    low: float
    direction: Direction  # the breaker's NEW/active direction, opposite of the OB it used to be
    formed_at_index: int  # index of the CHoCH event that flipped it into a breaker


def find_breaker_blocks(candles: list[Candle], lookback: int) -> list[Breaker]:
    """A breaker block is what used to be an order block for the prior
    trend, before that trend failed. When price reverses hard enough to
    print a CHoCH (a break of structure *against* the prevailing trend),
    the order block that had been anchoring the now-failed trend doesn't
    just get invalidated — ICT treats that same zone as flipping polarity
    and becoming support/resistance in the *new* direction instead:

    - A bullish CHoCH means the prior downtrend's bearish order block
      (the last up-close candle before the down move) just failed —
      that zone flips into a bullish breaker (support going forward).
    - A bearish CHoCH means the prior uptrend's bullish order block (the
      last down-close candle before the up move) just failed — that
      zone flips into a bearish breaker (resistance going forward).

    Scans the *entire* structure-event history, not just the most recent
    reversal, so every breaker that has ever formed shows up on the
    chart — same "call out every one of them" treatment already given to
    SMT divergences and liquidity grabs."""
    events = detect_structure(candles, lookback)
    last_event_by_dir: dict[Direction, StructureEvent] = {}
    breakers: list[Breaker] = []
    for ev in events:
        old_dir: Direction = "bearish" if ev.direction == "bullish" else "bullish"
        if ev.type == "CHoCH":
            prior = last_event_by_dir.get(old_dir)
            if prior is not None:
                ob = find_order_block(candles, prior.index, old_dir)
                if ob is not None:
                    breakers.append(Breaker(ob.index, ob.high, ob.low, ev.direction, ev.index))
        last_event_by_dir[ev.direction] = ev
    return breakers


# ----------------------------------------------------------------------
# Liquidity heat map
# ----------------------------------------------------------------------
def _provisional_extremes(candles: list[Candle], lookback: int) -> tuple[set[int], set[int]]:
    """Whether the single most recent candle — typically still forming —
    is itself a fresh local extreme relative to the `lookback` bars
    before it. find_swing_points (and find_liquidity_pools) need
    `lookback` bars of confirmation on BOTH sides, so neither can ever
    flag anything that recent — structurally, the live edge is always
    blind to it until confirmation catches up days... er, bars later.
    Resting liquidity just beyond a fresh high/low is real before that
    high/low has "structurally" confirmed — stops don't wait for a
    lookback window.

    Deliberately checks only the *last* candle, not a trailing window of
    several — during any ordinary trending/impulsive move, several
    consecutive bars near the live edge would *all* individually qualify
    as "a new local high" (each one is higher than the few before it,
    almost by definition of a trend), which piled up disproportionate
    weight into whatever the current price happens to be, well past
    what a single fresh extreme deserves, and could outrank a genuinely
    significant, already-proven level elsewhere. Checking only the very
    last candle gives the live-edge reactivity this exists for (its
    high/low changes on every live tick, so this re-evaluates fresh
    every call) without that runaway effect.

    Used only as a light, immediately-live layer in
    build_liquidity_heatmap(); every other detector in this module
    keeps the stricter, fully-confirmed definition."""
    n = len(candles)
    if n <= lookback:
        return set(), set()
    i = n - 1
    highs, lows = set(), set()
    window_high = candles[i].high
    window_low = candles[i].low
    if all(window_high > candles[i - j].high for j in range(1, lookback + 1)):
        highs.add(i)
    if all(window_low < candles[i - j].low for j in range(1, lookback + 1)):
        lows.add(i)
    return highs, lows


def _crypto_liquidity_heatmap(
    crypto_book: dict, current_price: float, lo: float, hi: float, num_buckets: int,
) -> list[dict]:
    """Real, live order-book depth (see
    data_source.get_crypto_liquidity_snapshot — MEXC's NAS100_USDT
    perpetual futures contract, free and public) reshaped onto NAS100's
    own price axis. NAS100 itself (the OANDA CFD) has no real public
    order book at all — it's quoted by a market maker, not traded on an
    exchange — so there is no genuine "resting liquidity at each NAS100
    price" to fetch from OANDA directly. NAS100_USDT is a *different*
    venue actually tracking the same underlying index (via funding
    rate), so its order book is real liquidity for NAS100, just quoted
    with a different basis/spread than OANDA's feed — this still
    anchors and rescales it onto NAS100's own price range rather than
    assuming the two venues quote identical numbers: each bid/ask's
    offset from its own book's mid-price, as a fraction of that book's
    own total depth span, is applied to NAS100's current price scaled by
    half of NAS100's own auto-fit range — so the map is anchored at the
    current NAS100 price with real liquidity thinning out around it
    exactly like a genuine depth map would, using real bid/ask *sizes*
    as weight. The raw NAS100_USDT price numbers never appear anywhere
    in the output; only the real, live liquidity distribution shape
    carries over.

    Returns [] (never raises) if the snapshot is missing/malformed, so
    the caller can fall back to the concept-based heat map."""
    bids = crypto_book.get("bids") or []
    asks = crypto_book.get("asks") or []
    if not bids or not asks:
        return []
    best_bid = max(p for p, _ in bids)
    best_ask = min(p for p, _ in asks)
    mid = (best_bid + best_ask) / 2
    all_levels = bids + asks
    span = max(abs(p - mid) for p, _ in all_levels)
    if span <= 0:
        return []

    target_half_range = max((hi - lo) / 2, 1e-9)
    bucket_size = (hi - lo) / num_buckets
    weights = [0.0] * num_buckets

    def _bucket_of(price: float) -> int:
        b = int((price - lo) / bucket_size)
        return max(0, min(num_buckets - 1, b))

    for p, qty in all_levels:
        rel_offset = (p - mid) / span  # in [-1, 1], real position within the real book
        mapped_price = current_price + rel_offset * target_half_range
        if mapped_price < lo or mapped_price > hi:
            continue
        weights[_bucket_of(mapped_price)] += qty  # real resting size at that level

    # Real order books routinely have one or two outlier-sized resting
    # orders (a whale/iceberg parked far from the touch) that dwarf
    # everything else by 10-100x. Normalizing linearly against that one
    # bucket crushes every other real level — including the genuinely
    # dense cluster right around the current price — down to near-zero
    # intensity, which is exactly what "the heat map is just clumped up
    # in one spot" looks like: one hot square, everything else reading
    # as flat and empty even though real liquidity is there. Same sqrt
    # dampening the ICT-concept map below already uses, for the same
    # reason: compress the gap between huge and merely-large weights
    # without hiding which level is genuinely biggest.
    weights = [w ** 0.5 for w in weights]

    max_w = max(weights) if weights else 0.0
    if max_w <= 0:
        return []

    out: list[dict] = []
    for bi, w in enumerate(weights):
        if w <= 0:
            continue
        band_lo = lo + bi * bucket_size
        band_hi = band_lo + bucket_size
        out.append({
            "top": round(band_hi, 2), "bottom": round(band_lo, 2),
            "intensity": round(w / max_w, 3),
        })
    return out


def build_liquidity_heatmap(
    candles: list[Candle],
    liquidity_highs: set[int],
    liquidity_lows: set[int],
    grabs: list[SweepEvent],
    breakers: list[Breaker],
    swing_lookback: int,
    num_buckets: int = 40,
    crypto_book: dict | None = None,
) -> list[dict]:
    """A price-bucketed density read of where liquidity is concentrated.

    If `crypto_book` (see data_source.get_crypto_liquidity_snapshot) is
    available, this defers entirely to _crypto_liquidity_heatmap() —
    real, live order-book depth, reshaped onto NAS100's price axis,
    since NAS100 itself (a market-maker CFD) has no real public order
    book of its own to read from anywhere. `candles`/liquidity_highs/
    etc. are only used for the fallback below when no crypto snapshot
    is available (or it comes back empty/malformed) — built from the
    same real ICT liquidity concepts this module already detects, not a
    generic "where has price spent time" congestion read (an earlier
    version of this function tried that, time-at-price/TPO-style, purely
    to make it feel more responsive between candle closes — but that
    isn't liquidity, and didn't track any of the real levels this
    strategy actually reasons about):

    - Every liquidity pool point (see find_liquidity_pools — the loose,
      inclusive-comparison definition, since "equal highs/lows piling
      into the same price" is itself a textbook liquidity concept) adds
      weight to its own bucket.
    - Every confirmed liquidity grab (see find_all_liquidity_grabs) adds
      the most weight of anything here — a level that's actually been
      swept is proven significant, not merely sitting there untouched.
    - Every breaker block (see find_breaker_blocks) adds weight across
      the whole zone it spans.
    - Provisional extremes (see _provisional_extremes) add a lighter
      weight at whatever the *current, still-forming* candle's high/low
      is doing right now, without waiting for the multi-bar confirmation
      the fully-structural detectors above need — this is what makes
      the map visibly react on every poll instead of only when a candle
      closes and confirms new structure, while every well-established,
      already-confirmed liquidity level still dominates the picture.

    Recency matters for all of the above: older events are linearly
    downweighted (oldest bar in the window counts at 30% strength, the
    most recent at 100%), so a map built from hundreds of bars of
    history isn't dominated by a liquidity pool from days ago that's
    since become irrelevant.

    Returns a sparse list of {top, bottom, intensity} bands — intensity
    normalized to [0, 1] against the single hottest bucket in this
    result — sorted low-to-high by price, with zero-weight buckets
    omitted entirely so a quiet stretch of price doesn't cost the
    frontend a wasted draw call. Never raises — degrades to an empty
    list for too little history or a degenerate (zero-range) candle
    set."""
    if not candles:
        return []
    # The bucket grid's price range is deliberately read from confirmed
    # (closed) candles only, excluding the still-forming last one. This
    # function gets called fresh on every ~1s /api/signal poll, and the
    # forming candle's high/low genuinely ticks with live price movement
    # between polls — if it were included, every tiny tick could nudge
    # lo/hi by a fraction of a point, which reshuffles all `num_buckets`
    # boundaries at once (bucket_size = range / num_buckets applies
    # globally), making already-hot buckets visibly jump to adjacent
    # price levels from one poll to the next instead of holding still.
    # Confirmed candles only change when a new bar actually closes, so
    # this keeps the grid itself stable within a candle, while genuine
    # new-candle-close range updates still come through normally.
    stable_candles = candles[:-1] if len(candles) > 1 else candles
    lo = min(c.low for c in stable_candles)
    hi = max(c.high for c in stable_candles)
    price_range = hi - lo
    if price_range <= 0:
        return []

    if crypto_book is not None:
        mapped = _crypto_liquidity_heatmap(crypto_book, candles[-1].close, lo, hi, num_buckets)
        if mapped:
            return mapped
        # Snapshot was present but empty/malformed — fall through to the
        # concept-based map below rather than returning nothing.

    bucket_size = price_range / num_buckets
    weights = [0.0] * num_buckets
    n = len(candles)

    def _bucket_of(price: float) -> int:
        b = int((price - lo) / bucket_size)
        return max(0, min(num_buckets - 1, b))

    def _recency(idx: int) -> float:
        if n <= 1:
            return 1.0
        return 0.3 + 0.7 * (max(0, min(n - 1, idx)) / (n - 1))

    for i in liquidity_highs:
        weights[_bucket_of(candles[i].high)] += 1.5 * _recency(i)
    for i in liquidity_lows:
        weights[_bucket_of(candles[i].low)] += 1.5 * _recency(i)

    for g in grabs:
        weights[_bucket_of(g.level)] += 2.5 * _recency(g.index)

    for b in breakers:
        b_lo, b_hi = _bucket_of(b.low), _bucket_of(b.high)
        for bi in range(min(b_lo, b_hi), max(b_lo, b_hi) + 1):
            weights[bi] += 1.8 * _recency(b.formed_at_index)

    prov_highs, prov_lows = _provisional_extremes(candles, swing_lookback)
    for i in prov_highs:
        weights[_bucket_of(candles[i].high)] += 0.8 * _recency(i)
    for i in prov_lows:
        weights[_bucket_of(candles[i].low)] += 0.8 * _recency(i)

    # A tight, quiet consolidation range can pile many points into one
    # bucket (real quiet sessions do this) — left as a raw linear sum,
    # that single crowded bucket would swamp the normalization and wash
    # out every other genuine cluster to near-zero by comparison. sqrt()
    # keeps the ordering (a busier bucket still reads hotter) while
    # giving each additional hit in the same bucket diminishing rather
    # than linear returns, so one dominant range doesn't flatten
    # everything else on the map.
    weights = [w ** 0.5 for w in weights]

    max_w = max(weights)
    if max_w <= 0:
        return []

    out: list[dict] = []
    for bi, w in enumerate(weights):
        if w <= 0:
            continue
        band_lo = lo + bi * bucket_size
        band_hi = band_lo + bucket_size
        out.append({
            "top": round(band_hi, 2), "bottom": round(band_lo, 2),
            "intensity": round(w / max_w, 3),
        })
    return out


# ----------------------------------------------------------------------
# Fair value gaps
# ----------------------------------------------------------------------
@dataclass
class FVG:
    index: int  # middle candle of the 3-candle pattern
    top: float
    bottom: float
    direction: Direction
    mitigated: bool = False


def find_fvgs(candles: list[Candle], start_index: int, end_index: int, direction: Direction) -> list[FVG]:
    out = []
    lo = max(1, start_index)
    hi = min(len(candles) - 1, end_index)
    for i in range(lo, hi):
        prev_c, next_c = candles[i - 1], candles[i + 1]
        if direction == "bullish" and prev_c.high < next_c.low:
            out.append(FVG(i, top=next_c.low, bottom=prev_c.high, direction="bullish"))
        if direction == "bearish" and prev_c.low > next_c.high:
            out.append(FVG(i, top=prev_c.low, bottom=next_c.high, direction="bearish"))

    # mark mitigation: has price traded back through the gap since it formed?
    for gap in out:
        for c in candles[gap.index + 2:]:
            if gap.direction == "bullish" and c.low <= gap.bottom:
                gap.mitigated = True
                break
            if gap.direction == "bearish" and c.high >= gap.top:
                gap.mitigated = True
                break
    return out


# ----------------------------------------------------------------------
# Premium / discount + Optimal Trade Entry (OTE)
# ----------------------------------------------------------------------
def ote_zone(candles: list[Candle], leg_start: int, leg_end: int, direction: Direction) -> tuple[float, float] | None:
    if leg_end <= leg_start:
        return None
    segment = candles[leg_start:leg_end + 1]
    if not segment:
        return None
    seg_high = max(c.high for c in segment)
    seg_low = min(c.low for c in segment)
    rng = seg_high - seg_low
    if rng <= 0:
        return None
    if direction == "bullish":
        # retracement down from the high into the 62%-79% zone
        return (seg_high - rng * 0.79, seg_high - rng * 0.62)
    else:
        return (seg_low + rng * 0.62, seg_low + rng * 0.79)


# ----------------------------------------------------------------------
# SMT (Smart Money Technique) divergence
# ----------------------------------------------------------------------
def _closest_swing_index(candles: list[Candle], pool: set[int], target_time: int, tolerance_ms: float) -> int | None:
    """The correlated instrument's swing index whose candle timestamp is
    nearest to `target_time`, within `tolerance_ms` — or None if nothing
    in `pool` is close enough to count as "the same moment"."""
    best_i: int | None = None
    best_gap: float | None = None
    for i in pool:
        gap = abs(candles[i].time - target_time)
        if gap <= tolerance_ms and (best_gap is None or gap < best_gap):
            best_i, best_gap = i, gap
    return best_i


@dataclass
class SMTDivergenceEvent:
    index: int  # index into `primary`'s candle list, at the diverging swing
    time: int
    direction: Direction  # 'bullish' | 'bearish'
    primary_price: float
    correlated_price: float
    correlated_time: int


def find_smt_divergences(
    primary: list[Candle], correlated: list[Candle], swing_lookback: int
) -> list[SMTDivergenceEvent]:
    """Classic ICT SMT: every time the primary instrument prints a new
    swing extreme that a correlated one (e.g. NAS100 vs ES/S&P 500)
    fails to confirm — one market showing relative strength/weakness the
    other doesn't, a classic smart-money "tell". Bullish: primary makes
    a lower low while the correlated instrument makes a higher low.
    Bearish: primary makes a higher high while the correlated instrument
    makes a lower high.

    This scans every consecutive pair of swing points across the whole
    shared history, not just the two most recent ones — a real SMT read
    (and every chart-based SMT indicator, e.g. on TradingView) calls out
    every divergence as it happens, not a single current yes/no. Only
    checking the latest swing pair meant the vast majority of genuine
    divergences sitting earlier in the same data were silently never
    seen at all, which is why setups a chart-based indicator flagged
    were going completely undetected here.

    Swings are matched between the two instruments by real timestamp,
    not by position in each series' own swing-point list — the primary
    and correlated candle series are fetched independently and aren't
    guaranteed to carry the same bar count/fractal swing count over the
    same window, so "the Nth swing" on one can land on a totally
    different moment than "the Nth swing" on the other.

    Returns events sorted oldest-to-newest. Never raises — degrades to
    an empty list if there isn't enough history yet."""
    min_needed = max(30, swing_lookback * 6)
    if len(primary) < min_needed or len(correlated) < min_needed:
        return []

    p_highs, p_lows = find_swing_points(primary, swing_lookback)
    c_highs, c_lows = find_swing_points(correlated, swing_lookback)

    # Two correlated instruments' bars won't line up to the millisecond
    # (feed lag, slightly different candle open times), but they should
    # agree on the same bar interval — a few bars' worth of slack is
    # "the same swing," anything further apart is a different move.
    bar_ms = correlated[-1].time - correlated[-2].time if len(correlated) > 1 else 60_000
    tolerance_ms = max(abs(bar_ms) * 6, 1)

    events: list[SMTDivergenceEvent] = []

    p_low_idx = sorted(p_lows)
    for i in range(1, len(p_low_idx)):
        prev_i, last_i = p_low_idx[i - 1], p_low_idx[i]
        c_prev_i = _closest_swing_index(correlated, c_lows, primary[prev_i].time, tolerance_ms)
        c_last_i = _closest_swing_index(correlated, c_lows, primary[last_i].time, tolerance_ms)
        if c_prev_i is None or c_last_i is None or c_prev_i == c_last_i:
            continue
        if primary[last_i].low < primary[prev_i].low and correlated[c_last_i].low > correlated[c_prev_i].low:
            events.append(SMTDivergenceEvent(
                last_i, primary[last_i].time, "bullish",
                primary[last_i].low, correlated[c_last_i].low, correlated[c_last_i].time,
            ))

    p_high_idx = sorted(p_highs)
    for i in range(1, len(p_high_idx)):
        prev_i, last_i = p_high_idx[i - 1], p_high_idx[i]
        c_prev_i = _closest_swing_index(correlated, c_highs, primary[prev_i].time, tolerance_ms)
        c_last_i = _closest_swing_index(correlated, c_highs, primary[last_i].time, tolerance_ms)
        if c_prev_i is None or c_last_i is None or c_prev_i == c_last_i:
            continue
        if primary[last_i].high > primary[prev_i].high and correlated[c_last_i].high < correlated[c_prev_i].high:
            events.append(SMTDivergenceEvent(
                last_i, primary[last_i].time, "bearish",
                primary[last_i].high, correlated[c_last_i].high, correlated[c_last_i].time,
            ))

    events.sort(key=lambda e: e.index)
    return events


# ----------------------------------------------------------------------
# Kill zones (America/New_York local time)
# ----------------------------------------------------------------------
def current_kill_zone(now_utc: datetime | None = None) -> str | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    if NY_TZ is not None:
        local = now_utc.astimezone(NY_TZ)
    else:
        # crude fallback: fixed UTC-5, no DST handling
        from datetime import timedelta
        local = now_utc.astimezone(timezone.utc) + timedelta(hours=-5)
    hour = local.hour

    if hour >= 20 or hour < 0:
        return "Asian"
    if 2 <= hour < 5:
        return "London"
    if 7 <= hour < 10:
        return "New York AM"
    if 10 <= hour < 12:
        return "London Close"
    return None


# ----------------------------------------------------------------------
# Daily / session liquidity extremes (for reversal detection below)
# ----------------------------------------------------------------------
def _ny_local(ts_ms: int) -> datetime:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.astimezone(NY_TZ) if NY_TZ is not None else dt + timedelta(hours=-5)


# A slightly wider set of named windows than KILL_ZONES above — this is
# for tagging every candle into "which session made this high/low", not
# for the kill-zone confluence, so the exact boundaries don't need to
# match. (Also fixes a latent off-by-one in KILL_ZONES' own Asian
# wraparound — `hour < 0` there never actually triggers, since `hour` is
# always 0-23 — by using `hour < 2` here instead, so hours 0-1 correctly
# still count as (the tail end of) the Asian session rather than falling
# into a no-session gap.)
def _session_label(hour: int) -> str | None:
    if hour >= 20 or hour < 2:
        return "Asian"
    if 2 <= hour < 5:
        return "London"
    if 7 <= hour < 10:
        return "New York AM"
    if 10 <= hour < 12:
        return "London Close"
    return None


def daily_and_session_levels(candles: list[Candle]) -> list[dict]:
    """Previous-day and previous-(named-)session high/low — the
    "significant" liquidity levels a real top/bottom-tick reversal is
    expected to sweep first (see find_reversal below), in priority
    order: previous day, then previous session. Both are *finalized*
    periods (already fully closed by the time the latest candle formed),
    so unlike "today's high so far" these values are stable regardless
    of which earlier candle a sweep is being checked against — no risk
    of a level silently growing past whatever wick supposedly swept it.

    Returns [] if there isn't at least one full prior day of candles yet
    (nothing significant to reference), or if the server's zoneinfo/tzdata
    isn't available (NY_TZ is None) — in which case reversal detection
    falls back to swing-point-based levels only (see find_reversal)."""
    if not candles or NY_TZ is None:
        return []

    day_hi: dict = {}
    day_lo: dict = {}
    day_hi_idx: dict = {}
    day_lo_idx: dict = {}
    sess_hi: dict = {}
    sess_lo: dict = {}
    sess_hi_idx: dict = {}
    sess_lo_idx: dict = {}
    sess_order: list = []

    for i, c in enumerate(candles):
        local = _ny_local(c.time)
        d = local.date()
        if d not in day_hi or c.high > day_hi[d]:
            day_hi[d], day_hi_idx[d] = c.high, i
        if d not in day_lo or c.low < day_lo[d]:
            day_lo[d], day_lo_idx[d] = c.low, i

        label = _session_label(local.hour)
        if label is not None:
            # The Asian session wraps midnight — anchor it to the date it
            # *started* on (the evening before), so 1am Tuesday is still
            # part of "Monday's Asian session", not treated as a brand
            # new one-hour session of its own.
            skey_date = d - timedelta(days=1) if (label == "Asian" and local.hour < 2) else d
            skey = (skey_date, label)
            if skey not in sess_hi:
                sess_order.append(skey)
            if skey not in sess_hi or c.high > sess_hi[skey]:
                sess_hi[skey], sess_hi_idx[skey] = c.high, i
            if skey not in sess_lo or c.low < sess_lo[skey]:
                sess_lo[skey], sess_lo_idx[skey] = c.low, i

    today = _ny_local(candles[-1].time).date()
    current_skey = None
    _cur_label = _session_label(_ny_local(candles[-1].time).hour)
    if _cur_label is not None:
        _cur_date = today - timedelta(days=1) if (_cur_label == "Asian" and _ny_local(candles[-1].time).hour < 2) else today
        current_skey = (_cur_date, _cur_label)

    levels: list[dict] = []

    prev_day = today - timedelta(days=1)
    if prev_day in day_hi:
        levels.append({"label": "Previous day high", "price": day_hi[prev_day], "is_high": True, "index": day_hi_idx[prev_day]})
        levels.append({"label": "Previous day low", "price": day_lo[prev_day], "is_high": False, "index": day_lo_idx[prev_day]})

    completed_sessions = [k for k in sess_order if k != current_skey]
    if completed_sessions:
        prev_skey = completed_sessions[-1]
        levels.append({"label": f"Previous {prev_skey[1]} session high", "price": sess_hi[prev_skey], "is_high": True, "index": sess_hi_idx[prev_skey]})
        levels.append({"label": f"Previous {prev_skey[1]} session low", "price": sess_lo[prev_skey], "is_high": False, "index": sess_lo_idx[prev_skey]})

    return levels


# ----------------------------------------------------------------------
# Master evaluation
# ----------------------------------------------------------------------
def evaluate_ict(
    ltf_raw: list[dict],
    htf_minutes: int = 15,
    swing_lookback: int = 3,
    sweep_lookback: int = 20,
    risk_reward: float = 2.0,
    require_kill_zone: bool = True,
    require_htf_bias: bool = True,
    require_structure_shift: bool = True,
    require_liquidity_sweep: bool = True,
    require_entry_zone: bool = True,
    require_smt_divergence: bool = False,
    smt_raw: list[dict] | None = None,
    smt_primary_raw: list[dict] | None = None,
    news_required: bool = True,
    news_ok: bool = True,
    news_note: str = "",
    as_of: datetime | None = None,
    crypto_liquidity_raw: dict | None = None,
    checklist_reset_after: int | None = None,
    reversal_reset_after: int | None = None,
) -> ICTResult:
    """
    Entry-zone selection is callout-driven (see the "callouts" section of
    evaluate_ict's body, and ICTResult.callouts): every distinct touch of
    every candidate zone (breaker/OB/live FVGs/OTE) across every
    structure trigger in the recency window is collected chronologically
    and classified as an entry/continuation/re-entry. The most recent
    one always becomes the active entry_zone automatically — there is no
    manual reset mechanism for the entry zone itself (an earlier
    entry_reset_after/exclude_zone_keys param pair was removed because it
    only patched around the real bug: zone selection used to be
    priority-based, breaker > OB > FVG > OTE, so a stale-but-still-valid
    higher-priority zone could permanently outrank a fresher, more
    relevant one. Recency-based callouts fix that at the root instead of
    requiring the trader to manually dismiss the stale zone).

    checklist_reset_after: epoch-ms timestamp (candle .time units), or
    None. This is a *different* reset than the one above — it's for the
    whole confluence checklist getting stuck on one still-active LTF
    trigger (see the "Reset checklist" button on the dashboard): in a
    sustained one-directional trend with no fresh BOS/CHoCH all day, the
    same trigger keeps being evaluated over and over, so a confluence
    that trigger genuinely never satisfied (e.g. "Liquidity sweep" if
    that particular break wasn't preceded by a real sweep) can look
    "stuck" for hours — which is an honest read of a real, unchanging
    condition, not a bug, but the trader may still want to stop waiting
    on that one trigger and have the whole checklist go back to "waiting
    for a trigger" until a genuinely new one forms. When set, every LTF
    structure event at or before this timestamp is excluded from
    `matching` below, which cascades naturally through every downstream
    step (LTF structure shift, liquidity sweep, entry-zone callouts, SMT
    divergence all key off `trigger`) — no separate handling needed
    elsewhere. Composes safely with a trend change on its own: filtering
    is applied *after* the direction filter, so a fresh trigger in either
    direction that forms after this timestamp is never blocked by a
    reset stamped under the old trend.

    reversal_reset_after: epoch-ms timestamp (candle .time units), or
    None. The same idea as checklist_reset_after, scoped to the separate
    reversal signal instead (see the "Reset reversal" button) — every
    CHoCH event at or before this timestamp is excluded from reversal
    consideration, so a reversal setup that's already played out (or one
    the trader just wants to stop watching) doesn't keep occupying the
    reversal card until a genuinely new CHoCH forms. Independent of
    checklist_reset_after — resetting one doesn't affect the other.

    as_of: the timestamp to treat as "now" for kill-zone purposes.
    Defaults to the real current time (live trading use). The backtester
    passes the timestamp of the candle being evaluated instead, so kill
    zones are judged against historical bar time rather than wall clock.

    require_*: per-confluence toggles — a disabled confluence is left
    out of the returned checklist entirely and doesn't factor into
    whether a signal fires. require_htf_bias / require_structure_shift
    are the two exceptions: evaluate_ict() structurally cannot determine
    a trade direction or an entry trigger without them, so disabling
    just hides them from the checklist/gate rather than changing
    whether the pipeline can proceed.

    smt_raw: candles for a correlated instrument (e.g. ES/S&P 500),
    only used when require_smt_divergence is True. Pass None if
    unavailable — the confluence degrades to "unmet, no correlated
    data" rather than erroring.

    smt_primary_raw: this instrument's OWN candles, but fetched at
    whichever timeframe SMT divergence should actually be read on —
    which the user can set independently of the main entry timeframe
    (`ltf_raw`/candle_type). SMT is a higher-timeframe-style read in
    ICT terms (comparing recent swing structure between two markets),
    and forcing it onto the same fast entry timeframe as the rest of
    the checklist tends to produce noisy, low-conviction swings. Falls
    back to `ltf_raw` itself (the old, hard-coded behavior) if not
    provided, so existing callers keep working unchanged.

    crypto_liquidity_raw: an optional real, live order-book snapshot
    (see data_source.get_crypto_liquidity_snapshot) used to drive the
    liquidity heat map with genuine bid/ask depth, since NAS100 itself
    has no real public order book to read from anywhere. Pass None if
    unavailable — the heat map falls back to its ICT-concept-based read
    (see build_liquidity_heatmap) rather than erroring.
    """
    ltf = normalize_candles(ltf_raw)
    min_bars = max(60, swing_lookback * 10)
    if len(ltf) < min_bars:
        return ICTResult(
            "FLAT", 0, None, None, None, None, None, None,
            reason=f"Not enough candles ({len(ltf)}/{min_bars}) to evaluate market structure",
        )

    htf = resample(ltf, htf_minutes)
    price = ltf[-1].close
    as_of = as_of or datetime.fromtimestamp(ltf[-1].time / 1000, tz=timezone.utc)
    kz = current_kill_zone(as_of)

    # The level(s) where market structure would next shift if price
    # closed beyond them — the current unbroken swing high/low on the
    # entry timeframe. Computed unconditionally (it only needs `ltf`,
    # not an established HTF bias or LTF trigger) so the chart overlay
    # always has this to show, even while everything else below is
    # still FLAT/waiting on a bias or trigger.
    pending_high, pending_low, ltf_trend = find_pending_structure_levels(ltf, swing_lookback)

    def _pending_structure_zones() -> list[dict]:
        out: list[dict] = []
        if pending_high is not None:
            idx, level = pending_high
            kind = "BOS" if ltf_trend == "bullish" else "CHoCH"
            out.append({
                "type": "structure_watch", "direction": "bullish",
                "top": round(level, 2), "bottom": round(level, 2),
                "from_time": ltf[idx].time, "to_time": None,
                "label": f"{kind} bullish above {level:.2f}",
                "active": False, "invalidated": False,
            })
        if pending_low is not None:
            idx, level = pending_low
            kind = "BOS" if ltf_trend == "bearish" else "CHoCH"
            out.append({
                "type": "structure_watch", "direction": "bearish",
                "top": round(level, 2), "bottom": round(level, 2),
                "from_time": ltf[idx].time, "to_time": None,
                "label": f"{kind} bearish below {level:.2f}",
                "active": False, "invalidated": False,
            })
        return out

    # SMT divergences (see find_smt_divergences) are also computed
    # unconditionally, like the pending structure levels above, so every
    # one found shows up on the chart even before a bias/trigger exists.
    # The checklist confluence further down just filters this same list
    # by htf_bias once that's known — it never gets recomputed.
    smt_divergences: list[SMTDivergenceEvent] = []
    if require_smt_divergence and smt_raw:
        _smt_correlated = normalize_candles(smt_raw)
        _smt_primary = normalize_candles(smt_primary_raw) if smt_primary_raw else ltf
        smt_divergences = find_smt_divergences(_smt_primary, _smt_correlated, swing_lookback)

    def _smt_divergence_zones() -> list[dict]:
        out: list[dict] = []
        for ev in smt_divergences:
            span_end = ev.correlated_time if ev.correlated_time > ev.time else ev.time + 5 * 60_000
            out.append({
                "type": "smt_divergence", "direction": ev.direction,
                "top": round(ev.primary_price, 2), "bottom": round(ev.primary_price, 2),
                "from_time": ev.time, "to_time": span_end,
                "label": f"SMT {ev.direction} divergence",
                "active": bool(htf_bias) and ev.direction == htf_bias,
                "invalidated": False,
            })
        return out

    # A zone counts as the live entry zone once price has *tapped* into
    # it, and it stays that way even after price ticks back out the way
    # it came — a retest-and-bounce off support/resistance is still a
    # valid, working setup. It only stops counting once price actually
    # *invalidates* the zone by closing through the far side of it.
    # Moved up here (out of the main trigger-dependent section further
    # down) since breaker blocks and liquidity pools/grabs — both also
    # computed unconditionally, right below — need this to report their
    # own tapped/invalidated status on the chart even in the early
    # "no bias yet" / "no trigger yet" return paths.
    #
    # "Tapped" requires *displacement first* — price must actually close
    # beyond the zone (confirming the move away from it) before any
    # later dip back into it counts as a real retracement. The
    # displacement close also has to clear the zone by a real margin (a
    # fraction of the zone's own height), not just barely poke past the
    # edge.
    def _tap_status(start_index: int, top: float, bottom: float, direction: Direction) -> tuple[bool, bool]:
        zone_height = max(top - bottom, 1e-9)
        buffer = zone_height * 0.15
        displaced = False
        tapped = False
        invalidated = False
        for c in ltf[max(0, start_index):]:
            if direction == "bullish":
                if not displaced:
                    if c.close > top + buffer:
                        displaced = True
                    continue
                if c.low <= top:
                    tapped = True
                if tapped and c.close < bottom:
                    invalidated = True
            else:
                if not displaced:
                    if c.close < bottom - buffer:
                        displaced = True
                    continue
                if c.high >= bottom:
                    tapped = True
                if tapped and c.close > top:
                    invalidated = True
        return tapped, invalidated

    # Every distinct touch of [bottom, top] — a generalization of
    # _tap_status for the re-entry/continuation callout feed below,
    # which needs to know *when* each touch happened, and whether a zone
    # was touched more than once, not just whether it's tapped right
    # now. Same displacement-first rule as _tap_status (a touch right
    # after the zone forms, with no real move away from it first,
    # doesn't count), plus: price has to displace away *again* after a
    # touch before the next dip back in counts as a new, distinct touch
    # — so sitting inside the zone for several bars in a row is one
    # touch, not several, and a genuine re-entry only fires once price
    # has actually left and come back.
    def _tap_events(start_index: int, top: float, bottom: float, direction: Direction) -> list[int]:
        zone_height = max(top - bottom, 1e-9)
        buffer = zone_height * 0.15
        displaced = False
        touching = False
        events: list[int] = []
        for i in range(max(0, start_index), len(ltf)):
            c = ltf[i]
            if direction == "bullish":
                if events and c.close < bottom:
                    break  # invalidated after at least one real touch — stop, don't count anything past this
                now_touching = c.low <= top
            else:
                if events and c.close > top:
                    break
                now_touching = c.high >= bottom
            # `touching` is tracked every bar regardless of `displaced` —
            # otherwise, once a touch starts and displaced resets below,
            # sitting inside the zone (never closing back beyond the
            # buffer) would leave `touching` permanently stuck True even
            # after price genuinely left, silently swallowing the next
            # real re-entry. `displaced` only gates whether a NEW
            # not-touching -> touching transition counts as a fresh event.
            if touching:
                if not now_touching:
                    touching = False
            elif displaced and now_touching:
                events.append(i)
                touching = True
                displaced = False  # must genuinely leave and redisplace before the next touch counts
            elif not displaced:
                if direction == "bullish":
                    if c.close > top + buffer:
                        displaced = True
                else:
                    if c.close < bottom - buffer:
                        displaced = True
        return events

    # Liquidity pools (see find_liquidity_pools) — the *loose*,
    # inclusive-comparison pool — are still what actually gates the
    # signal/stop-loss below (find_liquidity_sweep calls), unchanged
    # from the earlier sweep-sensitivity fix.
    ltf_liquidity_highs, ltf_liquidity_lows = find_liquidity_pools(ltf, swing_lookback)

    # The chart's "every grab" overlay deliberately uses the *stricter*
    # fully-isolated fractal definition (find_swing_points) instead of
    # the loose pool above — the loose version exists specifically to
    # catch marginal/equal-high sweep candidates for the one confluence
    # that actually gates a trade, but showing every one of those on the
    # chart as a permanent marker made the overlay noisy with low-
    # significance levels. Real, well-separated swing points only.
    ltf_swing_highs, ltf_swing_lows = find_swing_points(ltf, swing_lookback)
    all_liquidity_grabs: list[SweepEvent] = find_all_liquidity_grabs(ltf, ltf_swing_highs, ltf_swing_lows)
    _grab_bar_ms = ltf[-1].time - ltf[-2].time if len(ltf) > 1 else 60_000

    def _liquidity_grab_zones(_active_index: int | None = None) -> list[dict]:
        out: list[dict] = []
        for g in all_liquidity_grabs:
            side_label = "Buy-side" if g.side == "buyside" else "Sell-side"
            out.append({
                "type": "liquidity_grab", "direction": g.direction,
                "top": round(g.level, 2), "bottom": round(g.level, 2),
                # A grab is a one-off historical event, not a forward
                # level to watch — bounded to a short span after it
                # happened (like SMT divergence) instead of a line
                # stretching to the chart's right edge forever, which is
                # what made the overlay pile up into visual noise as
                # more grabs accumulated over a session.
                "from_time": ltf[g.index].time, "to_time": ltf[g.index].time + 15 * _grab_bar_ms,
                "label": f"{side_label} liquidity grab",
                "active": g.index == _active_index, "invalidated": False,
            })
        return out

    # Breaker blocks (see find_breaker_blocks) — every order block that
    # has ever flipped into a breaker after its trend failed via a CHoCH,
    # computed unconditionally across the whole history for the same
    # "show every one" reason as the liquidity grabs above.
    ltf_breakers: list[Breaker] = find_breaker_blocks(ltf, swing_lookback)

    def _breaker_zones(_active_index: int | None = None) -> list[dict]:
        out: list[dict] = []
        for b in ltf_breakers:
            _tapped, _invalidated = _tap_status(b.index + 1, b.high, b.low, b.direction)
            out.append({
                "type": "breaker_block", "direction": b.direction,
                "top": round(b.high, 2), "bottom": round(b.low, 2),
                "from_time": ltf[b.index].time, "to_time": None,
                "label": f"{'Bullish' if b.direction == 'bullish' else 'Bearish'} breaker block",
                "active": b.index == _active_index, "invalidated": _invalidated,
            })
        return out

    # Entry-zone candidate search + the callout-merging feed (see
    # ICTResult.callouts) — computed unconditionally, here, before either
    # the continuation (HTF-bias-aligned) or reversal (counter-trend,
    # off a swept high/low) sections below, because BOTH need it and
    # neither depends on the other existing. `direction` is passed in
    # explicitly rather than closing over `htf_bias` so the exact same,
    # already-tested logic serves a reversal trigger (whose direction is
    # whatever the CHoCH says, often *against* htf_bias — that's the
    # whole point of a reversal) just as well as a continuation trigger.
    #
    # Same top/bottom -> "top.toFixed(2):bottom.toFixed(2)" key the
    # frontend already builds (see index.html's zoneKey()) — used here
    # purely as a stable zone *identity* for the callout feed, so a
    # repeat touch of the exact same breaker/OB/FVG/OTE can be recognized
    # as a "re-entry" rather than looking like a brand new zone.
    def _zone_key(top: float, bottom: float) -> str:
        return f"{round(top, 2):.2f}:{round(bottom, 2):.2f}"

    def _zone_candidates_for_trigger(trig: StructureEvent, direction: Direction) -> dict:
        _sweep = find_liquidity_sweep(ltf, ltf_liquidity_highs, ltf_liquidity_lows, trig.index, sweep_lookback)
        _sweep_ok = _sweep is not None and _sweep.direction == direction
        _leg_start = _sweep.index if _sweep_ok else (trig.swept_index or max(0, trig.index - sweep_lookback))

        # Breaker blocks (see find_breaker_blocks) are checked first —
        # ICT treats a breaker as the highest-conviction POI, since it's
        # a zone that has *already* proven order flow shifted (the prior
        # trend's own order block failing is what created it), rather
        # than a plain order block that hasn't been tested that way yet.
        # Only the most recent breaker that (a) matches this trigger's
        # direction and (b) actually existed by the time this trigger
        # fired is eligible.
        _brk_candidates = [
            b for b in ltf_breakers if b.direction == direction and b.formed_at_index <= trig.index
        ]
        _brk = _brk_candidates[-1] if _brk_candidates else None
        _brk_tapped = _brk_invalidated = False
        if _brk is not None:
            _brk_tapped, _brk_invalidated = _tap_status(_brk.index + 1, _brk.high, _brk.low, direction)

        _ob = find_order_block(ltf, trig.index, direction)
        _ob_tapped = _ob_invalidated = False
        if _ob is not None:
            _ob_tapped, _ob_invalidated = _tap_status(_ob.index + 1, _ob.high, _ob.low, direction)

        # Search for unmitigated FVGs all the way through to the most recent
        # candle, not just the first few bars right after the trigger fired.
        # A trend (or a reversal leg) often prints several fresh FVGs as it
        # extends; all of them are legitimate re-entry magnets, not just
        # whichever one happened to form in the first few bars.
        _fvgs = [g for g in find_fvgs(ltf, _leg_start, len(ltf) - 1, direction) if not g.mitigated]
        _fvg_status = {g.index: _tap_status(g.index + 2, g.top, g.bottom, direction) for g in _fvgs}

        _ote = ote_zone(ltf, _leg_start, trig.index, direction)
        _ote_tapped = _ote_invalidated = False
        if _ote is not None:
            _ote_tapped, _ote_invalidated = _tap_status(trig.index + 1, _ote[1], _ote[0], direction)

        # No priority selection here — this just returns every raw
        # candidate (breaker/OB/live FVGs/OTE) for this one trigger,
        # tapped-status included for display purposes (e.g. graying out
        # an invalidated FVG on the chart). Which candidate actually
        # becomes the live entry zone is decided globally, across every
        # trigger considered at once, by _merge_touch_events_to_callouts
        # below — that's what fixes the old "stale higher-priority zone
        # permanently blocks a fresher lower-priority one" bug.
        return {
            "trigger": trig, "leg_start": _leg_start,
            "brk": _brk, "brk_tapped": _brk_tapped, "brk_invalidated": _brk_invalidated,
            "ob": _ob, "ob_tapped": _ob_tapped, "ob_invalidated": _ob_invalidated,
            "fvgs": _fvgs, "fvg_status": _fvg_status,
            "ote": _ote, "ote_tapped": _ote_tapped, "ote_invalidated": _ote_invalidated,
        }

    def _merge_touch_events_to_callouts(triggers: list[StructureEvent], direction: Direction) -> dict:
        """Shared by both the continuation entry-zone search and the
        reversal entry-zone search: merges every distinct touch
        (_tap_events) of every candidate zone (breaker/OB/each live
        FVG/OTE) across the given triggers into one chronological feed,
        classifies each as entry (first ever)/continuation (a zone never
        touched before)/re-entry (repeat touch), and resolves the live
        entry zone to whichever one was touched most recently — recency,
        not zone-type priority, is what actually fixes "new zones aren't
        getting rechecked"."""
        _cache: dict[int, dict] = {}

        def _get(trig: StructureEvent) -> dict:
            if trig.index not in _cache:
                _cache[trig.index] = _zone_candidates_for_trigger(trig, direction)
            return _cache[trig.index]

        _touch_events: list[dict] = []
        for _trig in triggers:
            _cand = _get(_trig)
            _brk, _ob, _fvgs, _ote = _cand["brk"], _cand["ob"], _cand["fvgs"], _cand["ote"]
            if _brk is not None:
                for _idx in _tap_events(_brk.index + 1, _brk.high, _brk.low, direction):
                    _touch_events.append({
                        "index": _idx, "zone_kind": "breaker block",
                        "top": _brk.high, "bottom": _brk.low,
                        "trigger_index": _trig.index, "zone_key": _zone_key(_brk.high, _brk.low),
                    })
            if _ob is not None:
                for _idx in _tap_events(_ob.index + 1, _ob.high, _ob.low, direction):
                    _touch_events.append({
                        "index": _idx, "zone_kind": "order block",
                        "top": _ob.high, "bottom": _ob.low,
                        "trigger_index": _trig.index, "zone_key": _zone_key(_ob.high, _ob.low),
                    })
            for _g in _fvgs:
                for _idx in _tap_events(_g.index + 2, _g.top, _g.bottom, direction):
                    _touch_events.append({
                        "index": _idx, "zone_kind": "fair value gap",
                        "top": _g.top, "bottom": _g.bottom,
                        "trigger_index": _trig.index, "zone_key": _zone_key(_g.top, _g.bottom),
                    })
            if _ote is not None:
                for _idx in _tap_events(_trig.index + 1, _ote[1], _ote[0], direction):
                    _touch_events.append({
                        "index": _idx, "zone_kind": "OTE (62-79% retracement)",
                        "top": _ote[1], "bottom": _ote[0],
                        "trigger_index": _trig.index, "zone_key": _zone_key(_ote[1], _ote[0]),
                    })
        _touch_events.sort(key=lambda e: e["index"])

        callouts: list[dict] = []
        _seen_zone_keys: set[str] = set()
        for _e in _touch_events:
            if not _seen_zone_keys:
                _kind = "entry"
            elif _e["zone_key"] in _seen_zone_keys:
                _kind = "re-entry"
            else:
                _kind = "continuation"
            _seen_zone_keys.add(_e["zone_key"])
            callouts.append({
                "index": _e["index"], "time": ltf[_e["index"]].time, "kind": _kind,
                "zone_kind": _e["zone_kind"], "top": round(_e["top"], 2), "bottom": round(_e["bottom"], 2),
                "trigger_index": _e["trigger_index"],
            })

        if callouts:
            _last = callouts[-1]
            zone_trigger = next(t for t in triggers if t.index == _last["trigger_index"])
            zone = (_last["bottom"], _last["top"])
            zone_kind = _last["zone_kind"]
        else:
            zone_trigger = None
            zone = None
            zone_kind = None

        return {
            "callouts": callouts, "zone_trigger": zone_trigger, "zone": zone, "zone_kind": zone_kind,
            "get_candidates": _get,
        }

    # Liquidity heat map (see build_liquidity_heatmap) — prefers real,
    # live crypto order-book depth (crypto_liquidity_raw) when available,
    # rescaled onto NAS100's own price range, since NAS100 has no real
    # public order book of its own. Falls back to the ICT-concept-based
    # read (real liquidity pools, grabs, and breaker blocks computed
    # above, plus a lightweight provisional-extreme layer for live-edge
    # reactivity) if no crypto snapshot is available. Computed
    # unconditionally too, so it's available on the chart even before a
    # bias/trigger exists — a density map of where liquidity sits
    # doesn't depend on there being an active setup.
    liquidity_heatmap: list[dict] = build_liquidity_heatmap(
        ltf, ltf_liquidity_highs, ltf_liquidity_lows, all_liquidity_grabs, ltf_breakers, swing_lookback,
        crypto_book=crypto_liquidity_raw,
    )

    # Every confirmed BOS/CHoCH on the entry timeframe — computed
    # unconditionally, here, since both the continuation section below
    # (which filters this to htf_bias's direction) and the reversal
    # section (which specifically wants the CHoCH events, independent of
    # any htf_bias) need it, and neither should have to wait on the
    # other.
    ltf_events = detect_structure(ltf, swing_lookback)

    # ------------------------------------------------------------------
    # Top/bottom-tick reversal detection (see ReversalResult's docstring)
    # — a real ICT "liquidity sweep + CHoCH" reversal signature, computed
    # unconditionally so it's available even when htf_bias or a
    # continuation trigger doesn't exist: a reversal is, by definition,
    # often exactly the case where the OLD trend has nothing further to
    # offer and price is doing something new instead.
    #
    # For "high probability", the swept level matters — sweeping a
    # random 3-bar fractal is common noise; sweeping the previous day's
    # or previous session's high/low (untested, well-known liquidity
    # that stops genuinely accumulate above/below) is a real, well-known
    # ICT reversal target. daily_and_session_levels() returns those in
    # priority order (previous day, then previous session); only when
    # neither is available (not enough day history yet) or neither was
    # actually swept does this fall back to the strongest recent swing
    # high/low, same mechanism the continuation signal's own liquidity-
    # sweep confluence already uses.
    REVERSAL_RECENCY_WINDOW_MS = 6 * 60 * 60 * 1000  # 6 hours — same window as the continuation callout feed
    _now_ms_rev = ltf[-1].time
    _major_levels = daily_and_session_levels(ltf)

    def _level_swept(level_price: float, is_high: bool, before_index: int, lookback: int) -> int | None:
        start = max(0, before_index - lookback)
        for i in range(start, before_index):
            c = ltf[i]
            if is_high and c.high > level_price and c.close < level_price:
                return i
            if not is_high and c.low < level_price and c.close > level_price:
                return i
        return None

    _choch_events = [
        e for e in ltf_events
        if e.type == "CHoCH" and (_now_ms_rev - ltf[e.index].time) <= REVERSAL_RECENCY_WINDOW_MS
        # "Reset reversal" (see reversal_reset_after docstring above) —
        # a CHoCH at or before the reset timestamp is treated exactly
        # like it never happened, for reversal purposes only.
        and (reversal_reset_after is None or ltf[e.index].time > reversal_reset_after)
    ]
    _reversal_was_reset = reversal_reset_after is not None and not _choch_events and any(
        e.type == "CHoCH" and (_now_ms_rev - ltf[e.index].time) <= REVERSAL_RECENCY_WINDOW_MS
        for e in ltf_events
    )

    # How far back to search for the sweep that preceded a given CHoCH.
    # This deliberately does NOT reuse `sweep_lookback` (the continuation
    # signal's tight, ~20-candle window for its own liquidity-sweep
    # confluence) — a genuinely big reversal is exactly the case where
    # that window is too small. A large move sweeps its extreme, then has
    # to travel all the way back through the prior swing structure before
    # a fractal-based CHoCH can even confirm (find_swing_points needs
    # `swing_lookback` bars of confirmation on both sides of a swing
    # point before it's "real"), which can easily take far more than 20
    # candles — with the old fixed window, a real 100+ point reversal's
    # own sweep candle would already have scrolled out of range by the
    # time its CHoCH confirmed, and the signal would never fire at all.
    # Bounded instead by the same time-based recency window already used
    # to decide which CHoCH events are worth considering in the first
    # place, converted to a bar count via each trigger's own candle
    # spacing — so it scales correctly regardless of candle timeframe
    # (a 1m chart and a 15m chart both get "up to ~6 hours back", not
    # "up to ~20 candles back").
    def _reversal_sweep_lookback(before_index: int) -> int:
        if before_index <= 0:
            return 0
        window_start_time = ltf[before_index].time - REVERSAL_RECENCY_WINDOW_MS
        i = before_index
        while i > 0 and ltf[i - 1].time >= window_start_time:
            i -= 1
        return before_index - i

    _rev_trigger: StructureEvent | None = None
    _rev_swept_label: str | None = None
    _rev_swept_price: float | None = None
    for _choch in reversed(_choch_events):  # newest qualifying CHoCH wins
        _want_high = _choch.direction == "bearish"  # a bearish reversal needs a buy-side (high) sweep just before it
        _rev_lookback = _reversal_sweep_lookback(_choch.index)
        for _lv in (lv for lv in _major_levels if lv["is_high"] == _want_high):
            if _level_swept(_lv["price"], _want_high, _choch.index, _rev_lookback) is not None:
                _rev_trigger, _rev_swept_label, _rev_swept_price = _choch, _lv["label"], _lv["price"]
                break
        if _rev_trigger is None:
            # Fall back to the strongest recent swing high/low when no
            # significant day/session level was available or swept.
            _sw = find_liquidity_sweep(ltf, ltf_swing_highs, ltf_swing_lows, _choch.index, _rev_lookback)
            _want_side = "buyside" if _want_high else "sellside"
            if _sw is not None and _sw.side == _want_side:
                _rev_trigger = _choch
                _rev_swept_label = "Recent swing high" if _want_high else "Recent swing low"
                _rev_swept_price = _sw.level
        if _rev_trigger is not None:
            break

    if _rev_trigger is None:
        _rev_flat_reason = (
            "Reversal reset — waiting for a genuinely new CHoCH before re-evaluating anything"
            if _reversal_was_reset else
            "No liquidity sweep + opposite CHoCH found yet — waiting for a genuine top/bottom-tick setup"
        )
        reversal_result = ReversalResult(
            signal="FLAT", direction=None, swept_level=None, swept_price=None,
            entry_zone=None, entry_zone_kind=None, stop_loss=None, target=None,
            reason=_rev_flat_reason,
        )
    else:
        _rev_dir = _rev_trigger.direction
        _rev_confluences: list[Confluence] = [
            Confluence(
                "Liquidity sweep of a significant level", True,
                f"Swept {_rev_swept_label} ({_rev_swept_price:.2f}) before the reversal",
            ),
            Confluence(
                "Reversal structure (CHoCH)", True,
                f"{_rev_dir} CHoCH on the entry timeframe at bar {_rev_trigger.index}",
            ),
            # Kill zone timing is what actually makes a swept-liquidity
            # reversal high-probability in ICT terms (the "Judas swing"
            # — manipulation right at a session open, then the real
            # move) — always checked for the reversal signal specifically
            # (unlike the main trend signal, where it's a user toggle),
            # since it's central to why a reversal setup is trustworthy
            # at all, not just one confluence among several.
            _kill_zone_confluence(kz),
        ]
        if news_required:
            _rev_confluences.append(_news_confluence(news_ok, news_note))

        _rev_merged = _merge_touch_events_to_callouts([_rev_trigger], _rev_dir)
        _rev_zone = _rev_merged["zone"]
        _rev_zone_kind = _rev_merged["zone_kind"]
        _rev_confluences.append(Confluence(
            "Entry zone (OB / FVG / OTE)",
            _rev_zone is not None,
            f"Price tapped into a {_rev_zone_kind} zone [{_rev_zone[0]:.2f}, {_rev_zone[1]:.2f}] off the reversal"
            if _rev_zone else f"Price {price:.2f} hasn't tapped into a reversal entry zone yet",
        ))

        _rev_entry_zone_met = _rev_zone is not None
        _rev_stop = _rev_target = None
        # Stop belongs just beyond the actual swept extreme — the
        # textbook ICT reversal stop placement, and tighter/more precise
        # than a generic swing-point fallback, since that exact price IS
        # the level that must not be revisited for the reversal thesis to
        # still hold.
        _rev_buffer = abs(price) * 0.0006
        if _rev_dir == "bullish":
            _rev_candidate_stop = round(_rev_swept_price - _rev_buffer, 2)
            _rev_dist = price - _rev_candidate_stop
        else:
            _rev_candidate_stop = round(_rev_swept_price + _rev_buffer, 2)
            _rev_dist = _rev_candidate_stop - price
        if _rev_dist > 0:
            _rev_stop = _rev_candidate_stop
            _rev_target = round(price + _rev_dist * risk_reward, 2) if _rev_dir == "bullish" \
                else round(price - _rev_dist * risk_reward, 2)

        _rev_all_met = all(c.met for c in _rev_confluences)
        _rev_signal: Signal = "FLAT"
        if _rev_all_met and _rev_stop is not None:
            _rev_signal = "BUY" if _rev_dir == "bullish" else "SELL"

        if _rev_signal != "FLAT":
            _rev_reason = f"Liquidity sweep + CHoCH reversal aligned — {_rev_signal} reversal entry"
        elif _rev_entry_zone_met and _rev_stop is not None:
            _rev_reason = "Reversal entry zone tapped, stop/target ready — waiting on: " + \
                ", ".join(c.name for c in _rev_confluences if not c.met)
        elif _rev_stop is not None:
            _rev_reason = "Reversal stop/target ready — waiting on: " + \
                ", ".join(c.name for c in _rev_confluences if not c.met)
        else:
            _rev_reason = "Computed stop landed on the wrong side of price — skipping reversal signal"

        reversal_result = ReversalResult(
            signal=_rev_signal, direction=_rev_dir,
            swept_level=_rev_swept_label, swept_price=round(_rev_swept_price, 2),
            entry_zone=_rev_zone, entry_zone_kind=_rev_zone_kind,
            stop_loss=_rev_stop, target=_rev_target,
            confluences=_rev_confluences, reason=_rev_reason,
            entry_zone_met=_rev_entry_zone_met, callouts=_rev_merged["callouts"],
        )

    confluences: list[Confluence] = []

    # 1. HTF bias
    htf_events = detect_structure(htf, swing_lookback)
    htf_bias = htf_events[-1].direction if htf_events else None
    if require_htf_bias:
        confluences.append(Confluence(
            "HTF bias",
            htf_bias is not None,
            f"{htf_minutes}m structure is {htf_bias or 'undefined (not enough history yet)'}",
        ))

    if htf_bias is None:
        if require_structure_shift:
            confluences.append(Confluence("LTF structure shift", False, "Skipped — no HTF bias yet"))
        if require_liquidity_sweep:
            confluences.append(Confluence("Liquidity sweep", False, "Skipped — no HTF bias yet"))
        if require_entry_zone:
            confluences.append(Confluence("Entry zone (OB / FVG / OTE)", False, "Skipped — no HTF bias yet"))
        if require_smt_divergence:
            confluences.append(Confluence("SMT divergence", False, "Skipped — no HTF bias yet"))
        if require_kill_zone:
            confluences.append(_kill_zone_confluence(kz))
        if news_required:
            confluences.append(_news_confluence(news_ok, news_note))
        return ICTResult("FLAT", price, htf_bias, None, None, None, None, kz, confluences,
                          "Waiting for higher-timeframe structure to establish a bias",
                          zones=_pending_structure_zones() + _smt_divergence_zones()
                          + _breaker_zones() + _liquidity_grab_zones(),
                          heatmap=liquidity_heatmap, reversal=reversal_result)

    # 2. LTF structure shift matching HTF bias
    # ltf_swing_highs/lows (strict) and ltf_liquidity_highs/lows (the
    # more inclusive pool used for liquidity sweep detection) were
    # already computed unconditionally above, alongside
    # all_liquidity_grabs. Order blocks and the fallback stop level key
    # off the stricter ltf_swing_highs/lows; only the sweep confluence
    # itself uses the looser pool, to widen what counts as sweepable
    # liquidity without loosening structure/order-block logic.
    # (ltf_events itself was already computed unconditionally above too,
    # shared with the reversal section.)
    matching = [e for e in ltf_events if e.direction == htf_bias]
    # "Reset checklist" (see checklist_reset_after docstring above) — a
    # trigger that existed before the reset is treated exactly like it
    # never happened, for every downstream confluence. Applied after the
    # direction filter so it can never block a genuinely fresh trigger
    # that only exists because the trend itself just changed.
    was_reset = False
    if checklist_reset_after is not None:
        _before_reset = matching
        matching = [e for e in matching if ltf[e.index].time > checklist_reset_after]
        was_reset = bool(_before_reset) and not matching
    trigger = matching[-1] if matching else None
    if require_structure_shift:
        confluences.append(Confluence(
            "LTF structure shift",
            trigger is not None,
            f"Latest {htf_bias} {trigger.type} on entry timeframe at bar {trigger.index}" if trigger
            else f"No {htf_bias} BOS/CHoCH on the entry timeframe yet",
        ))

    if trigger is None:
        if require_liquidity_sweep:
            confluences.append(Confluence("Liquidity sweep", False, "Skipped — no LTF trigger yet"))
        if require_entry_zone:
            confluences.append(Confluence("Entry zone (OB / FVG / OTE)", False, "Skipped — no LTF trigger yet"))
        if require_smt_divergence:
            confluences.append(Confluence("SMT divergence", False, "Skipped — no LTF trigger yet"))
        if require_kill_zone:
            confluences.append(_kill_zone_confluence(kz))
        if news_required:
            confluences.append(_news_confluence(news_ok, news_note))
        reason = (
            "Checklist reset — waiting for a genuinely new BOS/CHoCH before re-evaluating anything"
            if was_reset else
            f"HTF bias is {htf_bias} but entry timeframe hasn't confirmed a shift yet"
        )
        return ICTResult("FLAT", price, htf_bias, None, None, None, None, kz, confluences, reason,
                          zones=_pending_structure_zones() + _smt_divergence_zones()
                          + _breaker_zones() + _liquidity_grab_zones(),
                          heatmap=liquidity_heatmap, reversal=reversal_result)

    # 3. Liquidity sweep (manipulation leg) just before the trigger
    sweep_dir = htf_bias  # a bullish trigger should be preceded by a sellside (bullish) sweep, and vice versa
    sweep = find_liquidity_sweep(ltf, ltf_liquidity_highs, ltf_liquidity_lows, trigger.index, sweep_lookback)
    sweep_ok = sweep is not None and sweep.direction == sweep_dir
    if require_liquidity_sweep:
        confluences.append(Confluence(
            "Liquidity sweep",
            sweep_ok,
            f"Swept {sweep.side} liquidity at {sweep.level:.2f} (bar {sweep.index})"
            if sweep_ok else "No clean stop-hunt found before the structure shift — lower conviction",
        ))

    # 3b. SMT divergence against a correlated instrument — `smt_divergences`
    # was already computed above (unconditionally, alongside the pending
    # structure levels) so this just filters that same list by htf_bias
    # rather than re-scanning anything.
    if require_smt_divergence:
        if smt_raw:
            matching_divergences = [e for e in smt_divergences if e.direction == htf_bias]
            if matching_divergences:
                latest = matching_divergences[-1]
                smt_ok = True
                smt_detail = (
                    f"{len(matching_divergences)} {htf_bias} SMT divergence"
                    f"{'s' if len(matching_divergences) != 1 else ''} found — most recent at "
                    f"{latest.primary_price:.2f} vs correlated {latest.correlated_price:.2f}"
                )
            else:
                smt_ok = False
                smt_detail = f"No {htf_bias} SMT divergence found against the correlated instrument yet"
        else:
            smt_ok, smt_detail = False, "Correlated instrument data unavailable"
        confluences.append(Confluence("SMT divergence", smt_ok, smt_detail))

    # 4. Entry zone: breaker block, OB, unmitigated FVG, or OTE
    # (_tap_status/_zone_candidates_for_trigger/_merge_touch_events_to_callouts
    # are all defined earlier, unconditionally, alongside the breaker/
    # liquidity-grab zone helpers — shared with the reversal section
    # further below.)
    #
    # `trigger` is always the *newest* matching-direction structure
    # event — it keeps moving forward as the trend continues (every
    # further continuation break re-points it). The search below stays
    # bounded to *recent* triggers — searching the entire session/day
    # history meant that once some zone from hours (or a day) ago got
    # tapped and simply never invalidated, it would win forever, blocking
    # every new setup formed afterward from ever becoming the displayed
    # entry zone.
    RECENCY_WINDOW_MS = 6 * 60 * 60 * 1000  # 6 hours
    now_ms = ltf[-1].time
    recent_matching = [e for e in matching if (now_ms - ltf[e.index].time) <= RECENCY_WINDOW_MS]
    search_triggers = recent_matching or [trigger]

    _merged = _merge_touch_events_to_callouts(search_triggers, htf_bias)
    callouts = _merged["callouts"]
    zone_trigger = _merged["zone_trigger"] or trigger
    zone = _merged["zone"]
    zone_kind = _merged["zone_kind"]
    _get_candidates = _merged["get_candidates"]

    resolved = _get_candidates(zone_trigger)
    zone_leg_start = resolved["leg_start"]
    brk, brk_invalidated = resolved["brk"], resolved["brk_invalidated"]
    ob, ob_tapped, ob_invalidated = resolved["ob"], resolved["ob_tapped"], resolved["ob_invalidated"]
    fvgs, fvg_status = resolved["fvgs"], resolved["fvg_status"]
    ote, ote_tapped, ote_invalidated = resolved["ote"], resolved["ote_tapped"], resolved["ote_invalidated"]

    active_brk = zone_kind == "breaker block"
    active_ob = zone_kind == "order block"
    active_ote = zone_kind == "OTE (62-79% retracement)"
    active_fvg_index: int | None = None
    if zone_kind == "fair value gap" and zone is not None:
        active_fvg_index = next((g.index for g in fvgs if (g.bottom, g.top) == zone), None)

    # Always show the newest trigger's own candidates on the chart too,
    # even when an older trigger's zone is the one actually resolved
    # (because it was touched more recently) — otherwise a brand new
    # order block/FVG would stay invisible on screen until price
    # happened to tap it.
    latest = resolved if zone_trigger.index == trigger.index else _get_candidates(trigger)

    if require_entry_zone:
        confluences.append(Confluence(
            "Entry zone (OB / FVG / OTE)",
            zone is not None,
            f"Price tapped into a {zone_kind} zone [{zone[0]:.2f}, {zone[1]:.2f}] and hasn't invalidated it yet"
            if zone else f"Price {price:.2f} hasn't tapped into an order block, unmitigated FVG, or the OTE zone yet",
        ))

    # Structural zones the strategy actually looked at, for the chart
    # overlay — anchored to real candle timestamps and price levels, not
    # approximated. `active` marks the exact zone instance selected
    # above (by identity, not just kind — there can be several FVGs on
    # screen at once, so matching on kind alone was marking *every* FVG
    # of the right type as active instead of just the one actually in
    # play).
    def _zone(ztype: str, direction: Direction, top: float, bottom: float,
              from_time: int, to_time: int | None, label: str, active: bool = False,
              invalidated: bool = False) -> dict:
        return {
            "type": ztype, "direction": direction,
            "top": round(top, 2), "bottom": round(bottom, 2),
            "from_time": from_time, "to_time": to_time, "label": label,
            "active": active, "invalidated": invalidated,
        }

    zones: list[dict] = []
    _seen_ob_index: set[int] = set()
    _seen_fvg_index: set[int] = set()
    _seen_ote_trigger: set[int] = set()

    def _add_ob_zone(_ob, _invalidated: bool, _active: bool) -> None:
        if _ob is None or _ob.index in _seen_ob_index:
            return
        _seen_ob_index.add(_ob.index)
        zones.append(_zone("order_block", _ob.direction, _ob.high, _ob.low, ltf[_ob.index].time, None, "Order block", _active, _invalidated))

    def _add_fvg_zones(_fvgs, _status: dict, _active_index: int | None) -> None:
        for g in _fvgs:
            if g.index in _seen_fvg_index:
                continue
            _seen_fvg_index.add(g.index)
            g_tapped, g_invalidated = _status[g.index]
            zones.append(_zone("fvg", g.direction, g.top, g.bottom, ltf[max(0, g.index - 1)].time, None, "Fair value gap", g.index == _active_index, g_invalidated))

    def _add_ote_zone(_ote, _leg_start: int, _trig: StructureEvent, _invalidated: bool, _active: bool) -> None:
        if _ote is None or _trig.index in _seen_ote_trigger:
            return
        _seen_ote_trigger.add(_trig.index)
        zones.append(_zone("ote", htf_bias, _ote[1], _ote[0], ltf[_leg_start].time, ltf[_trig.index].time, "OTE 62-79% retracement", _active, _invalidated))

    # Always draw resolved's own zones first (this is the one actually
    # driving entry_zone/stop/target), then layer in the newest
    # trigger's candidates too if it's a different trigger — deduped by
    # identity so nothing is drawn twice.
    _add_ob_zone(ob, ob_invalidated, active_ob)
    _add_fvg_zones(fvgs, fvg_status, active_fvg_index)
    _add_ote_zone(ote, zone_leg_start, zone_trigger, ote_invalidated, active_ote)
    if latest is not resolved:
        _add_ob_zone(latest["ob"], latest["ob_invalidated"], False)
        _add_fvg_zones(latest["fvgs"], latest["fvg_status"], None)
        _add_ote_zone(latest["ote"], latest["leg_start"], latest["trigger"], latest["ote_invalidated"], False)
    zones.append({
        "type": "structure_break", "direction": htf_bias,
        "top": round(trigger.level, 2), "bottom": round(trigger.level, 2),
        "from_time": ltf[trigger.index].time, "to_time": None,
        "label": f"{trigger.type} level", "active": False, "invalidated": False,
    })
    # Every liquidity grab (buy-side and sell-side) across the whole
    # history is shown via _liquidity_grab_zones() below, with the one
    # actually gating this trigger's confluence (if any) marked active —
    # replaces the old single-zone-per-signal display.
    # Same treatment for breaker blocks via _breaker_zones() below.

    # 5. Kill zone timing
    if require_kill_zone:
        confluences.append(_kill_zone_confluence(kz))

    # 6. News blackout
    if news_required:
        confluences.append(_news_confluence(news_ok, news_note))

    # Every confluence still in this list at this point is one the user
    # has left enabled — all of them are required for a "sniper" grade
    # signal, deliberately strict. (Confluences the user disabled were
    # never appended above, so they're neither shown nor required.)
    all_required_met = all(c.met for c in confluences) if confluences else False

    # Where should the stop go? Prefer the swept liquidity level, then
    # the order block, then the nearest recent swing point behind the
    # entry — and refuse to fire rather than fall back to `price` itself,
    # which used to silently collapse the stop to a few points away (a
    # near-worthless, essentially meaningless stop/target) whenever
    # neither a sweep nor an order block was available, e.g. once
    # "Liquidity sweep" became an optional confluence a user could
    # disable.
    def _fallback_swing_level(direction: Direction) -> float | None:
        pool = ltf_swing_lows if direction == "bullish" else ltf_swing_highs
        idxs = sorted(i for i in pool if i < trigger.index)
        if not idxs:
            return None
        i = idxs[-1]
        return ltf[i].low if direction == "bullish" else ltf[i].high

    # Stop/target are computed as soon as a level is structurally
    # available (swept liquidity, an order block, or a recent swing
    # point) — deliberately *not* gated on every optional confluence
    # (kill zone, news, SMT, even the entry-zone tap itself if the user
    # disabled that requirement) being met too. Those extra confluences
    # still gate the strict `signal` (BUY/SELL) below, since that's what
    # fires the "ready" chime and counts as a trade in the backtester.
    # But the trader watching a setup develop wants to know "if I take
    # this, where's my stop/target" as soon as there's a real level to
    # work from — not only once every optional filter also lines up —
    # so the frontend's "Confirm position" button can appear, and show
    # real numbers, off of `entry_zone_met` well before `signal` goes
    # live.
    # The stop belongs just beyond whatever structure the trade is
    # actually being taken from. The entry zone itself (the OB/FVG/OTE
    # the trade is anchored to) is the tightest, most relevant level —
    # "just outside the zone" — so it takes priority. Only when there's
    # no tapped zone to work from does this fall back to the swept
    # liquidity level, the order block, or the nearest recent swing
    # point, in that order of preference.
    #
    # Previously this combined sweep/OB/swing levels with min()/max(),
    # which always picked the *farthest* of the available candidates —
    # producing a needlessly wide, "way too large" stop even when the
    # zone itself was right there to stop just beyond. Priority-first
    # selection (first available, not most extreme) fixes that.
    stop_loss = target = None
    no_stop_reason: str | None = None
    buffer = abs(price) * 0.0006  # small buffer beyond the chosen level

    zone_level = None
    if zone is not None:
        zone_bottom, zone_top = zone
        zone_level = zone_bottom if htf_bias == "bullish" else zone_top

    sweep_level = sweep.level if sweep_ok else None
    ob_level = (ob.low if htf_bias == "bullish" else ob.high) if ob is not None else None
    fallback_level = _fallback_swing_level(htf_bias)

    base_level = next(
        (lvl for lvl in (zone_level, sweep_level, ob_level, fallback_level) if lvl is not None),
        None,
    )

    if base_level is None:
        no_stop_reason = "No valid stop-loss reference nearby (no tapped zone, sweep, order block, or recent swing point)"
    else:
        if htf_bias == "bullish":
            candidate_stop = round(base_level - buffer, 2)
            candidate_dist = price - candidate_stop
        else:
            candidate_stop = round(base_level + buffer, 2)
            candidate_dist = candidate_stop - price
        if candidate_dist <= 0:
            no_stop_reason = "Computed stop landed on the wrong side of price — skipping signal"
        else:
            stop_loss = candidate_stop
            target = round(price + candidate_dist * risk_reward, 2) if htf_bias == "bullish" \
                else round(price - candidate_dist * risk_reward, 2)

    entry_zone_met = zone is not None
    signal: Signal = "FLAT"
    if all_required_met and stop_loss is not None:
        signal = "BUY" if htf_bias == "bullish" else "SELL"

    if signal != "FLAT":
        reason = f"All confluences aligned — {signal} sniper entry"
    elif entry_zone_met and stop_loss is not None and not all_required_met:
        reason = "Entry zone tapped, stop/target ready — waiting on: " + \
            ", ".join(c.name for c in confluences if not c.met)
    elif stop_loss is not None and not all_required_met:
        reason = "Stop/target ready — waiting on: " + \
            ", ".join(c.name for c in confluences if not c.met)
    elif no_stop_reason:
        reason = no_stop_reason
    else:
        reason = "Missing: " + ", ".join(c.name for c in confluences if not c.met)

    return ICTResult(
        signal=signal,
        price=round(price, 2),
        htf_bias=htf_bias,
        entry_zone=zone,
        entry_zone_kind=zone_kind,
        stop_loss=stop_loss,
        target=target,
        kill_zone=kz,
        confluences=confluences,
        reason=reason,
        trigger_index=trigger.index,
        zones=zones + _pending_structure_zones() + _smt_divergence_zones()
        + _breaker_zones(brk.index if active_brk else None)
        + _liquidity_grab_zones(sweep.index if sweep_ok else None),
        entry_zone_met=entry_zone_met,
        heatmap=liquidity_heatmap,
        callouts=callouts,
        reversal=reversal_result,
    )


def _kill_zone_confluence(kz: str | None) -> Confluence:
    return Confluence("Kill zone timing", kz is not None, f"Current session: {kz or 'outside London/NY/close kill zones'}")


def _news_confluence(news_ok: bool, news_note: str) -> Confluence:
    return Confluence("No high-impact news nearby", news_ok, news_note or ("clear" if news_ok else "blackout active"))
