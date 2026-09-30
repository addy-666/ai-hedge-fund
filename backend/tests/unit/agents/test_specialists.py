"""Specialist analysts (roadmap 8.3): family filtering, the analyst's validation reused, prompt, costs."""

from __future__ import annotations

import os
from decimal import Decimal as D
from pathlib import Path

from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.analyst import Verdict
from aifund.agents.prompting import PromptLibrary
from aifund.agents.specialists import BRIEFS, Specialist, brief
from aifund.config.trading_config import LLMConfig
from aifund.domain.decision import SetupCandidate
from aifund.domain.enums import Direction, ReasonCode
from tests.unit.agents.inputs import CANDIDATE, PLAYBOOKS, analyst_input
from tests.unit.agents.test_analyst import proposal

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-reasoner")
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "specialist_v1.golden.txt"
NR7 = SetupCandidate(
    setup_tag="nr7_breakout", playbook_id="nr7_breakout", direction_hint=Direction.LONG,
    key_levels={"invalidation": D("4140"), "target": D("4170")}, strength=0.75, notes="NR7 at value",
)  # fmt: skip


def trend(llm: FakeLLM) -> Specialist:
    return Specialist("trend", ["mtf_trend_pullback"], llm, CFG, playbooks=PLAYBOOKS)


async def test_a_specialist_sees_only_its_family() -> None:
    llm = FakeLLM([proposal()])
    result = await trend(llm).analyse(analyst_input([CANDIDATE, NR7]))
    assert result.verdict is Verdict.PROPOSAL
    assert result.prompt_version == "specialist_v1"
    (req,) = llm.requests
    assert (req.agent, req.prompt_template, req.model) == ("specialist_trend", "specialist", "deepseek-chat")
    system, user = (m.content for m in req.messages)
    assert system.startswith("You are the trend specialist")
    assert "nr7_breakout" not in system + user  # the other family's setup is neither described nor offered


async def test_it_cannot_take_another_familys_setup() -> None:
    llm = FakeLLM([proposal(setup_tag="nr7_breakout")])
    result = await trend(llm).analyse(analyst_input([CANDIDATE, NR7]))
    assert (result.verdict, result.reason) == (Verdict.HOLD, ReasonCode.SETUP_MISMATCH)


async def test_no_candidate_of_its_family_means_no_call() -> None:
    llm = FakeLLM([proposal()])
    result = await trend(llm).analyse(analyst_input([NR7]))
    assert (result.verdict, result.reason, result.detail) == (
        Verdict.HOLD,
        ReasonCode.ANALYST_HOLD,
        "no trend candidate",
    )
    assert llm.requests == []


def test_briefs() -> None:
    assert set(BRIEFS) == {"trend", "breakout", "reversal"}
    assert brief("carry") == "the carry setups listed below."


def test_rendered_prompt_matches_the_golden_file() -> None:
    specialist = trend(FakeLLM(["{}"]))
    rendered = PromptLibrary().render("specialist", 1, specialist.context(analyst_input()))
    text = "\n".join(f"=== {m.role} ===\n{m.content}" for m in rendered.messages) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text(), "prompt rendering changed: review the diff, then UPDATE_GOLDEN=1"
    assert "4150" not in rendered.messages[0].content  # nothing per-bar in the cached prefix
