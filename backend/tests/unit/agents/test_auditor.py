"""Auditor agent (roadmap 7.5, docs/04 §4): candidates must cite surviving clusters and pass the DSL checks;
one repair call; retire suggestions, lessons and findings are passed through."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

from aifund.adapters.llm.fake_llm import FakeLLM, Scripted
from aifund.agents.auditor import MAX_CANDIDATES, AuditBrief, Auditor, FeatureLine, RuleLine
from aifund.agents.prompting import PromptLibrary
from aifund.config.trading_config import LLMConfig
from aifund.ports.llm import LLMError
from aifund.rules import miner
from tests.unit.rules.synthetic import dataset

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-reasoner")
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "auditor_v1.golden.txt"


@cache
def brief() -> AuditBrief:
    samples = dataset(600, seed=1)
    result = miner.mine(samples, miner.MinerConfig(resamples=200), label="C-2026-09-30")
    return AuditBrief(
        run_id="01JAXAUDIT0000000000000000",
        miner=result,
        features=[
            FeatureLine("m15.rsi14", 5.0, 95.0), FeatureLine("ctx.session", None, None),
            FeatureLine("m15.nr7", 0.0, 1.0), FeatureLine("h1.adx14", 10.0, 50.0),
        ],
        symbols=["XAUUSD"],
        setup_tags=["mtf_trend_pullback"],
        window="2026-01-05 -> 2026-06-03",
        n_virtual=0,
        narratives=["XAUUSD LONG mtf_trend_pullback 2026-02-03 -1.00R SL: NY session, rsi 62"],
        rules=[RuleLine("R-0001", "SHADOW", "LONG on XAUUSD when m15.nr7 == true", "n=24 mean -0.4R")],
    )  # fmt: skip


def candidate(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scope": {"directions": ["LONG"], "symbols": ["XAUUSD"]},
        "conditions": {"all": [{"feature": "ctx.session", "op": "==", "value": "NY"},
                               {"feature": "m15.rsi14", "op": ">", "value": 40}]},
        "action": {"type": "penalty", "points": 15},
        "hypothesis": "NY-session longs above RSI 40 buy into the US open's liquidity sweep and get stopped.",
        "cited_clusters": [brief().miner.clusters[0].id],
    }  # fmt: skip
    return {**base, **over}


def reply(*candidates: dict[str, Any], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "candidate_rules": list(candidates),
        "retire_suggestions": [],
        "lessons_markdown": "NY longs lose.",
        "strategy_level_findings": [],
    }
    return {**base, **over}


async def run(*script: Scripted) -> tuple[Any, FakeLLM]:
    llm = FakeLLM(list(script))
    return await Auditor(llm, CFG).audit(brief()), llm


def test_the_rendered_prompt_matches_the_golden_file() -> None:
    rendered = PromptLibrary().render("auditor", 1, Auditor(FakeLLM([{}]), CFG).context(brief()))
    text = "\n=====\n".join(m.content for m in rendered.messages) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8"), (
        "prompt rendering changed: review, then UPDATE_GOLDEN=1"
    )


async def test_a_valid_candidate_with_its_citation() -> None:
    suggestion = {"rule_id": "R-0001", "reasoning": "its matches now win"}
    result, llm = await run(
        reply(candidate(), retire_suggestions=[suggestion], strategy_level_findings=["x"])
    )
    (c,) = result.candidates
    assert (c.rule.rule_id, c.cited, c.rule.action.points) == ("R-0000", (brief().miner.clusters[0].id,), 15)
    assert result.lessons_markdown == "NY longs lose." and result.findings == ["x"]  # noqa: PT018
    assert [s.rule_id for s in result.retire_suggestions] == ["R-0001"]
    (req,) = llm.requests
    assert (req.agent, req.model, req.audit_run_id) == ("auditor", "deepseek-reasoner", brief().run_id)
    assert result.prompt_version == "auditor_v1"


async def test_uncited_weak_or_invalid_rules_are_rejected_then_repaired_once() -> None:
    weak = brief().miner.weak[0].id
    bad = [
        candidate(cited_clusters=[]),  # uncited
        candidate(cited_clusters=[weak]),  # weak is not evidence
        candidate(cited_clusters=["C-made-up"]),
        candidate(conditions={"all": [{"feature": "m15.rsi14", "op": ">", "value": 99}]}),  # outside the data
        candidate(conditions={"all": [{"feature": "prop.rr_target", "op": "<", "value": 2}]}),  # after rules
        candidate(scope={"symbols": ["EURUSD"]}),
        candidate(action={"type": "boost", "points": 5}),
        "not an object",
    ]
    result, llm = await run(reply(*bad), reply(candidate()))
    assert len(result.candidates) == 1 and len(llm.requests) == 2  # noqa: PT018
    reasons = [r.reason for r in result.rejected]
    assert len(reasons) == len(bad)
    assert "cited_clusters" in reasons[0] and "not surviving" in reasons[1] and "not surviving" in reasons[2]  # noqa: PT018
    assert "observed range" in reasons[3] and "after the rules run" in reasons[4]  # noqa: PT018
    assert "EURUSD" in reasons[5] and "action" in reasons[6] and "JSON object" in reasons[7]  # noqa: PT018
    repair = llm.requests[1].messages[-1].content
    assert repair.startswith("Some of your reply was rejected") and "candidate 1:" in repair  # noqa: PT018


async def test_duplicates_and_the_candidate_limit() -> None:
    many = [candidate(action={"type": "penalty", "points": p}) for p in (5, 10, 15, 20, 25, 30)]
    result, _ = await run(reply(candidate(), candidate(), *many))
    assert len(result.candidates) == MAX_CANDIDATES
    assert any("over the limit" in r.reason for r in result.rejected)


async def test_bad_shapes_and_provider_errors() -> None:
    result, llm = await run({"candidates": []}, "not json at all", reply())
    assert result.candidates == [] and len(llm.requests) == 2  # noqa: PT018
    assert len(result.rejected) == 1  # the shape; "not json" is an LLMInvalidOutput, not an item
    result, _ = await run(LLMError("HTTP 500"))
    assert result.error == "HTTP 500"


async def test_calls_are_billed_and_their_verdicts_recorded() -> None:
    from tests.unit.agents.test_researcher import Billed

    parses: list[tuple[str, Any, bool, str | None]] = []

    async def record(call_id: str, parsed: Any, valid: bool, error: str | None) -> None:
        parses.append((call_id, parsed, valid, error))

    llm = Billed(
        reply(candidate(cited_clusters=["C-x"])),
        "not json",
    )
    result = await Auditor(llm, CFG, record_parse=record).audit(brief())  # type: ignore[arg-type]
    assert (result.call_ids, str(result.cost_usd)) == (["call-1", "call-0"], "0.02")
    assert [(c, v) for c, _, v, _ in parses] == [("call-1", False), ("call-0", False)]
    assert parses[0][1]["lessons_markdown"] == "NY longs lose." and parses[1][1] is None  # noqa: PT018


async def test_a_repair_without_lessons_keeps_the_first_ones() -> None:
    result, _ = await run(reply(candidate(cited_clusters=[])), reply(candidate(), lessons_markdown=""))
    assert (len(result.candidates), result.lessons_markdown) == (1, "NY longs lose.")
