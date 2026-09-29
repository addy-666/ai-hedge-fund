"""News calendar (roadmap 5.7b): parsing, the blackout window, minutes to / since an event."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aifund.adapters.clock import FakeClock
from aifund.engine.news_feed import CalendarFile
from aifund.market.news import CalendarError, NewsCalendar, parse_calendar

T = datetime(2026, 10, 2, 12, 30, tzinfo=UTC)  # US payrolls, Friday 12:30 UTC
CSV = """time_utc,currency,impact,event
2026-10-02 12:30,USD,HIGH,Nonfarm Payrolls
2026-10-02 08:00,EUR,HIGH,ECB Speech
2026-10-01 14:00,USD,MEDIUM,ISM Manufacturing
2026-10-05 14:00,USD,HIGH,ISM Services
"""


def cal() -> NewsCalendar:
    return NewsCalendar(parse_calendar(CSV), loaded_at=T)


def test_parse_sorts_and_normalises() -> None:
    events = parse_calendar(CSV.replace("USD,HIGH,Nonfarm", "usd,high,Nonfarm"))
    assert [e.time for e in events] == sorted(e.time for e in events)
    nfp = next(e for e in events if e.title == "Nonfarm Payrolls")
    assert (nfp.time, nfp.currency, nfp.impact) == (T, "USD", "HIGH")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("when,ccy\n", "header"),
        ("time_utc,currency,impact,event\n2026-10-02T12:30,USD,HIGH,x\n", "line 2: bad time"),
    ],
)
def test_parse_errors(text: str, error: str) -> None:
    with pytest.raises(CalendarError, match=error):
        parse_calendar(text)


@pytest.mark.parametrize(
    ("minutes", "blocked"),
    [(-16, False), (-15, True), (0, True), (15, True), (16, False)],  # 15 before .. 15 after, inclusive
)
def test_the_blackout_window(minutes: int, blocked: bool) -> None:
    hit = cal().blackout(["USD"], ["HIGH"], T + timedelta(minutes=minutes), before_min=15, after_min=15)
    assert (hit is not None) is blocked
    if hit:
        assert hit.title == "Nonfarm Payrolls"


def test_currency_and_impact_filters() -> None:
    c = cal()
    assert c.blackout(["JPY"], ["HIGH"], T, before_min=15, after_min=15) is None
    assert (
        c.blackout(["USD"], ["HIGH"], datetime(2026, 10, 1, 14, 0, tzinfo=UTC), before_min=5, after_min=5)
        is None
    )
    medium = c.blackout(
        ["USD"], ["HIGH", "MEDIUM"], datetime(2026, 10, 1, 14, 0, tzinfo=UTC), before_min=5, after_min=5
    )
    assert medium is not None and medium.title == "ISM Manufacturing"  # noqa: PT018


def test_minutes_to_next_and_since_last() -> None:
    c, now = cal(), T - timedelta(minutes=90)
    assert c.minutes_to_next(["USD"], ["HIGH"], now) == 90
    assert c.minutes_since_last(["USD"], ["HIGH"], now) is None  # the EUR speech is not USD
    assert c.minutes_since_last(["USD", "EUR"], ["HIGH"], now) == 180
    assert c.minutes_to_next(["USD"], ["HIGH"], datetime(2026, 10, 6, tzinfo=UTC)) is None


def test_the_calendar_file_is_reloaded_on_change_and_refused_when_stale(tmp_path: Path) -> None:
    clock = FakeClock(datetime.now(UTC))
    path = tmp_path / "calendar.csv"
    feed = CalendarFile(path, clock, max_age=timedelta(hours=3))
    assert feed.current() is None and feed.problem == "calendar.csv missing"  # noqa: PT018
    path.write_text(CSV)
    first = feed.current()
    assert first is not None and len(first.events) == 4 and feed.problem is None  # noqa: PT018
    assert feed.current() is first  # unchanged file: cached
    path.write_text("broken\n")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 1))
    assert feed.current() is None and "header" in (feed.problem or "")  # noqa: PT018
    clock.advance(timedelta(hours=4).total_seconds())
    path.write_text(CSV)
    os.utime(path, (path.stat().st_atime, clock.now().timestamp() - 4 * 3600))
    assert feed.current() is None and "stale" in (feed.problem or "")  # noqa: PT018
