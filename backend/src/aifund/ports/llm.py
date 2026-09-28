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


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str
    """Model id the provider reports it used; recorded per call so silent alias changes are visible."""
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    latency_ms: int = Field(ge=0)


class LLMError(Exception):
    """The provider failed (network, HTTP error, timeout, budget, open circuit). Callers fail closed."""


@runtime_checkable
class LLMPort(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Raise LLMError on any failure; never return partial output."""
        ...
