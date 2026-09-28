from __future__ import annotations

import math

import pytest

from aifund.domain.enums import EmaStack, Regime
from aifund.market.regime import RegimeThresholds, classify_regime

B, R, M = EmaStack.BULL, EmaStack.BEAR, EmaStack.MIXED


@pytest.mark.parametrize(
    ("adx", "stack", "rank", "expected"),
    [
        (30, B, 0.5, Regime.TREND_UP),
        (30, R, 0.5, Regime.TREND_DOWN),
        (30, M, 0.5, Regime.RANGE),  # strong ADX without an aligned stack is not a trend
        (15, B, 0.5, Regime.RANGE),  # aligned stack but weak ADX
        (20, B, 0.5, Regime.RANGE),  # threshold is strict (> 20)
        (30, B, 0.95, Regime.VOLATILE),  # volatility wins over trend
        (10, M, 0.95, Regime.VOLATILE),
        (10, M, 0.05, Regime.QUIET),
        (30, R, 0.05, Regime.TREND_DOWN),  # trend wins over quiet
        (10, M, 0.9, Regime.RANGE),  # 0.9 is not > 0.9
        (10, M, 0.1, Regime.RANGE),  # 0.1 is not < 0.1
        (None, B, 0.5, Regime.UNKNOWN),
        (math.nan, B, 0.5, Regime.UNKNOWN),
        (30, None, 0.5, Regime.UNKNOWN),
        (30, B, math.nan, Regime.UNKNOWN),
    ],
)
def test_regime_table(
    adx: float | None, stack: EmaStack | None, rank: float | None, expected: Regime
) -> None:
    assert classify_regime(adx=adx, ema_stack=stack, atr_pct_rank=rank) is expected


def test_thresholds_are_configurable() -> None:
    strict = RegimeThresholds(trend_adx=35)
    assert classify_regime(adx=30, ema_stack=B, atr_pct_rank=0.5, thresholds=strict) is Regime.RANGE
