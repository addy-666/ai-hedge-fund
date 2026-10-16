"""Turn a strategy's signals into trades on server-time candles (roadmap 11.2). Research only.

A strategy emits ``Signal``s decided on CLOSED bars (``decided_at`` = the signal bar's close). The harness
plays them one at a time per symbol, like the EAs' one-position rule:

- **market** entry at the open of the first path bar at/after ``decided_at``, refused when that bar opens
  more than ``max_entry_delay`` minutes later (an EA skips a stale signal); a BUY pays the bar's spread.
- **stop** entry (``stop_price``): pending like a limit; a BUY fills when the ask high reaches the price (at
  the worse of price and ask open), a SELL when the bid low does; refused when the first bar already opens
  more than ``max_chase`` past the price; cancelled when the stop side trades first. A bar that touches both
  the entry and the stop fills (and then stops out on the same bar): the pessimistic reading.
- **limit** entry: pending from ``decided_at`` until ``limit_expires_at`` or the next signal (which replaces
  it), cancelled when price trades beyond ``cancel_beyond`` or touches the stop first; a BUY fills when the
  ask low reaches the limit (at the better of limit and ask open), a SELL when the bid high does. No target
  on the fill bar (the bar's order of events is unknown).
- **stop**: ``stop_level`` (structure; distance = entry to level) or ``stop_distance``; raised to
  ``stop_floor``; above ``stop_cap`` it is clamped (``cap_skips`` False) or the trade is skipped.
- **target**: ``take_profit_r`` × the stop distance, or ``take_profit_level``; none = time/flat exit only.
- **exits** on each path bar with the production rule of ``market/fills.exit_on_bar`` (SL before TP when a bar
  touches both, gaps fill at the open, BUY exits on the bid, SELL on the ask); at ``max_hold`` after the fill
  or the next ``flats`` time, whichever is first, at the open of the first bar at/after it.
- ``max_trades_per_day``: entries per server day (the EAs' daily cap).

Paths use the finest timeframe exported for the whole window (M1 → M5 → M15 → M30 → H1); a coarser path is
more pessimistic (more bars touch both levels) and may run up to one bar past a time exit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from aifund.domain.enums import CloseReason, Direction, Side, Timeframe, VirtualStatus
from aifund.domain.market import SymbolSpec
from aifund.reconcile.pnl import R_PLACES
from aifund.research.basket.candles import DAY, Candles, day_of, utc, weekday
from aifund.research.signals import CostModel, SignalOutcome


class Skip(StrEnum):
    BUSY = "BUSY"  # a position of this strategy was open
    DAILY_CAP = "DAILY_CAP"
    NO_ENTRY = "NO_ENTRY"  # no bar soon enough after the signal / the limit never filled
    CHASED = "CHASED"  # a stop entry whose first bar opened too far past the price
    CANCELLED = "CANCELLED"  # limit cancelled (beyond, stop touched, expired or replaced)
    BAD_STOP = "BAD_STOP"  # stop on the wrong side of the fill
    STOP_TOO_WIDE = "STOP_TOO_WIDE"
    BAD_TARGET = "BAD_TARGET"
    OPEN_AT_END = "OPEN_AT_END"


@dataclass(frozen=True)
class FlatRule:
    """Close at ``minute`` (server minute of day); ``weekday`` 0=Mon…6=Sun, or None for every day."""

    minute: int
    weekday: int | None = None

    def next_after(self, minute: int) -> int:
        """The first time at/after ``minute`` this rule fires."""
        day = day_of(minute)
        for k in range(8):
            at = (day + k) * DAY + self.minute
            if at >= minute and (self.weekday is None or weekday(at) == self.weekday):
                return at
        raise AssertionError("a weekly rule fires within 8 days")  # pragma: no cover - by construction


@dataclass(frozen=True)
class Signal:
    decided_at: int  # server minute: the signal bar's close
    side: Side
    setup_tag: str
    stop_level: float | None = None
    stop_distance: float | None = None
    stop_floor: float = 0.0
    stop_cap: float | None = None
    cap_skips: bool = False
    take_profit_r: float | None = None
    take_profit_level: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None  # a stop entry (exclusive with limit_price); expires like a limit
    max_chase: float | None = None  # stop entry: price distance the first bar may open past it
    limit_expires_at: int | None = None
    cancel_beyond: float | None = None
    max_hold: int | None = None  # minutes after the fill
    flats: tuple[FlatRule, ...] = ()
    max_entry_delay: int = 15

    @property
    def sign(self) -> int:
        return 1 if self.side is Side.BUY else -1

    @property
    def pending(self) -> bool:
        """A pending order (limit or stop) rather than a market entry."""
        return self.limit_price is not None or self.stop_price is not None


@dataclass(frozen=True)
class Trade:
    signal: Signal
    entry_minute: int
    entry_price: float
    stop_distance: float
    exit_minute: int
    exit_price: float
    exit_reason: CloseReason
    r: float
    mae_r: float
    mfe_r: float


@dataclass
class BasketResult:
    symbol: str
    resolution: Timeframe
    signals: int = 0
    trades: list[Trade] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def _bid_ask_exit(
    side: Side, sl: float, tp: float | None, o: float, h: float, lo: float, spread: float
) -> tuple[CloseReason, float] | None:
    """``market/fills.exit_on_bar`` on floats."""
    if side is Side.BUY:
        if lo <= sl:
            return CloseReason.SL, (o if o <= sl else sl)
        if tp is not None and h >= tp:
            return CloseReason.TP, tp
        return None
    ao, ah, al = o + spread, h + spread, lo + spread
    if ah >= sl:
        return CloseReason.SL, (ao if ao >= sl else sl)
    if tp is not None and al <= tp:
        return CloseReason.TP, tp
    return None


def _fill_stop(
    sig: Signal, path: Candles, start: int, end: int | None, price: float
) -> tuple[int, float] | Skip:
    buy = sig.side is Side.BUY
    if (
        start < len(path)
        and sig.max_chase is not None
        and path.t[start] - sig.decided_at <= sig.max_entry_delay
    ):
        opened = path.o[start] + path.spread[start] if buy else path.o[start]
        if sig.sign * (opened - price) > sig.max_chase:
            return Skip.CHASED
    for i in range(start, len(path)):
        if end is not None and path.t[i] >= end:
            return Skip.CANCELLED
        o, h, lo, sp = path.o[i], path.h[i], path.low[i], path.spread[i]
        if buy:
            if h + sp >= price:
                return i, float(max(price, o + sp))
            if sig.stop_level is not None and lo <= sig.stop_level:
                return Skip.CANCELLED
        else:
            if lo <= price:
                return i, float(min(price, o))
            if sig.stop_level is not None and h + sp >= sig.stop_level:
                return Skip.CANCELLED
    return Skip.NO_ENTRY


def _fill(sig: Signal, path: Candles, start: int, until: int | None) -> tuple[int, float] | Skip:
    """(path index, price) of the entry, or why there is none."""
    end = sig.limit_expires_at if until is None else min(until, sig.limit_expires_at or until)
    if sig.stop_price is not None:
        return _fill_stop(sig, path, start, end, sig.stop_price)
    if sig.limit_price is None:
        if start >= len(path) or path.t[start] - sig.decided_at > sig.max_entry_delay:
            return Skip.NO_ENTRY
        price = path.o[start] + (path.spread[start] if sig.side is Side.BUY else 0.0)
        return start, float(price)
    limit = sig.limit_price
    for i in range(start, len(path)):
        if end is not None and path.t[i] >= end:
            return Skip.CANCELLED
        o, h, lo, sp = path.o[i], path.h[i], path.low[i], path.spread[i]
        if sig.side is Side.BUY:
            beyond = sig.cancel_beyond is not None and h > sig.cancel_beyond
            if beyond or (sig.stop_level is not None and lo <= sig.stop_level):
                return Skip.CANCELLED
            if lo + sp <= limit:
                return i, float(min(limit, o + sp))
        else:
            beyond = sig.cancel_beyond is not None and lo < sig.cancel_beyond
            if beyond or (sig.stop_level is not None and h + sp >= sig.stop_level):
                return Skip.CANCELLED
            if h >= limit:
                return i, float(max(limit, o))
    return Skip.NO_ENTRY


def _stop_distance(sig: Signal, entry: float) -> float | Skip:
    if sig.stop_distance is not None:
        raw = sig.stop_distance
    elif sig.stop_level is not None:
        raw = sig.sign * (entry - sig.stop_level)
    else:
        raise ValueError("a signal needs stop_level or stop_distance")
    dist = max(raw, sig.stop_floor)
    if sig.stop_cap is not None and dist > sig.stop_cap:
        if sig.cap_skips:
            return Skip.STOP_TOO_WIDE
        dist = sig.stop_cap
    return dist if dist > 0 else Skip.BAD_STOP


def simulate(sig: Signal, path: Candles, *, until: int | None = None) -> Trade | Skip:
    """One signal on ``path``; ``until``: when a newer signal replaces a still-pending limit."""
    start = path.first_open_at_or_after(sig.decided_at)
    filled = _fill(sig, path, start, until)
    if isinstance(filled, Skip):
        return filled
    i0, entry = filled
    dist: float | Skip
    if sig.pending and sig.stop_level is not None:
        raw = sig.sign * (entry - sig.stop_level)  # a pending order's stop is a level: no floor or cap
        dist = raw if raw > 0 else Skip.BAD_STOP
    else:
        dist = _stop_distance(sig, entry)
    if isinstance(dist, Skip):
        return dist
    sl = entry - sig.sign * dist
    if sig.take_profit_level is not None:
        tp: float | None = sig.take_profit_level
        if sig.sign * (sig.take_profit_level - entry) <= 0:
            return Skip.BAD_TARGET
    elif sig.take_profit_r is not None:
        tp = entry + sig.sign * sig.take_profit_r * dist
    else:
        tp = None
    t0 = int(path.t[i0])
    ends = [r.next_after(t0) for r in sig.flats]
    if sig.max_hold is not None:
        ends.append(t0 + sig.max_hold)
    expires = min(ends) if ends else None
    timed = sig.max_hold is not None and expires == t0 + sig.max_hold
    hold_reason = CloseReason.TIME_STOP if timed else CloseReason.FLATTEN
    worst = best = 0.0
    for i in range(i0, len(path)):
        o, h, lo, sp = float(path.o[i]), float(path.h[i]), float(path.low[i]), float(path.spread[i])
        if expires is not None and path.t[i] >= expires:
            price = o if sig.side is Side.BUY else o + sp
            return _trade(sig, t0, entry, dist, int(path.t[i]), price, hold_reason, worst, best)
        target = None if (i == i0 and sig.pending) else tp
        hit = _bid_ask_exit(sig.side, sl, target, o, h, lo, sp)
        adverse = lo if sig.side is Side.BUY else h + sp
        favour = h if sig.side is Side.BUY else lo + sp
        worst = min(worst, sig.sign * (adverse - entry) / dist)
        best = max(best, sig.sign * (favour - entry) / dist)
        if hit is not None:
            reason, price = hit
            return _trade(sig, t0, entry, dist, int(path.t[i]), price, reason, max(worst, -1.0), best)
    return Skip.OPEN_AT_END


def _trade(
    sig: Signal,
    t0: int,
    entry: float,
    dist: float,
    t1: int,
    price: float,
    reason: CloseReason,
    worst: float,
    best: float,
) -> Trade:
    r = sig.sign * (price - entry) / dist
    return Trade(sig, t0, entry, dist, t1, price, reason, r, worst, best)


def run(
    signals: Sequence[Signal],
    path: Candles,
    *,
    symbol: str,
    start: int,
    end: int,
    max_trades_per_day: int | None = None,
) -> BasketResult:
    """Play the signals decided in [start, end) in time order, one position at a time."""
    chosen = sorted((s for s in signals if start <= s.decided_at < end), key=lambda s: s.decided_at)
    result = BasketResult(symbol=symbol, resolution=path.tf, signals=len(chosen))
    busy_until = -1
    per_day: Counter[int] = Counter()
    for k, sig in enumerate(chosen):
        if sig.decided_at < busy_until:
            result.skipped[Skip.BUSY] += 1
            continue
        if max_trades_per_day is not None and per_day[day_of(sig.decided_at)] >= max_trades_per_day:
            result.skipped[Skip.DAILY_CAP] += 1
            continue
        nxt = chosen[k + 1].decided_at if k + 1 < len(chosen) else None
        out = simulate(sig, path, until=nxt if sig.pending else None)
        if isinstance(out, Skip):
            result.skipped[out] += 1
            continue
        per_day[day_of(out.entry_minute)] += 1
        result.trades.append(out)
        busy_until = out.exit_minute
    return result


def outcomes(
    result: BasketResult,
    *,
    spec: SymbolSpec,
    costs: CostModel,
    offset_minutes: int,
    detector: str,
) -> list[SignalOutcome]:
    """The trades as the research pipeline's outcomes (UTC times, R net of the cost model)."""
    out = []
    for tr in result.trades:
        status = (
            VirtualStatus.CLOSED
            if tr.exit_reason in (CloseReason.SL, CloseReason.TP)
            else (VirtualStatus.EXPIRED)
        )
        cost = float(costs.cost_price(spec, tr.exit_reason))
        gross = _r(tr.r)
        net = _r(tr.r - cost / tr.stop_distance)
        direction = Direction.LONG if tr.signal.side is Side.BUY else Direction.SHORT
        out.append(
            SignalOutcome(
                symbol=result.symbol,
                bar_time=utc(tr.signal.decided_at, offset_minutes),
                entry_time=utc(tr.entry_minute, offset_minutes),
                direction=direction,
                setup_tag=tr.signal.setup_tag,
                detector=detector,
                status=status,
                exit_reason=tr.exit_reason,
                exit_time=utc(tr.exit_minute, offset_minutes),
                entry_price=Decimal(repr(tr.entry_price)),
                sl_distance=Decimal(repr(tr.stop_distance)),
                tp_distance=Decimal(0),
                r_gross=gross,
                r_net=net,
                mae_r=_r(tr.mae_r),
                mfe_r=_r(tr.mfe_r),
                resolution=result.resolution,
                features={},
            )
        )
    return out


def _r(x: float) -> Decimal:
    return Decimal(repr(round(x, 6))).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)
