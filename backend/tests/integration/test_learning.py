"""The learning loop against a real database (roadmap 7.7-7.8): audit → SHADOW → ACTIVE → review → RETIRED
on a moving clock, the audit triggers, and the operator's rule commands."""

from __future__ import annotations

import itertools
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.auditor import Auditor
from aifund.config.loader import load_trading_config
from aifund.config.trading_config import LLMConfig, TradingConfig
from aifund.domain.enums import CommandType, Direction, RuleStatus, Side
from aifund.engine.learning import Learning, LearningError
from aifund.engine.rulebook import rule_of
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import (
    AuditRunRepository,
    RulebookRepository,
    RuleEvaluationRepository,
    RuleRepository,
)
from aifund.persistence.tables import EventRow, RuleRow
from aifund.ports.system import Severity
from aifund.rules import dsl
from aifund.rules.miner import MinerConfig, Sample
from tests.fakes.learning import seed_outcome
from tests.unit.rules.synthetic import dataset

from .conftest import T0

CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"
FAST = MinerConfig(resamples=200)
BATCHES = itertools.count()


class Notices:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str]] = []

    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        self.sent.append((severity, title, body))


def config(**learning: Any) -> TradingConfig:
    cfg = load_trading_config(CONFIG).config
    return cfg.model_copy(
        update={"learning": cfg.learning.model_copy(update={"window_days": 200, **learning})}
    )


def seed(factory: sessionmaker[Session], samples: list[Sample], *, end: Any = None) -> dict[str, str]:
    """Samples → closed trades (virtual trades when the sample says so) ending a day before ``end``."""
    shift = (end or T0) - timedelta(days=1, minutes=next(BATCHES)) - samples[-1].time  # batches never collide
    keys = {}
    with unit_of_work(factory) as s:
        for smp in samples:
            decision_id, _ = seed_outcome(
                s,
                when=smp.time + shift,
                r=smp.r,
                features={k: v for k, v in smp.features.items() if not k.startswith("prop.")},
                side=Side.BUY if smp.direction is Direction.LONG else Side.SELL,
                virtual=smp.virtual,
            )
            keys[smp.key] = decision_id
    return keys


def rules(factory: sessionmaker[Session]) -> list[RuleRow]:
    with factory() as s:
        return list(s.scalars(select(RuleRow).order_by(RuleRow.rule_id, RuleRow.version)).all())


def learning(
    factory: sessionmaker[Session], clock: FakeClock, cfg: TradingConfig | None = None, **kw: Any
) -> tuple[Learning, Notices]:
    notices = Notices()
    return Learning(cfg or config(), factory, clock, notices, miner_config=FAST, **kw), notices


def matched_by(rule: dsl.Rule, samples: list[Sample]) -> list[Sample]:
    return [s for s in samples if dsl.matches(rule, s.context())]


