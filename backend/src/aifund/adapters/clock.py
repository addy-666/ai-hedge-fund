"""Clock implementations: the real UTC clock and a controllable fake for tests and replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """Time only moves when told to. ``sleep`` advances time instantly (and yields to the event loop)."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("FakeClock start must be timezone-aware UTC")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **delta: float) -> datetime:
        step = timedelta(seconds=seconds, **delta)
        if step < timedelta(0):
            raise ValueError("time cannot move backwards")
        self._now += step
        return self._now

    def set(self, moment: datetime) -> None:
        if moment < self._now:
            raise ValueError("time cannot move backwards")
        self._now = moment

    async def sleep(self, seconds: float) -> None:
        self.advance(max(seconds, 0))
        await asyncio.sleep(0)
