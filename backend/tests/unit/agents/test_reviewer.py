"""Trade reviewer (roadmap 7.3, docs/04 §2): golden prompt, strict schema with one repair, R candles/path."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from aifund.adapters.llm.fake_llm import FakeLLM, Scripted
from aifund.agents.prompting import PromptLibrary
from aifund.agents.reviewer import Reviewer, ReviewInput, r_candles, r_path
from aifund.config.trading_config import LLMConfig
from aifund.domain.enums import Direction, MistakeTag, ThesisVerdict, Timeframe
from aifund.domain.market import Bar
from aifund.ports.llm import LLMError
from tests.unit.agents.inputs import BAR, SNAPSHOT

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-reasoner")
GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "reviewer_v1.golden.txt"
OPEN = BAR + timedelta(minutes=15)


def bar(i: int, o: str, h: str, low: str, c: str) -> Bar:
    return Bar(
        symbol="XAUUSD", timeframe=Timeframe.M15, time=OPEN + timedelta(minutes=15 * i),
        open=D(o), high=D(h), low=D(low), close=D(c), tick_volume=100, spread_points=28,
    )  # fmt: skip


CANDLES = [
    bar(-1, "4148", "4151", "4147", "4150"),
    bar(0, "4150", "4153", "4146", "4152"),
    bar(1, "4152", "4160", "4151", "4158"),
    bar(2, "4158", "4159", "4139", "4141"),
    bar(3, "4141", "4142", "4138", "4139"),
]
INPUT = ReviewInput(
    trade_id="01JAXTRADE0000000000000000", symbol="XAUUSD", direction=Direction.LONG,
    setup_tag="mtf_trend_pullback", trigger_tf="M15", opened=OPEN, closed=OPEN + timedelta(minutes=45),
    entry=D("4150"), stop=D("4140"), target=D("4170"), atr=D("9.67"), r=D("-1.0"), close_reason="SL",
    mae_r=D("-1.0"), mfe_r=D("0.8"), bars_held=3, minutes=45, snapshot=SNAPSHOT, candles=CANDLES,
    thesis="Trend up, pullback into the EMA50 zone.", key_risks=["US data at 12:30"],
    lessons=["R-0042"], rules=["R-0042v1"],
)  # fmt: skip


def review(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tags": ["GOOD_TRADE_BAD_OUTCOME"], "thesis_verdict": "UNCLEAR", "execution_quality": 4,
        "lesson": "The pullback entry was sound; a data release reversed it.",
    }  # fmt: skip
    return {**base, **over}


async def run(*script: Scripted, inp: ReviewInput = INPUT) -> tuple[Any, FakeLLM]:
    llm = FakeLLM(list(script))
    return await Reviewer(llm, CFG).review(inp), llm


def test_the_rendered_prompt_matches_the_golden_file() -> None:
    reviewer = Reviewer(FakeLLM([review()]), CFG)
    rendered = PromptLibrary().render("reviewer", 1, reviewer.context(INPUT))
    text = "\n=====\n".join(m.content for m in rendered.messages) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8"), (
        "prompt rendering changed: review, then UPDATE_GOLDEN=1"
    )


async def test_a_valid_review() -> None:
    result, llm = await run(review())
    assert result.review is not None and result.error is None  # noqa: PT018
    assert result.review.tags == [MistakeTag.GOOD_TRADE_BAD_OUTCOME]
    assert result.review.thesis_verdict is ThesisVerdict.UNCLEAR
    (req,) = llm.requests
    assert (req.agent, req.model, req.trade_id) == ("reviewer", "deepseek-chat", INPUT.trade_id)
    assert result.prompt_version == "reviewer_v1"


async def test_off_taxonomy_tags_are_repaired_once_then_fail() -> None:
    result, llm = await run(review(tags=["BAD_LUCK"]), review())
    assert result.review is not None and len(llm.requests) == 2  # noqa: PT018
    assert "tags" in llm.requests[1].messages[-1].content
    result, _ = await run(review(tags=["A", "B", "C", "D", "E"]), review(execution_quality=6))
    assert (result.review, result.error is not None) == (None, True)
    assert result.error.startswith("invalid output")


async def test_not_json_is_repaired_and_a_provider_error_is_reported() -> None:
    result, _ = await run("not json", review())
    assert result.review is not None
    result, _ = await run(LLMError("HTTP 503"))
    assert (result.review, result.error) == (None, "HTTP 503")


def test_candles_and_path_are_in_r_from_the_entry() -> None:
    rows = r_candles(INPUT)
    assert [r["mark"] for r in rows] == [" ", ">", " ", " ", " "]
    assert (rows[1]["o"], rows[1]["h"], rows[1]["l"], rows[1]["c"]) == ("+0.00", "+0.30", "-0.40", "+0.20")
    assert r_path(INPUT) == "+0.20 / +0.80 / -0.90 / -1.10"
    short = replace(INPUT, direction=Direction.SHORT, stop=D("4160"))
    assert r_candles(short)[1]["h"] == "+0.40"  # the best price for a short is the low
    no_unit = replace(INPUT, stop=None, atr=None)
    assert (r_candles(no_unit), r_path(no_unit)) == ([], "na")
    atr_unit = replace(INPUT, stop=None)
    assert r_candles(atr_unit)[1]["c"] == "+0.21"  # 2 / 9.67
    assert r_path(replace(INPUT, candles=[])) == "na"


def test_an_orphan_trade_without_a_snapshot_still_renders() -> None:
    orphan = replace(INPUT, snapshot=None, thesis=None, key_risks=[], stop=None)
    ctx = Reviewer(FakeLLM([{}]), CFG).context(orphan)
    assert ctx["feature_table"] == "(none: orphan trade)"
    assert ctx["thesis"].startswith("none recorded")
    assert (ctx["stop_atr"], ctx["key_risks"]) == ("na", "none")
    assert ctx["risk"].endswith("(no stop: 1 ATR)")


async def test_calls_are_billed_and_their_verdicts_recorded() -> None:
    from tests.unit.agents.test_researcher import Billed

    parses: list[tuple[str, bool]] = []

    async def record(call_id: str, parsed: Any, valid: bool, error: str | None) -> None:
        parses.append((call_id, valid))

    llm = Billed(review(tags=["NOPE"]), review())
    result = await Reviewer(llm, CFG, record_parse=record).review(INPUT)  # type: ignore[arg-type]
    assert result.review is not None and str(result.cost_usd) == "0.02"  # noqa: PT018
    assert parses == [("call-1", False), ("call-0", True)]
