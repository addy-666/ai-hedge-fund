"""Daily LLM spend limit (``llm.daily_budget_usd``), measured from ``llm_calls`` so a restart cannot reset it.

The day is the UTC calendar day. The check runs before each call: once today's billed cost reaches the budget,
no further call is made until tomorrow. Calls already in flight can overshoot by at most their own cost.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from decimal import ROUND_HALF_EVEN, Decimal

from sqlalchemy.orm import Session, sessionmaker

from aifund.config.trading_config import ModelPricing
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.ports.llm import LLMBudgetExhausted
from aifund.ports.system import ClockPort

MILLION = Decimal(1_000_000)
COST_PLACES = Decimal("0.00000001")


def call_cost(
    pricing: ModelPricing, prompt_tokens: int, cached_tokens: int, completion_tokens: int
) -> Decimal:
    """USD for one call: cached input, uncached input and output are priced separately."""
    miss = max(prompt_tokens - cached_tokens, 0)
    cost = (
        cached_tokens * pricing.input_cache_hit_per_mtok
        + miss * pricing.input_cache_miss_per_mtok
        + completion_tokens * pricing.output_per_mtok
    ) / MILLION
    return cost.quantize(COST_PLACES, rounding=ROUND_HALF_EVEN)


class DailyBudget:
    def __init__(self, limit_usd: Decimal, factory: sessionmaker[Session], clock: ClockPort) -> None:
        self._limit = limit_usd
        self._factory = factory
        self._clock = clock

    def _spent_sync(self, start: datetime) -> Decimal:
        with unit_of_work(self._factory) as session:
            return LLMCallRepository(session, self._clock).spent_since(start)

    async def spent_today(self) -> Decimal:
        start = datetime.combine(self._clock.now().astimezone(UTC).date(), time(0), tzinfo=UTC)
        return await asyncio.to_thread(self._spent_sync, start)

    async def check(self) -> None:
        spent = await self.spent_today()
        if spent >= self._limit:
            raise LLMBudgetExhausted(
                f"LLM spend today {spent} USD reached the daily budget {self._limit} USD"
            )
