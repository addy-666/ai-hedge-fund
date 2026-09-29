"""DeepSeek adapter against a mocked HTTP API (respx) and a migrated database (roadmap 4.1).

No request leaves the machine. Prices in these tests: 0.10 / 0.50 / 2.00 USD per million cached-input /
uncached-input / output tokens. A reply using 1,200 prompt tokens (1,000 from the cache) and 150 output tokens
costs (1000 x 0.10 + 200 x 0.50 + 150 x 2.00) / 1e6 = 0.0005 USD.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from tenacity import wait_none

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.circuit_breaker import BreakerState
from aifund.adapters.llm.deepseek import DeepSeekClient
from aifund.adapters.notify.null import NullNotifier
from aifund.config.trading_config import CircuitBreakerConfig, LLMConfig, ModelPricing
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.persistence.tables import LLMCallRow
from aifund.ports.llm import (
    LLMBudgetExhausted,
    LLMCircuitOpen,
    LLMError,
    LLMInvalidOutput,
    LLMMessage,
    LLMRequest,
)

BASE = "https://api.deepseek.com"
MODEL = "deepseek-chat"
PRICES = ModelPricing(
    input_cache_hit_per_mtok=D("0.10"), input_cache_miss_per_mtok=D("0.50"), output_per_mtok=D("2.00")
)


def completion(
    content: str = '{"direction": "LONG", "confidence": 72}', model: str = MODEL
) -> dict[str, Any]:
    return {
        "id": "cmpl-1", "object": "chat.completion", "created": 1_790_000_000, "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 1200, "completion_tokens": 150, "total_tokens": 1350,
            "prompt_cache_hit_tokens": 1000, "prompt_cache_miss_tokens": 200,
        },
    }  # fmt: skip


OK = httpx.Response(200, json=completion())


def request(**over: Any) -> LLMRequest:
    fields: dict[str, Any] = dict(
        agent="analyst", model=MODEL, temperature=D("0.1"), timeout_s=5,
        messages=[
            LLMMessage(role="system", content="You are an analyst."), LLMMessage(role="user", content="{}"),
        ],
        prompt_template="analyst", prompt_version="1",
    )  # fmt: skip
    return LLMRequest(**{**fields, **over})


def client(
    factory: sessionmaker[Session], clock: FakeClock, **cfg: Any
) -> tuple[DeepSeekClient, NullNotifier]:
    settings = LLMConfig(
        analyst_model=MODEL, auditor_model=MODEL, pricing={MODEL: PRICES}, max_retries=2,
        circuit_breaker=CircuitBreakerConfig(failures=3, open_minutes=10), **cfg,
    )  # fmt: skip
    notifier = NullNotifier()
    return DeepSeekClient(
        settings, "sk-test", factory=factory, clock=clock, notifier=notifier, retry_wait=wait_none()
    ), notifier


def calls(factory: sessionmaker[Session]) -> list[LLMCallRow]:
    with factory() as s:
        return list(s.scalars(select(LLMCallRow).order_by(LLMCallRow.created_at)).all())


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


async def test_success_records_tokens_cost_and_the_reported_model(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(
        return_value=httpx.Response(200, json=completion(model="deepseek-v3.2"))
    )
    llm, _ = client(factory, clock)
    response = await llm.complete(request(decision_id=None))
    assert response.text == '{"direction": "LONG", "confidence": 72}'
    assert (response.prompt_tokens, response.cached_tokens, response.completion_tokens) == (1200, 1000, 150)
    assert response.cost_usd == D("0.0005")
    assert response.model == "deepseek-v3.2"  # what the provider says it ran (aliases can move)
    sent = route.calls.last.request
    body = sent.read().decode()
    assert '"response_format":{"type":"json_object"}' in body.replace(" ", "")
    assert sent.headers["authorization"] == "Bearer sk-test"
    (row,) = calls(factory)
    assert row.id == response.call_id
    assert (row.agent, row.model, row.model_reported, row.valid, row.error) == (
        "analyst", MODEL, "deepseek-v3.2", True, None,
    )  # fmt: skip
    assert (row.prompt_template, row.prompt_version, len(row.prompt_sha256)) == ("analyst", "1", 64)
    assert row.messages is not None and row.messages[0]["role"] == "system"  # noqa: PT018
    assert (row.cost_usd, row.cached_tokens) == (D("0.0005"), 1000)


async def test_rate_limit_is_retried(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(
        side_effect=[httpx.Response(429, json={"error": {"message": "slow down"}}), OK]
    )
    llm, _ = client(factory, clock)
    assert (await llm.complete(request())).cost_usd == D("0.0005")
    assert route.call_count == 2
    assert len(calls(factory)) == 1  # one logical call, recorded once


async def test_timeout_after_retries_is_an_error(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(side_effect=httpx.ReadTimeout("timed out"))
    llm, _ = client(factory, clock)
    with pytest.raises(LLMError, match="timeout"):
        await llm.complete(request())
    assert route.call_count == 3  # max_retries 2 -> 3 attempts
    (row,) = calls(factory)
    assert (row.valid, row.error, row.response_text) == (False, "timeout", None)


async def test_client_errors_are_not_retried(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(
        return_value=httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})
    )
    llm, _ = client(factory, clock)
    with pytest.raises(LLMError, match="HTTP 402"):
        await llm.complete(request())
    assert route.call_count == 1


async def test_malformed_json_is_invalid_output_but_not_a_provider_failure(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    api.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion("Sure! LONG, 72")))
    llm, _ = client(factory, clock)
    with pytest.raises(LLMInvalidOutput):
        await llm.complete(request())
    (row,) = calls(factory)
    assert (row.valid, row.error, row.response_text) == (
        False,
        "output is not a JSON object",
        "Sure! LONG, 72",
    )
    assert row.cost_usd == D("0.0005")  # billed all the same
    assert llm.breaker.state is BreakerState.CLOSED
    api.post("/chat/completions").mock(return_value=httpx.Response(200, json=completion("[1, 2]")))
    with pytest.raises(LLMInvalidOutput):
        await llm.complete(request())  # JSON, but not an object


async def test_budget_exhaustion_makes_no_call(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(return_value=OK)
    llm, _ = client(factory, clock, daily_budget_usd=D("0.001"))
    with unit_of_work(factory) as s:
        repo = LLMCallRepository(s, clock)
        for when, cost in ((clock.now() - timedelta(days=1), "5"), (clock.now(), "0.0006")):
            repo.add(
                agent="analyst", model=MODEL, prompt_template="t", prompt_version="1", prompt_sha256="0" * 64,
                valid=True, cost_usd=D(cost), created_at=when,
            )  # fmt: skip
    await llm.complete(request())  # yesterday's 5 USD does not count; today 0.0006 < 0.001
    with pytest.raises(LLMBudgetExhausted, match=r"0\.00110000"):
        await llm.complete(request())  # 0.0006 + 0.0005 = 0.0011 >= 0.001
    assert route.call_count == 1
    clock.advance(days=1)
    await llm.complete(request())  # a new UTC day, a new budget


async def test_breaker_opens_then_half_opens(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(
        return_value=httpx.Response(503, json={"error": {"message": "busy"}})
    )
    llm, notifier = client(factory, clock)
    for _ in range(3):
        with pytest.raises(LLMError, match="HTTP 503"):
            await llm.complete(request())
    assert llm.breaker.state is BreakerState.OPEN
    assert notifier.sent[-1][1] == "LLM circuit open"
    attempts = route.call_count
    with pytest.raises(LLMCircuitOpen):
        await llm.complete(request())
    assert route.call_count == attempts  # refused without a request

    clock.advance(minutes=10)
    assert llm.breaker.state is BreakerState.HALF_OPEN
    with pytest.raises(LLMError, match="HTTP 503"):
        await llm.complete(request())  # the trial fails: open again
    assert llm.breaker.state is BreakerState.OPEN

    clock.advance(minutes=10)
    route.mock(return_value=OK)
    await llm.complete(request())  # the trial succeeds: closed
    assert llm.breaker.state is BreakerState.CLOSED


async def test_model_ids_are_verified_at_startup(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    api.get("/models").mock(
        return_value=httpx.Response(
            200, json={"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "deepseek"}]}
        )
    )
    llm, _ = client(factory, clock)
    await llm.verify_models([MODEL])
    with pytest.raises(LLMError, match="deepseek-v9"):
        await llm.verify_models([MODEL, "deepseek-v9"])


async def test_unpriced_models_are_refused(
    api: respx.MockRouter, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    route = api.post("/chat/completions").mock(return_value=OK)
    llm, _ = client(factory, clock)
    with pytest.raises(LLMError, match="no pricing"):
        await llm.complete(request(model="some-other-model"))
    assert route.call_count == 0
