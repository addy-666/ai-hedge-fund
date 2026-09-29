"""Economic calendar for the news gate (roadmap 5.7b, docs/03 §4 gate 5).

The Guardian EA exports upcoming events hourly as CSV (``time_utc,currency,impact,event``, times
``YYYY-MM-DD HH:MM`` in UTC). ``NewsCalendar`` answers: is there an event of the given impact for these
currencies within [now − after, now + before] (the blackout), and how many minutes to the next / since the
last one (the ``ctx.minutes_*_high_impact_news`` features). Pure: text in, answers out.
"""

from __future__ import annotations

import csv
import io
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class CalendarError(ValueError):
    pass


@dataclass(frozen=True)
class NewsEvent:
    time: datetime
    currency: str
    impact: str
    title: str


def parse_calendar(text: str) -> list[NewsEvent]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not {"time_utc", "currency", "impact", "event"} <= set(reader.fieldnames):
        raise CalendarError(
            f"calendar header must be time_utc,currency,impact,event (got {reader.fieldnames})"
        )
    events = []
    for line, row in enumerate(reader, start=2):
        try:
            when = datetime.strptime(row["time_utc"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except (ValueError, AttributeError) as exc:
            raise CalendarError(f"line {line}: bad time {row.get('time_utc')!r}") from exc
        events.append(
            NewsEvent(when, (row["currency"] or "").strip().upper(), (row["impact"] or "").strip().upper(),
                      (row["event"] or "").strip())
        )  # fmt: skip
    return sorted(events, key=lambda e: e.time)


class NewsCalendar:
    def __init__(self, events: Iterable[NewsEvent], *, loaded_at: datetime) -> None:
        self.events = sorted(events, key=lambda e: e.time)
        self._times = [e.time for e in self.events]
        self.loaded_at = loaded_at

    def _matching(self, currencies: Iterable[str], impacts: Iterable[str]) -> list[NewsEvent]:
        cur, imp = {c.upper() for c in currencies}, {i.upper() for i in impacts}
        return [e for e in self.events if e.currency in cur and e.impact in imp]

    def blackout(
        self,
        currencies: Iterable[str],
        impacts: Iterable[str],
        now: datetime,
        *,
        before_min: int,
        after_min: int,
    ) -> NewsEvent | None:
        """The event making ``now`` a blackout: one due within ``before`` minutes or past by ≤ ``after``."""
        lo, hi = now - timedelta(minutes=after_min), now + timedelta(minutes=before_min)
        start = bisect_left(self._times, lo)
        cur, imp = {c.upper() for c in currencies}, {i.upper() for i in impacts}
        for e in self.events[start:]:
            if e.time > hi:
                return None
            if e.currency in cur and e.impact in imp:
                return e
        return None

    def minutes_to_next(self, currencies: Iterable[str], impacts: Iterable[str], now: datetime) -> int | None:
        nxt = next((e for e in self._matching(currencies, impacts) if e.time >= now), None)
        return None if nxt is None else int((nxt.time - now) / timedelta(minutes=1))

    def minutes_since_last(
        self, currencies: Iterable[str], impacts: Iterable[str], now: datetime
    ) -> int | None:
        past = [e for e in self._matching(currencies, impacts) if e.time <= now]
        return None if not past else int((now - past[-1].time) / timedelta(minutes=1))
