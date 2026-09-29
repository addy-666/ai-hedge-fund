"""LLM researcher (roadmap R.6, docs/09 §6): validation per hypothesis, one repair, holdouts refused."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aifund.adapters.llm.fake_llm import FakeLLM, Scripted
from aifund.agents.researcher import LedgerLine, ResearchBrief, Researcher, feature_lines
from aifund.config.trading_config import LLMConfig
from aifund.domain.enums import Timeframe
from aifund.ports.llm import LLMError
from aifund.strategies.base import TfRoles

CFG = LLMConfig(analyst_model="chat", auditor_model="reasoner")
ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))
LINE = LedgerLine("mtf_trend_pullback grid", "walk_forward", 221, 0.03, -0.111, 0.18, 0.37)


def hyp(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "setup_tag": "rsi_reclaim_in_trend",
        "long": {"all": [{"feature": "h4.ema50_above_ema200", "op": "==", "value": True},
                         {"feature": "m15.rsi14", "op": "between", "value": [35, 50]}]},
        "short": "mirror",
        "invalidation": {"type": "atr", "tf": "trigger", "k": 1.5},
        "target": {"type": "rr", "rr": 2.0},
        "mechanism": "Pullbacks in an H4 uptrend that reclaim RSI mid-range resume the trend.",
    }  # fmt: skip
    return {**base, **over}


BAD_FEATURE = hyp(setup_tag="bad_one", long={"all": [{"feature": "m15.rsi_14", "op": "<", "value": 30}]})


def brief(ledger: list[LedgerLine] | None = None) -> ResearchBrief:
    return ResearchBrief(
        run_id="R-1",
        roles=ROLES,
        symbols=["XAUUSD"],
        playbooks=[],
        ledger=[LINE] if ledger is None else ledger,
    )


async def propose(*script: Scripted, limit: int = 5, ledger: list[LedgerLine] | None = None):  # type: ignore[no-untyped-def]
    llm = FakeLLM(list(script))
    result = await Researcher(llm, CFG, max_hypotheses=limit).propose(brief(ledger))
    return result, llm


async def test_valid_hypotheses_get_engine_ids() -> None:
    result, llm = await propose({"hypotheses": [hyp(), hyp(setup_tag="other_tag", target=None, id="X-1")]})
    assert [h.setup_tag for h in result.hypotheses] == ["rsi_reclaim_in_trend", "other_tag"]
    first = result.hypotheses[0]
    assert first.id == f"H-{first.params_sha256()[:10]}" and first.version == 1  # noqa: PT018
    assert result.hypotheses[1].id.startswith("H-")  # the model's own id is replaced
    assert result.rejected == [] and result.prompt_version == "researcher_v1"  # noqa: PT018
    (req,) = llm.requests
    assert (req.agent, req.model, req.audit_run_id, req.timeout_s) == ("researcher", "reasoner", "R-1", 180)


async def test_the_same_idea_twice_is_one_hypothesis() -> None:
    result, _ = await propose({"hypotheses": [hyp(), hyp(mechanism="The very same rule, other words.")]})
    assert len(result.hypotheses) == 1


async def test_an_invalid_hypothesis_is_repaired_once_then_dropped_with_its_reason() -> None:
    result, llm = await propose({"hypotheses": [hyp(), BAD_FEATURE]})  # the repair gets the same reply
    assert [h.setup_tag for h in result.hypotheses] == ["rsi_reclaim_in_trend"]
    assert len(llm.requests) == 2
    repair = llm.requests[1].messages[-1].content
    assert "hypothesis 2" in repair and "unknown feature 'm15.rsi_14'" in repair  # noqa: PT018
    assert [(r.round, "unknown feature" in r.reason) for r in result.rejected] == [(1, True), (2, True)]


async def test_the_repair_can_fix_a_rejected_hypothesis() -> None:
    fixed = hyp(setup_tag="bad_one", long={"all": [{"feature": "m15.rsi14", "op": "<", "value": 30}]})
    result, _ = await propose({"hypotheses": [hyp(), BAD_FEATURE]}, {"hypotheses": [fixed]})
    assert [h.setup_tag for h in result.hypotheses] == ["rsi_reclaim_in_trend", "bad_one"]


@pytest.mark.parametrize(
    "reply", ["Here are my ideas: ...", {"ideas": []}, {"hypotheses": {}}, {"hypotheses": [], "notes": "x"}]
)
async def test_a_malformed_reply_is_repaired_once(reply: Any) -> None:
    result, llm = await propose(reply, {"hypotheses": [hyp()]})
    assert len(result.hypotheses) == 1 and len(llm.requests) == 2  # noqa: PT018


async def test_two_malformed_replies_propose_nothing() -> None:
    result, llm = await propose({"ideas": []})
    assert result.hypotheses == [] and len(llm.requests) == 2  # noqa: PT018
    assert all("hypotheses" in r.reason for r in result.rejected)


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ("an idea", "must be a JSON object"),
        (hyp(long={"all": [{"feature": "d1.rsi14", "op": "<", "value": 30}]}), "uses D1"),
        (hyp(long={"all": [{"feature": "ctx.dist_pdh_atr", "op": ">", "value": 1}]}, short=None), "uses D1"),
        (hyp(volume=1), "volume"),
        (hyp(long={"all": [{"feature": "ctx.minutes_to_next_high_impact_news", "op": ">", "value": 30}]}),
         "not computed yet"),
    ],
)  # fmt: skip
async def test_items_are_validated_one_by_one(item: Any, reason: str) -> None:
    result, _ = await propose({"hypotheses": [hyp(), item]})
    assert len(result.hypotheses) == 1
    assert reason in result.rejected[0].reason


async def test_at_most_the_configured_number() -> None:
    items = [hyp(setup_tag=f"tag_{i}", target={"type": "rr", "rr": 1.0 + i / 10}) for i in range(4)]
    result, _ = await propose({"hypotheses": items}, limit=2)
    assert len(result.hypotheses) == 2
    assert [r.reason for r in result.rejected] == ["over the limit of 2 hypotheses"] * 2


async def test_a_provider_failure_proposes_nothing_without_a_repair() -> None:
    result, llm = await propose(LLMError("HTTP 503"))
    assert (result.hypotheses, result.error, len(llm.requests)) == ([], "HTTP 503", 1)


async def test_a_holdout_line_in_the_brief_is_refused() -> None:
    leak = LedgerLine("something", "holdout", 80, 0.4, 0.1, 0.7, 0.01)
    with pytest.raises(ValueError, match="holdout"):
        await propose({"hypotheses": []}, ledger=[LINE, leak])


async def test_the_prompt_lists_usable_features_only() -> None:
    _, llm = await propose({"hypotheses": [hyp()]})
    system, user = (m.content for m in llm.requests[0].messages)
    assert "JSON" in system and "at most 6 per side" in system  # noqa: PT018
    assert "prefix the name with m15., h1., h4." in user
    assert "\nrsi14 | float | 0-100 |" in user and "| 100 - value" in user  # noqa: PT018
    for absent in (
        "\nclose |",
        "\natr14 |",
        "ctx.drawdown_pct",
        "ctx.minutes_to_next",
        "ctx.dist_pdh_atr",
        "prop.",
    ):
        assert absent not in user, absent
    assert "mtf_trend_pullback grid | walk_forward | 221 | +0.030 | [-0.111, +0.180] | 0.370" in user
    assert "(empty: nothing tested yet)" not in user


def test_context_features_depend_on_the_profile() -> None:
    _, ctx = feature_lines(TfRoles(Timeframe.M15, Timeframe.H1, (Timeframe.H4, Timeframe.D1)))
    names = {row["name"]: row["mirror"] for row in ctx}
    assert names["ctx.dist_pdh_atr"] == "ctx.dist_pdl_atr (same)"
    assert names["ctx.regime"] == "TREND_DOWN<->TREND_UP"
    tf, _ = feature_lines(ROLES)
    assert {r["name"]: r["mirror"] for r in tf}["ema_stack"] == "BEAR<->BULL"


def test_a_feature_without_a_mirror_says_so() -> None:
    from aifund.agents.researcher import _mirror_text
    from aifund.market import feature_registry as reg

    assert _mirror_text(reg.get("ctx.drawdown_pct")) == "none"


class Billed:
    """An LLM that returns call ids and costs, like the recorded DeepSeek client."""

    def __init__(self, *replies: dict[str, Any] | str) -> None:
        self.replies = list(replies)

    async def complete(self, request: Any) -> Any:
        from decimal import Decimal

        from aifund.ports.llm import LLMResponse

        reply = self.replies.pop(0)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return LLMResponse(text=text, model="reasoner", prompt_tokens=10, completion_tokens=5, latency_ms=1,
                           cost_usd=Decimal("0.01"), call_id=f"call-{len(self.replies)}")  # fmt: skip


async def test_calls_are_accounted_and_their_verdicts_recorded() -> None:
    parses: list[tuple[str, bool, str | None]] = []

    async def record(call_id: str, parsed: Any, valid: bool, error: str | None) -> None:
        parses.append((call_id, valid, error))

    llm = Billed({"hypotheses": [hyp(), BAD_FEATURE]}, {"hypotheses": []})
    result = await Researcher(llm, CFG, max_hypotheses=5, record_parse=record).propose(brief())  # type: ignore[arg-type]
    assert (result.call_ids, str(result.cost_usd), result.model) == (["call-1", "call-0"], "0.02", "reasoner")
    assert [(c, v) for c, v, _ in parses] == [("call-1", False), ("call-0", True)]
    assert "unknown feature" in (parses[0][2] or "")


async def test_a_provider_that_does_not_enforce_json_is_repaired_too() -> None:
    llm = Billed("not json at all", {"hypotheses": [hyp()]})
    result = await Researcher(llm, CFG, max_hypotheses=5).propose(brief())  # type: ignore[arg-type]
    assert len(result.hypotheses) == 1
    assert result.rejected[0].item == "not json at all"
