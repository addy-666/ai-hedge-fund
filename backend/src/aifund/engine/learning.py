"""The learning loop (roadmap 7.7-7.8, docs/04): audits, the rule lifecycle, the operator's rule commands.

Audit run (``run_audit``; triggers: nightly at ``audit_schedule_utc``, ``audit_min_new_trades`` closed trades
since the last run outside ``audit_cooldown_hours``, or the RUN_AUDIT command):

1. the dataset: closed trades and finished virtual trades entered in the last ``window_days``, each with its
   entry snapshot, the ``prop.*`` features and the reviewer's tags;
2. the miner (discovery split only) → clusters;
3. the auditor LLM turns surviving clusters into candidate rules (without an auditor, each surviving cluster
   is a candidate as mined — the validator decides either way);
4. every candidate — and every operator-authored CANDIDATE — goes through the validator: pass → SHADOW,
   fail → REJECTED with its numbers; a near-duplicate of a live rule becomes that rule's next version when it
   is stronger, else it is rejected;
5. retire suggestions trigger an immediate re-validation; then the lifecycle pass;
6. the run is stored (miner output, candidates, validations, report markdown) and the operator notified.

Lifecycle pass (``lifecycle``, hourly and after every audit or rule command): SHADOW promotion/rejection from
the rule's live matches, ACTIVE re-validation at ``review_at`` and expiry, the ``max_active_rules`` cap. Every
transition records a rulebook version, a ``rule.status_changed`` event and, for ACTIVE/RETIRED/approval, a
notice. The pipeline picks up the new rulebook on its next decision.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.agents.auditor import AuditBrief, Auditor, FeatureLine, RuleLine
from aifund.config.trading_config import LearningConfig, TradingConfig
from aifund.domain.enums import CommandType, Direction, RuleStatus, Side
from aifund.engine.calibration import CALIBRATION_COMMANDS, Calibration, CalibrationError
from aifund.engine.rulebook import rule_of
from aifund.market import conditions as cond
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import (
    AuditRunRepository,
    OutcomeRepository,
    RulebookRepository,
    RuleEvaluationRepository,
    RuleRepository,
    TradeReviewRepository,
)
from aifund.persistence.repositories.system import EventRepository
from aifund.persistence.tables import RuleRow
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.rules import dsl, lifecycle
from aifund.rules.lifecycle import LifecycleConfig, ReviewVerdict, ShadowEvidence, ShadowVerdict
from aifund.rules.miner import MinerConfig, MinerResult, Sample, minable, mine
from aifund.rules.validator import Validation, ValidatorConfig, validate

log = structlog.get_logger(__name__)
LIFECYCLE_EVERY = timedelta(hours=1)
MAX_NARRATIVES = 15


class LearningError(Exception):
    """A rule command that cannot be carried out (its message becomes the command's result)."""


@dataclass
class _Changes:
    """Transitions in one transaction: the rulebook reason, events and notices to send after the commit."""

    reasons: list[str] = field(default_factory=list)
    notices: list[tuple[Severity, str, str]] = field(default_factory=list)

    def add(self, s: Session, clock: ClockPort, row: RuleRow, old: RuleStatus, why: str) -> None:
        key = f"{row.rule_id}v{row.version}"
        self.reasons.append(f"{key} {old.value}->{row.status.value}")
        severity = Severity.WARN if row.status in (RuleStatus.ACTIVE, RuleStatus.RETIRED) else Severity.INFO
        EventRepository(s, clock).append(
            "rule.status_changed",
            severity,
            {
                "rule_id": row.rule_id,
                "version": row.version,
                "from": old.value,
                "to": row.status.value,
                "why": why,
            },
        )
        if row.status in (RuleStatus.ACTIVE, RuleStatus.RETIRED):
            self.notices.append((Severity.WARN, f"Rule {key} {row.status.value}", why))


def _config(cfg: LearningConfig) -> tuple[MinerConfig, ValidatorConfig, LifecycleConfig]:
    miner = MinerConfig(
        min_matches_total=cfg.min_matches_total,
        min_effect_r=float(cfg.min_effect_r),
        fdr_q=float(cfg.fdr_q),
        holdout_fraction=float(cfg.holdout_fraction),
    )
    validator = ValidatorConfig(
        min_matches_total=cfg.min_matches_total,
        min_matches_holdout=cfg.min_matches_holdout,
        min_effect_r=float(cfg.min_effect_r),
        holdout_fraction=float(cfg.holdout_fraction),
        max_rule_coverage=float(cfg.max_rule_coverage),
        max_rule_conditions=cfg.max_rule_conditions,
    )
    life = LifecycleConfig(
        shadow_min_matches=cfg.shadow_min_matches,
        shadow_max_days=cfg.shadow_max_days,
        auto_promote_max_penalty=cfg.auto_promote_max_penalty,
        review_after_days=cfg.review_after_days,
        expire_after_days=cfg.expire_after_days,
        max_active_rules=cfg.max_active_rules,
    )
    return miner, validator, life


class Learning:
    def __init__(
        self,
        cfg: TradingConfig,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
        *,
        auditor: Auditor | None = None,
        miner_config: MinerConfig | None = None,
    ) -> None:
        self._cfg = cfg
        self._learning = cfg.learning
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._auditor = auditor
        self._miner_cfg, self._validator_cfg, self._life = _config(cfg.learning)
        if miner_config is not None:
            self._miner_cfg = miner_config
        self._canonical = {s.broker: s.canonical for s in cfg.symbols}
        self._last_lifecycle: datetime | None = None
        self._lock = asyncio.Lock()
        self.calibration = Calibration(cfg, factory, clock, notifier)  # docs/04 §9, weekly (roadmap 8.5)

    # ------------------------------------------------------------------ the dataset

    def _samples(self, s: Session, start: datetime, end: datetime) -> list[Sample]:
        outcomes = OutcomeRepository(s).between(start, end)
        tags = TradeReviewRepository(s, self._clock).tags_by_trade(
            [o.key[2:] for o in outcomes if not o.virtual]
        )
        out = []
        for o in outcomes:
            direction = Direction.LONG if o.side is Side.BUY else Direction.SHORT
            features = {
                **o.features,
                **dsl.proposal_features(direction, o.setup_tag, o.confidence, o.features),
            }
            out.append(
                Sample(
                    key=o.key,
                    time=o.time,
                    symbol=self._canonical.get(o.symbol, o.symbol),
                    direction=direction,
                    setup_tag=o.setup_tag,
                    trigger_tf=o.trigger_tf or "",
                    r=float(o.r),
                    virtual=o.virtual,
                    features=features,
                    tags=tuple(tags.get(o.key[2:], ())),
                )
            )
        return out

    def _window(self) -> tuple[datetime, datetime]:
        now = self._clock.now()
        return now - timedelta(days=self._learning.window_days), now + timedelta(seconds=1)

    def samples(self) -> list[Sample]:
        with unit_of_work(self._factory) as s:
            return self._samples(s, *self._window())

    # ------------------------------------------------------------------ the audit

    async def run_audit(self, trigger: str) -> dict[str, Any]:
        async with self._lock:
            return await self._audit(trigger)

    async def _audit(self, trigger: str) -> dict[str, Any]:
        start, end = self._window()
        samples = await asyncio.to_thread(self.samples)
        n_virtual = sum(s.virtual for s in samples)

        def begin(s: Session) -> str:
            run = AuditRunRepository(s, self._clock).start(
                trigger=trigger, window_from=start, window_to=end, n_trades=len(samples) - n_virtual,
                n_virtual=n_virtual,
            )  # fmt: skip
            return run.id

        run_id = await self._tx(begin)
        label = f"C-{self._clock.now():%Y%m%d-%H%M}"
        result = await asyncio.to_thread(mine, samples, self._miner_cfg, label=label)
        candidates: list[tuple[dsl.Rule, tuple[str, ...]]] = []
        lessons, findings, suggestions, auditor_error = "", [], [], None
        if result.clusters and self._auditor is not None:
            brief = await asyncio.to_thread(self._brief, run_id, result, samples)
            audit = await self._auditor.audit(brief)
            auditor_error = audit.error
            candidates = [(c.rule, c.cited) for c in audit.candidates]
            lessons, findings = audit.lessons_markdown, audit.findings
            suggestions = [s.rule_id for s in audit.retire_suggestions]
        elif result.clusters:  # no LLM: the surviving clusters themselves are the candidates
            candidates = [(_rule_from_cluster(c), (c.id,)) for c in result.clusters[:5]]
        validations, changes = await self._tx(
            lambda s: self._consider(s, run_id, candidates, samples, suggestions)
        )
        findings += [v["strategy_finding"] for v in validations if v.get("strategy_finding")]
        report = _report(trigger, result, validations, changes.reasons, findings, lessons, auditor_error)
        status = "DONE" if auditor_error is None else "PARTIAL"
        await self._tx(
            lambda s: AuditRunRepository(s, self._clock).finish(
                run_id,
                status,
                miner_output=result.to_json(),
                candidates=[dsl.dump(r) for r, _ in candidates],
                validation={"results": validations, "findings": findings},
                lessons_md=report,
            )
        )
        await self._send(changes)
        passed = sum(1 for v in validations if v["passed"])
        summary = (
            f"{len(samples)} outcomes, {len(result.clusters)} clusters, "
            f"{len(candidates)} candidates, {passed} passed"
        )
        await self._notifier.notify(
            Severity.INFO, f"Audit {trigger}", summary + ("\n" + "\n".join(findings) if findings else "")
        )
        more = await self.lifecycle(force=True)
        return {
            "audit_run_id": run_id,
            "summary": summary,
            "changes": changes.reasons + more,
            "findings": findings,
        }

    def _brief(self, run_id: str, result: MinerResult, samples: Sequence[Sample]) -> AuditBrief:
        names = sorted({k for smp in samples for k in smp.features if minable(k)})
        lines = []
        for name in names:
            values = [v for smp in samples if (v := smp.features.get(name)) is not None]
            nums = [float(v) for v in values if isinstance(v, int | float)]
            if nums and not all(isinstance(v, bool) for v in values):
                lines.append(FeatureLine(name, min(nums), max(nums)))
            else:
                lines.append(FeatureLine(name, None, None))
        worst = sorted(samples, key=lambda smp: smp.r)[:10]
        top = result.clusters[0] if result.clusters else None
        wins = [
            smp
            for smp in samples
            if smp.r > 0 and top is not None and dsl.matches(_rule_from_cluster(top), smp.context())
        ][:5]
        narratives = [_narrative(smp) for smp in [*worst, *wins]][:MAX_NARRATIVES]
        with unit_of_work(self._factory) as s:
            rules = [
                RuleLine(
                    f"{r.rule_id}v{r.version}",
                    r.status.value,
                    rule_of(r).describe(),
                    _evidence_text(r.evidence),
                )
                for r in RuleRepository(s, self._clock).live()
            ]
        return AuditBrief(
            run_id=run_id,
            miner=result,
            features=lines,
            symbols=[s.canonical for s in self._cfg.symbols],
            setup_tags=sorted({smp.setup_tag for smp in samples if smp.setup_tag}),
            window=f"{samples[0].time:%Y-%m-%d} -> {samples[-1].time:%Y-%m-%d}" if samples else "empty",
            n_virtual=sum(smp.virtual for smp in samples),
            narratives=narratives,
            rules=rules,
            max_conditions=self._learning.max_rule_conditions,
        )

    # ------------------------------------------------------------------ candidates → validator

    def _live_sets(self, s: Session, samples: Sequence[Sample]) -> dict[str, frozenset[str]]:
        out = {}
        for row in RuleRepository(s, self._clock).live():
            rule = rule_of(row)
            out[row.rule_id] = frozenset(smp.key for smp in samples if dsl.matches(rule, smp.context()))
        return out

    def _validate(self, s: Session, rule: dsl.Rule, samples: Sequence[Sample]) -> Validation:
        live = self._live_sets(s, samples)
        live.pop(rule.rule_id, None)
        return validate(rule, samples, self._validator_cfg, live=live)

    def _consider(
        self,
        s: Session,
        run_id: str,
        candidates: Sequence[tuple[dsl.Rule, tuple[str, ...]]],
        samples: Sequence[Sample],
        retire_suggestions: Sequence[str],
    ) -> tuple[list[dict[str, Any]], _Changes]:
        repo = RuleRepository(s, self._clock)
        changes = _Changes()
        results = []
        live = {_shape(rule_of(r)): r for r in repo.live()}
        for rule, cited in candidates:
            same = live.get(_shape(rule))
            if same is not None:  # proposed again: it is already being tried or enforced
                results.append(
                    {
                        "rule_id": same.rule_id,
                        "version": same.version,
                        "text": rule.describe(),
                        "passed": False,
                        "failures": [f"already {same.status.value} as {same.rule_id}"],
                        "action": {},
                        "evidence": {},
                        "strategy_finding": None,
                        "duplicate_of": same.rule_id,
                    }
                )
                continue
            v = self._validate(s, rule, samples)
            final = rule.model_copy(update={"action": v.action})
            target_id, version = repo.next_id(), 1
            if v.duplicate_of is not None:
                existing = repo.require(v.duplicate_of)
                if v.evidence["mean_matched"] < float((existing.evidence or {}).get("mean_matched", 0.0)):
                    target_id, version = existing.rule_id, existing.version + 1
                    v = _without_duplicate(v)
            stored = final.model_copy(update={"rule_id": target_id, "version": version})
            row = repo.add(
                rule_id=target_id, version=version, status=RuleStatus.CANDIDATE, dsl=dsl.dump(stored),
                dsl_sha256=dsl.dsl_sha256(stored), hypothesis=rule.hypothesis, origin="AUDITOR",
                audit_run_id=run_id,
            )  # fmt: skip
            self._settle(s, row, v, changes, cited=cited)
            results.append(_validation_json(stored, v))
        results += self._validate_candidates(s, samples, changes)
        for rule_id in retire_suggestions:
            suggested = repo.get(rule_id)
            if suggested is not None and suggested.status is RuleStatus.ACTIVE:
                self._review(s, suggested, samples, changes, why="auditor suggested retiring it")
        if changes.reasons:
            RulebookRepository(s, self._clock).record("; ".join(changes.reasons))
        return results, changes

    def _validate_candidates(
        self, s: Session, samples: Sequence[Sample], changes: _Changes
    ) -> list[dict[str, Any]]:
        """Operator-authored CANDIDATEs through the validator (which also sets their action)."""
        repo = RuleRepository(s, self._clock)
        results = []
        for row in repo.with_status(RuleStatus.CANDIDATE):
            v = self._validate(s, rule_of(row), samples)
            final = rule_of(row).model_copy(update={"action": v.action})
            repo.set_dsl(row.rule_id, row.version, dsl.dump(final), dsl.dsl_sha256(final))
            self._settle(s, row, v, changes, cited=())
            results.append(_validation_json(final, v))
        return results

    def _settle(
        self, s: Session, row: RuleRow, v: Validation, changes: _Changes, *, cited: tuple[str, ...]
    ) -> None:
        now = self._clock.now()
        evidence = {
            **v.evidence,
            "failures": list(v.failures),
            "validated_at": now.isoformat(),
            "cited_clusters": list(cited),
        }
        if v.strategy_finding:
            evidence["strategy_finding"] = v.strategy_finding
        if v.duplicate_of:
            evidence["duplicate_of"] = v.duplicate_of
        new = RuleStatus.SHADOW if v.passed else RuleStatus.REJECTED
        RuleRepository(s, self._clock).update(
            row.rule_id, row.version, status=new, evidence=evidence,
            shadow_started_at=now if v.passed else None,
            retire_reason=None if v.passed else "; ".join(v.failures)[:1000],
        )  # fmt: skip
        changes.add(
            s,
            self._clock,
            row,
            RuleStatus.CANDIDATE,
            "validator passed" if v.passed else "; ".join(v.failures),
        )

    # ------------------------------------------------------------------ lifecycle

    async def lifecycle(self, *, force: bool = False) -> list[str]:
        now = self._clock.now()
        if not force and self._last_lifecycle is not None and now - self._last_lifecycle < LIFECYCLE_EVERY:
            return []
        self._last_lifecycle = now
        samples = await asyncio.to_thread(self.samples)
        changes: _Changes = await self._tx(lambda s: self._lifecycle(s, samples))
        await self._send(changes)
        return changes.reasons

    def _lifecycle(self, s: Session, samples: Sequence[Sample]) -> _Changes:
        repo = RuleRepository(s, self._clock)
        changes = _Changes()
        now = self._clock.now()
        self._validate_candidates(s, samples, changes)  # operator-authored rules need not wait for an audit
        for row in repo.with_status(RuleStatus.SHADOW):
            ev = self._shadow_evidence(s, row)
            evidence = dict(row.evidence or {})
            evidence["shadow"] = {"n": ev.n, "mean_r": ev.mean_r, "days": round(ev.days, 1)}
            approved = row.approved_by is not None
            verdict, why = lifecycle.shadow_verdict(rule_of(row).action, ev, self._life, approved=approved)
            if verdict is ShadowVerdict.PROMOTE:
                repo.update(row.rule_id, row.version, evidence=evidence)
                self._activate(s, row, changes, why)
            elif verdict is ShadowVerdict.REJECT:
                repo.update(
                    row.rule_id, row.version, status=RuleStatus.REJECTED, evidence=evidence, retire_reason=why
                )
                changes.add(s, self._clock, row, RuleStatus.SHADOW, why)
            else:
                if verdict is ShadowVerdict.AWAIT_APPROVAL and not evidence.get("awaiting_approval"):
                    evidence["awaiting_approval"] = True
                    changes.notices.append(
                        (Severity.WARN, f"Rule {row.rule_id}v{row.version} needs approval", why)
                    )
                repo.update(row.rule_id, row.version, evidence=evidence)
        for row in repo.with_status(RuleStatus.ACTIVE):
            if row.expires_at is not None and now >= row.expires_at:
                self._retire(s, row, changes, "expired without a passing review")
            elif row.review_at is not None and now >= row.review_at:
                self._review(s, row, samples, changes, why="scheduled review")
        active = [
            (f"{r.rule_id}|{r.version}", float((r.evidence or {}).get("mean_matched", 0.0)))
            for r in repo.with_status(RuleStatus.ACTIVE)
        ]
        for key in lifecycle.over_limit(active, self._life.max_active_rules):
            rule_id, version = key.split("|")
            self._retire(
                s, repo.require(rule_id, int(version)), changes, "max_active_rules exceeded (weakest)"
            )
        if changes.reasons:
            RulebookRepository(s, self._clock).record("; ".join(changes.reasons))
        return changes

    def _shadow_evidence(self, s: Session, row: RuleRow) -> ShadowEvidence:
        started = row.shadow_started_at or row.created_at
        decisions = RuleEvaluationRepository(s).matched_decisions(row.rule_id, row.version, since=started)
        outcomes = OutcomeRepository(s).for_decisions(decisions)
        rs = [float(outcomes[d]) for d in decisions if d in outcomes]
        mean = sum(rs) / len(rs) if rs else None
        days = (self._clock.now() - started).total_seconds() / 86400
        return ShadowEvidence(len(rs), mean, days)

    def _activate(
        self, s: Session, row: RuleRow, changes: _Changes, why: str, *, by: str | None = None
    ) -> None:
        now = self._clock.now()
        old = row.status
        repo = RuleRepository(s, self._clock)
        repo.update(
            row.rule_id, row.version, status=RuleStatus.ACTIVE, activated_at=now,
            review_at=now + timedelta(days=self._life.review_after_days),
            expires_at=now + timedelta(days=self._life.expire_after_days),
            approved_by=by or row.approved_by, approved_at=now if by else row.approved_at,
        )  # fmt: skip
        changes.add(s, self._clock, row, old, why)
        for other in repo.versions(row.rule_id):  # a new version supersedes the live older ones
            if other.version != row.version and other.status in (RuleStatus.ACTIVE, RuleStatus.SHADOW):
                self._retire(s, other, changes, f"superseded by v{row.version}")

    def _retire(self, s: Session, row: RuleRow, changes: _Changes, why: str) -> None:
        old = row.status
        RuleRepository(s, self._clock).update(
            row.rule_id,
            row.version,
            status=RuleStatus.RETIRED,
            retired_at=self._clock.now(),
            retire_reason=why,
        )
        changes.add(s, self._clock, row, old, why)

    def _review(
        self, s: Session, row: RuleRow, samples: Sequence[Sample], changes: _Changes, *, why: str
    ) -> None:
        v = self._validate(s, rule_of(row), samples)
        evidence = dict(row.evidence or {})
        verdict, failures = lifecycle.review_verdict(v.passed, int(evidence.get("review_failures", 0)))
        evidence["review_failures"] = failures
        evidence["last_review"] = {"at": self._clock.now().isoformat(), "why": why, "passed": v.passed,
                                   "failures": list(v.failures), **v.evidence}  # fmt: skip
        now = self._clock.now()
        repo = RuleRepository(s, self._clock)
        if verdict is ReviewVerdict.RENEW:
            repo.update(
                row.rule_id, row.version, evidence=evidence,
                review_at=now + timedelta(days=self._life.review_after_days),
                expires_at=now + timedelta(days=self._life.expire_after_days),
            )  # fmt: skip
        elif verdict is ReviewVerdict.STRIKE:
            repo.update(
                row.rule_id, row.version, evidence=evidence,
                review_at=now + timedelta(days=self._life.review_after_days),
            )  # fmt: skip
        else:
            repo.update(row.rule_id, row.version, evidence=evidence)
            self._retire(s, row, changes, f"failed re-validation twice: {'; '.join(v.failures)}"[:1000])

    # ------------------------------------------------------------------ operator commands

    async def handle(self, command: CommandType, payload: dict[str, Any]) -> dict[str, Any]:
        if command is CommandType.RUN_AUDIT:
            return await self.run_audit("manual")
        if command in CALIBRATION_COMMANDS:
            try:
                return await self.calibration.handle(command, payload)
            except CalibrationError as exc:
                raise LearningError(str(exc)) from exc
        rule_id = payload.get("rule_id")
        if not isinstance(rule_id, str):
            raise LearningError(f"{command.value} needs a rule_id")
        version = payload.get("version")
        force = bool(payload.get("force"))
        async with self._lock:
            changes = await self._tx(lambda s: self._command(s, command, rule_id, version, force))
        await self._send(changes)
        return {"rule_id": rule_id, "changes": changes.reasons}

    def _command(self, s: Session, command: CommandType, rule_id: str, version: Any, force: bool) -> _Changes:
        repo = RuleRepository(s, self._clock)
        row = repo.get(rule_id, int(version) if version is not None else None)
        if row is None:
            raise LearningError(f"no rule {rule_id}")
        changes = _Changes()
        if command is CommandType.APPROVE_RULE:
            if row.status is RuleStatus.SHADOW:
                repo.update(row.rule_id, row.version, approved_by="operator", approved_at=self._clock.now())
                ev = self._shadow_evidence(s, row)
                verdict, why = lifecycle.shadow_verdict(rule_of(row).action, ev, self._life, approved=True)
                if verdict is ShadowVerdict.PROMOTE or force:
                    self._activate(s, row, changes, f"operator approved ({why})", by="operator")
            elif row.status is RuleStatus.CANDIDATE and force:
                self._activate(s, row, changes, "operator force-activated (no validation)", by="operator")
            else:
                raise LearningError(
                    f"{row.rule_id} is {row.status.value}: approve needs SHADOW (or force a CANDIDATE)"
                )
        elif command is CommandType.REJECT_RULE:
            if row.status not in (RuleStatus.CANDIDATE, RuleStatus.SHADOW):
                raise LearningError(
                    f"{row.rule_id} is {row.status.value}: only a CANDIDATE or SHADOW rule is rejected"
                )
            old = row.status
            repo.update(
                row.rule_id, row.version, status=RuleStatus.REJECTED, retire_reason="operator rejected"
            )
            changes.add(s, self._clock, row, old, "operator rejected")
        elif command is CommandType.RETIRE_RULE:
            if row.status not in (RuleStatus.ACTIVE, RuleStatus.SHADOW):
                raise LearningError(f"{row.rule_id} is {row.status.value}: only a live rule is retired")
            self._retire(s, row, changes, "operator retired")
        else:
            raise LearningError(f"{command.value} is not a rule command")
        if changes.reasons:
            RulebookRepository(s, self._clock).record("; ".join(changes.reasons))
        return changes

    # ------------------------------------------------------------------ the scheduler

    def due(self) -> str | None:
        """Which audit trigger fires now, if any (nightly, then the new-trades threshold)."""
        now = self._clock.now()
        with unit_of_work(self._factory) as s:
            last = AuditRunRepository(s, self._clock).last()
            hh, mm = (int(x) for x in self._learning.audit_schedule_utc.split(":"))
            scheduled = datetime.combine(now.date(), time(hh, mm), tzinfo=now.tzinfo)
            if now >= scheduled and (last is None or last.created_at < scheduled):
                return "nightly"
            since = last.created_at if last is not None else now - timedelta(days=self._learning.window_days)
            cooled = last is None or now - last.created_at >= timedelta(
                hours=self._learning.audit_cooldown_hours
            )
            if cooled and OutcomeRepository(s).closed_since(since) >= self._learning.audit_min_new_trades:
                return "threshold"
        return None

    async def run_once(self) -> None:
        if await asyncio.to_thread(self.calibration.due):
            await self.calibration.fit_all("weekly")
        trigger = await asyncio.to_thread(self.due)
        if trigger is not None:
            await self.run_audit(trigger)
        else:
            await self.lifecycle()

    # ------------------------------------------------------------------ helpers

    async def _tx(self, fn: Callable[[Session], Any]) -> Any:
        def run() -> Any:
            with unit_of_work(self._factory) as s:
                return fn(s)

        return await asyncio.to_thread(run)

    async def _send(self, changes: _Changes) -> None:
        for severity, title, body in changes.notices:
            await self._notifier.notify(severity, title, body)


# ------------------------------------------------------------------ helpers (pure)


def _rule_from_cluster(cluster: Any) -> dsl.Rule:
    return dsl.Rule(
        rule_id="R-0000",
        scope=cluster.scope,
        conditions=cluster.condition,
        action=dsl.RuleAction(type=dsl.ActionType.PENALTY, points=10),  # the validator sets the real action
        hypothesis=f"mined cluster {cluster.id} (no auditor): {cluster.describe()}",
        cited_clusters=(cluster.id,),
    )


def _shape(rule: dsl.Rule) -> str:
    """What a rule matches (scope and condition), whatever its action or id."""
    body = dsl.dump(rule)
    return cond.sha256({"scope": body["scope"], "conditions": body["conditions"]})


def _without_duplicate(v: Validation) -> Validation:
    failures = tuple(f for f in v.failures if not f.startswith("matches the same trades as"))
    return Validation(
        v.rule_id, not failures, failures, v.action, v.evidence, v.strategy_finding, None, v.matched
    )


def _validation_json(rule: dsl.Rule, v: Validation) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "text": rule.describe(),
        "passed": v.passed,
        "failures": list(v.failures),
        "action": v.action.model_dump(mode="json", exclude_none=True),
        "evidence": v.evidence,
        "strategy_finding": v.strategy_finding,
        "duplicate_of": v.duplicate_of,
    }


