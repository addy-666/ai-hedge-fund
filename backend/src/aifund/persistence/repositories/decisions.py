"""Decision records (docs/02 `decisions`): exactly one row per pipeline run, with full provenance."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.domain.enums import Direction
from aifund.domain.ids import new_id
from aifund.persistence.tables import DecisionRow
from aifund.ports.system import ClockPort


class DecisionRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def add(self, **fields: Any) -> DecisionRow:
        fields.setdefault("id", new_id())
        fields.setdefault("created_at", self._clock.now())
        row = DecisionRow(**fields)
        self._s.add(row)
        self._s.flush()
        return row

    def update(self, decision_id: str, **fields: Any) -> DecisionRow:
        row = self._s.get(DecisionRow, decision_id)
        if row is None:
            raise KeyError(decision_id)
        for key, value in fields.items():
            setattr(row, key, value)
        self._s.flush()
        return row

    def recent_directions(self, symbol: str, trigger_tf: str, limit: int = 10) -> list[Direction]:
        """Directions of the latest decisions that proposed a trade, oldest first (flip-flop input)."""
        rows: Sequence[DecisionRow] = self._s.scalars(
            select(DecisionRow)
            .where(DecisionRow.symbol == symbol, DecisionRow.trigger_tf == trigger_tf,
                   DecisionRow.proposal.is_not(None))
            .order_by(DecisionRow.bar_time.desc(), DecisionRow.id.desc())
            .limit(limit)
        ).all()  # fmt: skip
        out = []
        for row in reversed(rows):
            direction = (row.proposal or {}).get("direction")
            if direction in (Direction.LONG.value, Direction.SHORT.value):
                out.append(Direction(direction))
        return out

    def for_symbol_since(self, symbol: str, since: datetime) -> Sequence[DecisionRow]:
        return self._s.scalars(
            select(DecisionRow).where(DecisionRow.symbol == symbol, DecisionRow.bar_time >= since)
            .order_by(DecisionRow.bar_time)
        ).all()  # fmt: skip
