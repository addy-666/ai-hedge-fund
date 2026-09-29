"""Persist one LLM call in ``llm_calls``; shared by every LLM adapter so all calls are recorded alike."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.ports.llm import LLMRequest
from aifund.ports.system import ClockPort


def prompt_sha256(request: LLMRequest) -> str:
    messages = [m.model_dump() for m in request.messages]
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()


async def record_call(
    factory: sessionmaker[Session], clock: ClockPort, request: LLMRequest, **fields: Any
) -> str:
    """Insert the call (prompt metadata from the request + outcome ``fields``); returns its id."""

    def write() -> str:
        with unit_of_work(factory) as s:
            return LLMCallRepository(s, clock).add(
                agent=request.agent, decision_id=request.decision_id, trade_id=request.trade_id,
                audit_run_id=request.audit_run_id, model=request.model,
                prompt_template=request.prompt_template, prompt_version=request.prompt_version,
                prompt_sha256=prompt_sha256(request), messages=[m.model_dump() for m in request.messages],
                **fields,
            ).id  # fmt: skip

    return await asyncio.to_thread(write)