def _evidence_text(e: dict[str, Any] | None) -> str:
    if not e or "n_matched" not in e:
        return "no evidence yet"
    return f"n={e['n_matched']} mean {float(e['mean_matched']):+.2f}R vs {float(e['mean_unmatched']):+.2f}R"


def _narrative(smp: Sample) -> str:
    f = smp.features
    bits = [
        f"{k.split('.', 1)[-1]}={f[k]}"
        for k in ("ctx.session", "ctx.regime", "m15.rsi14")
        if f.get(k) is not None
    ]
    tags = f" [{', '.join(smp.tags)}]" if smp.tags else ""
    kind = " (virtual)" if smp.virtual else ""
    head = f"{smp.symbol} {smp.direction.value} {smp.setup_tag} {smp.time:%Y-%m-%d %H:%M} {smp.r:+.2f}R"
    return f"{head}{kind}: {' '.join(bits)}{tags}"


def _report(
    trigger: str,
    result: MinerResult,
    validations: Sequence[dict[str, Any]],
    changes: Sequence[str],
    findings: Sequence[str],
    lessons: str,
    auditor_error: str | None,
) -> str:
    lines = [
        f"# Audit ({trigger})",
        "",
        f"{result.n_samples} outcomes; the miner used the oldest {result.n_discovery} "
        f"(baseline {result.baseline_mean_r:+.3f}R), tested {result.tested} bins.",
        "",
        "## Clusters surviving FDR",
    ]
    lines += [
        f"- {c.id}: {c.describe()}: n={c.n}, {c.mean_r:+.2f}R vs {c.mean_r_rest:+.2f}R, p={c.p_value:.4f}"
        for c in result.clusters
    ] or ["- none"]
    lines += ["", "## Candidates"]
    for v in validations:
        verdict = "PASSED → SHADOW" if v["passed"] else "rejected: " + "; ".join(v["failures"])
        action = v["action"].get("type", "no action")
        lines.append(f"- {v['rule_id']}v{v['version']} {v['text']} ({action}): {verdict}")
    if not validations:
        lines.append("- none")
    lines += ["", "## Lifecycle changes", *([f"- {c}" for c in changes] or ["- none"])]
    if findings:
        lines += ["", "## Strategy-level findings (operator)", *[f"- {f}" for f in findings]]
    if auditor_error:
        lines += ["", f"Auditor failed: {auditor_error}"]
    if lessons:
        lines += ["", "## Auditor's lessons (not enforced)", lessons]
    return "\n".join(lines) + "\n"
