"""nr7_breakout detector: every rule of the playbook, hand-computed levels, the mirrored short, the card."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from aifund.domain.enums import Direction
from aifund.strategies.nr7_breakout import SETUP_TAG, Nr7Breakout, Nr7Params
from tests.unit.strategies.support import ROLES, reflect, snap

LONG: dict[str, Any] = {
    # D1 trend filter
    "d1.ema50_above_ema200": True, "d1.dist_ema50_atr": 1.2, "d1.ema_stack": "BULL",
    # H1 NR7 bar at value: close 2350, ATR 10, range 0.4 ATR = 4 (body 2, wicks 1 + 1), bullish
    #   -> open 2348, high 2351, low 2347
    "h1.nr7": True, "h1.dist_ema20_atr": 0.4, "h1.dist_ema50_atr": 0.8, "h1.ema50_slope_atr": 0.3,
    "h1.atr14": 10.0, "h1.close": 2350.0, "h1.range_to_atr": 0.4, "h1.body_to_range": 0.5,
    "h1.upper_wick_to_range": 0.25, "h1.lower_wick_to_range": 0.25, "h1.candle_dir": "BULL",
    # M15 breakout: close 2352, ATR 3, range 3 (body 2.4, wicks 0.3 + 0.3) -> open 2349.6: through 2351
    "m15.close": 2352.0, "m15.atr14": 3.0, "m15.range_to_atr": 1.0, "m15.body_to_range": 0.8,
    "m15.upper_wick_to_range": 0.1, "m15.lower_wick_to_range": 0.1, "m15.candle_dir": "BULL",
    "m15.rel_tick_volume20": 1.5,
}  # fmt: skip


def test_long_breakout_with_hand_computed_levels() -> None:
    (c,) = Nr7Breakout(ROLES).detect(snap(LONG))
    assert c.setup_tag == SETUP_TAG
    assert c.direction_hint is Direction.LONG
    # invalidation = NR7 low 2347 - 0.25 * ATR 10 = 2344.5 ; risk 7.5 ; target = 2352 + 2 * 7.5 = 2367
    assert c.key_levels == {
        "entry_ref": D("2352"), "invalidation": D("2344.5"), "target": D("2367"),
        "nr7_high": D("2351"), "nr7_low": D("2347"),
    }  # fmt: skip
    assert c.strength == 1.0  # full BULL stack and a close near the high


def test_the_short_mirrors_the_long() -> None:
    k = 4700.0
    (c,) = Nr7Breakout(ROLES).detect(snap(reflect(LONG, k)))
    assert c.direction_hint is Direction.SHORT
    swap = {"nr7_high": "nr7_low", "nr7_low": "nr7_high"}
    (long,) = Nr7Breakout(ROLES).detect(snap(LONG))
    for name, value in long.key_levels.items():
        assert float(c.key_levels[swap.get(name, name)]) == pytest.approx(k - float(value)), name


def test_a_doji_nr7_bar_uses_its_widest_extremes() -> None:
    # body 0.2 (sign unknown), wicks 1.9 + 1.9: high <= 2350 + 1.9 + 0.2 = 2352.1, low >= 2347.9
    doji = {"h1__candle_dir": "DOJI", "h1__body_to_range": 0.05, "h1__upper_wick_to_range": 0.475,
            "h1__lower_wick_to_range": 0.475}  # fmt: skip
    assert Nr7Breakout(ROLES).detect(snap(LONG, **doji)) == []  # 2352 does not clear 2352.1
    (c,) = Nr7Breakout(ROLES).detect(snap(LONG, **doji, m15__close=2353.0))
    assert c.key_levels["invalidation"] == D("2345.4")  # 2347.9 - 2.5


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"d1__ema50_above_ema200": False}, "HTF EMA50 below EMA200"),
        ({"d1__dist_ema50_atr": -0.1}, "HTF close below EMA50"),
        ({"h1__nr7": False}, "the setup bar is not an NR7"),
        ({"h1__dist_ema20_atr": 1.5, "h1__dist_ema50_atr": 1.2}, "NR7 bar away from value"),
        ({"h1__ema50_slope_atr": -0.1}, "EMA50 falling"),
        ({"m15__rel_tick_volume20": 1.1}, "no volume expansion"),
        ({"m15__body_to_range": 0.2, "m15__lower_wick_to_range": 0.7}, "opened above the NR7 high already"),
        ({"m15__close": 2350.5}, "closed below the NR7 high"),
        ({"m15__candle_dir": "DOJI"}, "trigger bar direction unknown"),
        ({"h1__atr14": None}, "missing setup ATR"),
        ({"m15__rel_tick_volume20": None}, "missing volume"),
    ],
)
def test_each_rule_is_required(override: dict[str, Any], why: str) -> None:
    assert Nr7Breakout(ROLES).detect(snap(LONG, **override)) == [], why


def test_parameters_are_fingerprinted() -> None:
    default, other = Nr7Breakout(ROLES), Nr7Breakout(ROLES, Nr7Params(min_rel_volume=1.5))
    assert default.params_sha256() != other.params_sha256()
    assert other.detect(snap(LONG)) == []  # 1.5 is not above 1.5
