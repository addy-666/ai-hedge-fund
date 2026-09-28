"""Trades (one per broker position) and the raw deal mirror (docs/02 §1.3). Written only by the reconciler."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.domain.enums import TradeStatus
from aifund.domain.errors import InvariantViolation
from aifund.domain.market import Deal
from aifund.domain.trade import OPEN_STATUSES, can_transition
from aifund.persistence.tables import DealRow, TradeRow
from aifund.ports.system import ClockPort

_UPDATABLE = {"volume_open_now", "current_sl", "current_tp"}
_CLOSE_FIELDS = {
    "close_time", "close_price_vwap", "close_reason", "gross_profit", "commission", "swap", "fee",
    "net_pnl", "r_multiple", "outcome",
}  # fmt: skip


class TradeRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def add(self, **fields: Any) -> TradeRow:
        now = self._clock.now()
        row = TradeRow(**fields, created_at=now, updated_at=now)
        if row.status not in OPEN_STATUSES:
            raise InvariantViolation(f"a trade is created open, not {row.status}")
        self._s.add(row)
        self._s.flush()
        return row

    def by_position(self, position_id: int) -> TradeRow | None:
        return self._s.scalars(select(TradeRow).where(TradeRow.position_id == position_id)).one_or_none()

    def open_trades(self) -> Sequence[TradeRow]:
        """OPEN and ORPHAN_OPEN trades: positions the ledger believes are still at the broker."""
        return self._s.scalars(
            select(TradeRow).where(TradeRow.status.in_(OPEN_STATUSES)).order_by(TradeRow.open_time)
        ).all()

    def update_open(self, position_id: int, **fields: Any) -> TradeRow:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise InvariantViolation(f"fields not updatable on an open trade: {sorted(unknown)}")
        row = self._require(position_id)
        if row.status not in OPEN_STATUSES:
            raise InvariantViolation(f"trade {row.id} is {row.status}, not open")
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = self._clock.now()
        self._s.flush()
        return row

    def close(self, position_id: int, new_status: TradeStatus, **fields: Any) -> TradeRow:
        missing = _CLOSE_FIELDS - set(fields)
        unknown = set(fields) - _CLOSE_FIELDS
        if missing or unknown:
            raise InvariantViolation(f"close fields: missing {sorted(missing)}, unknown {sorted(unknown)}")
        row = self._require(position_id)
        if not can_transition(row.status, new_status):
            raise InvariantViolation(f"illegal trade transition {row.status} -> {new_status} ({row.id})")
        row.status = new_status
        row.volume_open_now = Decimal(0)
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = self._clock.now()
        self._s.flush()
        return row

    def _require(self, position_id: int) -> TradeRow:
        row = self.by_position(position_id)
        if row is None:
            raise InvariantViolation(f"no trade for position {position_id}")
        return row


class DealRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, deals: Iterable[Deal], *, account_id: str) -> int:
        """Mirror broker deals; idempotent by ticket (a deal never changes once booked)."""
        count = 0
        for d in deals:
            self._s.merge(
                DealRow(
                    ticket=d.ticket,
                    account_id=account_id,
                    order=d.order,
                    position_id=d.position_id,
                    time_utc=d.time,
                    time_server=d.time_server_epoch,
                    side=d.side,
                    entry=d.entry,
                    reason=d.reason,
                    magic=d.magic,
                    symbol=d.symbol,
                    volume=d.volume,
                    price=d.price,
                    profit=d.profit,
                    commission=d.commission,
                    swap=d.swap,
                    fee=d.fee,
                    comment=d.comment or None,
                    raw=d.model_dump(mode="json"),
                )
            )
            count += 1
        self._s.flush()
        return count

    def for_position(self, position_id: int) -> Sequence[DealRow]:
        return self._s.scalars(
            select(DealRow)
            .where(DealRow.position_id == position_id)
            .order_by(DealRow.time_utc, DealRow.ticket)
        ).all()
