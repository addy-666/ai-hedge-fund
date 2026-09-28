"""Order-intent persistence. The UNIQUE idempotency key is duplicate-guard layer 1 (docs/03 §9.1)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aifund.domain.enums import IntentKind, IntentStatus
from aifund.domain.errors import DuplicateIntentError, InvariantViolation
from aifund.domain.intent import OrderIntent, can_transition
from aifund.persistence.tables import OrderIntentRow
from aifund.ports.system import ClockPort

_NON_TERMINAL = [s for s in IntentStatus if not s.is_terminal]
_UPDATABLE = {
    "retcode", "retcode_name", "broker_comment", "order_ticket", "deal_ticket", "position_id",
    "fill_price", "fill_volume", "slippage_points", "attempts", "sent_at",
}  # fmt: skip


class IntentRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def add(self, intent: OrderIntent, *, account_id: str) -> OrderIntentRow:
        """Persist a new PENDING intent BEFORE anything is sent to the broker.

        Raises DuplicateIntentError if the idempotency key was already used — the same bar and direction
        was already acted on, possibly before a restart.
        """
        row = OrderIntentRow(
            id=intent.id,
            idempotency_key=intent.idempotency_key,
            account_id=account_id,
            decision_id=intent.decision_id,
            kind=intent.kind,
            symbol=intent.symbol,
            side=intent.side,
            volume=intent.volume,
            price_ref=intent.price_ref,
            sl=intent.sl,
            tp=intent.tp,
            sl_distance=intent.sl_distance,
            tp_distance=intent.tp_distance,
            risk_money=intent.risk_money,
            risk_pct=intent.risk_pct,
            comment=intent.comment,
            magic=intent.magic,
            position_ticket=intent.position_ticket,
            status=IntentStatus.PENDING,
            attempts=0,
            created_at=intent.created_at,
        )
        nested = self._s.begin_nested()
        try:
            self._s.add(row)
            self._s.flush()
        except IntegrityError as exc:
            nested.rollback()
            if "idempotency_key" in str(exc.orig):
                raise DuplicateIntentError(intent.idempotency_key) from exc
            raise
        nested.commit()
        return row

    def get(self, intent_id: str) -> OrderIntentRow | None:
        return self._s.get(OrderIntentRow, intent_id)

    def non_terminal(self, symbol: str | None = None) -> Sequence[OrderIntentRow]:
        """Intents that lock their symbol: PENDING, SENT, RETRYING, UNKNOWN."""
        stmt = select(OrderIntentRow).where(OrderIntentRow.status.in_(_NON_TERMINAL))
        if symbol is not None:
            stmt = stmt.where(OrderIntentRow.symbol == symbol)
        return self._s.scalars(stmt.order_by(OrderIntentRow.created_at)).all()

    def filled_opens(self, position_ids: list[int]) -> dict[int, OrderIntentRow]:
        """FILLED OPEN intents for these broker positions (initial risk for exposure checks)."""
        if not position_ids:
            return {}
        rows = self._s.scalars(
            select(OrderIntentRow).where(
                OrderIntentRow.kind == IntentKind.OPEN,
                OrderIntentRow.status == IntentStatus.FILLED,
                OrderIntentRow.position_id.in_(position_ids),
            )
        ).all()
        return {r.position_id: r for r in rows if r.position_id is not None}

    def count_since(self, symbol: str, kind: IntentKind, since: datetime) -> int:
        """FILLED intents of ``kind`` on ``symbol`` created at or after ``since`` (daily caps)."""
        return len(
            self._s.scalars(
                select(OrderIntentRow.id).where(
                    OrderIntentRow.symbol == symbol,
                    OrderIntentRow.kind == kind,
                    OrderIntentRow.status == IntentStatus.FILLED,
                    OrderIntentRow.created_at >= since,
                )
            ).all()
        )

    def transition(self, intent_id: str, new_status: IntentStatus, **fields: Any) -> OrderIntentRow:
        """Move an intent along the state machine; illegal transitions are bugs and raise."""
        row = self._s.get(OrderIntentRow, intent_id, with_for_update=True)
        if row is None:
            raise InvariantViolation(f"unknown intent {intent_id}")
        if not can_transition(row.status, new_status):
            raise InvariantViolation(f"illegal intent transition {row.status} -> {new_status} ({intent_id})")
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise InvariantViolation(f"fields not updatable on transition: {sorted(unknown)}")
        row.status = new_status
        for key, value in fields.items():
            setattr(row, key, value)
        if new_status.is_terminal:
            row.resolved_at = self._clock.now()
        self._s.flush()
        return row
