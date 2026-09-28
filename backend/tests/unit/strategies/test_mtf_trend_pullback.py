"""mtf_trend_pullback detector: every rule of the playbook, hand-computed levels, and the playbook card."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml

from aifund.domain.decision import FeatureSnapshot
from aifund.domain.enums import Direction, Timeframe
from aifund.strategies.base import TfRoles
from aifund.strategies.mtf_trend_pullback import SETUP_TAG, MtfTrendPullback, PullbackParams

ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4, Timeframe.D1))
T = datetime(2026, 9, 28, 9, 45, tzinfo=UTC)
CARD = Path(__file__).resolve().parents[4] / "config" / "playbooks" / "mtf_trend_pullback.yaml"

LONG: dict[str, Any] = {
    # D1 trend filter
    "d1.ema50_above_ema200": True, "d1.dist_ema50_atr": 1.2, "d1.ema_stack": "BULL",
    # H1 setup: close 2350, ATR 10 -> EMA50 = 2350 - 0.3*10 = 2347 ; low = 2347 + 0.1*10 = 2348
    "h1.close": 2350.0, "h1.atr14": 10.0, "h1.dist_ema50_atr": 0.3, "h1.low_dist_ema50_atr": 0.1,
    "h1.high_dist_ema50_atr": 0.9, "h1.close_above_ema200": True, "h1.dist_swing_high_atr": 2.0,
    "h1.dist_swing_low_atr": 1.0,
    # M15 trigger
    "m15.stoch_cross_up": True, "m15.stoch_cross_down": False, "m15.stoch_k": 22.0, "m15.candle_dir": "BULL",
    "m15.body_to_range": 0.3, "m15.lower_wick_to_range": 0.55, "m15.upper_wick_to_range": 0.15,
    "m15.close": 2351.0,
}  # fmt: skip


def snap(**overrides: Any) -> FeatureSnapshot:
    features = {**LONG, **{k.replace("__", "."): v for k, v in overrides.items()}}
    return FeatureSnapshot(
        symbol="XAUUSD",
        trigger_tf=Timeframe.M15,
        bar_time=T,
        feature_set_version=2,
        features=features,
        bars_ref={Timeframe.M15: T},
    )


def test_long_setup_with_hand_computed_levels() -> None:
    (c,) = MtfTrendPullback(ROLES).detect(snap())
    assert c.setup_tag == SETUP_TAG
    assert c.direction_hint is Direction.LONG
    # invalidation = setup low 2348 - 0.5 * ATR 10 = 2343 ; target = 2350 + 2.0 * 10 = 2370
    assert c.key_levels == {"entry_ref": D("2351.0"), "invalidation": D("2343.0"), "target": D("2370.0")}
    assert c.strength == 0.75  # full BULL stack, but the body is not "strong"


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"d1__ema50_above_ema200": False}, "HTF EMA50 below EMA200"),
        ({"d1__dist_ema50_atr": -0.1}, "HTF close below EMA50"),
        ({"h1__low_dist_ema50_atr": 0.4}, "pullback did not reach the value zone"),
        ({"h1__dist_ema50_atr": -0.6}, "setup close too far below EMA50"),
        ({"h1__close_above_ema200": False}, "invalidation: setup close below EMA200"),
        ({"m15__stoch_cross_up": False}, "no stochastic cross"),
        ({"m15__stoch_k": 35.0}, "not oversold at the cross"),
        ({"m15__candle_dir": "BEAR"}, "no bullish confirmation candle"),
        ({"m15__lower_wick_to_range": 0.3, "m15__body_to_range": 0.4}, "neither hammer nor strong body"),
        ({"m15__close": 2342.0}, "price already below invalidation"),
        ({"h1__atr14": None}, "missing feature"),
        ({"d1__ema50_above_ema200": None}, "missing trend feature (e.g. warm-up)"),
    ],
)
def test_each_rule_is_required(override: dict[str, Any], why: str) -> None:
    assert MtfTrendPullback(ROLES).detect(snap(**override)) == [], why


def test_candle_confirmation_can_be_disabled() -> None:
    detector = MtfTrendPullback(ROLES, PullbackParams(require_candle=False))
    assert len(detector.detect(snap(m15__candle_dir="BEAR"))) == 1


def test_strong_body_counts_as_confirmation_and_raises_strength() -> None:
    (c,) = MtfTrendPullback(ROLES).detect(snap(m15__lower_wick_to_range=0.1, m15__body_to_range=0.7))
    assert c.strength == 1.0


def test_target_is_omitted_when_the_swing_high_is_not_above_entry() -> None:
    (c,) = MtfTrendPullback(ROLES).detect(snap(h1__dist_swing_high_atr=0.05))  # 2350.5 < entry 2351
    assert "target" not in c.key_levels


def test_short_setup_mirrors_the_long_rules() -> None:
    short = {
        "d1__ema50_above_ema200": False, "d1__dist_ema50_atr": -1.2, "d1__ema_stack": "BEAR",
        # H1: close 2350, ATR 10, EMA50 = 2350 + 0.3*10 = 2353 ; high = 2353 - 0.1*10 = 2352
        "h1__dist_ema50_atr": -0.3, "h1__high_dist_ema50_atr": -0.1, "h1__close_above_ema200": False,
        "h1__dist_swing_low_atr": 2.0,
        "m15__stoch_cross_up": False, "m15__stoch_cross_down": True, "m15__stoch_k": 78.0,
        "m15__candle_dir": "BEAR", "m15__upper_wick_to_range": 0.6, "m15__close": 2349.0,
    }  # fmt: skip
    (c,) = MtfTrendPullback(ROLES).detect(snap(**short))
    assert c.direction_hint is Direction.SHORT
    # invalidation = setup high 2352 + 0.5*10 = 2357 ; target = 2350 - 2.0*10 = 2330
    assert c.key_levels == {"entry_ref": D("2349.0"), "invalidation": D("2357.0"), "target": D("2330.0")}


def test_playbook_card_matches_the_detector_and_marks_claims_unvalidated() -> None:
    card = yaml.safe_load(CARD.read_text())
    assert card["setup_tag"] == SETUP_TAG == MtfTrendPullback.setup_tag
    assert card["source"] == "[[high_probability_multi_timeframe_trend_pullback]]"
    assert card["claimed_performance"]["status"] == "UNVALIDATED"
    assert card["approximations"]
