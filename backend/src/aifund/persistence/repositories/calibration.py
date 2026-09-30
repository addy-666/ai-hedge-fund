"""Calibration models and their samples (roadmap 8.5, docs/04 §9).

A sample is one decision of a source with its RAW confidence (before any calibration) and the R of what it
traded, real or virtual:

- analyst: the analyst's proposal (``proposal.confidence`` when it decided, ``proposal.shadow_analyst``
  when the baseline traded instead) with, in order of preference, the real trade it opened, its G-LLM shadow
  (SHADOW_ANALYST), or — when it decided and was blocked — the blocked signal's virtual trade;
- committee: the committee's confidence after the critic (``proposal.committee.confidence``) with its
  SHADOW_COMMITTEE virtual trade.

Only finished trades count (a result in R). Baseline decisions never do: their confidence is a constant.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aifund.domain.enums import CalibrationStatus, TradeStatus, VirtualArm
from aifund.persistence.tables import CalibrationModelRow, DecisionRow, TradeRow, VirtualTradeRow
from aifund.ports.system import ClockPort

SOURCES = ("analyst", "committee")
ARMS = {
    "analyst": (VirtualArm.SHADOW_ANALYST, VirtualArm.BLOCKED),
    "committee": (VirtualArm.SHADOW_COMMITTEE,),
}


def raw_confidence(proposal: dict[str, Any] | None, source: str) -> tuple[int, bool] | None:
    """(the source's raw confidence, whether that source made the decision) — None if it proposed nothing."""
    if not isinstance(proposal, dict):
        return None
    if source == "committee":
        record = proposal.get("committee")
        value = record.get("confidence") if isinstance(record, dict) else None
        return (value, False) if isinstance(value, int) else None
    decided = proposal.get("source") == "analyst"
    analyst = proposal if decided else proposal.get("shadow_analyst")
    if not isinstance(analyst, dict) or analyst.get("verdict") != "PROPOSAL":
        return None
    value = analyst.get("confidence")
    return (value, decided) if isinstance(value, int) else None


class CalibrationRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    # ------------------------------------------------------------------ samples

    def outcomes(self, account_id: str, source: str) -> list[tuple[int, Decimal, datetime]]:
        """(raw confidence, R, decision bar time) per decision of ``source``, oldest first."""
        if source not in ARMS:
            raise ValueError(f"unknown calibration source {source!r}")
        virtual = self._s.scalars(
            select(VirtualTradeRow).where(
                VirtualTradeRow.account_id == account_id,
                VirtualTradeRow.arm.in_(ARMS[source]),
                VirtualTradeRow.r_multiple.is_not(None),
            )
        ).all()
        by_arm: dict[str, dict[VirtualArm, Decimal]] = {}
        for v in virtual:
            assert v.r_multiple is not None  # filtered above
            by_arm.setdefault(v.decision_id, {})[v.arm] = v.r_multiple
        real: dict[str, Decimal] = {}
        if source == "analyst":
            for t in self._s.scalars(
                select(TradeRow).where(
                    TradeRow.account_id == account_id,
                    TradeRow.status == TradeStatus.CLOSED,
                    TradeRow.r_multiple.is_not(None),
                    TradeRow.decision_id.is_not(None),
                )
            ).all():
                assert t.decision_id is not None and t.r_multiple is not None  # noqa: PT018 - filtered above
                real[t.decision_id] = t.r_multiple
        ids = set(by_arm) | set(real)
        if not ids:
            return []
        out = []
        for d in self._s.scalars(select(DecisionRow).where(DecisionRow.id.in_(ids))).all():
            found = raw_confidence(d.proposal, source)
            if found is None:
                continue
            confidence, decided = found
            arms = by_arm.get(d.id, {})
            if source == "committee":
                r = arms.get(VirtualArm.SHADOW_COMMITTEE)
            elif decided and d.id in real:
                r = real[d.id]
            elif VirtualArm.SHADOW_ANALYST in arms:
                r = arms[VirtualArm.SHADOW_ANALYST]
            else:
                r = arms.get(VirtualArm.BLOCKED) if decided else None
            if r is not None:
                out.append((confidence, r, d.bar_time))
        return sorted(out, key=lambda o: o[2])

    # ------------------------------------------------------------------ models

    def add(self, **fields: Any) -> CalibrationModelRow:
        version = (self._s.scalar(select(func.max(CalibrationModelRow.version))) or 0) + 1
        row = CalibrationModelRow(version=version, created_at=self._clock.now(), **fields)
        self._s.add(row)
        self._s.flush()
        return row

    def get(self, version: int) -> CalibrationModelRow | None:
        return self._s.get(CalibrationModelRow, version)

    def with_status(self, source: str, status: CalibrationStatus) -> Sequence[CalibrationModelRow]:
        return self._s.scalars(
            select(CalibrationModelRow)
            .where(CalibrationModelRow.source == source, CalibrationModelRow.status == status)
            .order_by(CalibrationModelRow.version)
        ).all()

    def active(self, source: str) -> CalibrationModelRow | None:
        rows = self.with_status(source, CalibrationStatus.ACTIVE)
        return rows[-1] if rows else None

    def history(self, limit: int = 50) -> Sequence[CalibrationModelRow]:
        return self._s.scalars(
            select(CalibrationModelRow).order_by(CalibrationModelRow.version.desc()).limit(limit)
        ).all()

    def active_versions(self) -> tuple[int, ...]:
        """The ACTIVE versions, all sources: a cheap fingerprint for the pipeline's cache."""
        return tuple(
            self._s.scalars(
                select(CalibrationModelRow.version)
                .where(CalibrationModelRow.status == CalibrationStatus.ACTIVE)
                .order_by(CalibrationModelRow.version)
            ).all()
        )

    def decide(self, version: int, status: CalibrationStatus, by: str) -> CalibrationModelRow:
        row = self.get(version)
        if row is None:
            raise ValueError(f"no calibration model {version}")
        row.status, row.decided_by, row.decided_at = status, by, self._clock.now()
        self._s.flush()
        return row
