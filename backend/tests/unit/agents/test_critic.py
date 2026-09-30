"""Risk critic (roadmap 8.3): a strict Critique, one repair, fail closed on provider trouble, accounting."""

from __future__ import annotations

import json
import os
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from aifund.adapters.llm.fake_llm import FakeLLM, Scripted
from aifund.agents.critic import Critic, CriticInput
from aifund.agents.prompting import PromptLibrary
from aifund.config.trading_config import LLMConfig
from aifund.domain.decision import TradeProposal
from aifund.domain.enums import ObjectionSeverity, ReasonCode
from aifund.ports.llm import LLMBudgetExhausted, LLMCircuitOpen, LLMError
from tests.unit.agents.inputs import PLAYBOOKS, analyst_input
from tests.unit.agents.test_analyst import proposal

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-reasoner")
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "critic_v1.golden.txt"


def critic_input() -> CriticInput:
    return CriticInput(analyst_input(), TradeProposal.model_validate_json(json.dumps(proposal())), "trend")


def critique(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "objections": [
            {"severity": "HIGH", "point": "The H1 close is already stretched 2 ATR above EMA50."},
            {"severity": "LOW", "point": "Spread is 3% of ATR."},
        ],
        "summary": "Late entry into an extended move.",
    }
    return {**base, **over}


async def run(*script: Scripted) -> tuple[Any, FakeLLM]:
    llm = FakeLLM(list(script))
    return await Critic(llm, CFG, playbooks=PLAYBOOKS).critique(critic_input()), llm


async def test_a_valid_critique() -> None:
    result, llm = await run(critique())
    assert [o.severity for o in result.critique.objections] == [ObjectionSeverity.HIGH, ObjectionSeverity.LOW]
    assert (result.reason, result.prompt_version, result.model) == (None, "critic_v1", "fake-llm")
    (req,) = llm.requests
    assert (req.agent, req.decision_id, req.model) == (
        "critic",
        "01JAXDECISION000000000000A",
        "deepseek-chat",
    )
    assert "THE PROPOSAL (by the trend specialist)" in req.messages[1].content
    assert "direction LONG | confidence 74 | setup mtf_trend_pullback" in req.messages[1].content


async def test_no_objections_is_a_valid_answer() -> None:
    result, _ = await run(critique(objections=[]))
    assert result.critique.objections == []


async def test_schema_failure_is_repaired_once_then_none() -> None:
    result, llm = await run(critique(objections=[{"severity": "FATAL", "point": "x"}]), critique())
    assert result.critique is not None
    assert len(llm.requests) == 2
    assert "objections" in llm.requests[1].messages[-1].content
    result, llm = await run("not json", "still not json")
    assert (result.critique, result.reason) == (None, ReasonCode.LLM_INVALID_OUTPUT)
    result, _ = await run(critique(objections=[{"severity": "HIGH", "point": "x"}] * 6))
    assert (result.critique, result.reason) == (None, ReasonCode.LLM_INVALID_OUTPUT)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LLMBudgetExhausted("spent"), ReasonCode.LLM_BUDGET_EXHAUSTED),
        (LLMCircuitOpen("open"), ReasonCode.LLM_UNAVAILABLE),
        (LLMError("503"), ReasonCode.LLM_ERROR),
    ],
)
async def test_provider_failures_leave_no_critique(error: LLMError, reason: ReasonCode) -> None:
    result, _ = await run(error)
    assert (result.critique, result.reason) == (None, reason)


async def test_calls_are_recorded_with_their_cost() -> None:
    recorded: list[tuple[str, bool]] = []

    async def record(call_id: str, parsed: dict[str, Any] | None, valid: bool, error: str | None) -> None:
        recorded.append((call_id, valid))

    class Priced(FakeLLM):
        async def complete(self, request: Any) -> Any:
            response = await super().complete(request)
            return response.model_copy(update={"cost_usd": D("0.0004"), "call_id": f"c{len(self.requests)}"})

    llm = Priced([critique(summary=""), critique()])
    result = await Critic(llm, CFG, playbooks=PLAYBOOKS, record_parse=record).critique(critic_input())
    assert result.cost_usd == D("0.0008")
    assert result.call_ids == ["c1", "c2"]
    assert recorded == [("c1", False), ("c2", True)]


def test_rendered_prompt_matches_the_golden_file() -> None:
    critic = Critic(FakeLLM(["{}"]), CFG, playbooks=PLAYBOOKS)
    rendered = PromptLibrary().render("critic", 1, critic.context(critic_input()))
    text = "\n".join(f"=== {m.role} ===\n{m.content}" for m in rendered.messages) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text(), "prompt rendering changed: review the diff, then UPDATE_GOLDEN=1"
    assert "JSON" in rendered.messages[0].content
    assert "4150" not in rendered.messages[0].content
