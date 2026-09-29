"""Every LLM call (docs/02 §1.2 ``llm_calls``): prompts, raw output, tokens, cost, latency, errors."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.persistence.tables import LLMCallRow
from aifund.ports.system import ClockPort


class LLMCallRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def add(self, **fields: Any) -> LLMCallRow:
        fields.setdefault("id", new_id())
        fields.setdefault("created_at", self._clock.now())
        row = LLMCallRow(**fields)
        self._s.add(row)
        self._s.flush()
        return row

    def spent_since(self, start: datetime) -> Decimal:
        """USD billed since ``start`` (the daily budget). Rows without a cost (no usage reported) count 0."""
        rows = self._s.scalars(select(LLMCallRow.cost_usd).where(LLMCallRow.created_at >= start)).all()
        return sum((c for c in rows if c is not None), Decimal(0))

    def count_since(self, start: datetime, agent: str | None = None) -> int:
        stmt = select(func.count()).select_from(LLMCallRow).where(LLMCallRow.created_at >= start)
        if agent is not None:
            stmt = stmt.where(LLMCallRow.agent == agent)
        return int(self._s.scalar(stmt) or 0)

    def record_parse(
        self, call_id: str, *, parsed: dict[str, Any] | None, valid: bool, error: str | None
    ) -> None:
        """The agent's verdict on the output (schema validation happens after the adapter returns)."""
        row = self._s.get(LLMCallRow, call_id)
        if row is None:
            raise InvariantViolation(f"unknown llm call {call_id}")
        row.parsed, row.valid = parsed, valid
        if error is not None:
            row.error = error
        self._s.flush()
