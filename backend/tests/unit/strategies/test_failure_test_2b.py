"""failure_test_2b detector: probe, failure, context, hand-computed levels, the mirrored short."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from aifund.domain.enums import Direction
from aifund.strategies.failure_test_2b import SETUP_TAG, FailureTest2B, FailureTestParams
from tests.unit.strategies.support import ROLES, reflect, snap

LONG: dict[str, Any] = {
    # H1: close 2350, ATR 10; swing low 0.5 ATR below -> 2345; swing high 2.5 ATR above -> 2375
    "h1.close": 2350.0, "h1.atr14": 10.0, "h1.dist_swing_low_atr": 0.5, "h1.dist_swing_high_atr": 2.5,
    # M15 reversal bar: close 2346, ATR 4, range 4 (body 1.6, upper 0.4, lower 2.0), bullish
    #   -> open 2344.4, high 2346.4, low 2342.4: 2.6 below the swing low, closes 1.0 above it
    "m15.close": 2346.0, "m15.atr14": 4.0, "m15.range_to_atr": 1.0, "m15.body_to_range": 0.4,
    "m15.upper_wick_to_range": 0.1, "m15.lower_wick_to_range": 0.5, "m15.candle_dir": "BULL",
    "ctx.htf_trend_score": 1,
}  # fmt: skip


def test_long_2b_with_hand_computed_levels() -> None:
    (c,) = FailureTest2B(ROLES).detect(snap(LONG))
    assert c.setup_tag == SETUP_TAG
    assert c.direction_hint is Direction.LONG
    # invalidation = 2342.4 - 0.5 * 4 = 2340.4 ; 2R = 2346 + 2 * 5.6 = 2357.2 < the swing high 2375
    assert c.key_levels == {
        "entry_ref": D("2346"), "invalidation": D("2340.4"), "target": D("2375"), "swept_level": D("2345"),
    }  # fmt: skip
    assert c.strength == 1.0  # a hammer wick and the higher timeframes with it


@pytest.mark.parametrize(("to_high", "target"), [(0.6, D("2357.2")), (None, D("2357.2")), (0.8, D("2358"))])
def test_the_target_is_the_opposite_swing_but_at_least_2r(to_high: float | None, target: D) -> None:
    (c,) = FailureTest2B(ROLES).detect(snap(LONG, h1__dist_swing_high_atr=to_high))
    assert c.key_levels["target"] == target


def test_the_short_mirrors_the_long() -> None:
    k = 4700.0
    (c,) = FailureTest2B(ROLES).detect(snap(reflect(LONG, k)))
    (long,) = FailureTest2B(ROLES).detect(snap(LONG))
    assert c.direction_hint is Direction.SHORT
    for name, value in long.key_levels.items():
        assert float(c.key_levels[name]) == pytest.approx(k - float(value)), name
    assert c.strength == long.strength


@pytest.mark.parametrize(
    ("override", "why"),
    [
        (
            {
                "m15__lower_wick_to_range": 0.9,
                "m15__upper_wick_to_range": 0.05,
                "m15__body_to_range": 0.05,
                "m15__range_to_atr": 3.0,
            },
            "overshoot beyond 0.5 setup ATR",
        ),
        ({"m15__close": 2349.0}, "the bar never traded below the swing low"),
        (
            {"m15__close": 2344.9, "m15__lower_wick_to_range": 0.2, "m15__upper_wick_to_range": 0.4},
            "closed below the swing low",
        ),
        ({"m15__candle_dir": "BEAR"}, "bearish reversal bar"),
        ({"ctx__htf_trend_score": -3}, "every higher timeframe stacked against"),
        ({"ctx__htf_trend_score": None}, "missing context"),
        ({"h1__dist_swing_low_atr": None}, "no confirmed swing"),
        ({"m15__atr14": None}, "missing trigger ATR"),
    ],
)
def test_each_rule_is_required(override: dict[str, Any], why: str) -> None:
    assert FailureTest2B(ROLES).detect(snap(LONG, **override)) == [], why


def test_context_against_but_not_fully_aligned_still_counts() -> None:
    (c,) = FailureTest2B(ROLES).detect(snap(LONG, ctx__htf_trend_score=-2, m15__lower_wick_to_range=0.4,
                                             m15__upper_wick_to_range=0.2))  # fmt: skip
    assert c.strength == 0.5
    assert (
        FailureTest2B(ROLES).params_sha256() != FailureTest2B(ROLES, FailureTestParams(0.3)).params_sha256()
    )
