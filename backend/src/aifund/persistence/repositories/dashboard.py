"""Read models for the dashboard API (roadmap 6.2, docs/05 §3). Queries only: the API writes no trading data.

Lists are cursor-paginated by ULID (newest first): pass the last id of a page as ``cursor`` to get the next.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from aifund.domain.enums import TradeStatus
from aifund.persistence.tables import (
    CommandRow,
    ConfigVersionRow,
    DealRow,
    DecisionRow,
    EngineStateRow,
    EquitySnapshotRow,
    FeatureSnapshotRow,
    HeartbeatRow,
    LLMCallRow,
    OrderIntentRow,
    TradeRow,
    VirtualTradeRow,
)

R = TypeVar("R")
OPEN = (TradeStatus.OPEN, TradeStatus.ORPHAN_OPEN)
CLOSED = (TradeStatus.CLOSED, TradeStatus.ORPHAN_CLOSED)


def _page(  # noqa: UP047
    s: Session, stmt: Select[tuple[R]], id_col: Any, cursor: str | None, limit: int
) -> list[R]:
    if cursor:
        stmt = stmt.where(id_col < cursor)
    return list(s.scalars(stmt.order_by(id_col.desc()).limit(limit)).all())


class DashboardQueries:
    def __init__(self, session: Session) -> None:
        self._s = session

    # ------------------------------------------------------------------ system & account

    def engine_states(self) -> Sequence[EngineStateRow]:
        return self._s.scalars(select(EngineStateRow)).all()

    def heartbeats(self) -> Sequence[HeartbeatRow]:
        return self._s.scalars(select(HeartbeatRow).order_by(HeartbeatRow.component)).all()

    def latest_config(self) -> ConfigVersionRow | None:
        stmt = select(ConfigVersionRow).order_by(
            ConfigVersionRow.created_at.desc(), ConfigVersionRow.id.desc()
        )
        return self._s.scalars(stmt.limit(1)).first()

    def config_versions(self, limit: int = 50) -> Sequence[ConfigVersionRow]:
        stmt = select(ConfigVersionRow).order_by(
            ConfigVersionRow.created_at.desc(), ConfigVersionRow.id.desc()
        )
        return self._s.scalars(stmt.limit(limit)).all()

    def latest_equity(self, account: str) -> EquitySnapshotRow | None:
        stmt = select(EquitySnapshotRow).where(EquitySnapshotRow.account_id == account)
        return self._s.scalars(stmt.order_by(EquitySnapshotRow.ts.desc()).limit(1)).first()

    def equity(self, account: str, start: datetime, end: datetime) -> Sequence[EquitySnapshotRow]:
        stmt = (
            select(EquitySnapshotRow)
            .where(
                EquitySnapshotRow.account_id == account,
                EquitySnapshotRow.ts >= start,
                EquitySnapshotRow.ts < end,
            )
            .order_by(EquitySnapshotRow.ts)
        )
        return self._s.scalars(stmt).all()

    def command(self, command_id: str) -> CommandRow | None:
        return self._s.get(CommandRow, command_id)

    # ------------------------------------------------------------------ positions & trades

    def open_trades(self, account: str) -> Sequence[TradeRow]:
        stmt = select(TradeRow).where(TradeRow.account_id == account, TradeRow.status.in_(OPEN))
        return self._s.scalars(stmt.order_by(TradeRow.open_time)).all()

    def trade_by_position(self, position_id: int) -> TradeRow | None:
        return self._s.scalars(select(TradeRow).where(TradeRow.position_id == position_id)).first()

    def trades(
        self,
        account: str,
        *,
        symbol: str | None,
        setup: str | None,
        outcome: str | None,
        start: datetime | None,
        end: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> list[TradeRow]:
        stmt = select(TradeRow).where(TradeRow.account_id == account, TradeRow.status.in_(CLOSED))
        if symbol:
            stmt = stmt.where(TradeRow.symbol == symbol)
        if setup:
            stmt = stmt.where(TradeRow.setup_tag == setup)
        if outcome:
            stmt = stmt.where(TradeRow.outcome == outcome)
        if start:
            stmt = stmt.where(TradeRow.close_time >= start)
        if end:
            stmt = stmt.where(TradeRow.close_time < end)
        return _page(self._s, stmt, TradeRow.id, cursor, limit)

    def closed_trades(self, account: str, start: datetime, end: datetime) -> Sequence[TradeRow]:
        stmt = select(TradeRow).where(
            TradeRow.account_id == account,
            TradeRow.status.in_(CLOSED),
            TradeRow.close_time >= start,
            TradeRow.close_time < end,
        )
        return self._s.scalars(stmt.order_by(TradeRow.close_time)).all()

    def trade(self, trade_id: str) -> TradeRow | None:
        return self._s.get(TradeRow, trade_id)

    def deals(self, position_id: int) -> Sequence[DealRow]:
        stmt = select(DealRow).where(DealRow.position_id == position_id).order_by(DealRow.time_utc)
        return self._s.scalars(stmt).all()

    def intents_for(self, decision_id: str) -> Sequence[OrderIntentRow]:
        stmt = select(OrderIntentRow).where(OrderIntentRow.decision_id == decision_id)
        return self._s.scalars(stmt.order_by(OrderIntentRow.created_at)).all()

    # ------------------------------------------------------------------ decisions

    def decisions(
        self,
        account: str,
        *,
        symbol: str | None,
        outcome: str | None,
        reason: str | None,
        start: datetime | None,
        end: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> list[DecisionRow]:
        stmt = select(DecisionRow).where(DecisionRow.account_id == account)
        if symbol:
            stmt = stmt.where(DecisionRow.symbol == symbol)
        if outcome:
            stmt = stmt.where(DecisionRow.outcome == outcome)
        if reason:
            stmt = stmt.where(DecisionRow.reason_code == reason)
        if start:
            stmt = stmt.where(DecisionRow.created_at >= start)
        if end:
            stmt = stmt.where(DecisionRow.created_at < end)
        return _page(self._s, stmt, DecisionRow.id, cursor, limit)

    def decision(self, decision_id: str) -> DecisionRow | None:
        return self._s.get(DecisionRow, decision_id)

    def decision_outcomes(self, account: str, start: datetime, end: datetime) -> dict[str, int]:
        stmt = (
            select(DecisionRow.outcome, func.count())
            .where(
                DecisionRow.account_id == account,
                DecisionRow.created_at >= start,
                DecisionRow.created_at < end,
            )
            .group_by(DecisionRow.outcome)
        )
        return {o.value: int(n) for o, n in self._s.execute(stmt).all()}

    def snapshot(self, snapshot_id: str | None) -> FeatureSnapshotRow | None:
        return self._s.get(FeatureSnapshotRow, snapshot_id) if snapshot_id else None

    def llm_calls_for(self, decision_id: str) -> Sequence[LLMCallRow]:
        stmt = select(LLMCallRow).where(LLMCallRow.decision_id == decision_id).order_by(LLMCallRow.created_at)
        return self._s.scalars(stmt).all()

    def decisions_by_id(self, ids: Sequence[str]) -> dict[str, DecisionRow]:
        if not ids:
            return {}
        return {d.id: d for d in self._s.scalars(select(DecisionRow).where(DecisionRow.id.in_(ids))).all()}

    def snapshots_by_id(self, ids: Sequence[str]) -> dict[str, FeatureSnapshotRow]:
        if not ids:
            return {}
        rows = self._s.scalars(select(FeatureSnapshotRow).where(FeatureSnapshotRow.id.in_(ids))).all()
        return {r.id: r for r in rows}

    # ------------------------------------------------------------------ virtual trades & LLM

    def virtual_trades(
        self,
        account: str,
        *,
        arm: str | None,
        status: str | None,
        symbol: str | None,
        cursor: str | None,
        limit: int,
    ) -> list[VirtualTradeRow]:
        stmt = select(VirtualTradeRow).where(VirtualTradeRow.account_id == account)
        if arm:
            stmt = stmt.where(VirtualTradeRow.arm == arm)
        if status:
            stmt = stmt.where(VirtualTradeRow.status == status)
        if symbol:
            stmt = stmt.where(VirtualTradeRow.symbol == symbol)
        return _page(self._s, stmt, VirtualTradeRow.id, cursor, limit)

    def llm_calls(self, start: datetime, end: datetime) -> Sequence[LLMCallRow]:
        stmt = select(LLMCallRow).where(LLMCallRow.created_at >= start, LLMCallRow.created_at < end)
        return self._s.scalars(stmt).all()

    def llm_spend(self, start: datetime) -> Decimal:
        rows = self._s.scalars(select(LLMCallRow.cost_usd).where(LLMCallRow.created_at >= start)).all()
        return sum((c for c in rows if c is not None), Decimal(0))
