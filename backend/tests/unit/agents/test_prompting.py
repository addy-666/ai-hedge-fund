"""Prompt system (roadmap 4.3): golden rendering, stable prefix, released templates immutable."""

from __future__ import annotations

import os
from decimal import Decimal as D
from pathlib import Path

import pytest

from aifund.agents.analyst import Analyst
from aifund.agents.prompting import (
    PROMPTS_DIR,
    PromptLibrary,
    atr_candles,
    fmt,
    released_templates,
    template_sha256,
)
from aifund.config.trading_config import LLMConfig
from tests.unit.agents.inputs import PLAYBOOKS, analyst_input, bars

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "analyst_v1.golden.txt"
CFG = LLMConfig(analyst_model="m", auditor_model="m")


def render_text() -> str:
    analyst = Analyst(llm=None, cfg=CFG, playbooks=PLAYBOOKS)  # type: ignore[arg-type]
    rendered = PromptLibrary().render("analyst", 1, analyst.context(analyst_input()))
    return "\n".join(f"=== {m.role} ===\n{m.content}" for m in rendered.messages) + "\n"


def test_rendered_prompt_matches_the_golden_file() -> None:
    text = render_text()
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text(), "prompt rendering changed: review the diff, then UPDATE_GOLDEN=1"


def test_stable_prefix_first_and_json_asked_for() -> None:
    analyst = Analyst(llm=None, cfg=CFG, playbooks=PLAYBOOKS)  # type: ignore[arg-type]
    rendered = PromptLibrary().render("analyst", 1, analyst.context(analyst_input()))
    system, user = (m.content for m in rendered.messages)
    assert "JSON" in system  # JSON output mode requires the word
    assert "LESSONS FROM OUR OWN TRADE HISTORY" in system and "(none yet)" in system  # noqa: PT018
    assert "4150" not in system  # nothing that changes per bar sits in the cached prefix
    assert user.startswith("SYMBOL XAUUSD | trigger M15")
    assert rendered.token_estimate > 300


def test_released_templates_are_immutable() -> None:
    released = released_templates()
    templates = sorted(p.name for p in PROMPTS_DIR.glob("*.j2"))
    assert templates == sorted(released), "every template must be registered in prompts/released.json"
    for name, sha in released.items():
        actual = template_sha256(PROMPTS_DIR / name)
        assert actual == sha, f"{name} changed after release (sha {actual}): create a new _v<n+1> template"


def test_candles_are_atr_normalised_relative_to_the_last_close() -> None:
    candles = atr_candles(bars(3), D("2"))
    # closes 4140.0, 4140.5, 4141.0 (the reference); the last candle: open 4140 -> -0.50, high 4143 -> +1.00
    assert [c["c"] for c in candles] == ["-0.50", "-0.25", "+0.00"]
    assert (candles[-1]["o"], candles[-1]["h"], candles[-1]["l"]) == ("-0.50", "+1.00", "-1.50")
    assert candles[-1]["close"] == "4141"
    assert atr_candles(bars(3), D("0")) == []


@pytest.mark.parametrize(
    ("value", "text"),
    [(None, "na"), (True, "true"), (7, "7"), (4150.28, "4150.28"), (D("0.0290"), "0.029"),
     (0.00001234, "0.000012"), ("BULL", "BULL")],
)  # fmt: skip
def test_number_formatting_is_deterministic(value: object, text: str) -> None:
    assert fmt(value) == text  # type: ignore[arg-type]  # never scientific notation
