"""Account-level limits and portfolio exposure checks (docs/03 §11, docs/02 §4 ``risk.limits``). Pure.

Loss limits are measured against the equity at the start of the trading day / trading week and against
the peak equity; a breach halts new exposure (the engine state machine decides for how long).

Exposure checks run for every new trade with its sized initial risk and notional:
max open positions, portfolio heat (Σ initial risk), heat per correlation bucket, and notional leverage
(Σ notional ≤ equity × ``max_notional_leverage`` — broker margin alone is no limit at 1:500).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from aifund.config.trading_config import LimitsConfig
from aifund.domain.enums import ReasonCode
from aifund.domain.intent import Rejection


class LimitKind(StrEnum):
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"


@dataclass(frozen=True)
class LimitBreach:
    kind: LimitKind
    loss_pct: Decimal
    limit_pct: Decimal

    def describe(self) -> str:
        return f"{self.kind}: {self.loss_pct:.2f}% >= limit {self.limit_pct}%"


@dataclass(frozen=True)
class EquityState:
    equity: Decimal
    day_start_equity: Decimal
    week_start_equity: Decimal
    peak_equity: Decimal


def _loss_pct(reference: Decimal, equity: Decimal) -> Decimal:
    if reference <= 0:
        return Decimal(0)
    return max(Decimal(0), (reference - equity) / reference * 100)


def drawdown_pct(state: EquityState) -> Decimal:
    return _loss_pct(state.peak_equity, state.equity)


def check_loss_limits(state: EquityState, limits: LimitsConfig) -> LimitBreach | None:
    """The most severe breached limit (drawdown > weekly > daily), or None."""
    checks = (
        (LimitKind.MAX_DRAWDOWN, _loss_pct(state.peak_equity, state.equity), limits.max_drawdown_pct),
        (
            LimitKind.WEEKLY_LOSS,
            _loss_pct(state.week_start_equity, state.equity),
            limits.weekly_loss_limit_pct,
        ),
        (LimitKind.DAILY_LOSS, _loss_pct(state.day_start_equity, state.equity), limits.daily_loss_limit_pct),
    )
    for kind, loss, limit in checks:
        if loss >= limit:
            return LimitBreach(kind, loss, limit)
    return None


@dataclass(frozen=True)
class Exposure:
    """One engine position (open or proposed) as the exposure checks see it."""

    symbol: str
    bucket: str
    initial_risk_money: Decimal
    notional: Decimal


def check_new_exposure(
    new: Exposure, open_positions: list[Exposure], equity: Decimal, limits: LimitsConfig
) -> Rejection | None:
    if equity <= 0:
        return Rejection(reason=ReasonCode.PORTFOLIO_HEAT, detail=f"non-positive equity {equity}")
    if len(open_positions) + 1 > limits.max_open_positions:
        return Rejection(
            reason=ReasonCode.MAX_POSITIONS,
            detail=f"{len(open_positions)} open, max {limits.max_open_positions}",
        )
    heat = sum((p.initial_risk_money for p in open_positions), Decimal(0)) + new.initial_risk_money
    heat_pct = heat / equity * 100
    if heat_pct > limits.max_portfolio_heat_pct:
        return Rejection(
            reason=ReasonCode.PORTFOLIO_HEAT,
            detail=f"portfolio heat {heat_pct:.2f}% > {limits.max_portfolio_heat_pct}%",
        )
    bucket = sum((p.initial_risk_money for p in open_positions if p.bucket == new.bucket), Decimal(0))
    bucket_pct = (bucket + new.initial_risk_money) / equity * 100
    if bucket_pct > limits.max_bucket_heat_pct:
        return Rejection(
            reason=ReasonCode.BUCKET_HEAT,
            detail=f"bucket {new.bucket} heat {bucket_pct:.2f}% > {limits.max_bucket_heat_pct}%",
        )
    notional = sum((p.notional for p in open_positions), Decimal(0)) + new.notional
    leverage = notional / equity
    if leverage > limits.max_notional_leverage:
        return Rejection(
            reason=ReasonCode.LEVERAGE_CAP,
            detail=f"notional leverage {leverage:.2f}x > {limits.max_notional_leverage}x",
        )
    return None


def _boundary(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def trading_day_start(moment: datetime, boundary: str, tz: str) -> datetime:
    """Start (UTC) of the trading day containing ``moment``.

    Days roll over at ``boundary`` in the exchange's local time ``tz`` (17:00 New York for FX/metals/US
    index CFDs: the broker's server midnight), so the boundary follows DST: 21:00 UTC in summer, 22:00 UTC in
    winter, and the two DST Sundays have 23- and 25-hour trading days.
    """
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    zone = ZoneInfo(tz)
    local = moment.astimezone(zone)
    start = datetime.combine(local.date(), _boundary(boundary), tzinfo=zone)
    if start > local:
        start = datetime.combine(local.date() - timedelta(days=1), _boundary(boundary), tzinfo=zone)
    return start.astimezone(UTC)


def trading_week_start(moment: datetime, boundary: str, tz: str) -> datetime:
    """Start of the trading week: the trading-day boundary on a local Sunday (the FX/metals week open)."""
    zone = ZoneInfo(tz)
    start = trading_day_start(moment, boundary, tz).astimezone(zone)
    while start.weekday() != 6:  # Sunday
        start = datetime.combine(
            start.date() - timedelta(days=1), start.timetz().replace(tzinfo=None), tzinfo=zone
        )
    return start.astimezone(UTC)
