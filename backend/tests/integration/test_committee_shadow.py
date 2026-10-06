"""The committee in shadow (roadmap 8.4): it deliberates beside the analyst on every candidate bar, records
its SHADOW_COMMITTEE virtual trade and its deliberation, never reaches the Risk Manager, and a failure inside
it never touches the real decision. The comparison report reads what it recorded."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.committee import Committee
from aifund.agents.critic import Critic
from aifund.agents.specialists import Specialist
from aifund.config.trading_config import CommitteeConfig, LLMConfig, StrategyConfig
from aifund.domain.enums import DecisionOutcome, VirtualArm
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.persistence.tables import DecisionRow, OrderIntentRow
from aifund.ports.llm import LLMRequest
from aifund.research.committee import compare
from tests.integration.test_baseline_replay import (  # noqa: F401
    PLAYBOOKS,
    echo_candidate,
    market,
    replay,
    shadow_rows,
)

pytestmark = pytest.mark.scenario
CFG = LLMConfig(analyst_model="m", auditor_model="m")
FAMILIES = CommitteeConfig(mode="shadow", families={"trend": ["stub_hourly"]})


def by_agent(critic: Any) -> Any:
    def answer(request: LLMRequest) -> Any:
        if request.agent == "critic":
            return critic
        return echo_candidate(request)  # the analyst and the trend specialist take the detected setup at 75

    return answer


def committee(llm: FakeLLM) -> Committee:
    specialist = Specialist("trend", ["stub_hourly"], llm, CFG, playbooks=PLAYBOOKS)
    return Committee([specialist], Critic(llm, CFG, playbooks=PLAYBOOKS), FAMILIES)


async def test_the_committee_shadows_the_analyst_and_sends_nothing(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([by_agent({"objections": [], "summary": "fine"})], factory=factory, clock=clock)
    report = await replay(
        market, factory, strategy=StrategyConfig(), llm=llm, days=1, committee=committee(llm)
    )
    assert report.violations == []
    assert DecisionOutcome.ORDERED.value not in report.outcomes
    with factory() as s:
        assert s.scalars(select(OrderIntentRow)).all() == []
    analysed, arms = shadow_rows(factory)
    assert len(analysed) >= 12
    for d in analysed:
        assert set(arms[d.id]) == {"SHADOW_BASELINE", "SHADOW_ANALYST", "SHADOW_COMMITTEE"}, d.id
        c = (d.proposal or {})["committee"]
        assert (c["combined"], c["confidence"], c["tradable"], c["proposer"]) == (75, 75, True, "trend")
        assert c["specialists"]["trend"]["verdict"] == "PROPOSAL"
    agents = {r.agent for r in llm.requests}
    assert agents == {"analyst", "specialist_trend", "critic"}
    with factory() as s:
        bars = VirtualTradeRepository(s, clock).contender_bars(
            "acc", analysed[0].bar_time, analysed[-1].bar_time + timedelta(days=1)
        )
    assert bars
    assert all(b.contender_r == b.analyst_r and b.contender_traded for b in bars)
    out = compare(bars, risk_usd=D(50))
    assert out.agreement == 1.0
    assert out.vs_analyst is not None
    assert out.vs_analyst.mean == 0  # same trades, FakeLLM calls are free


async def test_a_critic_that_objects_keeps_the_committee_out(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    high = {"objections": [{"severity": "HIGH", "point": "no"}], "summary": "no"}  # 75 - 15 = 60 < 65
    llm = FakeLLM([by_agent(high)], factory=factory, clock=clock)
    report = await replay(
        market, factory, strategy=StrategyConfig(), llm=llm, days=1, committee=committee(llm)
    )
    assert report.violations == []
    analysed, arms = shadow_rows(factory)
    assert analysed
    for d in analysed:
        c = (d.proposal or {})["committee"]
        assert (c["final_confidence"], c["critic_penalty"], c["tradable"]) == (60, 15, False)
        assert "SHADOW_COMMITTEE" not in arms[d.id]
        assert "SHADOW_ANALYST" in arms[d.id]  # the analyst is untouched


async def test_a_broken_committee_never_touches_the_real_decision(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)

    class Broken(Committee):
        async def deliberate(self, inp: Any, rules: Any) -> Any:
            raise RuntimeError("bug in shadow code")

    broken = Broken([], Critic(llm, CFG, playbooks=PLAYBOOKS), FAMILIES)
    strategy = StrategyConfig(analyst_orders=True)
    report = await replay(market, factory, strategy=strategy, llm=llm, days=1, committee=broken)
    assert report.violations == []
    assert report.outcomes[DecisionOutcome.ORDERED.value] >= 1  # the analyst still trades
    with factory() as s:
        decided = s.scalars(select(DecisionRow).where(DecisionRow.setups.is_not(None))).all()
    assert all("committee" not in (d.proposal or {}) for d in decided)
    assert VirtualArm.SHADOW_COMMITTEE.value not in (report.virtual_arms or {})


async def test_shadow_mode_needs_the_committee(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)
    with pytest.raises(ValueError, match="needs the committee"):
        await replay(market, factory, strategy=StrategyConfig(), llm=llm, days=1, committee_mode="shadow")
