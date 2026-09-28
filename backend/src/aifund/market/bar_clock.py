"""Bar clock — emits exactly one BarClosed event per closed trigger bar (docs/03 §2, roadmap 1.7).

Design:
- It never computes timeframe boundaries itself (D1/H4 bars follow the broker's day, not UTC midnight);
  it asks the feed for the last CLOSED bar and compares with a per-(symbol, timeframe) cursor.
- Grace: a bar is only emitted ``grace`` after it closed, so the broker's final ticks are in.
- First start (no cursor): a bar that had already closed when the clock was created initialises the
  cursor silently — an old bar is never traded just because the process started. Bars closing after
  start-up are emitted normally.
- After downtime: only the latest closed bar is considered, and only if it closed within
  ``max_emit_delay`` (default: one timeframe). Missed bars are never traded retroactively; a stale latest
  bar just advances the cursor.
- Stale feed: when the last closed bar is older than ``stale_after_periods`` timeframes while the market is
  expected to be open, the watch is reported as newly stale (once) and as recovered when bars resume.
- Cursors: the production store (persistence/repositories/cursors.py) reads the latest
  ``decisions.bar_time`` per symbol/timeframe, so a crash
  between emitting and recording a decision re-emits that bar (order idempotency protects it) while a
  recorded bar is never emitted twice.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from aifund.domain.enums import Timeframe
from aifund.ports.broker import MarketDataPort
from aifund.ports.system import ClockPort

Watch = tuple[str, Timeframe]


@dataclass(frozen=True)
class BarClosed:
    symbol: str
    timeframe: Timeframe
    bar_time: datetime  # open time of the closed bar (UTC)
    close_time: datetime


@dataclass
class PollReport:
    events: list[BarClosed] = field(default_factory=list)
    newly_stale: list[Watch] = field(default_factory=list)
    recovered: list[Watch] = field(default_factory=list)
    skipped_stale_bars: list[Watch] = field(default_factory=list)
    errors: dict[Watch, str] = field(default_factory=dict)


class CursorStore(Protocol):
    def load(self, symbol: str, timeframe: Timeframe) -> datetime | None: ...

    def save(self, symbol: str, timeframe: Timeframe, bar_time: datetime) -> None: ...


class InMemoryCursorStore:
    def __init__(self) -> None:
        self.cursors: dict[Watch, datetime] = {}

    def load(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        return self.cursors.get((symbol, timeframe))

    def save(self, symbol: str, timeframe: Timeframe, bar_time: datetime) -> None:
        self.cursors[(symbol, timeframe)] = bar_time


def always_open(_symbol: str, _moment: datetime) -> bool:
    return True


class BarClock:
    def __init__(
        self,
        feed: MarketDataPort,
        clock: ClockPort,
        watches: list[Watch],
        cursors: CursorStore,
        *,
        grace: timedelta = timedelta(seconds=3),
        max_emit_delay: timedelta | None = None,
        stale_after_periods: int = 2,
        market_open: Callable[[str, datetime], bool] = always_open,
    ) -> None:
        self._feed = feed
        self._clock = clock
        self._watches = list(dict.fromkeys(watches))
        self._store = cursors
        self._grace = grace
        self._max_delay = max_emit_delay
        self._stale_periods = stale_after_periods
        self._market_open = market_open
        self._cursor: dict[Watch, datetime | None] = {}
        self._loaded: set[Watch] = set()
        self._started = clock.now()
        self.stale: set[Watch] = set()

    def _load(self, watch: Watch) -> datetime | None:
        if watch not in self._loaded:
            self._cursor[watch] = self._store.load(*watch)
            self._loaded.add(watch)
        return self._cursor.get(watch)

    def _advance(self, watch: Watch, bar_time: datetime) -> None:
        self._cursor[watch] = bar_time
        self._store.save(watch[0], watch[1], bar_time)

    async def poll(self) -> PollReport:
        report = PollReport()
        now = self._clock.now()
        for watch in self._watches:
            symbol, tf = watch
            period = timedelta(minutes=tf.minutes)
            try:
                latest = await self._feed.closed_bars(symbol, tf, 1)
            except Exception as exc:  # a broken watch must not stop the others
                report.errors[watch] = f"{type(exc).__name__}: {exc}"
                continue
            if not latest:
                continue
            bar = latest[-1]
            close_time = bar.time + period

            age = now - close_time
            is_stale = age > period * self._stale_periods and self._market_open(symbol, now)
            if is_stale and watch not in self.stale:
                self.stale.add(watch)
                report.newly_stale.append(watch)
            elif not is_stale and watch in self.stale:
                self.stale.discard(watch)
                report.recovered.append(watch)

            if age < self._grace:
                continue  # let the final ticks of the bar land
            cursor = self._load(watch)
            if cursor is None and close_time <= self._started:
                self._advance(watch, bar.time)  # first start: never trade a bar that closed before we did
                continue
            if cursor is not None and bar.time <= cursor:
                continue
            if age > (self._max_delay if self._max_delay is not None else period):
                self._advance(watch, bar.time)  # missed during downtime / feed stall: do not trade late
                report.skipped_stale_bars.append(watch)
                continue
            self._advance(watch, bar.time)
            report.events.append(BarClosed(symbol, tf, bar.time, close_time))
        return report

    async def run(
        self,
        on_bar: Callable[[BarClosed], Awaitable[None]],
        *,
        interval_s: float = 2.0,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        """Poll forever (until ``should_stop``). Supervision/restarts are the engine's job (Phase 5)."""
        while not should_stop():
            for event in (await self.poll()).events:
                await on_bar(event)
            await self._clock.sleep(interval_s)
