"""Circuit breaker states and per-call cost (roadmap 4.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.budget import call_cost
from aifund.adapters.llm.circuit_breaker import BreakerState, CircuitBreaker
from aifund.config.trading_config import ModelPricing
from aifund.ports.llm import LLMCircuitOpen


def test_only_one_trial_call_while_half_open() -> None:
    clock = FakeClock(datetime(2026, 9, 29, 9, 0, tzinfo=UTC))
    breaker = CircuitBreaker(2, timedelta(minutes=10), clock)
    assert not breaker.record_failure()
    assert breaker.record_failure()  # the second consecutive failure opens it
    clock.advance(minutes=10)
    breaker.before_call()  # the trial
    with pytest.raises(LLMCircuitOpen, match="trial call is already in flight"):
        breaker.before_call()  # a concurrent call waits for the verdict
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    breaker.before_call()


def test_a_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(2, timedelta(minutes=10), FakeClock(datetime(2026, 9, 29, tzinfo=UTC)))
    breaker.record_failure()
    breaker.record_success()
    assert not breaker.record_failure()  # counts consecutive failures only


def test_call_cost() -> None:
    prices = ModelPricing(
        input_cache_hit_per_mtok=D("0.07"), input_cache_miss_per_mtok=D("0.27"), output_per_mtok=D("1.10")
    )
    # (1000 x 0.07 + 200 x 0.27 + 150 x 1.10) / 1e6 = (70 + 54 + 165) / 1e6
    assert call_cost(prices, 1200, 1000, 150) == D("0.00028900")
    assert call_cost(prices, 100, 500, 0) == D("0.00003500")  # more cache hits than prompt: never negative
