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
from datetime import datetime, timezone
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


def detect_structure(candles: list[Candle], lookback: int = 3) -> list[StructureEvent]:
    """Walk forward through candles, tracking the most recent unbroken
    swing high/low, and flag BOS (continuation) or CHoCH (reversal) each
    time price closes through one."""
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

    return events


# ----------------------------------------------------------------------
# Liquidity sweeps
# ----------------------------------------------------------------------
@dataclass
class SweepEvent:
    index: int
    direction: Direction  # 'bullish' = sellside liquidity swept (grab below a low) -> bullish reversal
    level: float


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
                best = SweepEvent(i, "bearish", hi_price)  # swept buyside liquidity -> favors shorts
        for lo_idx, lo_price in recent_lows:
            if lo_idx < i and c.low < lo_price and c.close > lo_price:
                best = SweepEvent(i, "bullish", lo_price)  # swept sellside liquidity -> favors longs
    return best


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


def detect_smt_divergence(
    primary: list[Candle], correlated: list[Candle], direction: Direction, swing_lookback: int
) -> tuple[bool, str]:
    """Classic ICT SMT: compare the two most recent comparable swing
    points between this instrument and a correlated one (e.g. NAS100 vs
    ES/S&P 500). For a bullish read, the primary instrument should print
    a new/lower swing low while the correlated instrument fails to
    follow (prints a *higher* low instead) — one market showing relative
    strength the other doesn't, a classic smart-money "tell". For a
    bearish read, the mirror: a new/higher swing high on the primary
    not confirmed by the correlated instrument.

    Swings are matched between the two instruments by real timestamp,
    not by position in each series' own swing-point list. The primary
    and correlated candle series are fetched independently (different
    instrument, sometimes a different data source/cache path) and
    aren't guaranteed to carry the same number of bars or the same
    fractal swing count over the same window — "the Nth-from-last swing"
    on one instrument can land on a completely different moment in time
    than "the Nth-from-last swing" on the other. Comparing those isn't a
    real SMT read at all, just two unrelated numbers that happen to both
    be "recent," which is what made this read as inaccurate.

    Returns (met, detail) rather than raising — a genuinely simplified,
    codified reading of the concept (see module docstring), not a
    guarantee of the "real" institutional divergence."""
    min_needed = max(30, swing_lookback * 6)
    if len(primary) < min_needed or len(correlated) < min_needed:
        return False, "Not enough correlated-instrument history yet"

    p_highs, p_lows = find_swing_points(primary, swing_lookback)
    c_highs, c_lows = find_swing_points(correlated, swing_lookback)

    # Two correlated instruments' bars won't line up to the millisecond
    # (feed lag, slightly different candle open times), but they should
    # agree on the same bar interval — a few bars' worth of slack is
    # "the same swing," anything further apart is a different move.
    bar_ms = correlated[-1].time - correlated[-2].time if len(correlated) > 1 else 60_000
    tolerance_ms = max(abs(bar_ms) * 6, 1)

    if direction == "bullish":
        p_idx = sorted(p_lows)
        if len(p_idx) < 2:
            return False, "Not enough swing lows on the primary instrument yet"
        p_last_i, p_prev_i = p_idx[-1], p_idx[-2]
        c_last_i = _closest_swing_index(correlated, c_lows, primary[p_last_i].time, tolerance_ms)
        c_prev_i = _closest_swing_index(correlated, c_lows, primary[p_prev_i].time, tolerance_ms)
        if c_last_i is None or c_prev_i is None or c_last_i == c_prev_i:
            return False, "No comparable swing lows on the correlated instrument at the same times"
        p_last, p_prev = primary[p_last_i].low, primary[p_prev_i].low
        c_last, c_prev = correlated[c_last_i].low, correlated[c_prev_i].low
        met = p_last < p_prev and c_last > c_prev
        if met:
            return True, "Primary printed a lower low the correlated instrument didn't confirm (bullish divergence)"
        return False, "No bullish divergence — both instruments' recent lows agree"
    else:
        p_idx = sorted(p_highs)
        if len(p_idx) < 2:
            return False, "Not enough swing highs on the primary instrument yet"
        p_last_i, p_prev_i = p_idx[-1], p_idx[-2]
        c_last_i = _closest_swing_index(correlated, c_highs, primary[p_last_i].time, tolerance_ms)
        c_prev_i = _closest_swing_index(correlated, c_highs, primary[p_prev_i].time, tolerance_ms)
        if c_last_i is None or c_prev_i is None or c_last_i == c_prev_i:
            return False, "No comparable swing highs on the correlated instrument at the same times"
        p_last, p_prev = primary[p_last_i].high, primary[p_prev_i].high
        c_last, c_prev = correlated[c_last_i].high, correlated[c_prev_i].high
        met = p_last > p_prev and c_last < c_prev
        if met:
            return True, "Primary printed a higher high the correlated instrument didn't confirm (bearish divergence)"
        return False, "No bearish divergence — both instruments' recent highs agree"


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
) -> ICTResult:
    """
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
                          "Waiting for higher-timeframe structure to establish a bias")

    # 2. LTF structure shift matching HTF bias
    ltf_swing_highs, ltf_swing_lows = find_swing_points(ltf, swing_lookback)
    # A separate, more inclusive pool used only for liquidity sweep
    # detection (see find_liquidity_pools) — order blocks and the
    # fallback stop level still key off the stricter ltf_swing_highs/
    # lows above, so this only widens what counts as sweepable
    # liquidity, without loosening structure/order-block logic.
    ltf_liquidity_highs, ltf_liquidity_lows = find_liquidity_pools(ltf, swing_lookback)
    ltf_events = detect_structure(ltf, swing_lookback)
    matching = [e for e in ltf_events if e.direction == htf_bias]
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
        return ICTResult("FLAT", price, htf_bias, None, None, None, None, kz, confluences,
                          f"HTF bias is {htf_bias} but entry timeframe hasn't confirmed a shift yet")

    # 3. Liquidity sweep (manipulation leg) just before the trigger
    sweep_dir = htf_bias  # a bullish trigger should be preceded by a sellside (bullish) sweep, and vice versa
    sweep = find_liquidity_sweep(ltf, ltf_liquidity_highs, ltf_liquidity_lows, trigger.index, sweep_lookback)
    sweep_ok = sweep is not None and sweep.direction == sweep_dir
    if require_liquidity_sweep:
        confluences.append(Confluence(
            "Liquidity sweep",
            sweep_ok,
            f"Swept {'sellside' if sweep_dir == 'bullish' else 'buyside'} liquidity at {sweep.level:.2f} (bar {sweep.index})"
            if sweep_ok else "No clean stop-hunt found before the structure shift — lower conviction",
        ))

    # 3b. SMT divergence against a correlated instrument
    if require_smt_divergence:
        if smt_raw:
            correlated = normalize_candles(smt_raw)
            # Read SMT on its own configured timeframe when the caller
            # supplied one, rather than always reusing the fast entry
            # timeframe — see the smt_primary_raw docstring above.
            smt_primary = normalize_candles(smt_primary_raw) if smt_primary_raw else ltf
            smt_ok, smt_detail = detect_smt_divergence(smt_primary, correlated, htf_bias, swing_lookback)
        else:
            smt_ok, smt_detail = False, "Correlated instrument data unavailable"
        confluences.append(Confluence("SMT divergence", smt_ok, smt_detail))

    # A zone counts as the live entry zone once price has *tapped* into
    # it, and it stays that way even after price ticks back out the way
    # it came — a retest-and-bounce off support/resistance is still a
    # valid, working setup. It only stops counting once price actually
    # *invalidates* the zone by closing through the far side of it
    # (below a bullish zone's bottom, or above a bearish zone's top).
    #
    # "Tapped" requires *displacement first* — price must actually close
    # beyond the zone (confirming the move away from it) before any
    # later dip back into it counts as a real retracement. Without this,
    # the candle(s) immediately following the zone's own formation —
    # which are still part of the same impulsive break, not a pullback —
    # can easily wick back across the zone's near edge while the move is
    # still developing, and got counted as "tapped" almost the instant
    # the zone appeared. The displacement close also has to clear the
    # zone by a real margin (a fraction of the zone's own height), not
    # just barely poke past the edge — a close that only just clears the
    # boundary is still well within normal wick/noise range of that same
    # edge, and the very next candle grazing back to touch it isn't a
    # retracement, it's the same handful of points of chop the boundary
    # itself sits in.
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

    # 4. Entry zone: OB, unmitigated FVG, or OTE
    #
    # `trigger` is always the *newest* matching-direction structure
    # event — it keeps moving forward as the trend continues (every
    # further continuation break re-points it). Naively searching for
    # candidate zones relative only to that single newest trigger means
    # a fresh continuation break anywhere down the trend would silently
    # swap in brand-new, not-yet-tapped candidates and discard ones that
    # had *already been genuinely tapped* — the checklist would report a
    # real tap, then flicker back to "not tapped" moments later with no
    # actual invalidation, which is what this was fixing.
    #
    # Instead, resolve the full candidate set (OB, then unmitigated FVG,
    # then OTE) independently for every matching-direction trigger in
    # the current trend (bounded automatically — `matching` resets the
    # moment htf_bias itself flips), searching from the most recent
    # trigger backward and stopping at the first one that already has a
    # tapped-and-not-invalidated zone. Only when nothing in the whole
    # run qualifies yet does this fall back to the freshest trigger's
    # (likely still-untapped) candidates, for "not tapped yet" display.
    def _zone_candidates_for_trigger(trig: StructureEvent) -> dict:
        _sweep = find_liquidity_sweep(ltf, ltf_liquidity_highs, ltf_liquidity_lows, trig.index, sweep_lookback)
        _sweep_ok = _sweep is not None and _sweep.direction == htf_bias
        _leg_start = _sweep.index if _sweep_ok else (trig.swept_index or max(0, trig.index - sweep_lookback))

        _ob = find_order_block(ltf, trig.index, htf_bias)
        _ob_tapped = _ob_invalidated = False
        if _ob is not None:
            _ob_tapped, _ob_invalidated = _tap_status(_ob.index + 1, _ob.high, _ob.low, htf_bias)

        # Search for unmitigated FVGs all the way through to the most recent
        # candle, not just the first few bars right after the trigger fired.
        # Bounding this to trig.index + 5 meant any imbalance that formed
        # later in the *same still-active* trend (no new BOS/CHoCH yet,
        # htf_bias unchanged) was structurally invisible to the strategy —
        # it could never appear as a candidate zone, let alone become the
        # live target, no matter how fresh or clearly price was filling it.
        # A trend often prints several fresh FVGs as it extends; all of
        # them are legitimate re-entry magnets for the current structure,
        # not just whichever one happened to form in the first 5 bars.
        _fvgs = [g for g in find_fvgs(ltf, _leg_start, len(ltf) - 1, htf_bias) if not g.mitigated]
        _fvg_status = {g.index: _tap_status(g.index + 2, g.top, g.bottom, htf_bias) for g in _fvgs}

        _ote = ote_zone(ltf, _leg_start, trig.index, htf_bias)
        _ote_tapped = _ote_invalidated = False
        if _ote is not None:
            _ote_tapped, _ote_invalidated = _tap_status(trig.index + 1, _ote[1], _ote[0], htf_bias)

        _zone = None
        _zone_kind = None
        _active_ob = False
        _active_fvg_index: int | None = None
        _active_ote = False
        if _ob is not None and _ob_tapped and not _ob_invalidated:
            _zone, _zone_kind = (_ob.low, _ob.high), "order block"
            _active_ob = True
        if _zone is None and _fvgs:
            _live = [g for g in _fvgs if _fvg_status[g.index][0] and not _fvg_status[g.index][1]]
            if _live:
                _nearest = min(_live, key=lambda g: abs(price - (g.top + g.bottom) / 2))
                _zone, _zone_kind = (_nearest.bottom, _nearest.top), "fair value gap"
                _active_fvg_index = _nearest.index
        if _zone is None and _ote is not None and _ote_tapped and not _ote_invalidated:
            _zone, _zone_kind = _ote, "OTE (62-79% retracement)"
            _active_ote = True

        return {
            "trigger": trig, "zone": _zone, "zone_kind": _zone_kind, "leg_start": _leg_start,
            "ob": _ob, "ob_tapped": _ob_tapped, "ob_invalidated": _ob_invalidated, "active_ob": _active_ob,
            "fvgs": _fvgs, "fvg_status": _fvg_status, "active_fvg_index": _active_fvg_index,
            "ote": _ote, "ote_tapped": _ote_tapped, "ote_invalidated": _ote_invalidated, "active_ote": _active_ote,
        }

    # The sticky search above must stay bounded to *recent* triggers —
    # searching the entire session/day history meant that once some
    # zone from hours (or a day) ago got tapped and simply never
    # invalidated (easy: price just never came back anywhere near it),
    # it would win this search forever, blocking every new setup that
    # formed afterward from ever becoming the displayed entry zone. That
    # traded the original "flickers off" bug for an equally bad
    # "permanently stuck on a stale zone from yesterday" one. Limiting
    # the search to triggers within the last few hours keeps the
    # short-lived stability this was meant to fix (a tap surviving the
    # next continuation break, seconds or minutes later) without
    # letting a setup linger indefinitely once the market has clearly
    # moved on to something new.
    RECENCY_WINDOW_MS = 6 * 60 * 60 * 1000  # 6 hours
    now_ms = ltf[-1].time
    recent_matching = [e for e in matching if (now_ms - ltf[e.index].time) <= RECENCY_WINDOW_MS]
    search_triggers = recent_matching or [trigger]

    resolved = None
    for _trig in reversed(search_triggers):
        _r = _zone_candidates_for_trigger(_trig)
        if _r["zone"] is not None:
            resolved = _r
            break
    if resolved is None:
        resolved = _zone_candidates_for_trigger(trigger)

    zone_trigger = resolved["trigger"]
    zone = resolved["zone"]
    zone_kind = resolved["zone_kind"]
    zone_leg_start = resolved["leg_start"]
    ob, ob_tapped, ob_invalidated, active_ob = resolved["ob"], resolved["ob_tapped"], resolved["ob_invalidated"], resolved["active_ob"]
    fvgs, fvg_status, active_fvg_index = resolved["fvgs"], resolved["fvg_status"], resolved["active_fvg_index"]
    ote, ote_tapped, ote_invalidated, active_ote = resolved["ote"], resolved["ote_tapped"], resolved["ote_invalidated"], resolved["active_ote"]

    # `resolved` deliberately stays sticky on whichever trigger (up to
    # RECENCY_WINDOW_MS old) still has a tapped-and-not-invalidated zone,
    # so the entry decision itself doesn't flap. But that meant the chart
    # overlay — which only ever drew resolved's own ob/fvgs/ote — was
    # bottlenecked on that same stickiness: a brand new order block/FVG
    # forming right after the newest structure break simply never
    # appeared on screen until price happened to tap it, while whatever
    # resolved was still latched onto (possibly hours old) kept being
    # the only thing drawn. The strategy's *data* was already refreshing
    # every poll — it was the display that looked stuck on outdated
    # zones and slow to show new ones. Computing the newest trigger's
    # own candidates separately (reusing resolved's if it already is the
    # newest trigger) lets the chart show fresh structure immediately,
    # without touching what actually drives the entry zone/stop/target.
    latest = resolved if zone_trigger.index == trigger.index else _zone_candidates_for_trigger(trigger)

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
    if sweep_ok:
        zones.append({
            "type": "liquidity_sweep", "direction": sweep.direction,
            "top": round(sweep.level, 2), "bottom": round(sweep.level, 2), "invalidated": False,
            "from_time": ltf[sweep.index].time, "to_time": ltf[trigger.index].time,
            "label": "Swept liquidity", "active": False,
        })

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
        zones=zones,
        entry_zone_met=entry_zone_met,
    )


def _kill_zone_confluence(kz: str | None) -> Confluence:
    return Confluence("Kill zone timing", kz is not None, f"Current session: {kz or 'outside London/NY/close kill zones'}")


def _news_confluence(news_ok: bool, news_note: str) -> Confluence:
    return Confluence("No high-impact news nearby", news_ok, news_note or ("clear" if news_ok else "blackout active"))
