"""Reconciler (docs/03 §14.1): keep the trade ledger in agreement with the broker.

Every cycle (every 30 s, on startup, after every execution):

- each broker position with the engine magic gets exactly one trade: OPEN when a FILLED OPEN intent owns it,
  ORPHAN_OPEN (with an alert) when nothing of ours explains it. A symbol with an intent still in flight is
  deferred: the executor is settling that fill and owns the answer. (The spec had the reconciler resolve
  UNKNOWN intents itself; the executor's resolver already does that, and only the executor may move an
  intent's state.) A missing stop-loss on an orphan is repaired by the position manager, which manages
  every position with the engine magic.
- open trades are kept current: SL/TP, remaining volume (a decrease is a partial close; its deals are
  mirrored).
- a trade whose position has gone is closed from the position's deals once exit deals cover the full volume
  (position-scoped history query, no date windows). Until they do it retries every cycle, and after
  ``vanish_alert_after`` it raises one critical alert: "position vanished without closing deals".
- a FILLED OPEN intent whose position has already gone and never got a trade (it hit SL/TP between two
  cycles, or while the engine was down) gets its trade created and closed in the same transaction.

The database is read BEFORE the broker is asked for positions, so a fill that lands in between shows up as
a position whose symbol still has an in-flight intent (deferred), never as a false orphan.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import CloseReason, EventType, Side, TradeStatus
from aifund.domain.ids import new_id
from aifund.domain.intent import COMMENT_PREFIX, engine_close_reason
from aifund.domain.market import Deal, Position
from aifund.domain.trade import closed_status
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.repositories.system import EventRepository
from aifund.persistence.repositories.trades import DealRepository, TradeRepository
from aifund.persistence.tables import DecisionRow, OrderIntentRow, TradeRow
from aifund.ports.broker import BrokerError, BrokerPort
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.reconcile import pnl

T = TypeVar("T")
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ReconcilerConfig:
    vanish_alert_after: timedelta = timedelta(minutes=5)


@dataclass
class ReconcileReport:
    opened: list[int] = field(default_factory=list)
    orphans: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    partial: list[int] = field(default_factory=list)
    closed: list[tuple[int, CloseReason]] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)  # symbol has an intent in flight: next cycle
    waiting: list[int] = field(default_factory=list)  # gone from the broker, exit deals not complete yet
    alerts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Ledger:
    open_trades: dict[int, TradeRow]
    unseen_fills: dict[int, OrderIntentRow]  # FILLED OPEN intents without a trade, by position id
    busy_symbols: set[str]


class Reconciler:
    def __init__(
        self,
        broker: BrokerPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
        *,
        account_id: str,
        magic: int,
        config: ReconcilerConfig | None = None,
    ) -> None:
        self._broker = broker
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._account = account_id
        self._magic = magic
        self._cfg = config or ReconcilerConfig()
        self._missing_since: dict[int, datetime] = {}
        self._alerted: set[int] = set()

    # ------------------------------------------------------------------ persistence

    def _run_tx(self, fn: Callable[[Session], T]) -> T:
        with unit_of_work(self._factory) as session:
            return fn(session)

    async def _tx(self, fn: Callable[[Session], T]) -> T:
        return await asyncio.to_thread(self._run_tx, fn)

    def _load(self, session: Session) -> _Ledger:
        intents = IntentRepository(session, self._clock)
        return _Ledger(
            open_trades={t.position_id: t for t in TradeRepository(session, self._clock).open_trades()},
            unseen_fills={
                i.position_id: i for i in intents.filled_opens_without_trade() if i.position_id is not None
            },
            busy_symbols={i.symbol for i in intents.non_terminal()},
        )

    def _event(self, session: Session, type_: EventType, severity: Severity, **payload: Any) -> None:
        EventRepository(session, self._clock).append(
            type_, severity, {k: (str(v) if isinstance(v, Decimal) else v) for k, v in payload.items()}
        )

    # ------------------------------------------------------------------ cycle

    async def run_once(self) -> ReconcileReport:
        report = ReconcileReport()
        ledger = await self._tx(self._load)  # BEFORE positions(): see the module docstring
        try:
            live = {p.position_id: p for p in await self._broker.positions() if p.magic == self._magic}
        except BrokerError as exc:
            report.errors.append(f"positions: {exc}")
            return report

        for pos in live.values():
            self._missing_since.pop(pos.position_id, None)  # back (a transient gap): restart its timer
            self._alerted.discard(pos.position_id)
            trade = ledger.open_trades.get(pos.position_id)
            if trade is not None:
                await self._refresh(trade, pos, report)
            elif pos.position_id in ledger.unseen_fills:
                await self._open(ledger.unseen_fills[pos.position_id], pos, report)
            elif pos.symbol in ledger.busy_symbols:
                report.deferred.append(pos.position_id)
            else:
                await self._orphan(pos, report)

        for position_id, trade in ledger.open_trades.items():
            if position_id not in live:
                await self._settle(position_id, trade.symbol, trade.volume_opened, report, trade=trade)
        for position_id, intent in ledger.unseen_fills.items():
            if position_id not in live:
                volume = intent.fill_volume or intent.volume
                await self._settle(position_id, intent.symbol, volume, report, intent=intent)
        return report

    # ------------------------------------------------------------------ live positions

    async def _refresh(self, trade: TradeRow, pos: Position, report: ReconcileReport) -> None:
        changes: dict[str, Any] = {}
        if pos.volume != trade.volume_open_now:
            changes["volume_open_now"] = pos.volume
        if pos.sl != trade.current_sl:
            changes["current_sl"] = pos.sl
        if pos.tp != trade.current_tp:
            changes["current_tp"] = pos.tp
        if not changes:
            return
        partial = pos.volume < trade.volume_open_now
        deals: list[Deal] = []
        if partial:
            try:
                deals = await self._broker.deals_for_position(pos.position_id)
            except BrokerError as exc:
                report.errors.append(f"deals {pos.position_id}: {exc}")
                return

        def write(s: Session) -> None:
            TradeRepository(s, self._clock).update_open(pos.position_id, **changes)
            if partial:
                DealRepository(s).upsert(deals, account_id=self._account)
                self._event(
                    s, EventType.TRADE_PARTIAL_CLOSE, Severity.INFO, trade_id=trade.id,
                    position_id=pos.position_id, volume_open_now=pos.volume, was=trade.volume_open_now,
                )  # fmt: skip

        await self._tx(write)
        (report.partial if partial else report.updated).append(pos.position_id)

    async def _open(self, intent: OrderIntentRow, pos: Position, report: ReconcileReport) -> None:
        def write(s: Session) -> None:
            trade = TradeRepository(s, self._clock).add(
                **self._engine_trade(s, intent),
                open_time=pos.time,
                open_price=pos.price_open,
                volume_opened=intent.fill_volume or pos.volume,
                volume_open_now=pos.volume,
                initial_sl=pos.sl,
                initial_tp=pos.tp,
                current_sl=pos.sl,
                current_tp=pos.tp,
            )
            self._event(
                s, EventType.TRADE_OPENED, Severity.INFO, trade_id=trade.id, position_id=pos.position_id,
                symbol=pos.symbol, side=pos.side.value, volume=pos.volume, price=pos.price_open,
            )  # fmt: skip

        await self._tx(write)
        report.opened.append(pos.position_id)

    async def _orphan(self, pos: Position, report: ReconcileReport) -> None:
        def write(s: Session) -> None:
            trade = TradeRepository(s, self._clock).add(
                id=new_id(),
                account_id=self._account,
                position_id=pos.position_id,
                symbol=pos.symbol,
                side=pos.side,
                status=TradeStatus.ORPHAN_OPEN,
                open_time=pos.time,
                open_price=pos.price_open,
                volume_opened=pos.volume,
                volume_open_now=pos.volume,
                initial_sl=pos.sl,
                initial_tp=pos.tp,
                current_sl=pos.sl,
                current_tp=pos.tp,
            )
            self._event(
                s, EventType.TRADE_ORPHAN, Severity.WARN, trade_id=trade.id, position_id=pos.position_id,
                symbol=pos.symbol, comment=pos.comment,
            )  # fmt: skip

        await self._tx(write)
        message = (
            f"orphan position {pos.position_id} {pos.symbol} {pos.side.value} {pos.volume}: engine magic, "
            f"no intent of ours (comment {pos.comment!r}); tracked as ORPHAN_OPEN"
        )
        report.orphans.append(pos.position_id)
        report.alerts.append(message)
        log.warning("reconcile.orphan", position_id=pos.position_id, symbol=pos.symbol)
        await self._notifier.notify(Severity.WARN, "Orphan position", message)

    def _engine_trade(self, s: Session, intent: OrderIntentRow) -> dict[str, Any]:
        decision = s.get(DecisionRow, intent.decision_id) if intent.decision_id else None
        proposal = decision.proposal if decision is not None and isinstance(decision.proposal, dict) else {}
        assert intent.position_id is not None  # unseen fills are selected with a position id
        return dict(
            id=new_id(),
            account_id=self._account,
            position_id=intent.position_id,
            intent_id=intent.id,
            decision_id=intent.decision_id,
            snapshot_id=decision.snapshot_id if decision is not None else None,
            symbol=intent.symbol,
            side=intent.side,
            setup_tag=proposal.get("setup_tag"),
            trigger_tf=decision.trigger_tf if decision is not None else None,
            status=TradeStatus.OPEN,
            initial_risk_money=intent.risk_money,
        )

    # ------------------------------------------------------------------ positions that have gone

    async def _settle(
        self,
        position_id: int,
        symbol: str,
        volume_opened: Decimal,
        report: ReconcileReport,
        *,
        trade: TradeRow | None = None,
        intent: OrderIntentRow | None = None,
    ) -> None:
        try:
            deals = await self._broker.deals_for_position(position_id)
        except BrokerError as exc:
            report.errors.append(f"deals {position_id}: {exc}")
            return
        summary = pnl.aggregate(deals)
        if not summary.fully_closed(volume_opened):
            await self._still_missing(position_id, symbol, report)
            return

        def write(s: Session) -> CloseReason:
            trades = TradeRepository(s, self._clock)
            if trade is None:
                assert intent is not None
                self._create_from_deals(trades, s, intent, deals, summary)
            row = trades.by_position(position_id)
            assert row is not None
            assert summary.last_exit is not None
            assert summary.close_time is not None
            reason = pnl.close_reason(
                summary.last_exit, self._engine_reason(s, position_id, summary.last_exit)
            )
            r = pnl.r_multiple(summary.net, row.initial_risk_money)
            DealRepository(s).upsert(deals, account_id=self._account)
            trades.close(
                position_id,
                closed_status(row.status),
                close_time=summary.close_time,
                close_price_vwap=summary.close_price_vwap,
                close_reason=reason,
                gross_profit=summary.gross,
                commission=summary.commission,
                swap=summary.swap,
                fee=summary.fee,
                net_pnl=summary.net,
                r_multiple=r,
                outcome=pnl.outcome(r),
            )
            self._event(
                s, EventType.TRADE_CLOSED, Severity.INFO, trade_id=row.id, position_id=position_id,
                symbol=row.symbol, close_reason=reason.value, net_pnl=summary.net, r_multiple=r,
            )  # fmt: skip
            return reason

        reason = await self._tx(write)
        self._missing_since.pop(position_id, None)
        self._alerted.discard(position_id)
        report.closed.append((position_id, reason))

    def _create_from_deals(
        self,
        trades: TradeRepository,
        s: Session,
        intent: OrderIntentRow,
        deals: list[Deal],
        summary: pnl.PositionPnl,
    ) -> None:
        """A fill whose position closed before any cycle saw it open: build the trade from its deals."""
        entry = next(
            (d for d in sorted(deals, key=lambda d: (d.time, d.ticket)) if not d.entry.reduces_position), None
        )
        open_price = entry.price if entry else intent.fill_price or intent.price_ref
        sl = tp = None
        if intent.sl_distance is not None and intent.tp_distance is not None:
            sign = 1 if intent.side is Side.BUY else -1  # the executor places SL/TP at these distances
            sl, tp = open_price - sign * intent.sl_distance, open_price + sign * intent.tp_distance
        volume = summary.volume_in or intent.fill_volume or intent.volume
        trades.add(
            **self._engine_trade(s, intent),
            open_time=entry.time if entry else intent.resolved_at or intent.created_at,
            open_price=open_price,
            volume_opened=volume,
            volume_open_now=volume,
            initial_sl=sl,
            initial_tp=tp,
        )

    def _engine_reason(self, s: Session, position_id: int, last_exit: Deal) -> CloseReason | None:
        """The close reason our closing intent recorded, if the last exit deal is one of ours."""
        closes = IntentRepository(s, self._clock).filled_closes(position_id)
        for i in closes:
            if (
                (i.deal_ticket is not None and i.deal_ticket == last_exit.ticket)
                or (i.order_ticket is not None and i.order_ticket == last_exit.order)
                or (last_exit.comment and last_exit.comment == i.comment)
            ):
                return engine_close_reason(i.kind, i.close_reason)
        if last_exit.magic == self._magic and last_exit.comment.startswith(COMMENT_PREFIX):
            # ours, but its intent could not be matched (e.g. a broker rewrote the ticket fields)
            return (
                engine_close_reason(closes[-1].kind, closes[-1].close_reason)
                if closes
                else CloseReason.ENGINE
            )
        return None

    async def _still_missing(self, position_id: int, symbol: str, report: ReconcileReport) -> None:
        now = self._clock.now()
        since = self._missing_since.setdefault(position_id, now)
        report.waiting.append(position_id)
        if now - since < self._cfg.vanish_alert_after or position_id in self._alerted:
            return
        self._alerted.add(position_id)
        message = (
            f"position {position_id} {symbol} vanished without closing deals covering its volume "
            f"(missing since {since:%Y-%m-%d %H:%M:%S} UTC); still retrying every cycle"
        )
        report.alerts.append(message)
        log.error("reconcile.vanished", position_id=position_id, symbol=symbol)
        await self._tx(
            lambda s: self._event(
                s, EventType.TRADE_VANISHED, Severity.CRITICAL, position_id=position_id, symbol=symbol
            )
        )
        await self._notifier.notify(Severity.CRITICAL, "Position vanished", message)
