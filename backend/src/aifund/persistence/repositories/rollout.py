"""The rollout gates' evidence (docs/06 §10, roadmap 9.6-9.7), read from the engine's own tables.

Kept by retention on purpose (``retention.EVIDENCE_EVENTS``): the events a 4-week gate period needs —
restarts (``engine.state``), positions found without a stop (``position.sl_missing``), ledger mismatches.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.domain.enums import CommandStatus, CommandType, IntentKind, IntentStatus, TradeStatus
from aifund.domain.rollout import ClosedTrade, RolloutFacts
from aifund.persistence.tables import (
    AuditLogRow,
    CommandRow,
    EventRow,
    HeartbeatRow,
    OrderIntentRow,
    TradeRow,
)

SIGNOFF = "ROLLOUT_SIGNOFF"
LEDGER_JOB = "job.verify_ledger"


class RolloutRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def signoffs(self) -> Sequence[AuditLogRow]:
        return self._s.scalars(
            select(AuditLogRow).where(AuditLogRow.action == SIGNOFF).order_by(AuditLogRow.ts)
        ).all()

    def entered(self, level: str) -> datetime | None:
        """When the operator signed off INTO ``level`` (the latest time), if ever."""
        times = [r.ts for r in self.signoffs() if (r.after or {}).get("level") == level]
        return times[-1] if times else None

    def first_trade(self, account_id: str) -> datetime | None:
        return self._s.scalar(
            select(TradeRow.open_time).where(TradeRow.account_id == account_id).order_by(TradeRow.open_time)
        )

    def closed(
        self, account_id: str, start: datetime | None, end: datetime | None = None
    ) -> list[ClosedTrade]:
        q = select(TradeRow).where(TradeRow.account_id == account_id, TradeRow.status == TradeStatus.CLOSED)
        if start is not None:
            q = q.where(TradeRow.open_time >= start)
        if end is not None:
            q = q.where(TradeRow.open_time < end)
        return [
            ClosedTrade(
                r=t.r_multiple,
                net=t.net_pnl or Decimal(0),
                costs=(t.commission or Decimal(0)) + (t.swap or Decimal(0)) + (t.fee or Decimal(0)),
                volume=t.volume_opened,
                open_time=t.open_time,
            )
            for t in self._s.scalars(q.order_by(TradeRow.open_time)).all()
        ]

    def facts(
        self,
        account_id: str,
        start: datetime | None,
        *,
        baseline: tuple[datetime | None, datetime | None] | None = None,
    ) -> RolloutFacts:
        opens = self._s.scalars(
            select(OrderIntentRow).where(
                OrderIntentRow.kind == IntentKind.OPEN, OrderIntentRow.status == IntentStatus.FILLED
            )
        ).all()
        opens = [i for i in opens if start is None or i.created_at >= start]
        by_decision = Counter(i.decision_id for i in opens if i.decision_id is not None)
        by_position = Counter(i.position_id for i in opens if i.position_id is not None)
        duplicates = sum(n - 1 for n in by_decision.values() if n > 1) + sum(
            n - 1 for n in by_position.values() if n > 1
        )
        unknown = len(
            self._s.scalars(
                select(OrderIntentRow.id).where(OrderIntentRow.status == IntentStatus.UNKNOWN)
            ).all()
        )
        events = self._s.scalars(
            select(EventRow).where(EventRow.type.in_(("position.sl_missing", "engine.state")))
        ).all()
        events = [e for e in events if start is None or e.ts >= start]
        trades = self._s.scalars(select(TradeRow).where(TradeRow.account_id == account_id)).all()
        boots = [
            e.ts for e in events if e.type == "engine.state" and (e.payload or {}).get("trigger") == "BOOT"
        ]
        restarts = sum(
            1
            for ts in boots
            if any(t.open_time < ts and (t.close_time is None or t.close_time > ts) for t in trades)
        )
        without = [
            t for t in trades
            if t.status is TradeStatus.CLOSED and (t.decision_id is None or t.snapshot_id is None)
            and (start is None or t.open_time >= start)
        ]  # fmt: skip
        ledger = self._s.get(HeartbeatRow, LEDGER_JOB)
        flattens = [
            c for c in self._s.scalars(
                select(CommandRow).where(
                    CommandRow.type == CommandType.FLATTEN_ALL.value, CommandRow.status == CommandStatus.DONE
                )
            ).all()
            if start is None or c.created_at >= start
        ]  # fmt: skip
        return RolloutFacts(
            period_start=start,
            trades=self.closed(account_id, start),
            baseline_trades=self.closed(account_id, *baseline) if baseline is not None else (),
            duplicate_opens=duplicates,
            unknown_intents=unknown,
            sl_missing=sum(1 for e in events if e.type == "position.sl_missing"),
            without_context=len(without),
            ledger_status=ledger.status if ledger is not None else None,
            ledger_at=ledger.last_beat_at if ledger is not None else None,
            flatten_tested=len(flattens),
            restarts_with_open=restarts,
        )
