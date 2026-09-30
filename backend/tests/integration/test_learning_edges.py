"""Learning-loop edges (roadmap 7.7-7.10): stronger duplicates become new versions and supersede, auditor
retire suggestions, strategy-level findings, shadow rejection, a failing auditor, unreadable stored rules and
the repositories' guards."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.auditor import Auditor
from aifund.config.trading_config import LLMConfig
from aifund.domain.enums import CommandType, RuleStatus, Side, VirtualArm
from aifund.domain.errors import InvariantViolation
from aifund.engine.learning import Learning
from aifund.engine.rulebook import load
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import (
    AuditRunRepository,
    OutcomeRepository,
    RuleEvaluationRepository,
    RuleRepository,
    TradeReviewRepository,
)
from aifund.ports.llm import LLMError
from aifund.rules import dsl
from aifund.vault import review_exporter as rx
from tests.fakes.learning import seed_outcome
from tests.integration.test_learning import FAST, Notices, config, rules, seed
from tests.unit.rules.synthetic import dataset

from .conftest import T0

NY_LONG = {
    "all": [
        {"feature": "ctx.session", "op": "==", "value": "NY"},
        {"feature": "m15.rsi14", "op": ">", "value": 40},
    ]
}
SAME_SET = {
    "all": [
        {"feature": "m15.rsi14", "op": ">", "value": 40},
        {"feature": "ctx.session", "op": "==", "value": "NY"},
    ]
}


def add_rule(
    factory: sessionmaker[Session],
    clock: FakeClock,
    rid: str,
    status: RuleStatus,
    conditions: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    **fields: Any,
) -> dsl.Rule:
    r = dsl.parse(
        {
            "rule_id": rid,
            "scope": {"directions": ["LONG"]},
            "conditions": conditions,
            "action": {"type": "penalty", "points": 10},
        }
    )
    with unit_of_work(factory) as s:
        RuleRepository(s, clock).add(
            rule_id=rid,
            version=1,
            status=status,
            dsl=dsl.dump(r),
            dsl_sha256=dsl.dsl_sha256(r),
            origin="OPERATOR",
            evidence=evidence,
            shadow_started_at=clock.now(),
            **fields,
        )
    return r


def auditor(*answers: Any) -> Auditor:
    return Auditor(FakeLLM(list(answers)), LLMConfig(analyst_model="a", auditor_model="b"))


def proposing(conditions: dict[str, Any], **extra: Any) -> Any:
    def answer(request: Any) -> dict[str, Any]:
        cited = next(
            x.split(" | ")[0] for x in request.messages[1].content.splitlines() if x.startswith("C-")
        )
        return {
            "candidate_rules": [
                {
                    "scope": {"directions": ["LONG"]},
                    "conditions": conditions,
                    "action": {"type": "penalty", "points": 10},
                    "hypothesis": "NY longs get swept.",
                    "cited_clusters": [cited],
                }
            ],
            **extra,
        }

    return answer


async def test_a_stronger_duplicate_becomes_the_next_version_and_supersedes_it(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    seed(factory, dataset(600, seed=1))
    add_rule(factory, clock, "R-0001", RuleStatus.SHADOW, NY_LONG, {"mean_matched": -0.1})
    loop = Learning(
        config(), factory, clock, Notices(), auditor=auditor(proposing(SAME_SET)), miner_config=FAST
    )
    await loop.run_audit("manual")
    assert [(r.rule_id, r.version, r.status) for r in rules(factory)] == [
        ("R-0001", 1, RuleStatus.SHADOW),
        ("R-0001", 2, RuleStatus.SHADOW),
    ]
    out = await loop.handle(CommandType.APPROVE_RULE, {"rule_id": "R-0001", "version": 2, "force": True})
    assert out["changes"] == ["R-0001v2 SHADOW->ACTIVE", "R-0001v1 SHADOW->RETIRED"]
    assert rules(factory)[0].retire_reason == "superseded by v2"


async def test_the_same_rule_proposed_again_is_not_registered_twice(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    seed(factory, dataset(600, seed=1))
    add_rule(
        factory,
        clock,
        "R-0001",
        RuleStatus.ACTIVE,
        NY_LONG,
        {"n_matched": 50, "mean_matched": -0.7, "mean_unmatched": 0.1},
    )
    loop = Learning(
        config(), factory, clock, Notices(), auditor=auditor(proposing(NY_LONG)), miner_config=FAST
    )
    out = await loop.run_audit("manual")
    assert len(rules(factory)) == 1
    with factory() as s:
        run = AuditRunRepository(s, clock).get(out["audit_run_id"])
    assert run is not None and "already ACTIVE as R-0001" in (run.lessons_md or "")  # noqa: PT018


async def test_retire_suggestions_trigger_a_review_and_broad_rules_are_findings(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    seed(factory, dataset(600, seed=1), end=T0 - timedelta(days=50))  # the pattern lost, long ago
    seed(factory, dataset(240, seed=2, planted=False))  # lately it does not: the holdout is this
    add_rule(
        factory,
        clock,
        "R-0001",
        RuleStatus.ACTIVE,
        NY_LONG,
        {"mean_matched": -0.5, "review_failures": 1},
        review_at=T0 + timedelta(days=9),
    )
    broad = {"all": [{"feature": "m15.rsi14", "op": ">=", "value": 6}]}
    answer = proposing(broad, retire_suggestions=[{"rule_id": "R-0001", "reasoning": "no longer loses"}])
    loop = Learning(
        config(window_days=260), factory, clock, Notices(), auditor=auditor(answer), miner_config=FAST
    )
    out = await loop.run_audit("manual")
    retired = next(r for r in rules(factory) if r.rule_id == "R-0001")
    assert retired.status is RuleStatus.RETIRED, retired.evidence
    assert "failed re-validation twice" in (retired.retire_reason or "")
    assert any("too broad" in f for f in out["findings"])
    broad_row = next(r for r in rules(factory) if r.rule_id != "R-0001")
    assert broad_row.evidence is not None and "strategy_finding" in broad_row.evidence  # noqa: PT018


async def test_a_winning_shadow_rule_is_rejected_and_a_failing_auditor_is_reported(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    add_rule(
        factory,
        clock,
        "R-0001",
        RuleStatus.SHADOW,
        {"all": [{"feature": "m15.nr7", "op": "==", "value": False}]},
    )
    clock.advance(days=1)
    with unit_of_work(factory) as s:
        for i in range(12):
            decision_id, _ = seed_outcome(
                s, when=clock.now() + timedelta(hours=i), r=1.5, features={"m15.nr7": False}
            )
            RuleEvaluationRepository(s).add_many(
                decision_id,
                [
                    {
                        "rule_id": "R-0001",
                        "rule_version": 1,
                        "mode": "SHADOW",
                        "matched": True,
                        "action_applied": None,
                    }
                ],
            )
    seed(factory, dataset(600, seed=1), end=clock.now())
    loop = Learning(
        config(), factory, clock, Notices(), auditor=auditor(LLMError("HTTP 500")), miner_config=FAST
    )
    out = await loop.run_audit("manual")
    assert "R-0001v1 SHADOW->REJECTED" in out["changes"]
    assert "win" in (rules(factory)[0].retire_reason or "")
    with factory() as s:
        run = AuditRunRepository(s, clock).get(out["audit_run_id"])
    assert run is not None
    assert (run.status, "Auditor failed: HTTP 500" in (run.lessons_md or "")) == ("PARTIAL", True)
    assert (await loop.handle(CommandType.RUN_AUDIT, {}))["audit_run_id"]


def test_an_unreadable_stored_rule_is_skipped_not_enforced(
    factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    good = add_rule(factory, clock, "R-0001", RuleStatus.ACTIVE, NY_LONG)
    with unit_of_work(factory) as s:
        RuleRepository(s, clock).add(
            rule_id="R-0002",
            version=1,
            status=RuleStatus.ACTIVE,
            dsl_sha256="x",
            origin="OPERATOR",
            dsl={
                "rule_id": "R-0002",
                "conditions": {"all": [{"feature": "m15.rsi14", "op": ">", "value": 1}]},
                "action": {"type": "boost", "points": 5},
            },  # a shape this version no longer accepts
        )
        book = load(s, clock)
    assert [b.rule.rule_id for b in book.rules] == [good.rule_id]
    from aifund.api.routes.learning import rule_out

    with factory() as s:
        out = rule_out(RuleRepository(s, clock).require("R-0002"))
    assert out.text.startswith("unreadable") and out.action == {}  # noqa: PT018
    week = rx.Week(2026, 39, T0, T0)
    assert "no active rules" in rx.render(week, links={}, created=T0.date())
    with factory() as s:
        assert rx._describe(RuleRepository(s, clock).require("R-0002")) == "(unreadable rule)"
    assert rx._evidence(None) == "no evidence recorded"


def test_repository_guards(factory: sessionmaker[Session], clock: FakeClock) -> None:
    add_rule(factory, clock, "R-0001", RuleStatus.SHADOW, NY_LONG)
    with unit_of_work(factory) as s:
        repo = RuleRepository(s, clock)
        with pytest.raises(InvariantViolation, match="no rule R-0009"):
            repo.require("R-0009")
        with pytest.raises(InvariantViolation, match="DSL is fixed"):
            repo.set_dsl("R-0001", 1, {}, "x")
        with pytest.raises(InvariantViolation, match="cannot be updated"):
            repo.update("R-0001", 1, dsl={})
        with pytest.raises(InvariantViolation, match="no audit run"):
            AuditRunRepository(s, clock).finish("nope", "DONE")
        with pytest.raises(InvariantViolation, match="no trade"):
            TradeReviewRepository(s, clock).mark("nope", "DONE")
        assert TradeReviewRepository(s, clock).tags_by_trade([]) == {}
        assert OutcomeRepository(s).for_decisions([]) == {}


def test_outcomes_count_a_decision_once_and_by_its_own_side(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    with unit_of_work(factory) as s:
        decision_id, _ = seed_outcome(s, when=T0, r=-1, features={})
        from aifund.persistence.tables import VirtualTradeRow

        twin = s.query(VirtualTradeRow).count()
        seed_outcome(
            s,
            when=T0 + timedelta(hours=1),
            r=2,
            features={},
            virtual=True,
            side=Side.SELL,
            arm=VirtualArm.SHADOW_ANALYST,
        )
        other = s.query(VirtualTradeRow).one()
        other.decision_id = decision_id  # a shadow of the same bar pointing the other way
        s.flush()
        outcomes = OutcomeRepository(s).between(T0 - timedelta(days=1), T0 + timedelta(days=1))
        assert [o.key[0] for o in outcomes] == ["T", "V"] and twin == 0  # noqa: PT018 - different sides: both count
        assert float(OutcomeRepository(s).for_decisions([decision_id])[decision_id]) == -1.0
