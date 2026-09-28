"""Virtual (counterfactual) trades for blocked signals (docs/02 §1.3, docs/03 §14.4)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.domain.enums import VirtualStatus
from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.persistence.tables import VirtualTradeRow
from aifund.ports.system import ClockPort

ACTIVE = (VirtualStatus.PENDING, VirtualStatus.OPEN)
_TRANSITIONS = {
    VirtualStatus.PENDING: {
        VirtualStatus.OPEN,
        VirtualStatus.CLOSED,
        VirtualStatus.EXPIRED,
        VirtualStatus.NO_ENTRY,
    },
    VirtualStatus.OPEN: {VirtualStatus.CLOSED, VirtualStatus.EXPIRED},
}
_FIELDS = {
    "entry_price", "sl", "tp", "exit_time", "exit_price", "exit_reason", "r_multiple", "mae_r", "mfe_r",
}  # fmt: skip


class VirtualTradeRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def add_pending(self, **fields: Any) -> VirtualTradeRow:
        row = VirtualTradeRow(
            id=new_id(), status=VirtualStatus.PENDING, created_at=self._clock.now(), **fields
        )
        self._s.add(row)
        self._s.flush()
        return row

    def active(self) -> Sequence[VirtualTradeRow]:
        return self._s.scalars(
            select(VirtualTradeRow)
            .where(VirtualTradeRow.status.in_(ACTIVE))
            .order_by(VirtualTradeRow.entry_time)
        ).all()

    def update(self, virtual_id: str, status: VirtualStatus, **fields: Any) -> VirtualTradeRow:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise InvariantViolation(f"fields not updatable on a virtual trade: {sorted(unknown)}")
        row = self._s.get(VirtualTradeRow, virtual_id)
        if row is None:
            raise InvariantViolation(f"unknown virtual trade {virtual_id}")
        if status is not row.status and status not in _TRANSITIONS.get(row.status, set()):
            raise InvariantViolation(f"illegal virtual transition {row.status} -> {status} ({virtual_id})")
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        self._s.flush()
        return row
