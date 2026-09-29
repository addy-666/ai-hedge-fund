"""Rule DSL (roadmap 7.1, docs/04 §5): shape, validation, scope, evaluation, hash, lessons proximity."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from aifund.domain.enums import Direction
from aifund.rules import dsl
from aifund.rules.dsl import ActionType, RuleContext, RuleError

BASE: dict[str, Any] = {
    "rule_id": "R-0042",
    "scope": {"symbols": ["XAUUSD"], "directions": ["LONG"], "setup_tags": ["mtf_trend_pullback"]},
    "conditions": {
        "all": [
            {"feature": "h1.rsi14", "op": ">", "value": 70},
            {"feature": "m15.atr14_pct_rank100", "op": ">=", "value": 0.9},
        ]
    },
    "action": {"type": "penalty", "points": 20},
    "hypothesis": "late-trend longs into volatility expansion",
    "cited_clusters": ["C-1"],
}


def rule(**over: Any) -> dsl.Rule:
    return dsl.parse({**BASE, **over})


def ctx(features: dict[str, Any] | None = None, **over: Any) -> RuleContext:
    base: dict[str, Any] = {
        "symbol": "XAUUSD",
        "direction": Direction.LONG,
        "setup_tag": "mtf_trend_pullback",
        "trigger_tf": "M15",
        "features": {"h1.rsi14": 75.0, "m15.atr14_pct_rank100": 0.95} if features is None else features,
    }
    return RuleContext(**{**base, **over})


def test_a_rule_parses_dumps_and_describes_itself() -> None:
    r = rule()
    assert r.key == "R-0042v1" and r.action.type is ActionType.PENALTY  # noqa: PT018
    assert dsl.parse(dsl.dump(r)) == r
    assert r.describe() == (
        "LONG mtf_trend_pullback on XAUUSD when h1.rsi14 > 70 AND m15.atr14_pct_rank100 >= 0.9"
    )
    assert dsl.RuleScope().describe() == "LONG/SHORT any setup on any symbol"
    assert "(M15)" in dsl.RuleScope(trigger_tfs=("M15",)).describe()


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ({"type": "penalty", "points": 4}, "points 5..30"),
        ({"type": "penalty", "points": 31}, "points 5..30"),
        ({"type": "penalty", "points": 10, "factor": "0.5"}, "points 5..30"),
        ({"type": "risk_scale", "factor": "0.8"}, "factor 0.25..0.75"),
        ({"type": "risk_scale", "factor": "0.5", "points": 5}, "factor 0.25..0.75"),
        ({"type": "block", "points": 5}, "block takes no"),
        ({"type": "boost", "points": 5}, "action.type"),
    ],
)
def test_actions_only_ever_reduce_risk(action: dict[str, Any], text: str) -> None:
    with pytest.raises(RuleError, match=text):
        rule(action=action)


def test_severity_orders_block_then_scale_then_penalty() -> None:
    block, scale, pen = (
        dsl.RuleAction(type=ActionType.BLOCK),
        dsl.RuleAction(type=ActionType.RISK_SCALE, factor=Decimal("0.5")),
        dsl.RuleAction(type=ActionType.PENALTY, points=20),
    )
    assert block.severity > scale.severity > pen.severity
    assert [a.describe() for a in (block, scale, pen)] == ["block", "risk x0.5", "-20 confidence"]


def test_bad_ids_and_directions_are_refused() -> None:
    with pytest.raises(RuleError, match="rule_id"):
        rule(rule_id="42")
    with pytest.raises(RuleError, match="LONG and/or SHORT"):
        rule(scope={"directions": ["NONE"]})


def test_validation_uses_the_registry_the_scope_and_the_observed_range() -> None:
    dsl.validate(rule(), max_conditions=3, symbols={"XAUUSD"}, setup_tags={"mtf_trend_pullback"})
    with pytest.raises(RuleError, match="2 predicates > max 1"):
        dsl.validate(rule(), max_conditions=1)
    with pytest.raises(RuleError, match=r"scope.symbols: unknown \['XAUUSD'\]"):
        dsl.validate(rule(), max_conditions=3, symbols={"BTCUSD"})
    with pytest.raises(RuleError, match=r"scope\.setup_tags"):
        dsl.validate(rule(), max_conditions=3, setup_tags=set())
    with pytest.raises(RuleError, match=r"outside the observed range 20\.\.68"):
        dsl.validate(rule(), max_conditions=3, observed={"h1.rsi14": (20.0, 68.0)})
    between = {"all": [{"feature": "h1.rsi14", "op": "between", "value": [30, 90]}]}
    with pytest.raises(RuleError, match="90"):
        dsl.validate(rule(conditions=between), max_conditions=3, observed={"h1.rsi14": (20.0, 80.0)})
    flag = {"all": [{"feature": "m15.nr7", "op": "==", "value": True}]}
    dsl.validate(rule(conditions=flag), max_conditions=3, observed={"m15.nr7": (0.0, 1.0)})
    unknown = {"all": [{"feature": "m15.nope", "op": ">", "value": 1}]}
    with pytest.raises(RuleError, match="unknown feature"):
        dsl.validate(rule(conditions=unknown), max_conditions=3)


def test_features_known_only_after_the_stop_plan_are_refused() -> None:
    late = {"all": [{"feature": "prop.rr_target", "op": "<", "value": 1.5}]}
    with pytest.raises(RuleError, match="after the rules run"):
        dsl.validate(rule(conditions=late), max_conditions=3)
    early = {"all": [{"feature": "prop.htf_alignment", "op": "<", "value": 0}]}
    dsl.validate(rule(conditions=early), max_conditions=3)


def test_scope_and_condition_decide_a_match_and_missing_features_never_match() -> None:
    r = rule()
    assert dsl.matches(r, ctx())
    assert not dsl.matches(r, ctx(symbol="BTCUSD"))
    assert not dsl.matches(r, ctx(direction=Direction.SHORT))
    assert not dsl.matches(r, ctx(setup_tag="other"))
    assert dsl.matches(rule(scope={}), ctx(symbol="BTCUSD", setup_tag=None))
    assert not dsl.matches(rule(scope={"trigger_tfs": ["H1"]}), ctx())
    assert not dsl.matches(r, ctx({"h1.rsi14": 65.0, "m15.atr14_pct_rank100": 0.95}))
    partial = ctx({"h1.rsi14": 75.0})
    assert not dsl.matches(r, partial)
    assert dsl.missing(r, partial) == {"m15.atr14_pct_rank100"}


def test_the_hash_is_about_behaviour_not_bookkeeping() -> None:
    a = rule()
    assert dsl.dsl_sha256(a) == dsl.dsl_sha256(rule(rule_id="R-0100", version=3, hypothesis="x"))
    assert dsl.dsl_sha256(a) != dsl.dsl_sha256(rule(action={"type": "penalty", "points": 25}))
    same = {**BASE["conditions"]}
    same["all"] = [{"feature": "h1.rsi14", "op": ">", "value": 70.0}, BASE["conditions"]["all"][1]]
    assert dsl.dsl_sha256(a) == dsl.dsl_sha256(rule(conditions=same))  # 70 == 70.0


def test_lessons_show_rules_that_match_or_nearly_match() -> None:
    r = rule()
    assert dsl.near(r, ctx())
    assert dsl.near(r, ctx({"h1.rsi14": 64.0, "m15.atr14_pct_rank100": 0.95}))  # 70 - 10% = 63
    assert not dsl.near(r, ctx({"h1.rsi14": 60.0, "m15.atr14_pct_rank100": 0.95}))
    assert not dsl.near(r, ctx({"h1.rsi14": 75.0}))  # a missing feature is never "near"
    assert not dsl.near(r, ctx(symbol="BTCUSD"))
    flags = rule(
        conditions={"all": [{"feature": "m15.nr7", "op": "==", "value": True}, {"any": [
            {"feature": "h1.rsi14", "op": "between", "value": [70, 80]},
            {"feature": "ctx.session", "op": "in", "value": ["NY"]},
        ]}]}
    )  # fmt: skip
    assert dsl.near(flags, ctx({"m15.nr7": True, "h1.rsi14": 66.0, "ctx.session": "ASIA"}))
    assert not dsl.near(flags, ctx({"m15.nr7": False, "h1.rsi14": 75.0, "ctx.session": "NY"}))
    either = rule(conditions={"any": [{"feature": "h1.rsi14", "op": "<", "value": 30}]})
    assert dsl.near(either, ctx({"h1.rsi14": 32.0})) and not dsl.near(either, ctx({"h1.rsi14": 40.0}))  # noqa: PT018


def test_proposal_features_come_from_the_candidate_trade() -> None:
    assert dsl.proposal_features(Direction.SHORT, "tag", 70, {"ctx.htf_trend_score": 2}) == {
        "prop.direction": "SHORT",
        "prop.setup_tag": "tag",
        "prop.llm_confidence": 70,
        "prop.htf_alignment": -2,
    }
    assert dsl.proposal_features(Direction.LONG, None, None, {})["prop.htf_alignment"] is None
