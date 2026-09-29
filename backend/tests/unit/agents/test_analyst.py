"""Analyst agent (roadmap 4.4): every validation and fallback path of docs/03 §7.3, with FakeLLM.

The fixed input (tests/unit/agents/inputs.py): XAUUSD, one LONG mtf_trend_pullback candidate, bid 4150.00 /
ask 4150.28, so a LONG enters at 4150.28 and a SHORT at 4150.00.
"""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from aifund.adapters.llm.fake_llm import FakeLLM, Scripted
from aifund.agents.analyst import Analyst, Verdict
from aifund.config.trading_config import LLMConfig
from aifund.domain.enums import Direction, ReasonCode
from aifund.ports.llm import LLMBudgetExhausted, LLMCircuitOpen, LLMError
from tests.unit.agents.inputs import CANDIDATE, PLAYBOOKS, analyst_input

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-chat")


def proposal(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        direction="LONG", confidence=74, setup_tag="mtf_trend_pullback", invalidation_price=4138.2,
        target_price=4182.5, thesis="Trend up, pullback into the EMA50 zone, stochastic hook.",
        key_risks=["US data at 12:30"], lessons_considered=[], time_horizon_bars=12,
    )  # fmt: skip
    return {**base, **over}


async def run(*script: Scripted, candidates: list[Any] | None = None) -> tuple[Any, FakeLLM]:
    llm = FakeLLM(list(script))
    result = await Analyst(llm, CFG, playbooks=PLAYBOOKS).analyse(analyst_input(candidates))
    return result, llm


async def test_a_valid_proposal() -> None:
    result, llm = await run(proposal())
    assert result.verdict is Verdict.PROPOSAL
    assert result.proposal.direction is Direction.LONG
    assert (result.proposal.confidence, result.proposal.invalidation_price) == (74, D("4138.2"))
    assert (result.prompt_version, result.model, result.notes) == ("analyst_v1", "fake-llm", [])
    (req,) = llm.requests
    assert (req.agent, req.model, req.decision_id, req.prompt_version) == (
        "analyst", "deepseek-chat", "01JAXDECISION000000000000A", "1",
    )  # fmt: skip


async def test_schema_failure_is_repaired_once() -> None:
    result, llm = await run(proposal(confidence=74.5), proposal())
    assert result.verdict is Verdict.PROPOSAL
    assert len(llm.requests) == 2
    repair = llm.requests[1].messages
    assert repair[-2].role == "assistant"  # the rejected answer, then what was wrong with it
    assert "confidence" in repair[-1].content and "corrected JSON" in repair[-1].content  # noqa: PT018


async def test_two_schema_failures_are_invalid() -> None:
    result, llm = await run(proposal(direction="UP"))
    assert (result.verdict, result.reason) == (Verdict.INVALID, ReasonCode.LLM_INVALID_OUTPUT)
    assert "direction" in result.detail
    assert len(llm.requests) == 2


async def test_non_json_reply_is_repaired() -> None:
    result, _ = await run("Sure! I'd buy gold here.", proposal())
    assert result.verdict is Verdict.PROPOSAL


async def test_the_llm_never_supplies_volume() -> None:
    result, _ = await run(proposal(volume=1.5))  # extra keys are forbidden, twice -> INVALID
    assert (result.verdict, result.reason) == (Verdict.INVALID, ReasonCode.LLM_INVALID_OUTPUT)
    assert "volume" in result.detail


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (LLMBudgetExhausted("spent"), ReasonCode.LLM_BUDGET_EXHAUSTED),
        (LLMCircuitOpen("open"), ReasonCode.LLM_UNAVAILABLE),
        (LLMError("HTTP 500"), ReasonCode.LLM_ERROR),
    ],
)
async def test_provider_failures_are_invalid(error: LLMError, reason: ReasonCode) -> None:
    result, llm = await run(error)
    assert (result.verdict, result.reason) == (Verdict.INVALID, reason)
    assert len(llm.requests) == 1  # no repair for a provider failure


async def test_none_is_a_hold() -> None:
    result, _ = await run(
        proposal(direction="NONE", setup_tag="none", invalidation_price=None, target_price=None)
    )
    assert (result.verdict, result.reason) == (Verdict.HOLD, ReasonCode.ANALYST_HOLD)
    result, _ = await run(proposal(setup_tag="none"))  # a direction without a setup is still a hold
    assert result.verdict is Verdict.HOLD


async def test_setups_must_be_detected_ones() -> None:
    result, _ = await run(proposal(setup_tag="nr7_breakout"))  # not detected on this bar
    assert (result.verdict, result.reason) == (Verdict.HOLD, ReasonCode.SETUP_MISMATCH)
    reversed_, _ = await run(proposal(direction="SHORT", invalidation_price=4161.0, target_price=4120.0))
    assert (reversed_.verdict, reversed_.reason) == (Verdict.HOLD, ReasonCode.SETUP_MISMATCH)  # LONG detected
    none, _ = await run(proposal(), candidates=[])
    assert none.reason is ReasonCode.SETUP_MISMATCH


async def test_wrong_side_levels_are_dropped() -> None:
    result, _ = await run(proposal(invalidation_price=4155.0, target_price=4149.0))  # LONG enters at 4150.28
    assert result.verdict is Verdict.PROPOSAL
    assert (result.proposal.invalidation_price, result.proposal.target_price) == (None, None)
    assert [n.split(":")[0] for n in result.notes] == ["INVALIDATION_IGNORED", "TARGET_IGNORED"]
    assert D(result.raw["invalidation_price"]) == D("4155")  # what the model said is kept for the record


async def test_candidate_without_a_direction_hint_accepts_either_side() -> None:
    free = CANDIDATE.model_copy(update={"direction_hint": Direction.NONE})
    result, _ = await run(
        proposal(direction="SHORT", invalidation_price=4161.0, target_price=4120.0), candidates=[free]
    )
    assert result.verdict is Verdict.PROPOSAL
