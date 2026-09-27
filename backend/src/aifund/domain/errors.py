"""Domain-level exceptions."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for errors raised by domain logic."""


class InvariantViolation(DomainError):
    """A money-path invariant was about to be broken. Always a bug; never caught to continue trading."""


class DuplicateIntentError(DomainError):
    """An order intent with the same idempotency key already exists (docs/03 §9.1 layer 1)."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"order intent already exists for idempotency key {idempotency_key}")
        self.idempotency_key = idempotency_key
