"""Trade enrichment (docs/03 §14.3): what happened DURING a closed trade, for review and learning.

- MAE / MFE: the worst and best price reached while the trade was open: the M1 bars of every minute the
  trade was open IN FULL, plus its actual exit fills. A BUY is measured on bid prices (it exits at the bid),
  a SELL on ask prices (bid + that bar's spread). Stored in price and in R, where R is the INITIAL stop
  distance: ``mae_r`` <= 0, ``mfe_r`` >= 0. Never overstated: the entry and exit minutes' bars also hold
  prices from before the fill / after the exit (a stop-out minute can dip far below the stop), so they are
  left out and only the fills themselves count there.
- bars held (whole trigger-timeframe bars, as the time stop counts them) and holding minutes.
- slippage in points, positive = worse than requested: entry from the OPEN intent; exit against the stop /
  target level for SL/TP exits (the level MT5 writes in the deal comment, else the last known level), or the
  engine's closing intent for engine closes. Manual closes have no requested price: None.

Runs after the reconciler has closed a trade and marks it with ``enriched_at``, once. Deferred to later
phases: enqueueing the Trade Reviewer (Phase 7).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import DealReason, Side, Timeframe
from aifund.domain.market import Bar, Deal
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.repositories.trades import TradeRepository
from aifund.persistence.tables import TradeRow
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.reconcile.pnl import R_PLACES

T = TypeVar("T")
_LEVEL = re.compile(r"^\[(?:sl|tp) (\d+(?:\.\d+)?)\]")


# ---------------------------------------------------------------------------------------------- pure maths


@dataclass(frozen=True)
class Excursion:
    mae_price: Decimal  # >= 0: how far price went against the position
    mfe_price: Decimal  # >= 0: how far it went in favour


def bars_during(bars: Sequence[Bar], opened: datetime, closed: datetime) -> list[Bar]:
    """M1 bars of the minutes the trade was open for in full."""
    return [b for b in bars if b.time >= opened and b.time + timedelta(minutes=1) <= closed]


def excursion(
    side: Side, open_price: Decimal, bars: Sequence[Bar], point: Decimal, fills: Sequence[Decimal] = ()
) -> Excursion | None:
    """Worst / best prices over ``bars`` and the exit ``fills`` (prices the position actually exited at)."""
    if not bars and not fills:
        return None
    if side is Side.BUY:  # exits sell at the bid
        lows = [b.low for b in bars] + list(fills)
        highs = [b.high for b in bars] + list(fills)
        adverse, favourable = open_price - min(lows), max(highs) - open_price
    else:  # exits buy at the ask
        highs = [b.high + point * b.spread_points for b in bars] + list(fills)
        lows = [b.low + point * b.spread_points for b in bars] + list(fills)
        adverse, favourable = max(highs) - open_price, open_price - min(lows)
    zero = Decimal(0)
    return Excursion(mae_price=max(adverse, zero), mfe_price=max(favourable, zero))


def excursion_r(ex: Excursion, sl_distance: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    if sl_distance is None or sl_distance <= 0:
        return None, None
    mae = (-ex.mae_price / sl_distance).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)
    mfe = (ex.mfe_price / sl_distance).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)
    return abs(mae) if mae == 0 else mae, mfe  # no "-0.0000"


def holding(opened: datetime, closed: datetime, trigger_tf: Timeframe | None) -> tuple[int | None, int]:
    held = closed - opened
    bars = int(held / timedelta(minutes=trigger_tf.minutes)) if trigger_tf is not None else None
    return bars, int(held.total_seconds() // 60)


def level_from_comment(comment: str) -> Decimal | None:
    """The stop/target level MT5 records in an SL/TP exit deal's comment, e.g. ``[sl 4155.00]``."""
    match = _LEVEL.match(comment)
    return Decimal(match.group(1)) if match else None


def level_exit_slippage(
    position_side: Side, last_exit: Deal, known_level: Decimal | None, point: Decimal
) -> int | None:
    """Points worse than the stop/target level for SL/TP exits (negative = filled better); else None."""
    if last_exit.reason not in (DealReason.SL, DealReason.TP):
        return None
    level = level_from_comment(last_exit.comment) or known_level
    if level is None:
        return None
    # a BUY position exits by selling: a lower price is worse; a SELL position buys back: higher is worse
    worse = level - last_exit.price if position_side is Side.BUY else last_exit.price - level
    return int(worse / point)


# ---------------------------------------------------------------------------------------------- the job


@dataclass
class EnrichReport:
    enriched: list[int] = field(default_factory=list)
    waiting: list[int] = field(default_factory=list)  # bars not available yet
    errors: list[str] = field(default_factory=list)


