"""LLM port. Implemented by the DeepSeek adapter and FakeLLM."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LLMMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    model: str
    messages: list[LLMMessage] = Field(min_length=1)
    temperature: Decimal = Field(ge=0, le=2)
    timeout_s: float = Field(gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    json_output: bool = True
    # recorded with the call in llm_calls (what produced this prompt and what it was for)
    prompt_template: str = "adhoc"
    prompt_version: str = "0"
    decision_id: str | None = None
    trade_id: str | None = None
    audit_run_id: str | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    """Model id the provider reports it used; recorded per call so silent alias changes are visible."""
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(ge=0)
    cost_usd: Decimal | None = None
    call_id: str | None = None
    """The ``llm_calls`` row, so the agent can record the parsed/validated result on it."""


class LLMError(Exception):
    """The provider failed (network, HTTP error, timeout, budget, open circuit). Callers fail closed."""


class LLMBudgetExhausted(LLMError):
    """Today's LLM spend reached ``llm.daily_budget_usd``: no call was made."""


class LLMCircuitOpen(LLMError):
    """Too many consecutive failures: the provider is skipped until the breaker half-opens."""


class LLMInvalidOutput(LLMError):
    """The provider answered, but not with the JSON object that was asked for (the call was billed)."""


@runtime_checkable
class LLMPort(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Raise LLMError on any failure; never return partial output."""
        ...
