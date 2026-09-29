"""Repositories for the API process: dashboard sessions and the bar cache for charts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from aifund.domain.market import Bar
from aifund.persistence.tables import ApiSessionRow, BarCacheRow


class ApiSessionRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, row: ApiSessionRow) -> None:
        self._s.add(row)
        self._s.flush()

    def get(self, token_sha256: str, now: datetime) -> ApiSessionRow | None:
        row = self._s.get(ApiSessionRow, token_sha256)
        return row if row is not None and row.expires_at > now else None

    def remove(self, token_sha256: str) -> None:
        self._s.execute(delete(ApiSessionRow).where(ApiSessionRow.token_sha256 == token_sha256))

    def purge_expired(self, now: datetime) -> int:
        result = self._s.execute(delete(ApiSessionRow).where(ApiSessionRow.expires_at <= now))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class BarCacheRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, bars: Sequence[Bar]) -> None:
        if not bars:
            return
        rows = [
            {
                "symbol": b.symbol,
                "timeframe": b.timeframe.value,
                "time": b.time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "tick_volume": b.tick_volume,
                "spread_points": b.spread_points,
            }
            for b in bars
        ]
        stmt = insert(BarCacheRow).values(rows)
        self._s.execute(stmt.on_conflict_do_nothing(index_elements=["symbol", "timeframe", "time"]))

    def between(
        self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int = 2000
    ) -> Sequence[BarCacheRow]:
        stmt = (
            select(BarCacheRow)
            .where(BarCacheRow.symbol == symbol, BarCacheRow.timeframe == timeframe)
            .where(BarCacheRow.time >= start, BarCacheRow.time < end)
            .order_by(BarCacheRow.time)
            .limit(limit)
        )
        return self._s.scalars(stmt).all()

    def latest(self, symbol: str) -> BarCacheRow | None:
        stmt = (
            select(BarCacheRow).where(BarCacheRow.symbol == symbol).order_by(BarCacheRow.time.desc()).limit(1)
        )
        return self._s.scalars(stmt).first()
