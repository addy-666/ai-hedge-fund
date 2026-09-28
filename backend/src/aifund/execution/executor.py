"""Order execution (docs/03 §12) — the only code that calls ``BrokerPort.order_send``.

Crash-safety comes from ordering: every state is committed BEFORE the next step, in particular SENT is
committed before ``order_send``. After a crash at any line, ``recover()`` finds the intent in a state that
says exactly what might have happened:

- PENDING  → never sent → REJECTED (abandoned);
- RETRYING → the last send was rejected and nothing is pending → REJECTED (abandoned between retries);
- SENT / UNKNOWN → maybe executed → resolved against the broker (positions, then deals, by the
  intent's comment code, then by magic/symbol/side/volume/time); if nothing is found within the grace
  period → REJECTED (UNKNOWN_NOT_EXECUTED), otherwise it stays UNKNOWN and keeps its symbol locked.

``order_send`` is never retried blindly: only RETRYABLE codes (requote-type) are resent, bounded, and only
while the price is within half the stop distance of the risk-checked price. PAUSE-type codes (AutoTrading
off, no money, market closed, …) reject and ask the engine to pause new entries.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import IntentKind, IntentStatus, ReasonCode, Side
from aifund.domain.errors import DuplicateIntentError, InvariantViolation
from aifund.domain.intent import CLOSING_KINDS, OrderIntent
from aifund.domain.market import OrderRequest, OrderResult, SymbolSpec, TradeAction
from aifund.execution.filling import choose_filling
from aifund.execution.retcodes import RetcodeClass, classify
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.tables import OrderIntentRow
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort

T = TypeVar("T")


@dataclass(frozen=True)
class ExecutorConfig:
    deviation_points: int = 20
    max_attempts: int = 3
    unknown_grace: timedelta = timedelta(minutes=2)
    deal_search_window: timedelta = timedelta(seconds=60)
    max_reprice_fraction_of_stop: Decimal = Decimal("0.5")


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: str
    status: IntentStatus | None  # None only for a duplicate (nothing persisted, nothing sent)
    duplicate: bool = False
    retcode: int | None = None
    retcode_name: str | None = None
    position_id: int | None = None
    fill_price: Decimal | None = None
    fill_volume: Decimal | None = None
    slippage_points: int | None = None
    pause_engine: bool = False
    reason: ReasonCode | None = None
    detail: str = ""


class Executor:
    def __init__(
        self,
        broker: BrokerPort,
        market: MarketDataPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        *,
        account_id: str,
        config: ExecutorConfig | None = None,
        checkpoint: Callable[[str], None] = lambda _stage: None,
    ) -> None:
        self._broker = broker
        self._market = market
        self._factory = factory
        self._clock = clock
        self._account = account_id
        self._cfg = config or ExecutorConfig()
        self._checkpoint = checkpoint  # test hook: raise here to simulate a crash at that stage

    # ------------------------------------------------------------------ persistence (short transactions)

    def _run_tx(self, fn: Callable[[IntentRepository], T]) -> T:
        with unit_of_work(self._factory) as session:
            return fn(IntentRepository(session, self._clock))

    async def _tx(self, fn: Callable[[IntentRepository], T]) -> T:
        return await asyncio.to_thread(self._run_tx, fn)

    async def _move(self, intent_id: str, status: IntentStatus, **fields: object) -> OrderIntentRow:
        return await self._tx(lambda repo: repo.transition(intent_id, status, **fields))

    # ------------------------------------------------------------------ request building

    async def _build(self, row: OrderIntentRow, spec: SymbolSpec) -> OrderRequest | ExecutionResult:
        kind = row.kind
        if kind is IntentKind.MODIFY_SLTP:
            return OrderRequest(
                action=TradeAction.SLTP,
                symbol=row.symbol,
                position_ticket=row.position_ticket,
                sl=row.sl,
                tp=row.tp,
                magic=row.magic,
            )
        tick = await self._market.tick(row.symbol)
        if tick is None:
            return ExecutionResult(row.id, None, reason=ReasonCode.MARKET_CLOSED, detail="no quote")
        price = tick.entry_price(row.side)
        common = dict(
            action=TradeAction.DEAL,
            symbol=row.symbol,
            side=row.side,
            volume=row.volume,
            price=price,
            deviation_points=self._cfg.deviation_points,
            magic=row.magic,
            comment=row.comment,
            filling=choose_filling(spec),
        )
        if kind in CLOSING_KINDS:
            return OrderRequest(**common, position_ticket=row.position_ticket)
        assert row.sl_distance is not None and row.tp_distance is not None  # noqa: PT018 - OPEN invariant
        drift = abs(price - row.price_ref)
        if drift > self._cfg.max_reprice_fraction_of_stop * row.sl_distance:
            return ExecutionResult(row.id, None, reason=ReasonCode.PRICE_MOVED, detail=f"price moved {drift}")
        sign = 1 if row.side is Side.BUY else -1
        # re-derive the levels around the live price: distances (what was sized) stay exactly the same
        return OrderRequest(
            **common,
            sl=price - sign * row.sl_distance,
            tp=price + sign * row.tp_distance,
        )

    # ------------------------------------------------------------------ execution

    async def execute(self, intent: OrderIntent, spec: SymbolSpec) -> ExecutionResult:
        if not intent.is_issued():
            raise InvariantViolation(f"intent {intent.id} was not issued by the Risk Manager")
        try:
            row = await self._tx(lambda repo: repo.add(intent, account_id=self._account))
        except DuplicateIntentError as exc:
            return ExecutionResult(
                intent.id, None, duplicate=True, reason=ReasonCode.DUPLICATE_IDEMPOTENCY, detail=str(exc)
            )
        self._checkpoint("after_pending")

        built = await self._build(row, spec)
        if isinstance(built, ExecutionResult):
            await self._move(intent.id, IntentStatus.REJECTED, broker_comment=built.detail)
            return ExecutionResult(intent.id, IntentStatus.REJECTED, reason=built.reason, detail=built.detail)
        request = built

        try:
            check = await self._broker.order_check(request)
        except BrokerError as exc:
            await self._move(intent.id, IntentStatus.REJECTED, broker_comment=f"order_check failed: {exc}")
            return ExecutionResult(
                intent.id,
                IntentStatus.REJECTED,
                reason=ReasonCode.BROKER_REJECTED,
                detail=f"order_check failed: {exc}",
                pause_engine=True,
            )
        if check.retcode != 0:
            await self._move(
                intent.id,
                IntentStatus.CHECK_FAILED,
                retcode=check.retcode,
                retcode_name=check.retcode_name,
                broker_comment=check.comment,
            )
            return ExecutionResult(
                intent.id,
                IntentStatus.CHECK_FAILED,
                retcode=check.retcode,
                retcode_name=check.retcode_name,
                reason=ReasonCode.BROKER_REJECTED,
                pause_engine=classify(check.retcode) is RetcodeClass.PAUSE,
                detail=check.comment,
            )

        sent_at = self._clock.now()
        for attempt in range(1, self._cfg.max_attempts + 1):
            await self._move(intent.id, IntentStatus.SENT, sent_at=sent_at, attempts=attempt)
            self._checkpoint("after_sent")
            try:
                result = await self._broker.order_send(request)
            except BrokerError:
                result = None  # connection lost mid-send: the outcome is unknown, never assume either way
            self._checkpoint("after_send")
            if result is None:
                await self._move(intent.id, IntentStatus.UNKNOWN)
                return await self.resolve(intent.id, spec)

            outcome = classify(result.retcode)
            if outcome is RetcodeClass.FILLED:
                return await self._filled(row, spec, request, result, sent_at)
            fields = dict(
                retcode=result.retcode, retcode_name=result.retcode_name, broker_comment=result.comment
            )
            if outcome is RetcodeClass.RETRYABLE and attempt < self._cfg.max_attempts:
                await self._move(intent.id, IntentStatus.RETRYING, **fields)
                self._checkpoint("between_retries")
                rebuilt = await self._build(row, spec)
                if isinstance(rebuilt, ExecutionResult):
                    await self._move(intent.id, IntentStatus.REJECTED, broker_comment=rebuilt.detail)
                    return ExecutionResult(
                        intent.id, IntentStatus.REJECTED, reason=rebuilt.reason, detail=rebuilt.detail
                    )
                request = rebuilt
                continue
            await self._move(intent.id, IntentStatus.REJECTED, **fields)
            return ExecutionResult(
                intent.id,
                IntentStatus.REJECTED,
                retcode=result.retcode,
                retcode_name=result.retcode_name,
                reason=ReasonCode.BROKER_REJECTED,
                pause_engine=outcome is RetcodeClass.PAUSE,
                detail=result.comment,
            )
        raise AssertionError("unreachable: the attempt loop always returns")

    async def _filled(
        self,
        row: OrderIntentRow,
        spec: SymbolSpec,
        request: OrderRequest,
        result: OrderResult,
        sent_at: datetime,
    ) -> ExecutionResult:
        position_id = await self._position_id_for_fill(row, result, sent_at)
        slippage = None
        if request.price is not None and result.price > 0:  # opens and closes (modifications have no price)
            sign = 1 if row.side is Side.BUY else -1
            slippage = int(sign * (result.price - request.price) / spec.point)
        await self._move(
            row.id,
            IntentStatus.FILLED,
            retcode=result.retcode,
            retcode_name=result.retcode_name,
            broker_comment=result.comment,
            order_ticket=result.order or None,
            deal_ticket=result.deal or None,
            position_id=position_id,
            fill_price=result.price or None,
            fill_volume=result.volume or None,
            slippage_points=slippage,
        )
        self._checkpoint("after_filled")
        return ExecutionResult(
            row.id,
            IntentStatus.FILLED,
            retcode=result.retcode,
            retcode_name=result.retcode_name,
            position_id=position_id,
            fill_price=result.price,
            fill_volume=result.volume,
            slippage_points=slippage,
        )

    async def _position_id_for_fill(
        self, row: OrderIntentRow, result: OrderResult, sent_at: datetime
    ) -> int | None:
        if row.kind in CLOSING_KINDS or row.kind is IntentKind.MODIFY_SLTP:
            return row.position_ticket
        try:
            window_start = sent_at - self._cfg.deal_search_window
            for deal in await self._broker.deals_between(
                window_start, self._clock.now() + timedelta(seconds=1)
            ):
                if deal.ticket == result.deal:
                    return deal.position_id
            match = await self._find_open_position(row, sent_at)
        except BrokerError:
            match = None
        return match or (
            result.order or None
        )  # MT5: the position id normally equals the opening order ticket

    # ------------------------------------------------------------------ UNKNOWN resolution & recovery

    async def _find_open_position(self, row: OrderIntentRow, sent_at: datetime) -> int | None:
        positions = [p for p in await self._broker.positions(row.symbol) if p.magic == row.magic]
        for p in positions:
            if p.comment == row.comment:
                return p.position_id
        # brokers may truncate or replace comments: fall back to an unambiguous structural match
        candidates = [
            p
            for p in positions
            if p.side is row.side
            and p.volume == row.volume
            and p.time >= sent_at - self._cfg.deal_search_window
        ]
        return candidates[0].position_id if len(candidates) == 1 else None

    async def resolve(self, intent_id: str, spec: SymbolSpec | None = None) -> ExecutionResult:
        """Settle a SENT / RETRYING / UNKNOWN intent against the broker. Never sends anything."""
        row = await self._tx(lambda repo: repo.get(intent_id))
        if row is None:
            raise InvariantViolation(f"unknown intent {intent_id}")
        if row.status.is_terminal:
            return ExecutionResult(row.id, row.status, position_id=row.position_id)
        if row.status is IntentStatus.PENDING:  # crashed before sending (SENT is committed first)
            await self._move(row.id, IntentStatus.REJECTED, broker_comment="abandoned before send (recovery)")
            return ExecutionResult(
                row.id,
                IntentStatus.REJECTED,
                reason=ReasonCode.UNKNOWN_NOT_EXECUTED,
                detail="abandoned before send",
            )
        if row.status is IntentStatus.RETRYING:  # crashed between retries: the last send was rejected
            await self._move(
                row.id, IntentStatus.REJECTED, broker_comment="crashed between retries (recovery)"
            )
            return ExecutionResult(
                row.id,
                IntentStatus.REJECTED,
                reason=ReasonCode.UNKNOWN_NOT_EXECUTED,
                detail="abandoned between retries",
            )
        if row.status is not IntentStatus.UNKNOWN:
            row = await self._move(row.id, IntentStatus.UNKNOWN)
        sent_at = row.sent_at or row.created_at
        try:
            found = await self._search(row, sent_at)
        except BrokerError as exc:
            return ExecutionResult(row.id, IntentStatus.UNKNOWN, detail=f"broker unavailable: {exc}")
        if found is not None:
            position_id, price, volume = found
            await self._move(
                row.id,
                IntentStatus.FILLED,
                position_id=position_id,
                fill_price=price,
                fill_volume=volume,
                broker_comment="resolved after unknown outcome",
            )
            return ExecutionResult(
                row.id,
                IntentStatus.FILLED,
                position_id=position_id,
                fill_price=price,
                fill_volume=volume,
                detail="resolved after unknown outcome",
            )
        if self._clock.now() - sent_at > self._cfg.unknown_grace:
            await self._move(
                row.id, IntentStatus.REJECTED, broker_comment="not found at the broker after grace"
            )
            return ExecutionResult(
                row.id,
                IntentStatus.REJECTED,
                reason=ReasonCode.UNKNOWN_NOT_EXECUTED,
                detail="nothing executed at the broker",
            )
        return ExecutionResult(row.id, IntentStatus.UNKNOWN, detail="still unresolved; symbol stays locked")

    async def _search(
        self, row: OrderIntentRow, sent_at: datetime
    ) -> tuple[int, Decimal | None, Decimal | None] | None:
        if row.kind is IntentKind.OPEN:
            positions = {p.position_id: p for p in await self._broker.positions(row.symbol)}
            position_id = await self._find_open_position(row, sent_at)
            if position_id is not None:
                pos = positions[position_id]
                return position_id, pos.price_open, pos.volume
        deals = await self._broker.deals_between(sent_at - self._cfg.deal_search_window, self._clock.now())
        for deal in deals:
            if deal.magic == row.magic and deal.comment == row.comment:
                return deal.position_id, deal.price, deal.volume
        return None

    async def recover(self) -> list[ExecutionResult]:
        """Startup: settle every non-terminal intent before anything else trades."""
        rows = await self._tx(lambda repo: list(repo.non_terminal()))
        return [await self.resolve(row.id) for row in rows]
