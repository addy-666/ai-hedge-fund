"""sr_fade_range detector: range, wall test, oscillator, rejection, hand-computed levels, the mirror."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from aifund.domain.enums import Direction
from aifund.strategies.sr_fade_range import SETUP_TAG, FadeParams, SrFadeRange
from tests.unit.strategies.support import ROLES, reflect, snap

LONG: dict[str, Any] = {
    "ctx.regime": "RANGE",
    # H1: close 2350, ATR 10; resistance 2370, support 2340 (3-ATR range, midpoint 2355); oversold stochastic
    "h1.ema50_slope_atr": 0.05, "h1.close": 2350.0, "h1.atr14": 10.0, "h1.dist_swing_high_atr": 2.0,
    "h1.dist_swing_low_atr": 1.0, "h1.stoch_k": 15.0, "h1.rsi14": 40.0,
    # M15 hammer: close 2342, ATR 4, range 4 (body 1.2, upper 0.4, lower 2.4)
    #   -> open 2340.8, low 2338.4: 1.6 below support (within 0.25 x 10), closes above it
    "m15.close": 2342.0, "m15.atr14": 4.0, "m15.range_to_atr": 1.0, "m15.body_to_range": 0.3,
    "m15.upper_wick_to_range": 0.1, "m15.lower_wick_to_range": 0.6, "m15.candle_dir": "BULL",
}  # fmt: skip


def test_long_fade_with_hand_computed_levels() -> None:
    (c,) = SrFadeRange(ROLES).detect(snap(LONG))
    assert c.setup_tag == SETUP_TAG
    assert c.direction_hint is Direction.LONG
    # invalidation = 2338.4 - 0.25 * 4 = 2337.4 ; target = midpoint 2355
    assert c.key_levels == {
        "entry_ref": D("2342"), "invalidation": D("2337.4"), "target": D("2355"),
        "support": D("2340"), "resistance": D("2370"),
    }  # fmt: skip
    assert c.strength == 0.75  # hammer; the stochastic is stretched but RSI is not


def test_the_short_mirrors_the_long() -> None:
    k = 4700.0
    (c,) = SrFadeRange(ROLES).detect(snap(reflect(LONG, k)))
    (long,) = SrFadeRange(ROLES).detect(snap(LONG))
    assert c.direction_hint is Direction.SHORT
    swap = {"support": "resistance", "resistance": "support"}
    for name, value in long.key_levels.items():
        assert float(c.key_levels[swap.get(name, name)]) == pytest.approx(k - float(value)), name


def test_rsi_alone_is_enough_and_both_add_strength() -> None:
    (c,) = SrFadeRange(ROLES).detect(snap(LONG, h1__stoch_k=50.0, h1__rsi14=25.0))
    assert c.strength == 0.75
    (c,) = SrFadeRange(ROLES).detect(snap(LONG, h1__rsi14=25.0))
    assert c.strength == 1.0
    # a strong body stands in for the hammer
    strong = {"m15__body_to_range": 0.7, "m15__lower_wick_to_range": 0.2}
    (c,) = SrFadeRange(ROLES).detect(snap(LONG, **strong))
    assert c.strength == 0.5


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"ctx__regime": "TREND_UP"}, "not a range"),
        ({"h1__ema50_slope_atr": 0.3}, "EMA50 not flat"),
        ({"h1__ema50_slope_atr": None}, "missing slope"),
        ({"h1__dist_swing_high_atr": 0.5}, "range too small (1.5 ATR)"),
        ({"h1__dist_swing_low_atr": None}, "no swing low"),
        ({"m15__close": 2347.0}, "the bar stayed 3.4 above support"),
        (
            {
                "m15__range_to_atr": 2.0,
                "m15__body_to_range": 0.15,
                "m15__upper_wick_to_range": 0.05,
                "m15__lower_wick_to_range": 0.8,
            },
            "pierced 5.6 below support: a failure test, not a fade",
        ),
        ({"m15__close": 2339.8, "m15__range_to_atr": 0.5}, "closed below support"),
        ({"h1__stoch_k": 30.0}, "oscillator not stretched"),
        ({"m15__candle_dir": "BEAR"}, "no bullish rejection"),
        (
            {"m15__body_to_range": 0.4, "m15__upper_wick_to_range": 0.3, "m15__lower_wick_to_range": 0.3},
            "neither hammer nor strong body",
        ),
        ({"m15__atr14": None}, "missing trigger ATR"),
    ],
)
def test_each_rule_is_required(override: dict[str, Any], why: str) -> None:
    assert SrFadeRange(ROLES).detect(snap(LONG, **override)) == [], why


def test_no_room_left_to_the_midpoint() -> None:
    tight = SrFadeRange(ROLES, FadeParams(min_range_atr=0.1))
    assert tight.detect(snap(LONG, h1__dist_swing_high_atr=-0.7)) == []  # resistance 2343: midpoint 2341.5
    assert tight.params_sha256() != SrFadeRange(ROLES).params_sha256()
