"""Session calendar: closes, reopens and long closures, across DST and holidays. Expected times by hand.

New York is UTC-4 from 2026-03-08 to 2026-11-01 (and from 2027-03-14), UTC-5 otherwise. The calendar is the
Vantage US CFD session seen in the exported history: trading day D runs 18:00 New York on D-1 to 17:00 on D
(so 22:00 -> 21:00 UTC in summer), Sunday 18:00 to Friday 17:00; 2026-06-19 and 2026-07-03 closed at
17:00 UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from aifund.config.trading_config import SessionConfig
from aifund.market.sessions import SessionCalendar

H24 = timedelta(hours=24)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


CAL = SessionCalendar.from_config(
    SessionConfig(
        timezone="America/New_York",
        daily_close="17:00",
        daily_open="18:00",
        early_closes={
            date(2026, 6, 19): "13:00",
            date(2026, 12, 24): "13:00",
            date(2026, 9, 23): "13:00",  # a mid-week early close (hypothetical)
        },
        closed_days=[date(2026, 12, 25), date(2027, 1, 1), date(2027, 3, 26)],
    )
)


def test_regular_summer_week() -> None:
    wed = utc(2026, 9, 16, 12, 0)
    assert CAL.next_close(wed) == (utc(2026, 9, 16, 21, 0), utc(2026, 9, 16, 22, 0))  # daily 1 h break
    fri = utc(2026, 9, 18, 12, 0)
    assert CAL.next_close(fri) == (utc(2026, 9, 18, 21, 0), utc(2026, 9, 20, 22, 0))  # 49 h weekend
    assert CAL.long_close_ahead(wed, H24) is None
    assert CAL.long_close_ahead(fri, H24) == utc(2026, 9, 18, 21, 0)


def test_winter_week_moves_with_new_york() -> None:
    fri = utc(2026, 12, 4, 12, 0)
    assert CAL.next_close(fri) == (utc(2026, 12, 4, 22, 0), utc(2026, 12, 6, 23, 0))


def test_juneteenth_early_close_starts_the_weekend_at_17_utc() -> None:
    fri = utc(2026, 6, 19, 9, 0)
    assert CAL.next_close(fri) == (utc(2026, 6, 19, 17, 0), utc(2026, 6, 21, 22, 0))  # 53 h
    assert CAL.long_close_ahead(fri, H24) == utc(2026, 6, 19, 17, 0)


def test_mid_week_early_close_is_not_a_long_closure() -> None:
    wed = utc(2026, 9, 23, 9, 0)
    assert CAL.next_close(wed) == (utc(2026, 9, 23, 17, 0), utc(2026, 9, 23, 22, 0))  # 5 h
    assert CAL.long_close_ahead(wed, H24) is None


def test_christmas_eve_is_the_last_close_before_a_closed_friday() -> None:
    thu = utc(2026, 12, 24, 9, 0)
    assert CAL.next_close(thu) == (utc(2026, 12, 24, 18, 0), utc(2026, 12, 27, 23, 0))  # 13:00 NY, winter
    assert CAL.long_close_ahead(thu, H24) == utc(2026, 12, 24, 18, 0)


def test_good_friday_makes_thursday_the_weekend_close() -> None:
    thu = utc(2027, 3, 25, 12, 0)  # DST already started (2027-03-14)
    assert CAL.next_close(thu) == (utc(2027, 3, 25, 21, 0), utc(2027, 3, 28, 22, 0))  # 73 h


def test_new_year_friday_closed() -> None:
    thu = utc(2026, 12, 31, 12, 0)
    assert CAL.long_close_ahead(thu, H24) == utc(2026, 12, 31, 22, 0)


@pytest.mark.parametrize(
    ("now", "is_open"),
    [
        (utc(2026, 9, 16, 12, 0), True),  # Wednesday
        (utc(2026, 9, 16, 21, 30), False),  # daily break
        (utc(2026, 9, 16, 22, 0), True),  # reopened
        (utc(2026, 9, 19, 12, 0), False),  # Saturday
        (utc(2026, 9, 20, 21, 59), False),  # Sunday before the open
        (utc(2026, 9, 20, 22, 0), True),  # Sunday open
        (utc(2026, 6, 19, 17, 30), False),  # after the Juneteenth early close
        (utc(2026, 12, 25, 12, 0), False),  # Christmas
    ],
)
def test_is_open(now: datetime, is_open: bool) -> None:
    assert CAL.is_open(now) is is_open


def test_during_a_closure_only_the_next_close_counts() -> None:
    sat = utc(2026, 9, 19, 12, 0)
    assert CAL.next_close(sat) == (utc(2026, 9, 21, 21, 0), utc(2026, 9, 21, 22, 0))  # Monday's evening break
    assert CAL.long_close_ahead(sat, H24) is None  # already shut: nothing to flatten ahead of


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        CAL.next_close(datetime(2026, 9, 16, 12, 0))


def test_config_validation() -> None:
    with pytest.raises(ValueError, match="timezone"):
        SessionConfig(timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="HH:MM"):
        SessionConfig(early_closes={date(2026, 6, 19): "1pm"})
    with pytest.raises(ValueError, match="both"):
        SessionConfig(early_closes={date(2026, 12, 25): "13:00"}, closed_days=[date(2026, 12, 25)])
    assert CAL.daily_close == time(17, 0)


def test_next_long_close_skips_the_evening_breaks() -> None:
    assert CAL.next_long_close(utc(2026, 9, 16, 12, 0), H24) == utc(2026, 9, 18, 21, 0)  # Wed -> Friday close
    assert CAL.next_long_close(utc(2026, 6, 18, 12, 0), H24) == utc(2026, 6, 19, 17, 0)  # Juneteenth early
    assert CAL.next_long_close(utc(2026, 9, 19, 12, 0), H24) == utc(2026, 9, 25, 21, 0)  # from a Saturday
