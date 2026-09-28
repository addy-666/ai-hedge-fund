"""Tracks the equity references loss limits are measured against (day start, week start, peak).

In-memory for Phase 2; persisted with the equity snapshotter in Phase 3 (task 3.5).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aifund.risk.limits import EquityState, trading_day_start, trading_week_start


class EquityTracker:
    def __init__(self, boundary_utc: str) -> None:
        self._boundary = boundary_utc
        self._day: datetime | None = None
        self._week: datetime | None = None
        self.day_start_equity = Decimal(0)
        self.week_start_equity = Decimal(0)
        self.peak_equity = Decimal(0)

    def update(self, equity: Decimal, now: datetime) -> EquityState:
        day, week = trading_day_start(now, self._boundary), trading_week_start(now, self._boundary)
        if day != self._day:
            self._day, self.day_start_equity = day, equity
        if week != self._week:
            self._week, self.week_start_equity = week, equity
        self.peak_equity = max(self.peak_equity, equity)
        return EquityState(equity, self.day_start_equity, self.week_start_equity, self.peak_equity)

    @property
    def day_start(self) -> datetime | None:
        return self._day
