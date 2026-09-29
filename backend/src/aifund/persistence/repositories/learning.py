"""The learning loop's tables (docs/02 §1.5, docs/04): rules and their versions, rulebook versions, rule
evaluations, audit runs, trade reviews, and the outcome dataset the miner and the validator read."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aifund.domain.enums import RuleStatus, Side, TradeStatus, VirtualStatus
from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.persistence.tables import (
    AuditRunRow,
    DecisionRow,
    FeatureSnapshotRow,
    RulebookVersionRow,
    RuleEvaluationRow,
    RuleRow,
    TradeReviewRow,
    TradeRow,
    VirtualTradeRow,
)
from aifund.ports.system import ClockPort

CLOSED_TRADES = (TradeStatus.CLOSED, TradeStatus.ORPHAN_CLOSED)
FINISHED_VIRTUAL = (VirtualStatus.CLOSED, VirtualStatus.EXPIRED)
LIVE = (RuleStatus.ACTIVE, RuleStatus.SHADOW)


class RuleRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def next_id(self) -> str:
        ids = self._s.scalars(select(RuleRow.rule_id).distinct()).all()
        numbers = [int(i.split("-")[1]) for i in ids]
        return f"R-{max(numbers, default=0) + 1:04d}"

    def add(self, **fields: Any) -> RuleRow:
        fields.setdefault("created_at", self._clock.now())
        row = RuleRow(**fields)
        self._s.add(row)
        self._s.flush()
        return row

    def get(self, rule_id: str, version: int | None = None) -> RuleRow | None:
        """A rule version; the latest when ``version`` is None."""
        stmt = select(RuleRow).where(RuleRow.rule_id == rule_id)
        if version is not None:
            stmt = stmt.where(RuleRow.version == version)
        return self._s.scalars(stmt.order_by(RuleRow.version.desc()).limit(1)).first()

    def require(self, rule_id: str, version: int | None = None) -> RuleRow:
        row = self.get(rule_id, version)
        if row is None:
            raise InvariantViolation(f"no rule {rule_id} v{version}")
        return row

    def with_status(self, *statuses: RuleStatus) -> Sequence[RuleRow]:
        stmt = select(RuleRow).where(RuleRow.status.in_(statuses))
        return self._s.scalars(stmt.order_by(RuleRow.rule_id, RuleRow.version)).all()

    def live(self) -> Sequence[RuleRow]:
        return self.with_status(*LIVE)

    def by_sha(self, dsl_sha256: str) -> Sequence[RuleRow]:
        return self._s.scalars(select(RuleRow).where(RuleRow.dsl_sha256 == dsl_sha256)).all()

    def all(self, status: RuleStatus | None = None) -> Sequence[RuleRow]:
        stmt = select(RuleRow)
        if status is not None:
            stmt = stmt.where(RuleRow.status == status)
        return self._s.scalars(stmt.order_by(RuleRow.rule_id.desc(), RuleRow.version.desc())).all()

    def versions(self, rule_id: str) -> Sequence[RuleRow]:
        stmt = select(RuleRow).where(RuleRow.rule_id == rule_id).order_by(RuleRow.version.desc())
        return self._s.scalars(stmt).all()

    def set_dsl(self, rule_id: str, version: int, dsl: dict[str, Any], dsl_sha256: str) -> RuleRow:
        """Only a CANDIDATE's DSL may change (the validator sets its action); a tried rule is immutable."""
        row = self.require(rule_id, version)
        if row.status is not RuleStatus.CANDIDATE:
            raise InvariantViolation(f"{rule_id} v{version} is {row.status}: its DSL is fixed")
        row.dsl, row.dsl_sha256 = dsl, dsl_sha256
        self._s.flush()
        return row

    def update(self, rule_id: str, version: int, **fields: Any) -> RuleRow:
        row = self.require(rule_id, version)
        for key, value in fields.items():
            if not hasattr(RuleRow, key) or key in ("rule_id", "version", "dsl", "dsl_sha256"):
                raise InvariantViolation(f"rule field {key!r} cannot be updated")
            setattr(row, key, value)
        self._s.flush()
        return row


class RulebookRepository:
    """``rulebook_versions``: one row per change to the set of ACTIVE/SHADOW rules (docs/04 §8)."""

    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def latest(self) -> RulebookVersionRow | None:
        stmt = select(RulebookVersionRow).order_by(RulebookVersionRow.version.desc()).limit(1)
        return self._s.scalars(stmt).first()

    def version(self) -> int:
        return int(self._s.scalar(select(func.max(RulebookVersionRow.version))) or 0)

    def get(self, version: int) -> RulebookVersionRow | None:
        return self._s.get(RulebookVersionRow, version)

    def history(self, limit: int = 100) -> Sequence[RulebookVersionRow]:
        stmt = select(RulebookVersionRow).order_by(RulebookVersionRow.version.desc()).limit(limit)
        return self._s.scalars(stmt).all()

    def record(self, reason: str) -> RulebookVersionRow:
        """Snapshot the current ACTIVE/SHADOW rules as the next version (call after every status change)."""
        live = RuleRepository(self._s, self._clock).live()
        row = RulebookVersionRow(
            version=self.version() + 1,
            active_rules=[f"{r.rule_id}v{r.version}" for r in live if r.status is RuleStatus.ACTIVE],
            shadow_rules=[f"{r.rule_id}v{r.version}" for r in live if r.status is RuleStatus.SHADOW],
            reason=reason[:255],
            created_at=self._clock.now(),
        )
        self._s.add(row)
        self._s.flush()
        return row


class RuleEvaluationRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add_many(self, decision_id: str, rows: Iterable[dict[str, Any]]) -> None:
        for r in rows:
            self._s.add(RuleEvaluationRow(decision_id=decision_id, **r))
        self._s.flush()

    def matched_decisions(self, rule_id: str, version: int, since: datetime | None = None) -> list[str]:
        """Decisions a rule version matched (in either mode), oldest first."""
        stmt = (
            select(RuleEvaluationRow.decision_id)
            .join(DecisionRow, DecisionRow.id == RuleEvaluationRow.decision_id)
            .where(
                RuleEvaluationRow.rule_id == rule_id,
                RuleEvaluationRow.rule_version == version,
                RuleEvaluationRow.matched.is_(True),
            )
            .order_by(DecisionRow.created_at)
        )
        if since is not None:
            stmt = stmt.where(DecisionRow.created_at >= since)
        return list(dict.fromkeys(self._s.scalars(stmt).all()))

    def for_decision(self, decision_id: str) -> Sequence[RuleEvaluationRow]:
        stmt = select(RuleEvaluationRow).where(RuleEvaluationRow.decision_id == decision_id)
        return self._s.scalars(stmt.order_by(RuleEvaluationRow.id)).all()


class AuditRunRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def start(self, **fields: Any) -> AuditRunRow:
        row = AuditRunRow(id=new_id(), status="RUNNING", created_at=self._clock.now(), **fields)
        self._s.add(row)
        self._s.flush()
        return row

    def finish(self, run_id: str, status: str, **fields: Any) -> AuditRunRow:
        row = self._s.get(AuditRunRow, run_id)
        if row is None:
            raise InvariantViolation(f"no audit run {run_id}")
        for key, value in fields.items():
            setattr(row, key, value)
        row.status, row.finished_at = status, self._clock.now()
        self._s.flush()
        return row

    def get(self, run_id: str) -> AuditRunRow | None:
        return self._s.get(AuditRunRow, run_id)

    def last(self, status: str | None = None) -> AuditRunRow | None:
        stmt = select(AuditRunRow)
        if status is not None:
            stmt = stmt.where(AuditRunRow.status == status)
        return self._s.scalars(
            stmt.order_by(AuditRunRow.created_at.desc(), AuditRunRow.id.desc()).limit(1)
        ).first()

    def recent(self, limit: int = 50) -> Sequence[AuditRunRow]:
        stmt = select(AuditRunRow).order_by(AuditRunRow.created_at.desc(), AuditRunRow.id.desc()).limit(limit)
        return self._s.scalars(stmt).all()


class TradeReviewRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def pending(self, limit: int = 10) -> Sequence[TradeRow]:
        """Closed, enriched trades still waiting for their review, oldest first."""
        stmt = (
            select(TradeRow)
            .where(
                TradeRow.status.in_(CLOSED_TRADES),
                TradeRow.enriched_at.is_not(None),
                TradeRow.review_status == "PENDING",
            )
            .order_by(TradeRow.close_time)
            .limit(limit)
        )
        return self._s.scalars(stmt).all()

    def add(self, trade_id: str, **fields: Any) -> TradeReviewRow:
        row = TradeReviewRow(trade_id=trade_id, created_at=self._clock.now(), **fields)
        self._s.add(row)
        self.mark(trade_id, "DONE")
        return row

    def mark(self, trade_id: str, status: str) -> None:
        trade = self._s.get(TradeRow, trade_id)
        if trade is None:
            raise InvariantViolation(f"no trade {trade_id}")
        trade.review_status = status
        self._s.flush()

    def get(self, trade_id: str) -> TradeReviewRow | None:
        return self._s.get(TradeReviewRow, trade_id)

    def tags_by_trade(self, trade_ids: Sequence[str]) -> dict[str, list[str]]:
        if not trade_ids:
            return {}
        rows = self._s.scalars(select(TradeReviewRow).where(TradeReviewRow.trade_id.in_(trade_ids))).all()
        return {r.trade_id: [str(t) for t in r.tags] for r in rows}


# ------------------------------------------------------------------ the outcome dataset (docs/04 §3)


