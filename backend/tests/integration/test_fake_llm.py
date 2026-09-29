"""FakeLLM (roadmap 4.2): scripted answers in order, errors, callables, fixture files, recorded calls."""

from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.persistence.tables import LLMCallRow
from aifund.ports.llm import LLMBudgetExhausted, LLMInvalidOutput, LLMMessage, LLMPort, LLMRequest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "analyst_long_then_invalid.json"


def request(content: str = "{}") -> LLMRequest:
    return LLMRequest(
        agent="analyst", model="fake", temperature=D("0.1"), timeout_s=5,
        messages=[LLMMessage(role="user", content=content)], prompt_template="analyst", prompt_version="1",
    )  # fmt: skip


async def test_scripted_answers_in_order_then_the_last_repeats() -> None:
    llm = FakeLLM([{"a": 1}, '{"b": 2}'])
    assert isinstance(llm, LLMPort)
    assert json.loads((await llm.complete(request())).text) == {"a": 1}
    assert (await llm.complete(request())).text == '{"b": 2}'
    assert (await llm.complete(request())).text == '{"b": 2}'  # repeated
    assert len(llm.requests) == 3


async def test_errors_invalid_output_and_callables() -> None:
    llm = FakeLLM([LLMBudgetExhausted("spent"), "not json", lambda r: {"echo": r.messages[-1].content}])
    with pytest.raises(LLMBudgetExhausted):
        await llm.complete(request())
    with pytest.raises(LLMInvalidOutput):
        await llm.complete(request())
    assert json.loads((await llm.complete(request("hi"))).text) == {"echo": "hi"}


async def test_fixture_file_and_recorded_calls(factory: sessionmaker[Session], clock: FakeClock) -> None:
    llm = FakeLLM.from_fixture(FIXTURE, factory=factory, clock=clock)
    first = await llm.complete(request())
    assert json.loads(first.text)["direction"] == "LONG"
    with pytest.raises(LLMInvalidOutput):
        await llm.complete(request())
    with factory() as s:
        rows = s.scalars(select(LLMCallRow).order_by(LLMCallRow.created_at, LLMCallRow.valid.desc())).all()
    assert [r.valid for r in rows] == [True, False]
    assert rows[0].id == first.call_id
    assert (rows[0].model_reported, rows[0].cost_usd, rows[0].prompt_template) == (
        "fake-llm",
        D("0"),
        "analyst",
    )
    assert rows[1].response_text == "Sure, I'd go long here."


def test_an_empty_script_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FakeLLM([])
