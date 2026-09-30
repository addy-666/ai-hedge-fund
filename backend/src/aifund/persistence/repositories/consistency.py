"""Ledger consistency checks (roadmap 9.5 restore drill, 9.6 L2 gate): what must hold in any engine database.

Errors — the ledger contradicts itself:
- a CLOSED trade without its close time or net P&L;
- a filled OPEN intent without a position, or whose position has no trade;
- a CLOSED (non-orphan) trade without the decision and the feature snapshot it was entered on (the L2 gate:
  "every closed trade has snapshot + decision" — orphans are exempt by definition).

Warnings — normal in a live database, worth a look in a backup:
- intents still in flight (PENDING / SENT / RETRYING / UNKNOWN): the backup caught an order mid-send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aifund.domain.enums import IntentKind, IntentStatus, TradeStatus
from aifund.persistence.tables import Base, OrderIntentRow, TradeRow

IN_FLIGHT = (IntentStatus.PENDING, IntentStatus.SENT, IntentStatus.RETRYING, IntentStatus.UNKNOWN)


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    check: str
    detail: str


class ConsistencyRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def counts(self) -> dict[str, int]:
        return {
            table.name: int(self._s.scalar(select(func.count()).select_from(table)) or 0)
            for table in Base.metadata.sorted_tables
        }

    def closed_without_entry_context(self) -> Sequence[TradeRow]:
        return self._s.scalars(
            select(TradeRow).where(
                TradeRow.status == TradeStatus.CLOSED,
                (TradeRow.decision_id.is_(None)) | (TradeRow.snapshot_id.is_(None)),
            )
        ).all()

    def findings(self) -> list[Finding]:
        out: list[Finding] = []
        for t in self._s.scalars(
            select(TradeRow).where(
                TradeRow.status.in_((TradeStatus.CLOSED, TradeStatus.ORPHAN_CLOSED)),
                (TradeRow.close_time.is_(None)) | (TradeRow.net_pnl.is_(None)),
            )
        ).all():
            out.append(Finding("error", "closed_trade_incomplete", f"trade {t.id} (#{t.position_id})"))
        traded = set(self._s.scalars(select(TradeRow.position_id)).all())
        for i in self._s.scalars(
            select(OrderIntentRow).where(
                OrderIntentRow.kind == IntentKind.OPEN, OrderIntentRow.status == IntentStatus.FILLED
            )
        ).all():
            if i.position_id is None:
                out.append(Finding("error", "filled_open_without_position", f"intent {i.id}"))
            elif i.position_id not in traded:
                out.append(Finding("error", "position_without_trade", f"intent {i.id} -> #{i.position_id}"))
        for t in self.closed_without_entry_context():
            out.append(Finding("error", "closed_trade_without_context", f"trade {t.id} (#{t.position_id})"))
        for i in self._s.scalars(select(OrderIntentRow).where(OrderIntentRow.status.in_(IN_FLIGHT))).all():
            out.append(Finding("warning", "intent_in_flight", f"intent {i.id} {i.status.value}"))
        return out
