"""Order intents — the only thing the executor accepts (docs/03 §12, AGENTS.md invariant 1).

An ``OrderIntent`` is only *valid for execution* if it was issued through
``aifund.domain._issuance.issue_order_intent``, and an import-linter contract allows only ``aifund.risk``
to import that module. The executor checks ``is_issued()`` and refuses anything else, so LLM output can
never be turned into an order without passing the Risk Manager.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from aifund.domain.enums import Direction, IntentKind, ReasonCode, Side, Timeframe
from aifund.domain.values import UtcDatetime

MT5_COMMENT_MAX = 31
COMMENT_PREFIX = "AF:"


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
    material = "|".join(
        [str(account), symbol, trigger_tf.value, bar_time.isoformat(), direction.value, strategy_version]
    )
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
        elif self.position_ticket is None:
            raise ValueError(f"{self.kind} intents require position_ticket")
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
