"""Data-quality report for an exported history (roadmap R.8): is this history fit to research on?

Per symbol and timeframe:

- coverage: bars, first/last bar, whether the export hit the terminal's bar cap (``manifest.capped``);
- unexpected gaps: more than ``max(one bar, 10 minutes)`` missing between consecutive bars while the session
  calendar says the market was open the WHOLE missing stretch (weekends, daily breaks and holidays in the
  calendar are expected; a few quiet minutes without a tick print no M1 bar and are not gaps); symbols
  trading weekends (``trade_weekends``) have no closed periods to excuse a gap;
- spread: median, 99th percentile and max in points, and bars above the symbol's ``max_spread_points`` gate;
- OHLC sanity: bars whose high/low do not bracket open and close, and zero-range bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

import numpy as np

from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from aifund.market.sessions import SessionCalendar

MAX_OPEN_CHECKS = 2000  # a longer gap is sampled at this many points (a month of M1 would be 43,200)
QUIET = timedelta(minutes=10)  # shorter holes are minutes without ticks, not missing data


@dataclass(frozen=True)
class Gap:
    after: datetime  # open time of the last bar before the gap
    missing: timedelta


@dataclass
class SeriesQuality:
    symbol: str
    timeframe: Timeframe
    bars: int
    first: datetime | None
    last: datetime | None
    capped: bool
    unexpected_gaps: list[Gap] = field(default_factory=list)
    spread_p50: float | None = None
    spread_p99: float | None = None
    spread_max: int | None = None
    over_spread_gate: int = 0
    bad_ohlc: int = 0
    zero_range: int = 0

    @property
    def missing_hours(self) -> float:
        return sum(g.missing.total_seconds() for g in self.unexpected_gaps) / 3600

    @property
    def verdict(self) -> str:
        problems = []
        if self.capped:
            problems.append("CAPPED")
        if self.bad_ohlc:
            problems.append("BAD OHLC")
        if self.unexpected_gaps:
            problems.append(f"{len(self.unexpected_gaps)} gaps")
        return ", ".join(problems) or "ok"

    def row(self) -> str:
        span = f"{self.first:%Y-%m-%d} → {self.last:%Y-%m-%d}" if self.first and self.last else "-"
        worst = max((g.missing for g in self.unexpected_gaps), default=None)
        spread = (
            f"{self.spread_p50:g} / {self.spread_p99:g} / {self.spread_max}"
            if self.spread_p50 is not None
            else "-"
        )
        gaps = f"{len(self.unexpected_gaps)} ({self.missing_hours:.1f} h" + (
            f", worst {_td(worst)})" if worst else ")"
        )
        capped = "yes" if self.capped else "no"
        return (
            f"| {self.symbol} | {self.timeframe.value} | {self.bars:,} | {span} | {capped} | {gaps} "
            f"| {spread} | {self.over_spread_gate} | {self.bad_ohlc} / {self.zero_range} | {self.verdict} |"
        )


HEADER = (
    "| Symbol | TF | Bars | Span | Capped | Unexpected gaps | Spread p50 / p99 / max (pts) | > gate "
    "| Bad OHLC / zero range | Verdict |\n|---|---|---|---|---|---|---|---|---|---|"
)


def likely_capped(bars: int, first: datetime | None, requested_start: datetime) -> bool:
    """For exports made before the cap was recorded: ~100,000 bars (MT5's default "Max bars in chart") that
    start well after the requested start is the cap, not the broker's history."""
    return 99_000 <= bars <= 101_000 and first is not None and first - requested_start > timedelta(days=7)


def _td(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours / 24:.1f} d" if hours >= 48 else f"{hours:.1f} h"


def _open_throughout(calendar: SessionCalendar, start: datetime, end: datetime, step: timedelta) -> bool:
    """Was the market open at every bar open in [start, end)? Long stretches are sampled."""
    n = int((end - start) / step)
    stride = max(1, n // MAX_OPEN_CHECKS)
    return all(calendar.is_open(start + i * step) for i in range(0, n, stride))


def assess(
    symbol: str,
    timeframe: Timeframe,
    bars: Sequence[Bar],
    *,
    calendar: SessionCalendar | None,
    trade_weekends: bool,
    max_spread_points: int | None,
    capped: bool,
) -> SeriesQuality:
    q = SeriesQuality(
        symbol=symbol,
        timeframe=timeframe,
        bars=len(bars),
        first=bars[0].time if bars else None,
        last=bars[-1].time if bars else None,
        capped=capped,
    )
    if not bars:
        return q
    span = timedelta(minutes=timeframe.minutes)
    for prev, cur in pairwise(bars):
        missing_from = prev.time + span
        if cur.time - missing_from <= max(span, QUIET):
            continue
        expected = (
            not trade_weekends
            and calendar is not None
            and not _open_throughout(calendar, missing_from, cur.time, span)
        )
        if not expected:
            q.unexpected_gaps.append(Gap(prev.time, cur.time - missing_from))
    spreads = np.array([b.spread_points for b in bars], dtype=float)
    q.spread_p50 = float(np.percentile(spreads, 50))
    q.spread_p99 = float(np.percentile(spreads, 99))
    q.spread_max = int(spreads.max())
    if max_spread_points is not None:
        q.over_spread_gate = int((spreads > max_spread_points).sum())
    for b in bars:
        if b.high < max(b.open, b.close) or b.low > min(b.open, b.close):
            q.bad_ohlc += 1
        if b.high == b.low:
            q.zero_range += 1
    return q
