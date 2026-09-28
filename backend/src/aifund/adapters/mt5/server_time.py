"""Broker server time ⇄ UTC (docs/03 §1).

MT5 reports bar, tick and deal times as *server wall-clock* seconds encoded like a Unix epoch (e.g. a
broker on UTC+3 reports 12:00 UTC as 15:00). The offset is not exposed by the API, so it is estimated by
comparing the freshest tick time with the real UTC clock and rounding to the nearest 15 minutes (all real
offsets are multiples of 15 min). The estimate is only trusted when a tick is recent — on a closed
market the difference contains the tick's age and would be wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

GRANULARITY = timedelta(minutes=15)
MAX_TICK_AGE = timedelta(minutes=2)
MAX_ABS_OFFSET = timedelta(hours=14)


class OffsetUnavailable(Exception):
    """No tick is fresh enough to determine the broker's UTC offset."""


def estimate_offset(freshest_tick_server_epoch: int, utc_now: datetime) -> timedelta:
    """Offset such that ``utc = server_wall_clock - offset``.

    Raises OffsetUnavailable if the implied tick age (distance from a 15-minute boundary) is larger than
    MAX_TICK_AGE, i.e. the tick is too old to pin the offset down, or the offset is implausible.
    """
    if utc_now.tzinfo is None:
        raise ValueError("utc_now must be timezone-aware")
    raw = datetime.fromtimestamp(freshest_tick_server_epoch, UTC) - utc_now.astimezone(UTC)
    steps = round(raw / GRANULARITY)
    offset = steps * GRANULARITY
    residual = abs(raw - offset)
    if residual > MAX_TICK_AGE:
        raise OffsetUnavailable(f"freshest tick implies {residual} of drift from a 15-minute offset")
    if abs(offset) > MAX_ABS_OFFSET:
        raise OffsetUnavailable(f"implausible server offset {offset}")
    return offset


def server_epoch_to_utc(server_epoch: float, offset: timedelta) -> datetime:
    return datetime.fromtimestamp(server_epoch, UTC) - offset


def utc_to_server_epoch(moment: datetime, offset: timedelta) -> int:
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    return int((moment.astimezone(UTC) + offset).timestamp())
