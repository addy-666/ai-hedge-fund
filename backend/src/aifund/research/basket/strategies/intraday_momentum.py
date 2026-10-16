"""Intraday momentum (roadmap 11.13, docs/09 §9.1). Pre-registered 2026-10-16, before any run.

Each exchange cash session: the sign of the return over the signal window decides the side, entered at the
window's end and held to 15 minutes before the cash close. Windows (declared; the first is the default):

- ``close30``: the prior session's cash close -> 30 minutes after today's open
- ``open30``:  today's open -> 30 minutes after it
- ``open60``:  today's open -> 60 minutes after it

The stop is 1.0 x ATR20(D1) (the D1 bars closed at the decision) from the entry, and is the R unit. Prices are
read only from path bars that had closed at the time they describe; "the open" is the open of the bar starting
at the cash open. Exchange times are converted to the server clock (the broker's New York + 7 h) with each
exchange's own zone, so the anchors are exact in every season (summer, winter and the weeks the US and Europe
change on different dates). A day whose path has no bar at the cash open, at the end of the window, or within
the 10 minutes before the exit (an exchange holiday or a half day: the calendar knew it in advance) gives no
signal. BTCUSD has no session and is not in this family.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import Candles, atr_sma, last_closed_index
from aifund.research.basket.segments import Hold
from aifund.research.basket.strategies.base import Params, hhmm

EXIT_BEFORE_CLOSE = 15  # minutes
SLACK = 10  # minutes a reference bar may be missing before the day is skipped
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)  # origin of wall-clock times, held as UTC-labelled datetimes
WINDOWS = {"close30": (True, 30), "open30": (False, 30), "open60": (False, 60)}  # (from prior close, minutes)


@dataclass(frozen=True)
class Session:
    zone: str
    open: str  # exchange local "HH:MM"
    close: str


# canonical symbol -> its exchange cash session (docs/09 §9.1)
SESSIONS: dict[str, Session] = {
    "NAS100": Session("America/New_York", "09:30", "16:00"),
    "US30": Session("America/New_York", "09:30", "16:00"),
    "XAUUSD": Session("America/New_York", "08:20", "13:30"),  # COMEX
    "GER40": Session("Europe/Berlin", "09:00", "17:30"),
    "UK100": Session("Europe/London", "08:00", "16:30"),
    "JPN225": Session("Asia/Tokyo", "09:00", "15:00"),
}
ALIASES = {"JP225": "JPN225", "NIKKEI225": "JPN225", "DE40": "GER40", "USTEC": "NAS100", "DJ30": "US30"}


def session_for(symbol: str) -> Session | None:
    """The session of a broker symbol ("NAS100.r" -> NAS100), or None when the family does not trade it."""
    base = symbol.split(".")[0].upper()
    return SESSIONS.get(ALIASES.get(base, base))


@dataclass(frozen=True)
class ServerClock:
    """The broker's server wall clock: ``zone`` shifted by ``shift_hours`` (Vantage: New York + 7 h)."""

    zone: str = "America/New_York"
    shift_hours: int = 7

    def minute(self, day: date, hh_mm: str, exchange_zone: str) -> int:
        """Server minute of exchange local ``hh_mm`` on ``day``."""
        local = datetime.combine(day, time(*divmod(hhmm(hh_mm), 60)), tzinfo=ZoneInfo(exchange_zone))
        wall = local.astimezone(ZoneInfo(self.zone)).replace(tzinfo=UTC) + timedelta(hours=self.shift_hours)
        return int((wall - _EPOCH) // timedelta(minutes=1))

    def day_of(self, minute: int, exchange_zone: str) -> date:
        """The exchange's local date at server ``minute``."""
        wall = _EPOCH + timedelta(minutes=minute) - timedelta(hours=self.shift_hours)
        instant = wall.replace(tzinfo=ZoneInfo(self.zone))
        return instant.astimezone(ZoneInfo(exchange_zone)).date()


@dataclass(frozen=True)
class IntradayParams(Params):
    window: str = "close30"
    atr_period: int = 20
    stop_atr: float = 1.0


class IntradayMomentum:
    name: ClassVar[str] = "intraday_momentum"
    version: ClassVar[str] = "1"
    setup_tag: ClassVar[str] = "intraday_momentum"
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.D1,)

    def __init__(self, params: IntradayParams | None = None, clock: ServerClock | None = None) -> None:
        self.p = params or IntradayParams()
        if self.p.window not in WINDOWS:
            raise ValueError(f"unknown signal window {self.p.window!r}")
        self.clock = clock or ServerClock()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def holds(
        self, symbol: str, data: Mapping[Timeframe, Candles], path: Candles, *, force: Side | None = None
    ) -> list[Hold]:
        """One hold per cash session (``force``: that side on every session with a signal — a control)."""
        session = session_for(symbol)
        d1 = data.get(Timeframe.D1)
        if session is None or d1 is None or len(path) == 0:
            return []
        p, clock, zone = self.p, self.clock, session.zone
        from_close, minutes = WINDOWS[p.window]
        atr = atr_sma(d1, p.atr_period)
        first = clock.day_of(int(path.t[0]), zone)
        last = clock.day_of(int(path.t[-1]), zone)
        out: list[Hold] = []
        prior_close: int | None = None
        day = first
        while day <= last:
            if day.weekday() < 5:
                opens, closes = clock.minute(day, session.open, zone), clock.minute(day, session.close, zone)
                hold = self._hold(path, d1, atr, opens, closes, prior_close, from_close, minutes, force)
                if hold is not None:
                    out.append(hold)
                if _bar_near(path, closes - path.span, SLACK):  # the session traded to its close
                    prior_close = closes
            day += timedelta(days=1)
        return out

    def _hold(
        self,
        path: Candles,
        d1: Candles,
        atr: Any,
        opens: int,
        closes: int,
        prior_close: int | None,
        from_close: bool,
        minutes: int,
        force: Side | None,
    ) -> Hold | None:
        decided_at = opens + minutes
        exit_at = closes - EXIT_BEFORE_CLOSE
        i_open = path.first_open_at_or_after(opens)
        if i_open >= len(path) or path.t[i_open] != opens:
            return None  # no bar at the cash open
        if not _bar_near(path, exit_at - path.span, SLACK):
            return None  # the session does not trade to the exit (holiday, half day)
        now = _closed_price(path, decided_at)
        if now is None:
            return None
        if from_close:
            if prior_close is None:
                return None
            ref = _closed_price(path, prior_close)
        else:
            ref = float(path.o[i_open])
        k = last_closed_index(d1, decided_at)
        if ref is None or ref <= 0 or k < 0 or math.isnan(atr[k]) or atr[k] <= 0:
            return None
        ret = math.log(now / ref)
        side = force or (Side.BUY if ret > 0 else Side.SELL if ret < 0 else None)
        if side is None:
            return None
        return Hold(
            decided_at=decided_at,
            side=side,
            stop_distance=self.p.stop_atr * float(atr[k]),
            end_at=exit_at,
            setup_tag=self.setup_tag,
            max_entry_delay=SLACK,
        )


def _closed_price(path: Candles, minute: int) -> float | None:
    """Close of the last bar that had closed at ``minute``, if it closed within ``SLACK`` minutes of it."""
    i = last_closed_index(path, minute)
    if i < 0 or minute - (int(path.t[i]) + path.span) > SLACK:
        return None
    return float(path.c[i])


def _bar_near(path: Candles, minute: int, slack: int) -> bool:
    """A bar opens in [minute - slack, minute]."""
    i = path.first_open_at_or_after(minute - slack)
    return i < len(path) and int(path.t[i]) <= minute
