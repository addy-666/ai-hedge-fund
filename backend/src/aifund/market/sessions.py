"""Trading-session calendar: when a symbol's market closes next, and for how long (pure, UTC in and out).

Trading day D (Monday to Friday, not a closed day) runs from ``daily_open`` local time on the previous
calendar day to ``daily_close`` on D, or to D's early close. Between two trading days the market is shut:
an hour on a normal evening, ~49 h over a weekend, longer around a holiday. The position manager flattens
symbols that do not trade weekends before any closure of at least ``long_close_hours``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aifund.config.trading_config import SessionConfig

_SEARCH_DAYS = 21  # longer than any realistic run of closed days


def _hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class SessionCalendar:
    tz: ZoneInfo
    daily_close: time
    daily_open: time
    early_closes: Mapping[date, time]
    closed_days: frozenset[date]

    @classmethod
    def from_config(cls, cfg: SessionConfig) -> SessionCalendar:
        return cls(
            tz=ZoneInfo(cfg.timezone),
            daily_close=_hhmm(cfg.daily_close),
            daily_open=_hhmm(cfg.daily_open),
            early_closes={d: _hhmm(t) for d, t in cfg.early_closes.items()},
            closed_days=frozenset(cfg.closed_days),
        )

    # ------------------------------------------------------------------ trading days

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.closed_days

    def _at(self, day: date, t: time) -> datetime:
        return datetime.combine(day, t, tzinfo=self.tz).astimezone(UTC)

    def close_of(self, day: date) -> datetime:
        return self._at(day, self.early_closes.get(day, self.daily_close))

    def open_of(self, day: date) -> datetime:
        return self._at(day - timedelta(days=1), self.daily_open)

    def _next_trading_day(self, day: date) -> date:
        for i in range(1, _SEARCH_DAYS):
            if self.is_trading_day(day + timedelta(days=i)):
                return day + timedelta(days=i)
        raise ValueError(f"no trading day within {_SEARCH_DAYS} days after {day}")

    def _days_from(self, now: datetime) -> list[date]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware (UTC)")
        start = now.astimezone(self.tz).date() - timedelta(days=1)
        return [start + timedelta(days=i) for i in range(_SEARCH_DAYS)]

    # ------------------------------------------------------------------ queries

    def is_open(self, now: datetime) -> bool:
        return any(
            self.open_of(d) <= now < self.close_of(d)
            for d in self._days_from(now)[:3]
            if self.is_trading_day(d)
        )

    def next_close(self, now: datetime) -> tuple[datetime, datetime]:
        """(the next close strictly after ``now``, when the market reopens after it), both UTC."""
        for d in self._days_from(now):
            if self.is_trading_day(d) and self.close_of(d) > now:
                return self.close_of(d), self.open_of(self._next_trading_day(d))
        raise ValueError(f"no trading day within {_SEARCH_DAYS} days of {now}")

    def long_close_ahead(self, now: datetime, min_closed: timedelta) -> datetime | None:
        """The next close if the market then stays shut for at least ``min_closed``, else None."""
        close, reopen = self.next_close(now)
        return close if reopen - close >= min_closed else None


def calendars(sessions: Mapping[str, SessionConfig]) -> dict[str, SessionCalendar]:
    return {name: SessionCalendar.from_config(cfg) for name, cfg in sessions.items()}
