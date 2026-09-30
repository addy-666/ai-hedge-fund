"""The committee (roadmap 8.4): who is asked, fail-closed holds, the ballot, the critic, rules, the record."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal as D
from typing import Any

from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.committee import Committee
from aifund.agents.critic import Critic
from aifund.agents.portfolio_manager import PortfolioManager
from aifund.agents.specialists import Specialist
from aifund.config.trading_config import CommitteeConfig, LLMConfig
from aifund.domain.decision import SetupCandidate
from aifund.domain.enums import Direction, ReasonCode
from aifund.ports.llm import LLMError, LLMRequest
from aifund.rules.engine import NO_RULES, RuleVerdict
from tests.unit.agents.inputs import CANDIDATE, PLAYBOOKS, analyst_input
from tests.unit.agents.test_analyst import proposal

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-reasoner")
NR7 = SetupCandidate(
    setup_tag="nr7_breakout", playbook_id="nr7_breakout", direction_hint=Direction.LONG,
    key_levels={"invalidation": D("4140")}, strength=0.75,
)  # fmt: skip
NO_OBJECTIONS: dict[str, Any] = {"objections": [], "summary": "Sound."}
HIGH: dict[str, Any] = {"objections": [{"severity": "HIGH", "point": "Into resistance."}], "summary": "Late."}


def responder(answers: dict[str, Any]) -> Callable[[LLMRequest], Any]:
    def answer(request: LLMRequest) -> Any:
        return answers[request.agent]

    return answer


class Priced(FakeLLM):
    """Every call costs $0.001 and gets an llm_calls id, as with a real provider and a database."""

    async def complete(self, request: LLMRequest) -> Any:
        response = await super().complete(request)
        return response.model_copy(update={"cost_usd": D("0.001"), "call_id": f"c{len(self.requests)}"})


def committee(answers: dict[str, Any], cfg: CommitteeConfig | None = None) -> tuple[Committee, FakeLLM]:
    cfg = cfg or CommitteeConfig()
    llm = Priced([responder(answers)])
    specialists = [Specialist(f, tags, llm, CFG, playbooks=PLAYBOOKS) for f, tags in cfg.families.items()]
    return Committee(specialists, Critic(llm, CFG, playbooks=PLAYBOOKS), cfg), llm


def no_rules(direction: Direction, setup_tag: str, confidence: int) -> RuleVerdict:
    return NO_RULES


async def test_two_specialists_agree_and_the_critic_objects() -> None:
    answers = {
        "specialist_trend": proposal(confidence=70),
        "specialist_breakout": proposal(confidence=80, setup_tag="nr7_breakout", invalidation_price=4140.0),
        "critic": HIGH,
    }
    c, llm = committee(answers)
    seen: list[tuple[Direction, str, int]] = []

    def rules(direction: Direction, setup_tag: str, confidence: int) -> RuleVerdict:
        seen.append((direction, setup_tag, confidence))
        return RuleVerdict(rulebook_version=1, penalty_points=5, active=("R-0001v1",))

    out = await c.deliberate(analyst_input([CANDIDATE, NR7]), rules)
    assert sorted(r.agent for r in llm.requests) == ["critic", "specialist_breakout", "specialist_trend"]
    assert "reversal" not in out.specialists  # no reversal candidate: not asked
    # (70 + 80) / 2 = 75 ; HIGH objection -15 -> 60 ; rule penalty 5 -> 55
    assert seen == [(Direction.LONG, "nr7_breakout", 60)]
    d = out.decision.decision
    assert (d.llm_confidence, d.final_confidence, d.setup_tag) == (60, 55, "nr7_breakout")
    rec = out.record()
    assert (rec["combined"], rec["proposer"], rec["critic_penalty"], rec["confidence"]) == (
        75,
        "breakout",
        15,
        60,
    )
    assert rec["critique"]["objections"][0]["severity"] == "HIGH"
    assert rec["rules_matched"] == ["R-0001v1"]
    assert rec["specialists"]["trend"]["verdict"] == "PROPOSAL"
    assert (rec["calls"], rec["cost_usd"], rec["reason"]) == (3, "0.003", None)


async def test_opposite_directions_split_the_committee() -> None:
    nr7_short = NR7.model_copy(update={"direction_hint": Direction.SHORT})
    answers = {
        "specialist_trend": proposal(),
        "specialist_breakout": proposal(
            direction="SHORT", setup_tag="nr7_breakout", invalidation_price=4160.0
        ),
        "critic": NO_OBJECTIONS,
    }
    c, llm = committee(answers)
    out = await c.deliberate(analyst_input([CANDIDATE, nr7_short]), no_rules)
    assert (out.decision, out.reason) == (None, ReasonCode.COMMITTEE_SPLIT)
    assert out.detail == "trend LONG, breakout SHORT"
    assert "critic" not in [r.agent for r in llm.requests]


async def test_nobody_proposes_or_nobody_is_asked() -> None:
    c, _ = committee(
        {"specialist_trend": proposal(direction="NONE", setup_tag="none"), "critic": NO_OBJECTIONS}
    )
    out = await c.deliberate(analyst_input(), no_rules)
    assert (out.reason, out.detail) == (ReasonCode.COMMITTEE_HOLD, "trend NONE")
    stray = CANDIDATE.model_copy(update={"setup_tag": "unmapped_setup"})
    out = await c.deliberate(analyst_input([stray]), no_rules)
    assert (out.reason, out.detail) == (
        ReasonCode.COMMITTEE_HOLD,
        "no specialist has a candidate of its family",
    )
    assert out.record()["calls"] == 0


async def test_an_unreadable_specialist_holds_the_committee() -> None:
    c, _ = committee({"specialist_trend": LLMError("503"), "specialist_breakout": proposal(), "critic": HIGH})
    out = await c.deliberate(analyst_input([CANDIDATE, NR7]), no_rules)
    assert (out.decision, out.reason) == (None, ReasonCode.LLM_ERROR)
    assert out.detail == "trend specialist: 503"


async def test_no_critique_holds_the_committee() -> None:
    c, _ = committee({"specialist_trend": proposal(), "critic": LLMError("timeout")})
    out = await c.deliberate(analyst_input(), no_rules)
    assert (out.decision, out.reason) == (None, ReasonCode.CRITIC_UNAVAILABLE)
    assert out.detail == "LLM_ERROR: timeout"
    assert out.record()["direction"] is None


async def test_weights_and_its_own_calibrator() -> None:
    cfg = CommitteeConfig(weights={"trend": D(3)})
    answers = {
        "specialist_trend": proposal(confidence=80),
        "specialist_breakout": proposal(confidence=60, setup_tag="nr7_breakout", invalidation_price=4140.0),
        "critic": NO_OBJECTIONS,
    }
    c, _ = committee(answers, cfg)
    c = Committee(c._specialists, c._critic, cfg, portfolio=PortfolioManager(calibrator=lambda p: p - 20))
    out = await c.deliberate(analyst_input([CANDIDATE, NR7]), no_rules)
    d = out.decision.decision
    assert (d.llm_confidence, d.calibrated_confidence, d.setup_tag) == (75, 55, "mtf_trend_pullback")