async def test_an_audit_mines_validates_and_shadows_the_planted_pattern(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    seed(factory, dataset(600, seed=1))
    loop, notices = learning(factory, clock)
    out = await loop.run_audit("manual")
    shadow = [r for r in rules(factory) if r.status is RuleStatus.SHADOW]
    assert shadow, [(r.rule_id, r.retire_reason) for r in rules(factory)]
    r = shadow[0]
    assert (r.origin, r.audit_run_id, r.shadow_started_at) == ("AUDITOR", out["audit_run_id"], T0)
    assert rule_of(r).action.type is dsl.ActionType.BLOCK  # the validator set it: mean ≈ -0.75R, n ≥ 30
    assert r.evidence is not None and r.evidence["n_matched"] >= 20 and r.evidence["cited_clusters"]  # noqa: PT018
    with factory() as s:
        run = AuditRunRepository(s, clock).get(out["audit_run_id"])
        book = RulebookRepository(s, clock).latest()
        events = s.scalars(select(EventRow).where(EventRow.type == "rule.status_changed")).all()
    assert run is not None and run.status == "DONE" and run.finished_at == T0  # noqa: PT018
    assert run.lessons_md is not None and "## Clusters surviving FDR" in run.lessons_md  # noqa: PT018
    assert run.miner_output is not None and run.miner_output["clusters"]  # noqa: PT018
    assert run.validation is not None and any(v["passed"] for v in run.validation["results"])  # noqa: PT018
    assert book is not None and f"{r.rule_id}v1" in book.shadow_rules  # noqa: PT018
    assert {e.payload["to"] for e in events} <= {"SHADOW", "REJECTED"}
    assert notices.sent[0][1] == "Audit manual"
    rejected = [x for x in rules(factory) if x.status is RuleStatus.REJECTED]
    assert all(x.retire_reason for x in rejected)  # rejected ideas keep their reasons


async def test_the_auditor_path_and_an_operator_rule_share_the_validator(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    seed(factory, dataset(600, seed=1))
    with unit_of_work(factory) as s:  # an operator-authored rule waiting as a CANDIDATE
        nr7 = dsl.parse(
            {
                "rule_id": "R-0100",
                "conditions": {"all": [{"feature": "m15.nr7", "op": "==", "value": True}]},
                "action": {"type": "block"},
            }
        )
        RuleRepository(s, clock).add(
            rule_id="R-0100",
            version=1,
            status=RuleStatus.CANDIDATE,
            dsl=dsl.dump(nr7),
            dsl_sha256=dsl.dsl_sha256(nr7),
            origin="OPERATOR",
        )

    def answer(request: Any) -> dict[str, Any]:
        cited = next(
            line.split(" | ")[0] for line in request.messages[1].content.splitlines() if line.startswith("C-")
        )
        return {
            "candidate_rules": [
                {
                    "scope": {"directions": ["LONG"]},
                    "conditions": {
                        "all": [
                            {"feature": "ctx.session", "op": "==", "value": "NY"},
                            {"feature": "m15.rsi14", "op": ">", "value": 40},
                        ]
                    },
                    "action": {"type": "penalty", "points": 10},
                    "hypothesis": "NY longs get swept at the open.",
                    "cited_clusters": [cited],
                }
            ],
            "lessons_markdown": "NY longs above RSI 40 lose.",
            "strategy_level_findings": ["check NY entries"],
        }

    llm = FakeLLM([answer])
    loop, _ = learning(factory, clock, auditor=Auditor(llm, LLMConfig(analyst_model="a", auditor_model="b")))
    out = await loop.run_audit("manual")
    by_id = {r.rule_id: r for r in rules(factory)}
    operator = by_id.pop("R-0100")
    assert operator.status is RuleStatus.REJECTED and "effect" in (operator.retire_reason or "")  # noqa: PT018
    assert rule_of(operator).action.type is dsl.ActionType.PENALTY  # the validator's action replaced "block"
    (auditor_rule,) = by_id.values()
    assert auditor_rule.status is RuleStatus.SHADOW
    assert auditor_rule.hypothesis == "NY longs get swept at the open."
    assert "check NY entries" in out["findings"]
    with factory() as s:
        run = AuditRunRepository(s, clock).get(out["audit_run_id"])
    assert run is not None and "NY longs above RSI 40 lose." in (run.lessons_md or "")  # noqa: PT018


async def shadow_rule(
    factory: sessionmaker[Session], clock: FakeClock, action: dict[str, Any]
) -> tuple[Learning, Notices, RuleRow]:
    """One SHADOW rule for the planted condition, registered directly."""
    rule = dsl.parse(
        {
            "rule_id": "R-0001",
            "scope": {"directions": ["LONG"]},
            "conditions": {
                "all": [
                    {"feature": "ctx.session", "op": "==", "value": "NY"},
                    {"feature": "m15.rsi14", "op": ">", "value": 40},
                ]
            },
            "action": action,
        }
    )
    with unit_of_work(factory) as s:
        row = RuleRepository(s, clock).add(
            rule_id="R-0001",
            version=1,
            status=RuleStatus.SHADOW,
            dsl=dsl.dump(rule),
            dsl_sha256=dsl.dsl_sha256(rule),
            origin="OPERATOR",
            evidence={"mean_matched": -0.8},
            shadow_started_at=clock.now(),
        )
        RulebookRepository(s, clock).record("test")
        s.expunge(row)
    loop, notices = learning(factory, clock)
    return loop, notices, row


def live_matches(
    factory: sessionmaker[Session], samples: list[Sample], keys: dict[str, str], rule: dsl.Rule
) -> None:
    """What the pipeline records when a SHADOW rule matches a decision."""
    with unit_of_work(factory) as s:
        for smp in matched_by(rule, samples):
            RuleEvaluationRepository(s).add_many(
                keys[smp.key],
                [
                    {
                        "rule_id": rule.rule_id,
                        "rule_version": 1,
                        "mode": "SHADOW",
                        "matched": True,
                        "action_applied": None,
                    }
                ],
            )


async def test_shadow_to_active_to_retired_on_a_moving_clock(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    loop, notices, row = await shadow_rule(factory, clock, {"type": "penalty", "points": 10})
    rule = rule_of(row)
    assert await loop.lifecycle() == []  # no live matches yet: kept in shadow

    clock.advance(
        days=55
    )  # 200 live trades (6 h apart) after the rule entered shadow; the pattern still loses
    live = dataset(600, seed=1)[400:]
    keys = seed(factory, live, end=clock.now())
    live_matches(factory, live, keys, rule)
    changes = await loop.lifecycle(force=True)
    assert changes == ["R-0001v1 SHADOW->ACTIVE"]
    (active,) = rules(factory)
    assert active.status is RuleStatus.ACTIVE and active.review_at == clock.now() + timedelta(days=30)  # noqa: PT018
    assert active.expires_at == clock.now() + timedelta(days=90)
    assert active.evidence is not None and active.evidence["shadow"]["mean_r"] < 0  # noqa: PT018
    assert notices.sent[-1][1] == "Rule R-0001v1 ACTIVE"
    assert await loop.lifecycle() == []  # hourly: nothing again this soon

    # the market changes: the pattern stops losing; two failed reviews in a row retire the rule
    clock.advance(days=31)
    seed(factory, dataset(200, seed=2, planted=False), end=clock.now())  # the holdout is the new regime
    await loop.lifecycle(force=True)
    (struck,) = rules(factory)
    assert struck.status is RuleStatus.ACTIVE and struck.evidence["review_failures"] == 1, struck.evidence  # noqa: PT018
    clock.advance(days=31)
    assert await loop.lifecycle(force=True) == ["R-0001v1 ACTIVE->RETIRED"]
    (retired,) = rules(factory)
    assert "failed re-validation twice" in (retired.retire_reason or "")
    with factory() as s:
        versions = RulebookRepository(s, clock).history()
    assert [v.active_rules for v in versions[:2]] == [[], ["R-0001v1"]]


async def test_a_block_waits_for_approval_then_the_operator_activates_it(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    loop, notices, row = await shadow_rule(factory, clock, {"type": "block"})
    clock.advance(days=55)
    live = dataset(600, seed=1)[400:]
    keys = seed(factory, live, end=clock.now())
    live_matches(factory, live, keys, rule_of(row))
    assert await loop.lifecycle(force=True) == []
    (waiting,) = rules(factory)
    assert waiting.status is RuleStatus.SHADOW and waiting.evidence["awaiting_approval"]  # noqa: PT018
    assert notices.sent[-1][1] == "Rule R-0001v1 needs approval"
    await loop.lifecycle(force=True)
    assert [n for n in notices.sent if "needs approval" in n[1]].__len__() == 1  # asked once
    out = await loop.handle(CommandType.APPROVE_RULE, {"rule_id": "R-0001"})
    assert out["changes"] == ["R-0001v1 SHADOW->ACTIVE"]
    (approved,) = rules(factory)
    assert (approved.status, approved.approved_by) == (RuleStatus.ACTIVE, "operator")
    out = await loop.handle(CommandType.RETIRE_RULE, {"rule_id": "R-0001"})
    assert rules(factory)[0].retire_reason == "operator retired"


async def test_expiry_and_the_active_rule_cap(factory: sessionmaker[Session], clock: FakeClock) -> None:
    loop, _, _ = await shadow_rule(factory, clock, {"type": "penalty", "points": 10})
    now = clock.now()
    with unit_of_work(factory) as s:
        repo = RuleRepository(s, clock)
        repo.update(
            "R-0001",
            1,
            status=RuleStatus.ACTIVE,
            expires_at=now + timedelta(days=1),
            review_at=now + timedelta(days=5),
        )
        for i, mean in ((2, -0.9), (3, -0.2)):
            r = dsl.parse(
                {
                    "rule_id": f"R-000{i}",
                    "conditions": {"all": [{"feature": "m15.nr7", "op": "==", "value": True}]},
                    "action": {"type": "penalty", "points": 5 + i},
                }
            )
            repo.add(
                rule_id=r.rule_id,
                version=1,
                status=RuleStatus.ACTIVE,
                dsl=dsl.dump(r),
                dsl_sha256=dsl.dsl_sha256(r),
                origin="OPERATOR",
                evidence={"mean_matched": mean},
                review_at=now + timedelta(days=5),
            )
    capped, _ = learning(factory, clock, config(max_active_rules=2))
    changes = await capped.lifecycle(force=True)
    assert changes == ["R-0003v1 ACTIVE->RETIRED"]  # the weakest of three
    clock.advance(days=2)
    assert await capped.lifecycle(force=True) == ["R-0001v1 ACTIVE->RETIRED"]  # expired
    assert {r.rule_id: r.retire_reason for r in rules(factory) if r.status is RuleStatus.RETIRED} == {
        "R-0001": "expired without a passing review",
        "R-0003": "max_active_rules exceeded (weakest)",
    }
    del loop


async def test_operator_commands_refuse_what_they_cannot_do(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    loop, _, _ = await shadow_rule(factory, clock, {"type": "penalty", "points": 10})
    with pytest.raises(LearningError, match="needs a rule_id"):
        await loop.handle(CommandType.APPROVE_RULE, {})
    with pytest.raises(LearningError, match="no rule R-9999"):
        await loop.handle(CommandType.REJECT_RULE, {"rule_id": "R-9999"})
    assert (await loop.handle(CommandType.RETIRE_RULE, {"rule_id": "R-0001"}))["changes"] == [
        "R-0001v1 SHADOW->RETIRED"
    ]
    with pytest.raises(LearningError, match="only a live rule"):
        await loop.handle(CommandType.RETIRE_RULE, {"rule_id": "R-0001"})
    with pytest.raises(LearningError, match="only a CANDIDATE or SHADOW"):
        await loop.handle(CommandType.REJECT_RULE, {"rule_id": "R-0001", "version": 1})
    with pytest.raises(LearningError, match="approve needs SHADOW"):
        await loop.handle(CommandType.APPROVE_RULE, {"rule_id": "R-0001", "force": True})
    with pytest.raises(LearningError, match="not a rule command"):
        await loop.handle(CommandType.PAUSE, {"rule_id": "R-0001"})


async def test_a_candidate_can_be_rejected_or_force_activated(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    loop, _ = learning(factory, clock)
    with unit_of_work(factory) as s:
        for rid in ("R-0010", "R-0011"):
            r = dsl.parse(
                {
                    "rule_id": rid,
                    "conditions": {"all": [{"feature": "m15.nr7", "op": "==", "value": True}]},
                    "action": {"type": "block"},
                }
            )
            RuleRepository(s, clock).add(
                rule_id=rid,
                version=1,
                status=RuleStatus.CANDIDATE,
                dsl=dsl.dump(r),
                dsl_sha256=dsl.dsl_sha256(r),
                origin="OPERATOR",
            )
    await loop.handle(CommandType.REJECT_RULE, {"rule_id": "R-0010"})
    out = await loop.handle(CommandType.APPROVE_RULE, {"rule_id": "R-0011", "force": True})
    assert out["changes"] == ["R-0011v1 CANDIDATE->ACTIVE"]
    assert [(r.rule_id, r.status) for r in rules(factory)] == [
        ("R-0010", RuleStatus.REJECTED),
        ("R-0011", RuleStatus.ACTIVE),
    ]


async def test_audit_triggers(factory: sessionmaker[Session], clock: FakeClock) -> None:
    loop, _ = learning(
        factory, clock, config(audit_schedule_utc="10:00", audit_min_new_trades=3, audit_cooldown_hours=12)
    )
    assert loop.due() is None  # 09:00, nothing new
    seed(factory, dataset(10, seed=4), end=T0 + timedelta(days=1))  # closed within the window
    assert loop.due() == "threshold"
    await loop.run_once()
    assert loop.due() is None  # cooldown
    clock.advance(hours=1)
    assert loop.due() == "nightly"
    await loop.run_once()
    assert loop.due() is None
    clock.advance(hours=13)
    assert loop.due() is None  # nothing new since the last run
    with factory() as s:
        triggers = [r.trigger for r in AuditRunRepository(s, clock).recent()]
    assert triggers == ["nightly", "threshold"]
