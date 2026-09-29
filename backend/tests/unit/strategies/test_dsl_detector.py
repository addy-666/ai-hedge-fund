"""Entry-rule DSL detector (roadmap R.5, docs/09 §5): parsing, feature restrictions, mirrors, levels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from pydantic import ValidationError

from aifund.domain.decision import FeatureSnapshot
from aifund.domain.enums import Direction, Timeframe
from aifund.research.signals import run_study
from aifund.strategies.base import TfRoles
from aifund.strategies.dsl_detector import MAX_PREDICATES, DslDetector, EntryHypothesis
from tests.unit.research import test_signals as sig

ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))
BAR = datetime(2026, 9, 28, 9, 45, tzinfo=UTC)


def spec(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {  # the example of docs/09 §5
        "id": "H-0007", "version": 1, "setup_tag": "h1_trend_m15_rsi_reclaim",
        "long": {"all": [{"feature": "h4.ema50_above_ema200", "op": "==", "value": True},
                         {"feature": "m15.rsi14", "op": "between", "value": [35, 50]}]},
        "short": "mirror",
        "invalidation": {"type": "atr", "tf": "trigger", "k": 1.5},
        "target": {"type": "rr", "rr": 2.0},
        "mechanism": "Pullbacks in an H4 uptrend that reclaim RSI mid-range resume the trend.",
    }  # fmt: skip
    return {**base, **over}


def snapshot(**features: Any) -> FeatureSnapshot:
    base: dict[str, Any] = {
        "m15.close": 100.0, "m15.atr14": 2.0, "h1.atr14": 5.0, "h4.ema50_above_ema200": True,
        "m15.rsi14": 42.0,
    }  # fmt: skip
    return FeatureSnapshot(
        symbol="X", trigger_tf=Timeframe.M15, bar_time=BAR, feature_set_version=2,
        features={**base, **features}, bars_ref={Timeframe.M15: BAR},
    )  # fmt: skip


def detect(h: dict[str, Any], **features: Any):  # type: ignore[no-untyped-def]
    return DslDetector(EntryHypothesis.model_validate(h), ROLES).detect(snapshot(**features))


def test_the_spec_example_parses_and_mirrors() -> None:
    h = EntryHypothesis.model_validate(spec())
    short = h.short_condition
    assert short is not None
    assert short.model_dump(mode="json", by_alias=True) == {
        "all": [
            {"feature": "h4.ema50_above_ema200", "op": "==", "value": False},
            {"feature": "m15.rsi14", "op": "between", "value": [50.0, 65.0]},
        ]
    }
    assert h.timeframes() == {Timeframe.M15, Timeframe.H4}


def test_long_levels_are_atr_multiples_of_the_trigger_close() -> None:
    (c,) = detect(spec())
    assert (c.direction_hint, c.setup_tag, c.playbook_id) == (
        Direction.LONG,
        "h1_trend_m15_rsi_reclaim",
        "H-0007",
    )
    # close 100, ATR 2, k 1.5 -> risk 3; rr 2 -> target 6 away
    assert c.key_levels == {"entry_ref": D("100.0"), "invalidation": D("97.0"), "target": D("106.0")}


def test_the_mirrored_short_side_fires_on_the_mirrored_situation() -> None:
    (c,) = detect(spec(), **{"h4.ema50_above_ema200": False, "m15.rsi14": 58.0})
    assert c.direction_hint is Direction.SHORT
    assert c.key_levels == {"entry_ref": D("100.0"), "invalidation": D("103.0"), "target": D("94.0")}
    assert detect(spec(), **{"h4.ema50_above_ema200": False, "m15.rsi14": 42.0}) == []


def test_setup_atr_and_no_target() -> None:
    (c,) = detect(spec(invalidation={"type": "atr", "tf": "setup", "k": 1.0}, target=None, short=None))
    assert c.key_levels == {"entry_ref": D("100.0"), "invalidation": D("95.0")}  # H1 ATR 5


@pytest.mark.parametrize(
    "features",
    [{"m15.rsi14": None}, {"h4.ema50_above_ema200": None}, {"m15.atr14": None}, {"m15.atr14": 0.0},
     {"m15.close": None}],
)  # fmt: skip
def test_missing_inputs_give_no_signal(features: dict[str, Any]) -> None:
    assert detect(spec(), **features) == []


def test_explicit_short_side_and_long_only() -> None:
    short = {"all": [{"feature": "m15.rsi14", "op": ">", "value": 70}]}
    only_short = spec(long=None, short=short)
    (c,) = detect(only_short, **{"m15.rsi14": 75.0})
    assert c.direction_hint is Direction.SHORT
    assert detect(spec(short=None), **{"h4.ema50_above_ema200": False, "m15.rsi14": 58.0}) == []


def five() -> list[dict[str, Any]]:
    return [{"feature": "m15.rsi14", "op": ">", "value": v} for v in range(MAX_PREDICATES + 1)]


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"long": {"all": [{"feature": "m15.rsi_14", "op": "<", "value": 30}]}}, "long: .*unknown feature"),
        ({"long": {"all": [{"feature": "m15.close", "op": ">", "value": 100}]}}, "raw price level"),
        ({"long": {"all": [{"feature": "m15.atr14", "op": ">", "value": 1}]}}, "raw price level"),
        ({"long": {"all": [{"feature": "ctx.drawdown_pct", "op": "<", "value": 5}]}}, "portfolio feature"),
        ({"long": {"all": [{"feature": "prop.llm_confidence", "op": ">", "value": 5}]}}, "proposal feature"),
        ({"long": {"all": five()}}, "predicates > max"),
        ({"short": {"all": [{"feature": "m15.rsi14", "op": ">", "value": 170}]}}, "short: .*range"),
        ({"long": None, "short": None}, "needs a long side, a short side, or both"),
        ({"long": None}, "mirror\" needs a long side"),
        ({"setup_tag": "H1 Trend"}, "setup_tag"),
        ({"invalidation": {"type": "atr", "k": 0}}, "k"),
        ({"target": {"type": "rr", "rr": 20}}, "rr"),
        ({"mechanism": "why"}, "mechanism"),
        ({"volume": 1.0}, "volume"),
    ],
)  # fmt: skip
def test_invalid_hypotheses_are_rejected(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        EntryHypothesis.model_validate(spec(**over))


def test_a_timeframe_outside_the_profile_is_refused() -> None:
    h = EntryHypothesis.model_validate(
        spec(long={"all": [{"feature": "d1.ema50_above_ema200", "op": "==", "value": True}]})
    )
    with pytest.raises(ValueError, match="uses D1"):
        DslDetector(h, ROLES)


def test_params_hash_covers_behaviour_not_prose() -> None:
    h = EntryHypothesis.model_validate(spec())
    assert (
        h.params_sha256()
        == EntryHypothesis.model_validate(spec(mechanism="Different words, same rule.")).params_sha256()
    )
    assert (
        h.params_sha256()
        != EntryHypothesis.model_validate(spec(target={"type": "rr", "rr": 2.5})).params_sha256()
    )
    explicit = spec(short=h.short_condition.model_dump(mode="json", by_alias=True))  # type: ignore[union-attr]
    assert (
        h.params_sha256() == EntryHypothesis.model_validate(explicit).params_sha256()
    )  # mirror == written out
    assert DslDetector(h, ROLES).version == "1"


def test_the_signal_study_runs_a_dsl_detector_with_the_production_stops() -> None:
    """On the flat synthetic world (ATR 2.00, doji bars, quote 100.00/100.10) a doji hypothesis fires on every
    trigger bar with invalidation 100 - 1.5 x 2 = 97.00 and target 100 + 2 x 3 = 106.00. The live planner
    measures from the ask: SL = (100.10 - 97.00) + 0.1 ATR buffer 0.20 + spread 0.10 = 3.40, TP = 5.90."""
    h = EntryHypothesis.model_validate(
        spec(
            long={"all": [{"feature": "m15.candle_dir", "op": "==", "value": "DOJI"},
                          {"feature": "m15.range_to_atr", "op": "between", "value": [0.9, 1.1]}]},
            short=None,
        )
    )  # fmt: skip
    history = sig.world()
    run = run_study(
        history, sig.spec(), [DslDetector(h, sig.ROLES)], sig.START, sig.START + timedelta(hours=6)
    )
    assert run.outcomes
    first = run.outcomes[0]
    assert (first.detector, first.setup_tag, first.direction) == (
        "H-0007@1",
        "h1_trend_m15_rsi_reclaim",
        Direction.LONG,
    )
    assert (first.sl_distance, first.tp_distance) == (D("3.40"), D("5.90"))
    again = run_study(
        history, sig.spec(), [DslDetector(h, sig.ROLES)], sig.START, sig.START + timedelta(hours=6)
    )
    assert [o.r_net for o in again.outcomes] == [o.r_net for o in run.outcomes]


def test_the_detector_fingerprint_is_the_hypothesis_behaviour() -> None:
    h = EntryHypothesis.model_validate(spec())
    assert DslDetector(h, ROLES).params_sha256() == h.params_sha256()
