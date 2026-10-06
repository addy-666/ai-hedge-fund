"""Learning Lab endpoints (roadmap 7.9, docs/05 §3): rules with their evidence and matches, the operator's
rule actions (as engine commands), operator-authored rules, rulebook history and diffs, audit runs, and the
feature registry for the rule editor; confidence calibration models (8.5: fit, approve, reject). Re-auth:
approving a block rule, force-activating, approving a calibration (docs/05 §2)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from aifund.api import context as ctx
from aifund.api.auth import Authenticated, Mutating, require_reauth
from aifund.api.routes.commands import enqueue
from aifund.api.schemas import (
    AuditRunDetail,
    AuditRunOut,
    CommandAccepted,
    FeatureOut,
    RulebookDiff,
    RulebookVersionOut,
    RuleCreated,
    RuleDetail,
    RuleIn,
    RuleMatch,
    RuleOut,
)
from aifund.domain.enums import CalibrationStatus, CommandType, RuleStatus
from aifund.engine.rulebook import rule_of
from aifund.market import feature_registry as reg
from aifund.persistence.repositories.calibration import CalibrationRepository
from aifund.persistence.repositories.learning import (
    AuditRunRepository,
    OutcomeRepository,
    RulebookRepository,
    RuleRepository,
)
from aifund.persistence.tables import DecisionRow, RuleEvaluationRow, RuleRow
from aifund.rules import dsl
from aifund.rules.miner import minable
from aifund.vault.review_exporter import NAME, listing

router = APIRouter(prefix="/api", tags=["learning"])
MAX_MATCHES = 100


def rule_out(row: RuleRow) -> RuleOut:
    out = RuleOut.model_validate(row)
    try:
        rule = rule_of(row)
        text, action = rule.describe(), rule.action.model_dump(mode="json", exclude_none=True)
    except dsl.RuleError as exc:  # stored under an older registry: still listed, never enforced
        text, action = f"unreadable: {exc}", {}
    awaiting = bool((row.evidence or {}).get("awaiting_approval")) and row.status is RuleStatus.SHADOW
    return out.model_copy(update={"text": text, "action": action, "awaiting_approval": awaiting})


@router.get("/rules")
def rules(
    request: Request, _s: Authenticated, status_: Annotated[RuleStatus | None, Query(alias="status")] = None
) -> list[RuleOut]:
    with ctx.read(request) as s:
        found = RuleRepository(s, ctx.clock(request)).all(status_)
        return [rule_out(r) for r in found]


@router.get("/rules/{rule_id}")
def rule(request: Request, rule_id: str, _s: Authenticated) -> RuleDetail:
    with ctx.read(request) as s:
        versions = RuleRepository(s, ctx.clock(request)).versions(rule_id)
        if not versions:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
        rows = s.execute(
            select(RuleEvaluationRow, DecisionRow)
            .join(DecisionRow, DecisionRow.id == RuleEvaluationRow.decision_id)
            .where(RuleEvaluationRow.rule_id == rule_id)
            .order_by(DecisionRow.created_at.desc())
            .limit(MAX_MATCHES)
        ).all()
        outcomes = OutcomeRepository(s).for_decisions([d.id for _, d in rows])
        matches = [
            RuleMatch(
                decision_id=d.id,
                symbol=d.symbol,
                bar_time=d.bar_time,
                outcome=d.outcome.value,
                mode=e.mode,
                matched=e.matched,
                r=outcomes.get(d.id),
            )
            for e, d in rows
        ]
        out = [rule_out(v) for v in versions]
    return RuleDetail(rule=out[0], versions=out, matches=matches)


def _rule_command(request: Request, rule_id: str, type_: CommandType, payload: dict[str, Any]) -> str:
    with ctx.read(request) as s:
        if RuleRepository(s, ctx.clock(request)).get(rule_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rule")
    return enqueue(request, type_, {"rule_id": rule_id, **payload})


@router.post("/rules/{rule_id}/approve", status_code=status.HTTP_202_ACCEPTED)
def approve(request: Request, rule_id: str, session: Mutating, force: bool = False) -> CommandAccepted:
    with ctx.read(request) as s:
        row = RuleRepository(s, ctx.clock(request)).get(rule_id)
        blocks = row is not None and row.dsl.get("action", {}).get("type") == dsl.ActionType.BLOCK.value
    if force or blocks:
        require_reauth(request, session)
    payload: dict[str, Any] = {"force": True} if force else {}
    return CommandAccepted(command_id=_rule_command(request, rule_id, CommandType.APPROVE_RULE, payload))


@router.post("/rules/{rule_id}/reject", status_code=status.HTTP_202_ACCEPTED)
def reject(request: Request, rule_id: str, _s: Mutating) -> CommandAccepted:
    return CommandAccepted(command_id=_rule_command(request, rule_id, CommandType.REJECT_RULE, {}))


@router.post("/rules/{rule_id}/retire", status_code=status.HTTP_202_ACCEPTED)
def retire(request: Request, rule_id: str, _s: Mutating) -> CommandAccepted:
    return CommandAccepted(command_id=_rule_command(request, rule_id, CommandType.RETIRE_RULE, {}))


@router.post("/rules", status_code=status.HTTP_201_CREATED)
def create(body: RuleIn, request: Request, _s: Mutating) -> RuleCreated:
    cfg = ctx.state(request).config.current().config
    try:
        parsed = dsl.parse(
            {"rule_id": "R-0000", "scope": body.scope, "conditions": body.conditions, "action": body.action,
             "hypothesis": body.hypothesis}
        )  # fmt: skip
        dsl.validate(
            parsed,
            max_conditions=cfg.learning.max_rule_conditions,
            symbols=[x.canonical for x in cfg.symbols],
        )
    except dsl.RuleError as exc:
        raise HTTPException(422, str(exc)) from None
    with ctx.tx(request) as s:
        repo = RuleRepository(s, ctx.clock(request))
        rule_id = repo.next_id()
        final = parsed.model_copy(update={"rule_id": rule_id})
        repo.add(
            rule_id=rule_id, version=1, status=RuleStatus.CANDIDATE, dsl=dsl.dump(final),
            dsl_sha256=dsl.dsl_sha256(final), hypothesis=body.hypothesis, origin="OPERATOR",
        )  # fmt: skip
    return RuleCreated(rule_id=rule_id, version=1, status=RuleStatus.CANDIDATE.value)


@router.get("/rulebook/versions")
def rulebook_versions(request: Request, _s: Authenticated) -> list[RulebookVersionOut]:
    with ctx.read(request) as s:
        return [
            RulebookVersionOut.model_validate(v) for v in RulebookRepository(s, ctx.clock(request)).history()
        ]


@router.get("/rulebook/versions/{version}/diff")
def rulebook_diff(request: Request, version: int, _s: Authenticated) -> RulebookDiff:
    with ctx.read(request) as s:
        repo = RulebookRepository(s, ctx.clock(request))
        row = repo.get(version)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rulebook version")
        prev = repo.get(version - 1)
        before_a, before_s = (set(prev.active_rules), set(prev.shadow_rules)) if prev else (set(), set())
        after_a, after_s = set(row.active_rules), set(row.shadow_rules)
        return RulebookDiff(
            version=version,
            previous=prev.version if prev else None,
            activated=sorted(after_a - before_a),
            deactivated=sorted(before_a - after_a),
            shadowed=sorted(after_s - before_s),
            unshadowed=sorted(before_s - after_s),
            reason=row.reason,
        )


@router.get("/audits")
def audits(request: Request, _s: Authenticated) -> list[AuditRunOut]:
    with ctx.read(request) as s:
        return [AuditRunOut.model_validate(r) for r in AuditRunRepository(s, ctx.clock(request)).recent()]


@router.get("/audits/{run_id}")
def audit(request: Request, run_id: str, _s: Authenticated) -> AuditRunDetail:
    with ctx.read(request) as s:
        row = AuditRunRepository(s, ctx.clock(request)).get(run_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such audit run")
        return AuditRunDetail.model_validate(row)


@router.post("/audits/run", status_code=status.HTTP_202_ACCEPTED)
def run_audit(request: Request, _s: Mutating) -> CommandAccepted:
    return CommandAccepted(command_id=enqueue(request, CommandType.RUN_AUDIT, None))


def _calibration_candidate(request: Request, version: int) -> None:
    with ctx.read(request) as s:
        row = CalibrationRepository(s, ctx.clock(request)).get(version)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such calibration model")
    if row.status is not CalibrationStatus.CANDIDATE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"calibration v{version} is {row.status.value}")


@router.post("/calibration/{version}/approve", status_code=status.HTTP_202_ACCEPTED)
def approve_calibration(request: Request, version: int, session: Mutating) -> CommandAccepted:
    """Activating a calibration can raise confidences over the threshold (more trades): re-auth required."""
    _calibration_candidate(request, version)
    require_reauth(request, session)
    return CommandAccepted(command_id=enqueue(request, CommandType.APPROVE_CALIBRATION, {"version": version}))


@router.post("/calibration/{version}/reject", status_code=status.HTTP_202_ACCEPTED)
def reject_calibration(request: Request, version: int, _s: Mutating) -> CommandAccepted:
    _calibration_candidate(request, version)
    return CommandAccepted(command_id=enqueue(request, CommandType.REJECT_CALIBRATION, {"version": version}))


@router.post("/calibration/fit", status_code=status.HTTP_202_ACCEPTED)
def fit_calibration(request: Request, _s: Mutating) -> CommandAccepted:
    return CommandAccepted(command_id=enqueue(request, CommandType.FIT_CALIBRATION, None))


@router.get("/features")
def features(request: Request, _s: Authenticated) -> list[FeatureOut]:
    """The rule-usable features (entry-time, known before the rules run) for the rule editor, with the
    cross-asset features of the configured instruments (roadmap 10.5)."""
    slugs = [i.slug for i in ctx.state(request).config.current().config.instruments()]
    out = []
    for name, spec in sorted(reg.registry(instruments=slugs).items()):
        if not minable(name):
            continue
        out.append(
            FeatureOut(
                name=name,
                dtype=spec.dtype.value,
                unit=spec.unit,
                description=spec.description,
                categories=list(spec.categories) if spec.categories else None,
                bounds=list(spec.bounds) if spec.bounds else None,
            )
        )
    return out


@router.get("/exports/vault")
def vault_exports(request: Request, _s: Authenticated) -> list[str]:
    """Weekly review notes waiting for the Mac (scripts/pull_vault_reviews.py), newest first."""
    return listing(ctx.state(request).exports)


@router.get("/exports/vault/{name}", response_class=PlainTextResponse)
def vault_export(request: Request, name: str, _s: Authenticated) -> str:
    path = ctx.state(request).exports / name
    if not NAME.match(name) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such export")
    text: str = path.read_text(encoding="utf-8")
    return text
