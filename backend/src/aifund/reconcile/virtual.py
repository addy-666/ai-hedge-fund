"""Virtual trades (docs/03 §14.4): what a blocked signal would have done, with the same maths as real trades.

When a directional signal is stopped by a rule, the confidence threshold, a guard or a limit (see
``domain.trade.VIRTUAL_REASONS``), the pipeline records a PENDING virtual trade with the stop/target distances
stage 10 would have planned. This tracker then, from M1 bars:

- enters at the next trigger bar's open (the moment the blocked decision was made): a BUY pays the ask
  (bid open + that bar's spread), a SELL gets the bid, as a real fill would. (The spec's "open ± half spread"
  assumed mid prices; MT5 bars are bid prices.) SL/TP sit at exactly the planned distances from the fill, as
  the executor places them.
- exits with ``market.fills.exit_on_bar`` (the SimBroker's rule): stop first when both are touched in one
  bar, a gap through the stop fills at the bar open. Otherwise it expires at ``expires_at`` (the time stop,
  or the pre-close flatten if that comes first) at the market: the last bar's close, bid for a BUY, ask for
  a SELL.
- R = realised price move / stop distance (no commission: virtual trades have no size); MAE/MFE with the
  enrichment rule (full minutes held + the exit fill).

The state is recomputed from the entry every cycle (deterministic, no cursor to lose); a result is final
once CLOSED, EXPIRED or NO_ENTRY.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TypeVar

from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import CloseReason, DealReason, Side, Timeframe, VirtualStatus
from aifund.domain.market import Bar
from aifund.market.fills import exit_on_bar
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.persistence.tables import VirtualTradeRow
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort
from aifund.reconcile.enrichment import bars_during, excursion, excursion_r
from aifund.reconcile.pnl import R_PLACES

T = TypeVar("T")
ENTRY_WINDOW = timedelta(minutes=5)  # the entry bar must open within this of the planned entry time
MINUTE = timedelta(minutes=1)
_EXIT_REASON = {DealReason.SL: CloseReason.SL, DealReason.TP: CloseReason.TP}


@dataclass(frozen=True)
class VirtualPath:
    status: VirtualStatus
    entry_price: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    exit_time: datetime | None = None
    exit_price: Decimal | None = None
    exit_reason: CloseReason | None = None
    r_multiple: Decimal | None = None
    mae_r: Decimal | None = None
    mfe_r: Decimal | None = None


def simulate(
    *,
    side: Side,
    entry_time: datetime,
    sl_distance: Decimal,
    tp_distance: Decimal,
    expires_at: datetime,
    expire_reason: CloseReason,
    bars: Sequence[Bar],
    point: Decimal,
    now: datetime,
    give_up_after: timedelta = timedelta(days=1),
) -> VirtualPath:
    """Where a virtual trade stands at ``now``, given the CLOSED M1 bars from its entry time on."""
    ordered = sorted(
        (b for b in bars if b.time >= entry_time and b.time + MINUTE <= now), key=lambda b: b.time
    )
    entry_bar = next((b for b in ordered if b.time <= entry_time + ENTRY_WINDOW), None)
    if entry_bar is None:
        later = any(b.time > entry_time + ENTRY_WINDOW for b in ordered)
        gone = later or now - entry_time > give_up_after
        return VirtualPath(VirtualStatus.NO_ENTRY if gone else VirtualStatus.PENDING)

    sign = 1 if side is Side.BUY else -1
    entry = entry_bar.open + (point * entry_bar.spread_points if side is Side.BUY else Decimal(0))
    sl, tp = entry - sign * sl_distance, entry + sign * tp_distance
    held = [b for b in ordered if b.time >= entry_bar.time]

    def finish(status: VirtualStatus, when: datetime, price: Decimal, reason: CloseReason) -> VirtualPath:
        r = (sign * (price - entry) / sl_distance).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)
        ex = excursion(side, entry, bars_during(held, entry_bar.time, when), point, [price])
        mae, mfe = excursion_r(ex, sl_distance) if ex is not None else (None, None)
        return VirtualPath(status, entry, sl, tp, when, price, reason, r, mae, mfe)

    last: Bar | None = None
    for b in held:
        if b.time >= expires_at:
            break
        spread = point * b.spread_points
        fill = exit_on_bar(side, sl, tp, open_=b.open, high=b.high, low=b.low, spread=spread)
        if fill is not None:
            return finish(VirtualStatus.CLOSED, b.time, fill.price, _EXIT_REASON[fill.reason])
        last = b
    if now >= expires_at and last is not None:
        price = last.close + (point * last.spread_points if side is Side.SELL else Decimal(0))
        return finish(VirtualStatus.EXPIRED, expires_at, price, expire_reason)
    return VirtualPath(VirtualStatus.OPEN, entry, sl, tp)


# ---------------------------------------------------------------------------------------------- the job


@dataclass
class VirtualReport:
    entered: list[str] = field(default_factory=list)
    finished: list[tuple[str, VirtualStatus]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class VirtualTracker:
    def __init__(
        self,
        broker: BrokerPort,
        market: MarketDataPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
    ) -> None:
        self._broker = broker
        self._market = market
        self._factory = factory
        self._clock = clock

    def _run_tx(self, fn: Callable[[Session], T]) -> T:
        with unit_of_work(self._factory) as session:
            return fn(session)

    async def _tx(self, fn: Callable[[Session], T]) -> T:
        return await asyncio.to_thread(self._run_tx, fn)

    async def run_once(self) -> VirtualReport:
        report = VirtualReport()
        rows = await self._tx(lambda s: list(VirtualTradeRepository(s, self._clock).active()))
        now = self._clock.now()
        for v in rows:
            if now < v.entry_time + MINUTE:
                continue  # the entry bar has not closed yet
            try:
                await self._advance(v, now, report)
            except BrokerError as exc:
                report.errors.append(f"{v.id}: {exc}")
        return report

    async def _advance(self, v: VirtualTradeRow, now: datetime, report: VirtualReport) -> None:
        spec = await self._broker.symbol_spec(v.symbol)
        bars = await self._market.bars_range(v.symbol, Timeframe.M1, v.entry_time, min(now, v.expires_at))
        path = simulate(
            side=v.side,
            entry_time=v.entry_time,
            sl_distance=v.sl_distance,
            tp_distance=v.tp_distance,
            expires_at=v.expires_at,
            expire_reason=v.expire_reason,
            bars=bars,
            point=spec.point,
            now=now,
        )
        if path.status is VirtualStatus.PENDING or (path.status is VirtualStatus.OPEN and v.entry_price):
            return  # nothing new
        fields = {k: getattr(path, k) for k in ("entry_price", "sl", "tp")}
        if path.status is not VirtualStatus.OPEN:
            fields.update(
                exit_time=path.exit_time, exit_price=path.exit_price, exit_reason=path.exit_reason,
                r_multiple=path.r_multiple, mae_r=path.mae_r, mfe_r=path.mfe_r,
            )  # fmt: skip
        await self._tx(lambda s: VirtualTradeRepository(s, self._clock).update(v.id, path.status, **fields))
        if path.status is VirtualStatus.OPEN:
            report.entered.append(v.id)
        else:
            report.finished.append((v.id, path.status))
