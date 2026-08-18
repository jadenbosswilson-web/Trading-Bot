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

    Returns (met, detail) rather than raising — a genuinely simplified,
    codified reading of the concept (see module docstring), not a
    guarantee of the "real" institutional divergence."""
    min_needed = max(30, swing_lookback * 6)
    if len(correlated) < min_needed:
        return False, "Not enough correlated-instrument history yet"

    p_highs, p_lows = find_swing_points(primary, swing_lookback)
    c_highs, c_lows = find_swing_points(correlated, swing_lookback)

    if direction == "bullish":
        p_idx, c_idx = sorted(p_lows), sorted(c_lows)
        if len(p_idx) < 2 or len(c_idx) < 2:
            return False, "Not enough swing lows on one side to compare yet"
        p_last, p_prev = primary[p_idx[-1]].low, primary[p_idx[-2]].low
        c_last, c_prev = correlated[c_idx[-1]].low, correlated[c_idx[-2]].low
        met = p_last < p_prev and c_last > c_prev
        if met:
            return True, "Primary printed a lower low the correlated instrument didn't confirm (bullish divergence)"
        return False, "No bullish divergence — both instruments' recent lows agree"
    else:
        p_idx, c_idx = sorted(p_highs), sorted(c_highs)
        if len(p_idx) < 2 or len(c_idx) < 2:
            return False, "Not enough swing highs on one side to compare yet"
        p_last, p_prev = primary[p_idx[-1]].high, primary[p_idx[-2]].high
        c_last, c_prev = correlated[c_idx[-1]].high, correlated[c_idx[-2]].high
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
    sweep = find_liquidity_sweep(ltf, ltf_swing_highs, ltf_swing_lows, trigger.index, sweep_lookback)
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
            smt_ok, smt_detail = detect_smt_divergence(ltf, correlated, htf_bias, swing_lookback)
        else:
            smt_ok, smt_detail = False, "Correlated instrument data unavailable"
        confluences.append(Confluence("SMT divergence", smt_ok, smt_detail))

    # 4. Entry zone: OB, unmitigated FVG, or OTE
    ob = find_order_block(ltf, trigger.index, htf_bias)
    leg_start = sweep.index if sweep_ok else trigger.swept_index or max(0, trigger.index - sweep_lookback)
    fvgs = [g for g in find_fvgs(ltf, leg_start, min(len(ltf) - 1, trigger.index + 5), htf_bias) if not g.mitigated]
    ote = ote_zone(ltf, leg_start, trigger.index, htf_bias)

    # A zone counts as the live entry zone once price has *tapped* into
    # it, and it stays that way even after price ticks back out the way
    # it came — a retest-and-bounce off support/resistance is still a
    # valid, working setup. It only stops counting once price actually
    # *invalidates* the zone by closing through the far side of it
    # (below a bullish zone's bottom, or above a bearish zone's top).
    # Previously this was re-checked every call against only the
    # instantaneous latest close, so the checklist/chart would flip
    # "met" -> "unmet" the moment price ticked a fraction outside the
    # zone, even with no real invalidation — this is what looked like a
    # zone "disappearing" as soon as it was left.
    def _tap_status(start_index: int, top: float, bottom: float, direction: Direction) -> tuple[bool, bool]:
        tapped = False
        invalidated = False
        for c in ltf[max(0, start_index):]:
            if direction == "bullish":
                if c.low <= top:
                    tapped = True
                if tapped and c.close < bottom:
                    invalidated = True
            else:
                if c.high >= bottom:
                    tapped = True
                if tapped and c.close > top:
                    invalidated = True
        return tapped, invalidated

    # Compute tapped/invalidated for *every* candidate zone up front —
    # not just whichever one ends up selected — so the invalidated flag
    # below reflects each zone's own real state (used by the frontend to
    # fire a one-shot "zone invalidated" sound the instant one actually
    # breaks, not just whenever a *different* zone becomes the priority
    # pick).
    ob_tapped = ob_invalidated = False
    if ob is not None:
        ob_tapped, ob_invalidated = _tap_status(ob.index + 1, ob.high, ob.low, htf_bias)

    fvg_status: dict[int, tuple[bool, bool]] = {
        g.index: _tap_status(g.index + 2, g.top, g.bottom, htf_bias) for g in fvgs
    }

    ote_tapped = ote_invalidated = False
    if ote is not None:
        ote_tapped, ote_invalidated = _tap_status(trigger.index + 1, ote[1], ote[0], htf_bias)

    zone = None
    zone_kind = None
    active_ob = False
    active_fvg_index: int | None = None
    active_ote = False

    if ob is not None and ob_tapped and not ob_invalidated:
        zone, zone_kind = (ob.low, ob.high), "order block"
        active_ob = True

    if zone is None and fvgs:
        live = [g for g in fvgs if fvg_status[g.index][0] and not fvg_status[g.index][1]]
        if live:
            nearest = min(live, key=lambda g: abs(price - (g.top + g.bottom) / 2))
            zone, zone_kind = (nearest.bottom, nearest.top), "fair value gap"
            active_fvg_index = nearest.index

    if zone is None and ote is not None and ote_tapped and not ote_invalidated:
        zone, zone_kind = ote, "OTE (62-79% retracement)"
        active_ote = True

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
    if ob is not None:
        zones.append(_zone("order_block", ob.direction, ob.high, ob.low, ltf[ob.index].time, None, "Order block", active_ob, ob_invalidated))
    for g in fvgs:
        g_tapped, g_invalidated = fvg_status[g.index]
        zones.append(_zone("fvg", g.direction, g.top, g.bottom, ltf[max(0, g.index - 1)].time, None, "Fair value gap", g.index == active_fvg_index, g_invalidated))
    if ote is not None:
        zones.append(_zone("ote", htf_bias, ote[1], ote[0], ltf[leg_start].time, ltf[trigger.index].time, "OTE 62-79% retracement", active_ote, ote_invalidated))
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

    signal: Signal = "FLAT"
    stop_loss = target = None
    no_stop_reason: str | None = None
    if all_required_met:
        sweep_level = sweep.level if sweep_ok else None
        ob_level = (ob.low if htf_bias == "bullish" else ob.high) if ob is not None else None
        fallback_level = _fallback_swing_level(htf_bias)
        levels = [lvl for lvl in (sweep_level, ob_level, fallback_level) if lvl is not None]
        buffer = abs(price) * 0.0006  # small buffer beyond the chosen level
        if not levels:
            no_stop_reason = "No valid stop-loss reference nearby (no sweep, order block, or recent swing point)"
        else:
            base_level = min(levels) if htf_bias == "bullish" else max(levels)
            if htf_bias == "bullish":
                candidate_stop = round(base_level - buffer, 2)
                candidate_dist = price - candidate_stop
            else:
                candidate_stop = round(base_level + buffer, 2)
                candidate_dist = candidate_stop - price
            if candidate_dist <= 0:
                no_stop_reason = "Computed stop landed on the wrong side of price — skipping signal"
            else:
                signal = "BUY" if htf_bias == "bullish" else "SELL"
                stop_loss = candidate_stop
                target = round(price + candidate_dist * risk_reward, 2) if signal == "BUY" \
                    else round(price - candidate_dist * risk_reward, 2)

    if signal != "FLAT":
        reason = f"All confluences aligned — {signal} sniper entry"
    elif all_required_met and no_stop_reason:
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
    )


def _kill_zone_confluence(kz: str | None) -> Confluence:
    return Confluence("Kill zone timing", kz is not None, f"Current session: {kz or 'outside London/NY/close kill zones'}")


def _news_confluence(news_ok: bool, news_note: str) -> Confluence:
    return Confluence("No high-impact news nearby", news_ok, news_note or ("clear" if news_ok else "blackout active"))
