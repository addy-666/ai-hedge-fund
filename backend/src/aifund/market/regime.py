"""Deterministic regime classification (roadmap task 1.6).

Precedence (first match wins): VOLATILE > TREND_UP / TREND_DOWN > QUIET > RANGE.
Volatility comes first because an ATR in the top decile of its recent history changes how every setup
behaves (wider stops, smaller size) regardless of direction; a clean trend beats "quiet" because low
volatility inside an aligned EMA stack with a strong ADX is still a trend. Any missing input -> UNKNOWN,
never a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aifund.domain.enums import EmaStack, Regime


@dataclass(frozen=True)
class RegimeThresholds:
    trend_adx: float = 20.0
    volatile_atr_rank: float = 0.9
    quiet_atr_rank: float = 0.1


def _missing(value: float | None) -> bool:
    return value is None or math.isnan(value)


def classify_regime(
    *,
    adx: float | None,
    ema_stack: EmaStack | None,
    atr_pct_rank: float | None,
    thresholds: RegimeThresholds = RegimeThresholds(),  # noqa: B008 - frozen dataclass
) -> Regime:
    if _missing(adx) or _missing(atr_pct_rank) or ema_stack is None:
        return Regime.UNKNOWN
    assert adx is not None and atr_pct_rank is not None  # noqa: PT018 - narrowed above
    if atr_pct_rank > thresholds.volatile_atr_rank:
        return Regime.VOLATILE
    if adx > thresholds.trend_adx and ema_stack is EmaStack.BULL:
        return Regime.TREND_UP
    if adx > thresholds.trend_adx and ema_stack is EmaStack.BEAR:
        return Regime.TREND_DOWN
    if atr_pct_rank < thresholds.quiet_atr_rank:
        return Regime.QUIET
    return Regime.RANGE
