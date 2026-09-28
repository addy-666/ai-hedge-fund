"""Equity snapshots (docs/02 §1.3): the equity curve and what the loss limits were measured on."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.persistence.tables import EquitySnapshotRow


class EquitySnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, **fields: Any) -> EquitySnapshotRow:
        row = EquitySnapshotRow(**fields)
        self._s.add(row)
        self._s.flush()
        return row

    def latest(self, account_id: str) -> EquitySnapshotRow | None:
        return self._s.scalars(
            select(EquitySnapshotRow)
            .where(EquitySnapshotRow.account_id == account_id)
            .order_by(EquitySnapshotRow.ts.desc(), EquitySnapshotRow.id.desc())
            .limit(1)
        ).one_or_none()

    def between(self, account_id: str, start: datetime, end: datetime) -> Sequence[EquitySnapshotRow]:
        return self._s.scalars(
            select(EquitySnapshotRow)
            .where(
                EquitySnapshotRow.account_id == account_id,
                EquitySnapshotRow.ts >= start,
                EquitySnapshotRow.ts <= end,
            )
            .order_by(EquitySnapshotRow.ts)
        ).all()
