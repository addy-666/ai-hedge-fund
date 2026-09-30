"""Learned rules in the decision pipeline (roadmap 7.2, docs/04 §7): the full stack replayed with a seeded
rulebook. ACTIVE rules block or penalise whichever decision is taken (baseline or analyst), SHADOW rules are
only logged, every match is a ``rule_evaluations`` row, and the analyst sees the lessons in its prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.config.trading_config import StrategyConfig
from aifund.domain.enums import DecisionOutcome, ReasonCode, RuleStatus, VirtualArm
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import RulebookRepository, RuleRepository
from aifund.persistence.tables import DecisionRow, RuleEvaluationRow, VirtualTradeRow
from aifund.ports.llm import LLMRequest
from aifund.rules import dsl
from tests.integration.test_baseline_replay import BASELINE, echo_candidate, market, replay  # noqa: F401

pytestmark = pytest.mark.scenario
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "prompts" / "lessons_block.golden.txt"
EVIDENCE = {
    "n_matched": 23,
    "win_matched": 0.26,
    "win_unmatched": 0.48,
    "mean_matched": -0.41,
    "mean_unmatched": 0.12,
}
ALWAYS = {"all": [{"feature": "m15.rsi14", "op": ">=", "value": 0}]}  # holds on every bar


def seed(factory: sessionmaker[Session], clock: FakeClock, *rules: tuple[dict[str, Any], RuleStatus]) -> None:
    with unit_of_work(factory) as s:
        repo = RuleRepository(s, clock)
        for data, status in rules:
            r = dsl.parse(data)
            repo.add(
                rule_id=r.rule_id, version=r.version, status=status, dsl=dsl.dump(r),
                dsl_sha256=dsl.dsl_sha256(r), hypothesis=r.hypothesis, evidence=EVIDENCE, origin="OPERATOR",
            )  # fmt: skip
        RulebookRepository(s, clock).record("test rulebook")


def rule(
    rid: str, direction: str, action: dict[str, Any], conditions: dict[str, Any] = ALWAYS
) -> dict[str, Any]:
    return {
        "rule_id": rid,
        "scope": {"symbols": ["XAUUSD"], "directions": [direction], "setup_tags": ["stub_hourly"]},
        "conditions": conditions,
        "action": action,
    }


def decisions(factory: sessionmaker[Session]) -> list[DecisionRow]:
    with factory() as s:
        return list(s.scalars(select(DecisionRow).where(DecisionRow.setups.is_not(None))).all())


def direction(d: DecisionRow) -> str:
    return str((d.proposal or {}).get("direction"))


async def test_active_rules_block_and_penalise_the_baseline_shadow_rules_only_log(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    seed(
        factory,
        clock,
        (rule("R-0001", "LONG", {"type": "block"}), RuleStatus.ACTIVE),
        (rule("R-0002", "SHORT", {"type": "penalty", "points": 10}), RuleStatus.ACTIVE),  # 70 - 10 < 65
        (rule("R-0003", "SHORT", {"type": "block"}), RuleStatus.SHADOW),
    )
    report = await replay(market, factory, strategy=BASELINE, days=1)
    assert report.violations == []
    assert report.fills == 0  # every candidate is blocked or below the threshold
    ruled = [
        d for d in decisions(factory) if d.outcome not in (DecisionOutcome.SKIPPED, DecisionOutcome.ERROR)
    ]
    longs = [d for d in ruled if direction(d) == "LONG"]
    shorts = [d for d in ruled if direction(d) == "SHORT"]
    assert longs and shorts  # noqa: PT018
    for d in longs:
        assert (d.outcome, d.reason_code, d.reason_detail) == (
            DecisionOutcome.RULE_BLOCKED, ReasonCode.RULE_BLOCK.value, "R-0001v1",
        )  # fmt: skip
        assert (d.rules_matched, d.rulebook_version, d.penalty_points) == (["R-0001v1"], 1, 0)
    for d in shorts:
        assert (d.outcome, d.reason_detail) == (DecisionOutcome.BELOW_THRESHOLD, "60 < 65 (rule penalty 10)")
        assert (d.rules_matched, d.final_confidence) == (["R-0002v1", "R-0003v1 (shadow)"], 60)
    with factory() as s:
        evaluations = s.scalars(select(RuleEvaluationRow)).all()
        blocked = s.scalars(select(VirtualTradeRow).where(VirtualTradeRow.arm == VirtualArm.BLOCKED)).all()
    assert len(evaluations) == len(longs) + 2 * len(shorts)
    assert {(e.rule_id, e.mode) for e in evaluations} == {
        ("R-0001", "ACTIVE"), ("R-0002", "ACTIVE"), ("R-0003", "SHADOW"),
    }  # fmt: skip
    assert {v.decision_id for v in blocked} == {d.id for d in ruled}  # blocked signals keep being measured


async def test_the_analyst_is_ruled_the_same_way_and_sees_the_lessons(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    near_miss = {"all": [{"feature": "m15.rsi14", "op": ">=", "value": 100}]}  # never matches: no lesson
    seed(
        factory,
        clock,
        (rule("R-0001", "LONG", {"type": "block"}), RuleStatus.ACTIVE),
        (rule("R-0002", "SHORT", {"type": "risk_scale", "factor": "0.5"}), RuleStatus.ACTIVE),
        (rule("R-0003", "LONG", {"type": "penalty", "points": 5}, near_miss), RuleStatus.ACTIVE),
    )
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)
    report = await replay(market, factory, strategy=StrategyConfig(analyst_orders=True), llm=llm, days=1)
    assert report.violations == []
    analysed = [d for d in decisions(factory) if d.model == "fake-llm"]
    assert analysed
    for d in analysed:
        if direction(d) == "LONG":
            assert (d.outcome, d.lessons_shown) == (DecisionOutcome.RULE_BLOCKED, ["R-0001"]), d.id
        else:
            assert d.risk_factor is not None and float(d.risk_factor) == 0.5  # noqa: PT018
            assert d.lessons_shown == ["R-0002"], d.id
    long_prompt = next(r.messages[0].content for r in llm.requests if candidate_direction(r) == "LONG")
    block = long_prompt[long_prompt.index("LESSONS FROM OUR OWN TRADE HISTORY") :].split("\n\n")[0]
    assert block + "\n" == GOLDEN.read_text(encoding="utf-8")


def candidate_direction(request: LLMRequest) -> str:
    return str(echo_candidate(request)["direction"])
