from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aifund.adapters.mt5.server_time import (
    OffsetUnavailable,
    estimate_offset,
    server_epoch_to_utc,
    utc_to_server_epoch,
)

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def server_epoch(offset: timedelta, tick_age_s: float = 20) -> int:
    return int((NOW + offset - timedelta(seconds=tick_age_s)).timestamp())


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(hours=3),
        timedelta(hours=2),
        timedelta(0),
        timedelta(hours=-5),
        timedelta(hours=5, minutes=30),
        timedelta(hours=5, minutes=45),
    ],
)
def test_offset_is_recovered_from_a_fresh_tick(offset: timedelta) -> None:
    assert estimate_offset(server_epoch(offset), NOW) == offset


def test_stale_tick_cannot_pin_the_offset() -> None:
    with pytest.raises(OffsetUnavailable, match="drift"):
        estimate_offset(server_epoch(timedelta(hours=3), tick_age_s=600), NOW)


def test_implausible_offset_is_rejected() -> None:
    with pytest.raises(OffsetUnavailable, match="implausible"):
        estimate_offset(server_epoch(timedelta(hours=20)), NOW)


def test_conversions_round_trip() -> None:
    offset = timedelta(hours=3)
    epoch = utc_to_server_epoch(NOW, offset)
    assert datetime.fromtimestamp(epoch, UTC) == NOW + offset  # server wall clock
    assert server_epoch_to_utc(epoch, offset) == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_to_server_epoch(NOW.replace(tzinfo=None), offset)
