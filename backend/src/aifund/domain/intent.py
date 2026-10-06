"""Order intents — the only thing the executor accepts (docs/03 §12, AGENTS.md invariant 1).

An ``OrderIntent`` is only *valid for execution* if it was issued through
``aifund.domain._issuance.issue_order_intent``, and an import-linter contract allows only ``aifund.risk``
to import that module. The executor checks ``is_issued()`` and refuses anything else, so LLM output can
never be turned into an order without passing the Risk Manager.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from aifund.domain.enums import (
    CloseReason,
    Direction,
    IntentKind,
    IntentStatus,
    ReasonCode,
    Side,
    Timeframe,
)
from aifund.domain.values import UtcDatetime

MT5_COMMENT_MAX = 31
COMMENT_PREFIX = "AF:"
CLOSING_KINDS = frozenset({IntentKind.CLOSE, IntentKind.REVERSE_CLOSE, IntentKind.FLATTEN})


def make_idempotency_key(
    *,
    account: int,
    symbol: str,
    trigger_tf: Timeframe,
    bar_time: datetime,
    direction: Direction,
    strategy_version: str,
) -> str:
    """Deterministic key: the same bar/direction can be acted on at most once, even across restarts."""
    if bar_time.tzinfo is None:
        raise ValueError("bar_time must be timezone-aware")
    instant = bar_time.astimezone(UTC).isoformat()  # same instant -> same key, whatever the tz
    material = "|".join([str(account), symbol, trigger_tf.value, instant, direction.value, strategy_version])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def intent_comment(intent_id: str) -> str:
    """Broker comment carrying the intent code (≤ 31 chars).

    Brokers may truncate or replace comments; never rely on it alone.
    """
    return f"{COMMENT_PREFIX}{intent_id[-8:]}"


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    id: str = Field(min_length=26, max_length=26)  # ULID
    idempotency_key: str = Field(min_length=24, max_length=24)
    decision_id: str | None
    kind: IntentKind
    symbol: str
    side: Side
    volume: Decimal = Field(gt=0)
    price_ref: Decimal = Field(gt=0)
    sl: Decimal | None = Field(default=None, gt=0)
    tp: Decimal | None = Field(default=None, gt=0)
    sl_distance: Decimal | None = Field(default=None, gt=0)
    tp_distance: Decimal | None = Field(default=None, gt=0)
    risk_money: Decimal = Field(ge=0)
    risk_pct: Decimal = Field(ge=0, le=100)
    magic: int = Field(gt=0)
    comment: str = Field(max_length=MT5_COMMENT_MAX)
    position_ticket: int | None = Field(default=None, gt=0)
    """Target position for CLOSE / REVERSE_CLOSE / MODIFY_SLTP / FLATTEN."""
    close_reason: CloseReason | None = None
    """Why a closing intent closes (TIME_STOP, FLATTEN, REVERSAL, ...): the reconciler's close reason."""
    setup_tag: str | None = Field(default=None, pattern=r"^[a-z0-9_]{3,40}$")
    """The strategy an OPEN intent trades (roadmap 10.7): its position's family for the family slots."""
    created_at: UtcDatetime

    _issued: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _geometry(self) -> Self:
        if self.kind is IntentKind.OPEN:
            if self.sl is None or self.tp is None or self.sl_distance is None or self.tp_distance is None:
                raise ValueError("OPEN intents require sl, tp, sl_distance and tp_distance")
            if self.risk_money <= 0:
                raise ValueError("OPEN intents require positive risk_money")
            if self.side is Side.BUY and not self.sl < self.price_ref < self.tp:
                raise ValueError("BUY requires sl < price_ref < tp")
            if self.side is Side.SELL and not self.tp < self.price_ref < self.sl:
                raise ValueError("SELL requires tp < price_ref < sl")
            # The executor re-derives SL/TP from these distances around the live price (docs/03 §12),
            # so they must describe exactly the levels the position was sized on.
            if self.sl_distance != abs(self.price_ref - self.sl):
                raise ValueError("sl_distance must equal |price_ref - sl|")
            if self.tp_distance != abs(self.tp - self.price_ref):
                raise ValueError("tp_distance must equal |tp - price_ref|")
        elif self.position_ticket is None:
            raise ValueError(f"{self.kind} intents require position_ticket")
        if self.close_reason is not None and self.kind not in CLOSING_KINDS:
            raise ValueError(f"{self.kind} intents do not close anything: close_reason must be None")
        if not self.comment.startswith(COMMENT_PREFIX):
            raise ValueError(f"comment must start with {COMMENT_PREFIX!r}")
        return self

    def is_issued(self) -> bool:
        return self._issued

    # Copies are never executable: model_copy(update=...) skips validation, so an issued intent must not
    # be mutable into another issued intent (e.g. with a larger volume).
    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        copied = super().model_copy(update=update, deep=deep)
        copied._issued = False
        return copied

    def __copy__(self) -> Self:
        copied = super().__copy__()
        copied._issued = False
        return copied

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Self:
        copied = super().__deepcopy__(memo)
        copied._issued = False
        return copied


class Rejection(BaseModel):
    """Why the Risk Manager (or a gate) refused to produce an intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: ReasonCode
    detail: str = ""


# docs/02 §2.1 — the only legal OrderIntent status transitions.
INTENT_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    # PENDING -> REJECTED: abandoned by a crash before order_send (SENT is persisted before sending)
    IntentStatus.PENDING: frozenset({IntentStatus.CHECK_FAILED, IntentStatus.SENT, IntentStatus.REJECTED}),
    IntentStatus.SENT: frozenset(
        {IntentStatus.FILLED, IntentStatus.REJECTED, IntentStatus.RETRYING, IntentStatus.UNKNOWN}
    ),
    IntentStatus.RETRYING: frozenset({IntentStatus.SENT, IntentStatus.REJECTED}),
    IntentStatus.UNKNOWN: frozenset({IntentStatus.FILLED, IntentStatus.REJECTED}),
    IntentStatus.FILLED: frozenset(),
    IntentStatus.REJECTED: frozenset(),
    IntentStatus.CHECK_FAILED: frozenset(),
}


def can_transition(current: IntentStatus, new: IntentStatus) -> bool:
    return new in INTENT_TRANSITIONS[current]


def engine_close_reason(kind: IntentKind, close_reason: CloseReason | None) -> CloseReason:
    """Close reason of a position the engine closed with this intent (docs/03 §14.2, DEAL_REASON_EXPERT)."""
    if close_reason is not None:
        return close_reason
    return {IntentKind.REVERSE_CLOSE: CloseReason.REVERSAL, IntentKind.FLATTEN: CloseReason.FLATTEN}.get(
        kind, CloseReason.ENGINE
    )
