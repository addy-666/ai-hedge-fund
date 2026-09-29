"""Circuit breaker for the LLM provider (docs/01 §9): after N consecutive failures, skip it for a while.

CLOSED: calls go through; each failure counts, a success resets the count. After ``failures`` in a row it
OPENs: every call is refused (no request is sent) until ``open_for`` has passed. Then it is HALF_OPEN: one
trial call goes through; success closes it, failure opens it again for another ``open_for``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from aifund.ports.llm import LLMCircuitOpen
from aifund.ports.system import ClockPort


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, failures: int, open_for: timedelta, clock: ClockPort) -> None:
        self._threshold = failures
        self._open_for = open_for
        self._clock = clock
        self._failures = 0
        self._opened_at: datetime | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock.now() - self._opened_at >= self._open_for:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def before_call(self) -> None:
        """Raise LLMCircuitOpen if the call must not be made."""
        state = self.state
        if state is BreakerState.OPEN:
            assert self._opened_at is not None
            until = self._opened_at + self._open_for
            raise LLMCircuitOpen(
                f"LLM circuit open until {until:%H:%M:%S} UTC after {self._failures} failures"
            )
        if state is BreakerState.HALF_OPEN:
            if self._trial_in_flight:
                raise LLMCircuitOpen("LLM circuit half-open: a trial call is already in flight")
            self._trial_in_flight = True

    def record_success(self) -> None:
        self._failures, self._opened_at, self._trial_in_flight = 0, None, False

    def record_failure(self) -> bool:
        """Count a failure; True if this one opened (or re-opened) the circuit."""
        self._failures += 1
        was_trial, self._trial_in_flight = self._trial_in_flight, False
        if was_trial or self._failures >= self._threshold:
            self._opened_at = self._clock.now()
            return True
        return False
