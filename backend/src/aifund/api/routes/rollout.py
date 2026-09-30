"""Rollout gates (roadmap 9.6-9.7, docs/06 §10): the level, its measured exit gates, the operator sign-off.

Signing off needs a fresh password and is refused (409) while any measured gate fails; it records the decision
in ``audit_log`` (ROLLOUT_SIGNOFF, with the gates as they were). Changing ``engine.mode`` / the risk in the
config is the operator's separate, re-authenticated step; moving down a level never needs a sign-off.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request, status

from aifund.api import context as ctx
from aifund.api.auth import Authenticated, Mutating, client_ip, require_reauth
from aifund.api.schemas import GateOut, RolloutOut, SignoffIn, SignoffOut
from aifund.engine.rollout import NAMES, NEXT, SIGNOFF, Level, Report, evaluate, level_of
from aifund.persistence.repositories.rollout import RolloutRepository
from aifund.persistence.repositories.system import AuditLogRepository

router = APIRouter(prefix="/api/rollout", tags=["rollout"])


def _report(request: Request) -> tuple[Report, list[SignoffOut]]:
    cfg = ctx.state(request).config.current().config
    account = ctx.account(request)
    level = level_of(cfg)
    with ctx.read(request) as s:
        repo = RolloutRepository(s)
        start = repo.entered(level.value) or repo.first_trade(account)
        baseline = None
        if level is Level.L3:  # live costs are compared with the DEMO period before it
            baseline = (
                repo.entered(Level.L2.value) or repo.first_trade(account),
                repo.entered(Level.L3.value),
            )
        facts = repo.facts(account, start, baseline=baseline)
        signoffs = [
            SignoffOut(
                ts=r.ts,
                level_from=(r.before or {}).get("level"),
                level_to=(r.after or {}).get("level"),
                note=(r.after or {}).get("note"),
            )
            for r in repo.signoffs()
        ]
    return evaluate(cfg, facts, ctx.clock(request).now()), signoffs


def _out(report: Report, signoffs: list[SignoffOut]) -> RolloutOut:
    nxt = NEXT.get(report.level)
    return RolloutOut(
        level=report.level.value,
        level_name=NAMES[report.level],
        next_level=nxt.value if nxt else None,
        period_start=report.period_start,
        ready=report.ready,
        gates=[GateOut(name=g.name, status=g.status.value, value=g.value, need=g.need) for g in report.gates],
        signoffs=list(reversed(signoffs)),
    )


@router.get("")
def rollout(request: Request, _s: Authenticated) -> RolloutOut:
    return _out(*_report(request))


@router.post("/signoff")
def signoff(body: SignoffIn, request: Request, session: Mutating) -> RolloutOut:
    require_reauth(request, session)
    report, _ = _report(request)
    nxt = NEXT.get(report.level)
    if nxt is None or not report.ready:
        failing = [g.name for g in report.gates if g.status.value == "FAIL"]
        raise HTTPException(status.HTTP_409_CONFLICT, f"{report.level.value} is not ready: failing {failing}")
    with ctx.tx(request) as s:
        AuditLogRepository(s, ctx.clock(request)).record(
            actor="operator", action=SIGNOFF, before={"level": report.level.value},
            after={"level": nxt.value, "note": body.note, "gates": [asdict(g) for g in report.gates]},
            ip=client_ip(request),
        )  # fmt: skip
    return _out(*_report(request))
