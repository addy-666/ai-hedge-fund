"""The facts the rollout gates are judged on (docs/06 §10, roadmap 9.6-9.7): gathered from the database by
``persistence/repositories/rollout.py``, judged by ``engine/rollout.py``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ClosedTrade:
    r: Decimal | None
    net: Decimal
    costs: Decimal  # commission + swap + fee (negative = paid)
    volume: Decimal
    open_time: datetime


@dataclass(frozen=True)
class RolloutFacts:
    period_start: datetime | None
    trades: Sequence[ClosedTrade] = ()
    baseline_trades: Sequence[ClosedTrade] = ()  # the DEMO period before an L3 sign-off (cost comparison)
    duplicate_opens: int = 0
    unknown_intents: int = 0
    sl_missing: int = 0
    without_context: int = 0
    ledger_status: str | None = None
    ledger_at: datetime | None = None
    flatten_tested: int = 0
    restarts_with_open: int = 0