@dataclass(frozen=True)
class Outcome:
    """A finished trade or virtual trade with its entry snapshot: one sample for the miner and validator."""

    key: str  # "T:<trade id>" or "V:<virtual id>"
    decision_id: str | None
    time: datetime  # entry time
    symbol: str  # as the broker names it
    side: Side
    setup_tag: str | None
    trigger_tf: str | None
    r: Decimal
    virtual: bool
    features: dict[str, Any]
    feature_set_version: int
    confidence: int | None  # the decision's raw (LLM or baseline) confidence


class OutcomeRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def between(self, start: datetime, end: datetime) -> list[Outcome]:
        """Closed real trades and finished virtual trades entered in [start, end), each with its snapshot,
        oldest first. A decision that both traded and left shadow copies counts once per (side, setup): the
        real trade wins over its virtual twins."""
        trades = self._s.execute(
            select(TradeRow, FeatureSnapshotRow, DecisionRow.llm_confidence, DecisionRow.trigger_tf)
            .join(FeatureSnapshotRow, FeatureSnapshotRow.id == TradeRow.snapshot_id)
            .outerjoin(DecisionRow, DecisionRow.id == TradeRow.decision_id)
            .where(
                TradeRow.status.in_(CLOSED_TRADES),
                TradeRow.r_multiple.is_not(None),
                TradeRow.open_time >= start,
                TradeRow.open_time < end,
            )
        ).all()
        virtuals = self._s.execute(
            select(VirtualTradeRow, FeatureSnapshotRow, DecisionRow.llm_confidence, DecisionRow.trigger_tf)
            .join(FeatureSnapshotRow, FeatureSnapshotRow.id == VirtualTradeRow.snapshot_id)
            .join(DecisionRow, DecisionRow.id == VirtualTradeRow.decision_id)
            .where(
                VirtualTradeRow.status.in_(FINISHED_VIRTUAL),
                VirtualTradeRow.r_multiple.is_not(None),
                VirtualTradeRow.entry_time >= start,
                VirtualTradeRow.entry_time < end,
            )
            .order_by(VirtualTradeRow.arm)  # BLOCKED before the shadow arms
        ).all()
        out: dict[tuple[str, Side, str | None], Outcome] = {}
        for t, snap, confidence, tf in trades:
            o = Outcome(
                f"T:{t.id}", t.decision_id, t.open_time, t.symbol, t.side, t.setup_tag, t.trigger_tf or tf,
                t.r_multiple, False, dict(snap.features), snap.feature_set_version, confidence,
            )  # fmt: skip
            out[(t.decision_id or o.key, t.side, t.setup_tag)] = o
        for v, snap, confidence, tf in virtuals:
            dedup = (v.decision_id, v.side, v.setup_tag)
            if dedup in out:
                continue
            out[dedup] = Outcome(
                f"V:{v.id}", v.decision_id, v.entry_time, v.symbol, v.side, v.setup_tag, tf, v.r_multiple,
                True, dict(snap.features), snap.feature_set_version, confidence,
            )  # fmt: skip
        return sorted(out.values(), key=lambda o: (o.time, o.key))

    def closed_since(self, since: datetime) -> int:
        """Real trades closed at or after ``since`` (the audit's new-trades trigger)."""
        stmt = (
            select(func.count())
            .select_from(TradeRow)
            .where(TradeRow.status.in_(CLOSED_TRADES), TradeRow.close_time >= since)
        )
        return int(self._s.scalar(stmt) or 0)

    def for_decisions(self, decision_ids: Sequence[str]) -> dict[str, Decimal]:
        """R earned by each decision's own direction: its real trade, else the finished virtual trade on the
        side the decision took (a bar's shadow arms may point both ways; docs/04 §8)."""
        if not decision_ids:
            return {}
        ids = list(decision_ids)
        found: dict[str, Decimal] = {}
        for decision_id, r in self._s.execute(
            select(TradeRow.decision_id, TradeRow.r_multiple).where(
                TradeRow.decision_id.in_(ids),
                TradeRow.status.in_(CLOSED_TRADES),
                TradeRow.r_multiple.is_not(None),
            )
        ).all():
            found[decision_id] = r
        sides = {
            d.id: Side.BUY if (d.proposal or {}).get("direction") == "LONG" else Side.SELL
            for d in self._s.scalars(select(DecisionRow).where(DecisionRow.id.in_(ids))).all()
        }
        for v in self._s.scalars(
            select(VirtualTradeRow)
            .where(
                VirtualTradeRow.decision_id.in_(ids),
                VirtualTradeRow.status.in_(FINISHED_VIRTUAL),
                VirtualTradeRow.r_multiple.is_not(None),
            )
            .order_by(VirtualTradeRow.arm)
        ).all():
            if v.side is sides.get(v.decision_id) and v.r_multiple is not None:
                found.setdefault(v.decision_id, v.r_multiple)
        return found
