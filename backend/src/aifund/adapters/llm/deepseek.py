"""DeepSeek adapter (roadmap 4.1): the ``openai`` SDK pointed at DeepSeek's OpenAI-compatible API.

Per call, in order: daily budget (refuse once spent), circuit breaker (refuse while open), then the request
in JSON output mode with a per-call timeout. Only transient failures are retried, with jittered backoff:
HTTP 429, 5xx, timeouts and connection errors (never 4xx such as bad request, auth or insufficient balance).
Every call that reaches the provider is recorded in ``llm_calls`` (messages, raw output, tokens incl. cache
hits, cost, latency, the model id the provider reports, error). A reply that is not the JSON object asked
for raises ``LLMInvalidOutput``: the provider worked (the breaker is not tripped) but the output is
unusable.

Model ids come from config and are checked against the provider's ``/models`` at startup (``verify_models``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

import httpx
import openai
import structlog
from openai import AsyncOpenAI
from sqlalchemy.orm import Session, sessionmaker
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential
from tenacity.wait import wait_base

from aifund.adapters.llm.budget import DailyBudget, call_cost
from aifund.adapters.llm.circuit_breaker import CircuitBreaker
from aifund.config.trading_config import LLMConfig
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.ports.llm import LLMError, LLMInvalidOutput, LLMRequest, LLMResponse
from aifund.ports.system import ClockPort, NotifierPort, Severity

T = TypeVar("T")
log = structlog.get_logger(__name__)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return True
    return isinstance(exc, openai.APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, openai.APITimeoutError):
        return "timeout"
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code}: {exc.message}"
    return f"{type(exc).__name__}: {exc}"


class DeepSeekClient:
    def __init__(
        self,
        cfg: LLMConfig,
        api_key: str,
        *,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort | None = None,
        http_client: httpx.AsyncClient | None = None,
        retry_wait: wait_base | None = None,
    ) -> None:
        self._cfg = cfg
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=cfg.base_url, max_retries=0, http_client=http_client
        )
        self._breaker = CircuitBreaker(
            cfg.circuit_breaker.failures, timedelta(minutes=cfg.circuit_breaker.open_minutes), clock
        )
        self._budget = DailyBudget(cfg.daily_budget_usd, factory, clock)
        self._wait = retry_wait or wait_random_exponential(multiplier=0.5, max=4)

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    # ------------------------------------------------------------------ persistence

    def _run_tx(self, fn: Callable[[Session], T]) -> T:
        with unit_of_work(self._factory) as session:
            return fn(session)

    async def _record(self, request: LLMRequest, **fields: Any) -> str:
        messages = [m.model_dump() for m in request.messages]
        sha = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()

        def write(s: Session) -> str:
            return LLMCallRepository(s, self._clock).add(
                agent=request.agent, decision_id=request.decision_id, trade_id=request.trade_id,
                audit_run_id=request.audit_run_id, model=request.model,
                prompt_template=request.prompt_template, prompt_version=request.prompt_version,
                prompt_sha256=sha, messages=messages, **fields,
            ).id  # fmt: skip

        return await asyncio.to_thread(self._run_tx, write)

    # ------------------------------------------------------------------ calls

    async def verify_models(self, models: list[str]) -> None:
        """Startup check: every configured model id must exist at the provider (catches renamed aliases)."""
        try:
            available = {m.id async for m in self._client.models.list()}
        except openai.OpenAIError as exc:
            raise LLMError(f"could not list models: {_describe(exc)}") from exc
        missing = sorted(set(models) - available)
        if missing:
            raise LLMError(
                f"model ids not offered by {self._cfg.base_url}: {missing} (available: {sorted(available)})"
            )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        pricing = self._cfg.pricing.get(request.model)
        if pricing is None:
            raise LLMError(f"no pricing configured for {request.model!r}: refusing an unbudgeted call")
        await self._budget.check()  # before the breaker: a refused call must not take a half-open trial slot
        self._breaker.before_call()
        started = time.perf_counter()
        kwargs: dict[str, Any] = dict(
            model=request.model,
            messages=[m.model_dump() for m in request.messages],
            temperature=float(request.temperature),
            timeout=request.timeout_s,
        )
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.json_output:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._cfg.max_retries + 1),
                wait=self._wait,
                retry=retry_if_exception(_retryable),
                reraise=True,
            ):
                with attempt:
                    completion = await self._client.chat.completions.create(**kwargs)
        except openai.OpenAIError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            await self._record(request, valid=False, error=_describe(exc), latency_ms=latency)
            if self._breaker.record_failure():
                await self._breaker_opened(exc)
            raise LLMError(f"{request.agent}: {_describe(exc)}") from exc

        latency = int((time.perf_counter() - started) * 1000)
        text = (completion.choices[0].message.content or "") if completion.choices else ""
        usage = completion.usage
        prompt = usage.prompt_tokens if usage else 0
        output = usage.completion_tokens if usage else 0
        cached = _cached_tokens(usage)
        cost = call_cost(pricing, prompt, cached, output) if usage else None
        self._breaker.record_success()
        record = dict(
            model_reported=completion.model, response_text=text, prompt_tokens=prompt,
            completion_tokens=output, cached_tokens=cached, cost_usd=cost, latency_ms=latency,
        )  # fmt: skip
        if request.json_output and not _is_json_object(text):
            await self._record(request, valid=False, error="output is not a JSON object", **record)
            raise LLMInvalidOutput(f"{request.agent}: output is not a JSON object ({len(text)} chars)")
        call_id = await self._record(request, valid=True, **record)
        return LLMResponse(
            text=text, model=completion.model, prompt_tokens=prompt, completion_tokens=output,
            cached_tokens=cached, latency_ms=latency, cost_usd=cost, call_id=call_id,
        )  # fmt: skip

    async def _breaker_opened(self, exc: BaseException) -> None:
        minutes = self._cfg.circuit_breaker.open_minutes
        log.error("llm.circuit_open", error=_describe(exc), minutes=minutes)
        if self._notifier is not None:
            await self._notifier.notify(
                Severity.WARN, "LLM circuit open", f"{_describe(exc)}; skipping the LLM for {minutes} min"
            )


def _cached_tokens(usage: Any) -> int:
    """Cache-hit input tokens (DeepSeek ``prompt_cache_hit_tokens``, OpenAI ``prompt_tokens_details``)."""
    if usage is None:
        return 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None and getattr(usage, "model_extra", None):
        hit = usage.model_extra.get("prompt_cache_hit_tokens")
    if hit is None and getattr(usage, "prompt_tokens_details", None) is not None:
        hit = usage.prompt_tokens_details.cached_tokens
    return int(hit or 0)


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False
