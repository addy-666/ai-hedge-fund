"""The analyst records each call's verdict on its llm_calls row (roadmap 4.4)."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.agents.analyst import Analyst, Verdict
from aifund.config.trading_config import LLMConfig
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.persistence.tables import LLMCallRow
from tests.unit.agents.inputs import BAR, PLAYBOOKS, analyst_input
from tests.unit.agents.test_analyst import proposal


async def test_rejected_and_repaired_answers_are_both_recorded(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    inp = analyst_input()
    with unit_of_work(factory) as s:  # llm_calls.decision_id references the decision
        DecisionRepository(s, clock).add(
            id=inp.decision_id, account_id="acc", symbol="XAUUSD", trigger_tf="M15", bar_time=BAR,
            stage_reached="DECISION", outcome="ERROR",
        )  # fmt: skip

    async def record(call_id: str, parsed: dict[str, Any] | None, valid: bool, error: str | None) -> None:
        def write() -> None:
            with unit_of_work(factory) as s:
                LLMCallRepository(s, clock).record_parse(call_id, parsed=parsed, valid=valid, error=error)

        await asyncio.to_thread(write)

    llm = FakeLLM([proposal(confidence=74.5), proposal()], factory=factory, clock=clock)
    result = await Analyst(
        llm, LLMConfig(analyst_model="m", auditor_model="m"), playbooks=PLAYBOOKS, record_parse=record
    ).analyse(inp)
    assert result.verdict is Verdict.PROPOSAL
    with factory() as s:
        rows = {r.id: r for r in s.scalars(select(LLMCallRow)).all()}
    first, second = (rows[i] for i in result.call_ids)
    assert (first.valid, first.parsed) == (False, None)
    assert first.error is not None and first.error.startswith("schema: confidence")  # noqa: PT018
    assert (second.valid, second.parsed["confidence"], second.decision_id) == (True, 74, inp.decision_id)  # type: ignore[index]
