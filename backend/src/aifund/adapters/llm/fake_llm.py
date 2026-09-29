"""FakeLLM (roadmap 4.2): an ``LLMPort`` with scripted answers for tests and replays; no network.

Answers are served in order from a script. Each entry is one of:

- ``str``: returned as the reply text (valid JSON or not: that is how invalid output is tested);
- ``dict`` / ``list``: returned JSON-encoded;
- an ``LLMError`` instance: raised (budget, open circuit, provider failure);
- a callable ``(LLMRequest) -> str | dict | LLMError``: computed per request (e.g. an echo of the candidates).

When the script runs out, the last entry is repeated (so a single responder serves a whole replay). Fixture
files (``tests/fixtures/llm/*.json``) hold a list of such entries. With a session factory, every call is
recorded in ``llm_calls`` exactly like a real call; ``requests`` keeps what was asked, for assertions.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.llm.recording import record_call
from aifund.ports.llm import LLMError, LLMInvalidOutput, LLMRequest, LLMResponse
from aifund.ports.system import ClockPort

Reply = str | dict[str, Any] | list[Any] | LLMError
Scripted = Reply | Callable[[LLMRequest], Reply]


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)  # the usual rough estimate: ~4 characters per token


class FakeLLM:
    def __init__(
        self,
        script: list[Scripted],
        *,
        factory: sessionmaker[Session] | None = None,
        clock: ClockPort | None = None,
        model_reported: str = "fake-llm",
    ) -> None:
        if not script:
            raise ValueError("FakeLLM needs at least one scripted answer")
        if factory is not None and clock is None:
            raise ValueError("recording calls needs a clock")
        self._script = list(script)
        self._factory = factory
        self._clock = clock
        self._model = model_reported
        self.requests: list[LLMRequest] = []

    @classmethod
    def from_fixture(cls, path: Path, **kwargs: Any) -> FakeLLM:
        entries = json.loads(path.read_text())
        if not isinstance(entries, list):
            raise ValueError(f"{path.name}: a fixture is a JSON list of scripted answers")
        return cls(entries, **kwargs)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        entry = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        self.requests.append(request)
        answer = entry(request) if callable(entry) else entry
        prompt_tokens = sum(_tokens(m.content) for m in request.messages)
        if isinstance(answer, LLMError):
            await self._record(request, valid=False, error=str(answer), prompt_tokens=prompt_tokens)
            raise answer
        text = answer if isinstance(answer, str) else json.dumps(answer)
        fields: dict[str, Any] = dict(
            model_reported=self._model, response_text=text, prompt_tokens=prompt_tokens,
            completion_tokens=_tokens(text), cost_usd=Decimal(0),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )  # fmt: skip
        if request.json_output and not _is_object(text):
            await self._record(request, valid=False, error="output is not a JSON object", **fields)
            raise LLMInvalidOutput(f"{request.agent}: output is not a JSON object")
        call_id = await self._record(request, valid=True, **fields)
        return LLMResponse(
            text=text, model=self._model, prompt_tokens=prompt_tokens,
            completion_tokens=fields["completion_tokens"], latency_ms=fields["latency_ms"],
            cost_usd=Decimal(0), call_id=call_id,
        )  # fmt: skip

    async def _record(self, request: LLMRequest, **fields: Any) -> str | None:
        if self._factory is None or self._clock is None:
            return None
        return await record_call(self._factory, self._clock, request, **fields)


def _is_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False
