"""Bar-clock cursor store backed by the decisions table (see market/bar_clock.py)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import Timeframe
from aifund.persistence.tables import DecisionRow


class DecisionCursorStore:
    """Cursor = latest ``decisions.bar_time`` for the symbol and trigger timeframe.

    ``save`` is a no-op: the decision pipeline writes the row that advances the cursor, so a bar is
    re-emitted after a crash only if its decision was never recorded.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def load(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        with self._factory() as s:
            value: datetime | None = s.scalar(
                select(func.max(DecisionRow.bar_time)).where(
                    DecisionRow.symbol == symbol, DecisionRow.trigger_tf == timeframe.value
                )
            )
        return value

    def save(self, symbol: str, timeframe: Timeframe, bar_time: datetime) -> None:
        return None