class Enricher:
    """Enrich closed trades once. ``trigger_tfs`` maps broker symbol -> trigger timeframe (for orphans)."""

    def __init__(
        self,
        broker: BrokerPort,
        market: MarketDataPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
        *,
        trigger_tfs: Mapping[str, Timeframe],
        give_up_after: timedelta = timedelta(days=1),
    ) -> None:
        self._broker = broker
        self._market = market
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._trigger_tfs = trigger_tfs
        self._give_up_after = give_up_after

    def _run_tx(self, fn: Callable[[Session], T]) -> T:
        with unit_of_work(self._factory) as session:
            return fn(session)

    async def _tx(self, fn: Callable[[Session], T]) -> T:
        return await asyncio.to_thread(self._run_tx, fn)

    async def run_once(self, limit: int = 20) -> EnrichReport:
        report = EnrichReport()
        trades = await self._tx(lambda s: list(TradeRepository(s, self._clock).unenriched(limit)))
        for trade in trades:
            try:
                await self._enrich(trade, report)
            except BrokerError as exc:
                report.errors.append(f"{trade.position_id}: {exc}")
        return report

    async def _enrich(self, trade: TradeRow, report: EnrichReport) -> None:
        assert trade.close_time is not None  # unenriched() selects closed trades
        now = self._clock.now()
        if now < trade.close_time + timedelta(minutes=1):
            report.waiting.append(trade.position_id)  # the closing minute's bar has not closed yet
            return
        spec = await self._broker.symbol_spec(trade.symbol)
        start = trade.open_time.replace(second=0, microsecond=0)
        bars = bars_during(
            await self._market.bars_range(trade.symbol, Timeframe.M1, start, trade.close_time),
            trade.open_time,
            trade.close_time,
        )
        full_minutes = int((trade.close_time - trade.open_time) / timedelta(minutes=1)) - 1
        if len(bars) < min(full_minutes, 1) and now - trade.close_time < self._give_up_after:
            report.waiting.append(trade.position_id)  # history not synced yet: try again next cycle
            return
        deals = await self._broker.deals_for_position(trade.position_id)
        exits = sorted((d for d in deals if d.entry.reduces_position), key=lambda d: (d.time, d.ticket))
        ex = excursion(trade.side, trade.open_price, bars, spec.point, [d.price for d in exits])
        sl_distance = abs(trade.open_price - trade.initial_sl) if trade.initial_sl is not None else None
        mae_r, mfe_r = excursion_r(ex, sl_distance) if ex is not None else (None, None)
        tf = Timeframe(trade.trigger_tf) if trade.trigger_tf else self._trigger_tfs.get(trade.symbol)
        bars_held, minutes = holding(trade.open_time, trade.close_time, tf)

        def write(s: Session) -> TradeRow:
            intents = IntentRepository(s, self._clock)
            opening = intents.get(trade.intent_id) if trade.intent_id else None
            exit_slip = None
            if exits:
                level = trade.current_sl if exits[-1].reason is DealReason.SL else trade.current_tp
                exit_slip = level_exit_slippage(trade.side, exits[-1], level, spec.point)
                if exit_slip is None and exits[-1].reason is DealReason.EXPERT:
                    closes = [
                        i for i in intents.filled_closes(trade.position_id) if i.slippage_points is not None
                    ]
                    exit_slip = closes[-1].slippage_points if closes else None
            fields: dict[str, Any] = dict(
                mae_price=ex.mae_price if ex else None,
                mfe_price=ex.mfe_price if ex else None,
                mae_r=mae_r,
                mfe_r=mfe_r,
                bars_held=bars_held,
                holding_minutes=minutes,
                entry_slippage_points=opening.slippage_points if opening else None,
                exit_slippage_points=exit_slip,
            )
            return TradeRepository(s, self._clock).enrich(trade.position_id, **fields)

        row = await self._tx(write)
        report.enriched.append(trade.position_id)
        await self._notifier.notify(
            Severity.INFO, f"Trade closed: {row.symbol} {row.side.value}", _summary(row)
        )


def _summary(t: TradeRow) -> str:
    r = f"{t.r_multiple:+}R" if t.r_multiple is not None else "no R (orphan)"
    parts = [f"{t.close_reason.value if t.close_reason else '?'}", f"net {t.net_pnl}", r]
    if t.mae_r is not None and t.mfe_r is not None:
        parts.append(f"MAE {t.mae_r}R / MFE {t.mfe_r}R")
    parts.append(f"held {t.holding_minutes} min")  # always set once enriched
    return ", ".join(parts)
