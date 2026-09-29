"""Virtual (counterfactual) trades for blocked signals (docs/02 §1.3, docs/03 §14.4)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.domain.enums import VirtualArm, VirtualStatus
from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.persistence.tables import DecisionRow, VirtualTradeRow
from aifund.ports.system import ClockPort

ACTIVE = (VirtualStatus.PENDING, VirtualStatus.OPEN)
_TRANSITIONS = {
    VirtualStatus.PENDING: {
        VirtualStatus.OPEN,
        VirtualStatus.CLOSED,
        VirtualStatus.EXPIRED,
        VirtualStatus.NO_ENTRY,
    },
    VirtualStatus.OPEN: {VirtualStatus.CLOSED, VirtualStatus.EXPIRED},
}
_FIELDS = {
    "entry_price", "sl", "tp", "exit_time", "exit_price", "exit_reason", "r_multiple", "mae_r", "mfe_r",
}  # fmt: skip


@dataclass(frozen=True)
class ShadowPair:
    """One bar the analyst decided on (docs/09 §7 G-LLM): what each arm's shadow earned, in R. An arm that did
    not trade (the analyst held, or no entry happened) earned 0."""

    decision_id: str
    symbol: str
    bar_time: Any
    baseline_r: Decimal
    analyst_r: Decimal
    cost_usd: Decimal


def _earned(row: VirtualTradeRow | None) -> Decimal:
    if row is None or row.r_multiple is None:
        return Decimal(0)  # no trade on that arm, or NO_ENTRY
    return row.r_multiple


class VirtualTradeRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def add_pending(self, **fields: Any) -> VirtualTradeRow:
        row = VirtualTradeRow(
            id=new_id(), status=VirtualStatus.PENDING, created_at=self._clock.now(), **fields
        )
        self._s.add(row)
        self._s.flush()
        return row

    def active(self) -> Sequence[VirtualTradeRow]:
        return self._s.scalars(
            select(VirtualTradeRow)
            .where(VirtualTradeRow.status.in_(ACTIVE))
            .order_by(VirtualTradeRow.entry_time)
        ).all()

    def update(self, virtual_id: str, status: VirtualStatus, **fields: Any) -> VirtualTradeRow:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise InvariantViolation(f"fields not updatable on a virtual trade: {sorted(unknown)}")
        row = self._s.get(VirtualTradeRow, virtual_id)
        if row is None:
            raise InvariantViolation(f"unknown virtual trade {virtual_id}")
        if status is not row.status and status not in _TRANSITIONS.get(row.status, set()):
            raise InvariantViolation(f"illegal virtual transition {row.status} -> {status} ({virtual_id})")
        row.status = status
        for key, value in fields.items():
            setattr(row, key, value)
        self._s.flush()
        return row

    def shadow_pairs(self, account_id: str) -> list[ShadowPair]:
        """Bars with a baseline shadow whose shadows have all finished, oldest first."""
        rows = self._s.scalars(
            select(VirtualTradeRow).where(
                VirtualTradeRow.account_id == account_id,
                VirtualTradeRow.arm.in_((VirtualArm.SHADOW_BASELINE, VirtualArm.SHADOW_ANALYST)),
            )
        ).all()
        by_decision: dict[str, dict[VirtualArm, VirtualTradeRow]] = {}
        for row in rows:
            by_decision.setdefault(row.decision_id, {})[row.arm] = row
        out = []
        for decision_id, arms in by_decision.items():
            baseline, analyst = arms.get(VirtualArm.SHADOW_BASELINE), arms.get(VirtualArm.SHADOW_ANALYST)
            if baseline is None or any(r.status in ACTIVE for r in arms.values()):
                continue
            decision = self._s.get(DecisionRow, decision_id)
            assert decision is not None  # a foreign key
            out.append(
                ShadowPair(
                    decision_id=decision_id,
                    symbol=baseline.symbol,
                    bar_time=decision.bar_time,
                    baseline_r=_earned(baseline),
                    analyst_r=_earned(analyst),
                    cost_usd=decision.cost_usd or Decimal(0),
                )
            )
        return sorted(out, key=lambda p: (p.bar_time, p.symbol))
