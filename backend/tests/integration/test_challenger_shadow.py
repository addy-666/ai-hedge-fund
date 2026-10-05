"""The challenger analyst prompt in shadow (roadmap 10.4, the prompt A/B): analyst_v2 decides beside the
analyst on every candidate bar, records a SHADOW_CHALLENGER virtual trade and its decision, never reaches the
Risk Manager, and a failure inside it never touches the real decision. v1 never sees the cross-asset block."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.analyst import Analyst
from aifund.config.trading_config import LLMConfig, StrategyConfig
from aifund.domain.enums import DecisionOutcome, VirtualArm
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.persistence.tables import DecisionRow, LLMCallRow, OrderIntentRow
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
WITH_CHALLENGER = StrategyConfig(challenger_prompt_version=2)


def answers(challenger_confidence: int) -> Any:
    def answer(request: LLMRequest) -> Any:
        out = echo_candidate(request)  # the detected setup at 75
        if request.agent == "challenger":
            out["confidence"] = challenger_confidence
        return out

    return answer


def challenger(llm: FakeLLM) -> Analyst:
    return Analyst(llm, CFG, playbooks=PLAYBOOKS, version=2, agent="challenger")


async def test_the_challenger_shadows_the_analyst_and_sends_nothing(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([answers(80)], factory=factory, clock=clock)
    report = await replay(
        market, factory, strategy=WITH_CHALLENGER, llm=llm, days=1, challenger=challenger(llm)
    )
    assert report.violations == []
    assert DecisionOutcome.ORDERED.value not in report.outcomes
    with factory() as s:
        assert s.scalars(select(OrderIntentRow)).all() == []
        agents = {row.agent for row in s.scalars(select(LLMCallRow)).all()}
    assert agents == {"analyst", "challenger"}  # its spend is booked apart
    analysed, arms = shadow_rows(factory)
    assert len(analysed) >= 12
    for d in analysed:
        assert set(arms[d.id]) == {"SHADOW_BASELINE", "SHADOW_ANALYST", "SHADOW_CHALLENGER"}, d.id
        c = (d.proposal or {})["challenger"]
        assert (c["prompt_version"], c["verdict"], c["tradable"]) == ("analyst_v2", "PROPOSAL", True)
        assert c["final_confidence"] == 80  # its own confidence: no calibration fitted on another prompt
    v1 = next(r for r in llm.requests if r.agent == "analyst").messages[1].content
    v2 = next(r for r in llm.requests if r.agent == "challenger").messages[1].content
    assert "INTERMARKET" not in v1
    assert "INTERMARKET" in v2
    assert "EURUSD: closed (no fresh bars)" in v2  # the example config's reference has no data here
    with factory() as s:
        bars = VirtualTradeRepository(s, clock).contender_bars(
            "acc", analysed[0].bar_time, analysed[-1].bar_time + timedelta(days=1), "challenger"
        )
    assert bars
    assert all(b.contender_traded for b in bars)
    out = compare(bars, risk_usd=D(50), contender="challenger")
    assert [a.name for a in out.arms] == ["baseline", "analyst", "challenger"]
    assert out.agreement == 1.0  # same setups, same direction
    with factory() as s:  # the committee's comparison does not count the challenger's bars
        repo = VirtualTradeRepository(s, clock)
        assert repo.contender_bars("acc", bars[0].bar_time, bars[-1].bar_time) == []


async def test_a_challenger_below_the_threshold_stays_out(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([answers(50)], factory=factory, clock=clock)
    await replay(market, factory, strategy=WITH_CHALLENGER, llm=llm, days=1, challenger=challenger(llm))
    analysed, arms = shadow_rows(factory)
    assert analysed
    for d in analysed:
        c = (d.proposal or {})["challenger"]
        assert (c["final_confidence"], c["tradable"]) == (50, False)
        assert "SHADOW_CHALLENGER" not in arms[d.id]
        assert "SHADOW_ANALYST" in arms[d.id]


async def test_a_broken_challenger_never_touches_the_real_decision(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)

    class Broken(Analyst):
        async def analyse(self, inp: Any) -> Any:
            raise RuntimeError("bug in shadow code")

    strategy = StrategyConfig(analyst_orders=True, challenger_prompt_version=2)
    broken = Broken(llm, CFG, playbooks=PLAYBOOKS, version=2, agent="challenger")
    report = await replay(market, factory, strategy=strategy, llm=llm, days=1, challenger=broken)
    assert report.violations == []
    assert report.outcomes[DecisionOutcome.ORDERED.value] >= 1  # the analyst still trades
    with factory() as s:
        decided = s.scalars(select(DecisionRow).where(DecisionRow.setups.is_not(None))).all()
    assert all("challenger" not in (d.proposal or {}) for d in decided)
    assert VirtualArm.SHADOW_CHALLENGER.value not in (report.virtual_arms or {})


async def test_a_configured_challenger_must_be_supplied(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)
    with pytest.raises(ValueError, match="needs the challenger"):
        await replay(market, factory, strategy=WITH_CHALLENGER, llm=llm, days=1)
