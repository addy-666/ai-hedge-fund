"""The Guardian EA's calendar file as the engine sees it (roadmap 5.7b).

``CalendarFile.current()`` re-reads ``calendar.csv`` when it changes and returns the calendar, or None when it
is missing, unreadable, malformed or older than ``max_age`` (the EA rewrites it hourly). With the news gate
enabled, None blocks entries: without a calendar a blackout cannot be ruled out (fail closed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from aifund.market.news import CalendarError, NewsCalendar, parse_calendar
from aifund.ports.system import ClockPort

log = structlog.get_logger(__name__)


class CalendarFile:
    def __init__(self, path: Path, clock: ClockPort, *, max_age: timedelta = timedelta(hours=3)) -> None:
        self.path = path
        self._clock = clock
        self._max_age = max_age
        self._mtime: float | None = None
        self._calendar: NewsCalendar | None = None
        self.problem: str | None = "not loaded yet"

    def current(self) -> NewsCalendar | None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._fail(f"{self.path.name} missing")
        written = datetime.fromtimestamp(mtime, UTC)
        if self._clock.now() - written > self._max_age:
            return self._fail(f"{self.path.name} is stale (written {written:%Y-%m-%d %H:%M} UTC)")
        if mtime != self._mtime:
            try:
                events = parse_calendar(self.path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, CalendarError) as exc:
                return self._fail(f"{self.path.name}: {exc}")
            self._mtime, self._calendar = mtime, NewsCalendar(events, loaded_at=self._clock.now())
        self.problem = None
        return self._calendar

    def _fail(self, problem: str) -> NewsCalendar | None:
        if problem != self.problem:
            log.warning("news.calendar_unavailable", problem=problem)
        self.problem = problem
        self._mtime, self._calendar = None, None
        return self._calendar
