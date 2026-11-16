"""
Economic calendar / high-impact news awareness.

Source: the public JSON feed that powers the ForexFactory calendar widget
(https://nfs.faireconomy.media/ff_calendar_thisweek.json). This is an
unofficial, publicly-served endpoint with no API key or auth — it can
change shape or go away without notice, so treat it as best-effort. If
it fails, this module fails *closed* by default (treats news status as
unknown/blocking) rather than silently assuming it's safe to trade —
see FAIL_OPEN_ON_NEWS_ERROR in config.py if you'd rather it fail open.

Only a lightweight in-memory cache is used (TTL-based) — this app is a
single local process, no persistence needed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger("news")

try:
    from zoneinfo import ZoneInfo
    NY_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - fallback if tzdata isn't installed
    logger.warning("zoneinfo/tzdata unavailable — 'today' will be judged in UTC, not Eastern time")
    NY_TZ = None

FEED_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

IMPACT_RANK = {"Low": 0, "Medium": 1, "High": 2, "Holiday": -1}


@dataclass
class NewsEvent:
    title: str
    country: str
    impact: str
    time: datetime  # timezone-aware


class NewsCalendar:
    def __init__(self, cache_ttl_seconds: int = 900, timeout: float = 10.0):
        self.cache_ttl_seconds = cache_ttl_seconds
        self._client = httpx.Client(timeout=timeout)
        self._cache: list[NewsEvent] | None = None
        self._cached_at: float = 0.0
        self._last_error: str | None = None

    def _fetch(self) -> list[NewsEvent]:
        events: list[NewsEvent] = []
        errors = []
        for url in FEED_URLS:
            try:
                resp = self._client.get(url)
                resp.raise_for_status()
                for row in resp.json():
                    try:
                        events.append(NewsEvent(
                            title=row["title"],
                            country=row["country"],
                            impact=row["impact"],
                            time=datetime.fromisoformat(row["date"]),
                        ))
                    except Exception as e:
                        logger.debug("Skipping malformed calendar row %s: %s", row, e)
            except Exception as e:
                errors.append(f"{url}: {e}")
        if not events and errors:
            raise RuntimeError("; ".join(errors))
        return events

    def get_events(self) -> list[NewsEvent]:
        now = time.time()
        if self._cache is not None and (now - self._cached_at) < self.cache_ttl_seconds:
            return self._cache
        try:
            self._cache = self._fetch()
            self._cached_at = now
            self._last_error = None
        except Exception as e:
            self._last_error = str(e)
            logger.warning("Failed to refresh economic calendar: %s", e)
            if self._cache is None:
                raise
        return self._cache

    def upcoming(self, hours_ahead: int = 24, min_impact: str = "Medium", currencies: list[str] | None = None) -> list[NewsEvent]:
        events = self.get_events()
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours_ahead)
        min_rank = IMPACT_RANK.get(min_impact, 1)
        out = [
            e for e in events
            if now <= e.time <= horizon
            and IMPACT_RANK.get(e.impact, -1) >= min_rank
            and (currencies is None or e.country in currencies)
        ]
        out.sort(key=lambda e: e.time)
        return out

    def events_for_day(
        self, day: date | None = None, min_impact: str = "Medium", currencies: list[str] | None = None
    ) -> list[NewsEvent]:
        """All events on a given Eastern-time calendar day (default:
        today in America/New_York) at or above min_impact — i.e. the
        ForexFactory 'orange and red folder' events for that 24h day,
        regardless of what time it currently is. Unlike upcoming(), this
        includes events earlier in the day that have already passed, and
        isn't a rolling window."""
        events = self.get_events()
        tz = NY_TZ or timezone.utc
        target_day = day or datetime.now(tz).date()
        min_rank = IMPACT_RANK.get(min_impact, 1)
        out = [
            e for e in events
            if e.time.astimezone(tz).date() == target_day
            and IMPACT_RANK.get(e.impact, -1) >= min_rank
            and (currencies is None or e.country in currencies)
        ]
        out.sort(key=lambda e: e.time)
        return out

    def blackout_status(
        self,
        buffer_before_min: int = 15,
        buffer_after_min: int = 15,
        min_impact: str = "High",
        currencies: list[str] | None = None,
        fail_open: bool = False,
    ) -> tuple[bool, str]:
        """Returns (news_ok, note). news_ok=False means don't trade right now."""
        try:
            events = self.get_events()
        except Exception as e:
            note = f"Could not fetch economic calendar ({e})"
            return (fail_open, note if not fail_open else note + " — failing open per config")

        now = datetime.now(timezone.utc)
        min_rank = IMPACT_RANK.get(min_impact, 2)
        for e in events:
            if currencies is not None and e.country not in currencies:
                continue
            if IMPACT_RANK.get(e.impact, -1) < min_rank:
                continue
            window_start = e.time - timedelta(minutes=buffer_before_min)
            window_end = e.time + timedelta(minutes=buffer_after_min)
            if window_start <= now <= window_end:
                return (False, f"Inside news blackout: {e.title} ({e.country}, {e.impact}) at {e.time.strftime('%H:%M %Z')}")

        return (True, "clear")


_calendar: NewsCalendar | None = None


def get_calendar() -> NewsCalendar:
    global _calendar
    if _calendar is None:
        _calendar = NewsCalendar()
    return _calendar
