"""Positions held from one decision to the next, measured segment by segment (roadmap 11.12, docs/09 §9.2).

A momentum strategy decides on every closed decision bar which side to hold until the next decision. Each such
period is a ``Hold``; played on the price path it becomes a ``Segment`` — one research outcome, its R measured
on its own stop distance (the position is re-sized each period, as the strategy would live).

- **Entry** at the open of the first path bar at/after ``decided_at``, refused when that bar opens more than
  ``max_entry_delay`` minutes later, or inside the no-entries window before a long close (below).
- **Continuing boundary**: when the previous segment ended on time at the very bar this one enters, on the
  same side, the position simply carries on — the new segment enters at the mark (a BUY at the bid, a SELL at
  the ask), so no spread, commission or slippage is paid there. Any other entry is real and pays the spread (a
  BUY fills at the ask), half the round-turn commission and the slippage. ``stress`` makes every entry real.
- **Exits** with the harness rule (``harness._bid_ask_exit``: SL first, gaps at the open, a BUY exits on the
  bid, a SELL on the ask); on time at the open of the first bar at/after ``end_at``; and, for symbols that are
  shut over weekends (``flat_before_long_close``), at the open of the first bar at/after ``flatten_minutes``
  before a close of ``long_close_minutes`` or more — the engine's flatten (``position_management``). Long
  closes come from the path itself: a gap of that length after a bar is a session close the calendar knew in
  advance.
- An exit pays the slippage and the other half of the commission unless the next segment continues from it.
- **Swap** (``swap.SwapModel``) on every rollover the segment holds through; none without a model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum

import numpy as np

from aifund.domain.enums import CloseReason, Direction, Side, Timeframe, VirtualStatus
from aifund.domain.market import SymbolSpec
from aifund.research.basket.candles import DAY, Candles, Ints, utc, weekday
from aifund.research.basket.harness import _bid_ask_exit, _r
from aifund.research.basket.swap import SwapModel
from aifund.research.signals import CostModel, SignalOutcome

WEEK = 7 * DAY


class Skip(StrEnum):
    NO_ENTRY = "NO_ENTRY"  # no bar soon enough after the decision
    PRE_CLOSE = "PRE_CLOSE"  # the entry bar is inside the no-entries window before a long close
    BUSY = "BUSY"  # the previous segment had not ended yet
    OPEN_AT_END = "OPEN_AT_END"


@dataclass(frozen=True)
class Hold:
    """Hold ``side`` from the first bar at/after ``decided_at`` (a decision bar's close) until ``end_at``."""

    decided_at: int  # server minutes
    side: Side
    stop_distance: float  # price distance; also the segment's R unit
    end_at: int
    setup_tag: str
    max_entry_delay: int = 15

    @property
    def sign(self) -> int:
        return 1 if self.side is Side.BUY else -1


@dataclass(frozen=True)
class Segment:
    hold: Hold
    entry_index: int
    entry_minute: int
    entry_price: float
    exit_index: int
    exit_minute: int
    exit_price: float
    exit_reason: CloseReason
    real_entry: bool
    real_exit: bool
    r_gross: float  # marks and spread only
    mae_r: float
    mfe_r: float


@dataclass
class SegmentResult:
    symbol: str
    resolution: Timeframe
    holds: int = 0
    segments: list[Segment] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def long_closes(path: Candles, long_close_minutes: int, spans: Ints | None = None) -> list[int]:
    """Close times (server minutes) of the bars followed by a gap of ``long_close_minutes`` or more.
    ``spans``: each bar's length when the path is stitched from several timeframes (``stitched_spans``)."""
    if len(path) < 2:
        return []
    ends = path.t[:-1] + (path.span if spans is None else spans[:-1])
    gaps = path.t[1:] - ends
    return [int(e) for e in ends[gaps >= long_close_minutes]]


def stitched_spans(series: Sequence[Candles]) -> Ints:
    """Each bar's length in ``study.stitch(series)`` (coarse -> fine; the stitched series keeps the coarsest
    timeframe as its own, so its ``span`` is not every bar's)."""
    t: Ints = np.zeros(0, dtype=np.int64)
    spans: Ints = np.zeros(0, dtype=np.int64)
    for s in series:
        if len(s) == 0:
            continue
        cut = int(np.searchsorted(t, int(s.t[0]), side="left"))
        t = np.concatenate([t[:cut], s.t])
        spans = np.concatenate([spans[:cut], np.full(len(s), s.span, dtype=np.int64)])
    return spans


def trades_weekends(path: Candles) -> bool:
    """True when the market quotes on Saturdays in most weeks it quotes at all (crypto)."""
    if len(path) == 0:
        return False
    weeks = (path.t + 3 * DAY) // WEEK  # Monday-based weeks (1970-01-01 was a Thursday)
    saturday = np.array([weekday(int(m)) == 5 for m in path.t])
    all_weeks = set(weeks.tolist())
    sat_weeks = set(weeks[saturday].tolist())
    return len(sat_weeks) * 2 > len(all_weeks)


def play(
    holds: Sequence[Hold],
    path: Candles,
    *,
    symbol: str,
    start: int,
    end: int,
    flat_before_long_close: bool,
    flatten_minutes: int = 30,
    long_close_minutes: int = 24 * 60,
    stress: bool = False,
    spans: Ints | None = None,
) -> SegmentResult:
    """Play the holds decided in [start, end) in time order, one position at a time."""
    chosen = sorted((h for h in holds if start <= h.decided_at < end), key=lambda h: h.decided_at)
    result = SegmentResult(symbol=symbol, resolution=path.tf, holds=len(chosen))
    found = long_closes(path, long_close_minutes, spans) if flat_before_long_close else []
    closes = np.asarray(found, dtype=np.int64)
    previous: Segment | None = None
    pending: list[Segment] = []  # the last segment waits until we know whether the next one continues it
    for h in chosen:
        i0 = path.first_open_at_or_after(h.decided_at)
        if i0 >= len(path) or path.t[i0] - h.decided_at > h.max_entry_delay:
            result.skipped[Skip.NO_ENTRY] += 1
            continue
        if previous is not None and i0 < previous.exit_index:
            result.skipped[Skip.BUSY] += 1
            continue
        t0 = int(path.t[i0])
        k = int(np.searchsorted(closes, t0, side="right"))  # the first long close after the entry bar opens
        flat_at = int(closes[k]) - flatten_minutes if k < len(closes) else None
        if flat_at is not None and t0 >= flat_at:
            result.skipped[Skip.PRE_CLOSE] += 1
            continue
        continues = (
            not stress
            and previous is not None
            and previous.exit_reason is CloseReason.TIME_STOP
            and previous.exit_index == i0
            and previous.hold.side is h.side
        )
        if pending:
            result.segments.append(_with_exit(pending.pop(), real=not continues))
        seg = _simulate(h, path, i0, real_entry=not continues, flat_at=flat_at)
        if seg is None:
            result.skipped[Skip.OPEN_AT_END] += 1
            previous = None
            continue
        pending.append(seg)
        previous = seg
    if pending:
        result.segments.append(_with_exit(pending.pop(), real=True))
    return result


def _with_exit(seg: Segment, *, real: bool) -> Segment:
    return seg if seg.real_exit == real else replace(seg, real_exit=real)


def _simulate(h: Hold, path: Candles, i0: int, *, real_entry: bool, flat_at: int | None) -> Segment | None:
    sign, dist = h.sign, h.stop_distance
    o0, sp0 = float(path.o[i0]), float(path.spread[i0])
    # a real BUY fills at the ask and a real SELL at the bid; a carried-on position enters at the mark the
    # previous segment exited at: a long at the bid, a short at the ask
    at_ask = (h.side is Side.BUY) == real_entry
    entry = o0 + sp0 if at_ask else o0
    sl = entry - sign * dist
    ends = [h.end_at] + ([flat_at] if flat_at is not None else [])
    expires = min(ends)
    reason_at_end = CloseReason.TIME_STOP if expires == h.end_at else CloseReason.FLATTEN
    worst = best = 0.0
    for i in range(i0, len(path)):
        o, hi, lo, sp = float(path.o[i]), float(path.h[i]), float(path.low[i]), float(path.spread[i])
        if path.t[i] >= expires:
            price = o if h.side is Side.BUY else o + sp
            return _segment(h, path, i0, entry, i, price, reason_at_end, real_entry, worst, best)
        hit = _bid_ask_exit(h.side, sl, None, o, hi, lo, sp)
        adverse = lo if h.side is Side.BUY else hi + sp
        favour = hi if h.side is Side.BUY else lo + sp
        worst = min(worst, sign * (adverse - entry) / dist)
        best = max(best, sign * (favour - entry) / dist)
        if hit is not None:
            reason, price = hit
            return _segment(h, path, i0, entry, i, price, reason, real_entry, max(worst, -1.0), best)
    return None


def _segment(
    h: Hold,
    path: Candles,
    i0: int,
    entry: float,
    i1: int,
    price: float,
    reason: CloseReason,
    real_entry: bool,
    worst: float,
    best: float,
) -> Segment:
    r = h.sign * (price - entry) / h.stop_distance
    return Segment(
        hold=h,
        entry_index=i0,
        entry_minute=int(path.t[i0]),
        entry_price=entry,
        exit_index=i1,
        exit_minute=int(path.t[i1]),
        exit_price=price,
        exit_reason=reason,
        real_entry=real_entry,
        real_exit=True,
        r_gross=r,
        mae_r=worst,
        mfe_r=best,
    )


def leg_costs(spec: SymbolSpec, costs: CostModel) -> tuple[float, float]:
    """(half the round-turn commission, the slippage) as price distances for one real leg."""
    commission = float(costs.commission_per_lot * spec.tick_size / spec.tick_value)
    return commission / 2, float(spec.point * costs.slippage_points)


def outcomes(
    result: SegmentResult,
    *,
    spec: SymbolSpec,
    costs: CostModel,
    swap: SwapModel | None,
    offset_minutes: int,
    detector: str,
) -> list[SignalOutcome]:
    """The segments as research outcomes (UTC times; R net of commission, slippage and swap)."""
    half_commission, slip = leg_costs(spec, costs)
    out = []
    for s in result.segments:
        legs = int(s.real_entry) + int(s.real_exit)
        cost = half_commission * legs + slip * legs
        if swap is not None:
            cost += swap.charge(s.hold.side, s.entry_minute, s.exit_minute, s.entry_price)
        dist = s.hold.stop_distance
        out.append(
            SignalOutcome(
                symbol=result.symbol,
                bar_time=utc(s.hold.decided_at, offset_minutes),
                entry_time=utc(s.entry_minute, offset_minutes),
                direction=Direction.LONG if s.hold.side is Side.BUY else Direction.SHORT,
                setup_tag=s.hold.setup_tag,
                detector=detector,
                status=VirtualStatus.CLOSED if s.exit_reason is CloseReason.SL else VirtualStatus.EXPIRED,
                exit_reason=s.exit_reason,
                exit_time=utc(s.exit_minute, offset_minutes),
                entry_price=Decimal(repr(s.entry_price)),
                sl_distance=Decimal(repr(dist)),
                tp_distance=Decimal(0),
                r_gross=_r(s.r_gross),
                r_net=_r(s.r_gross - cost / dist),
                mae_r=_r(s.mae_r),
                mfe_r=_r(s.mfe_r),
                resolution=result.resolution,
                features={},
            )
        )
    return out
