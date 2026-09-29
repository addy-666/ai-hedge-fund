"""Rule engine at decision time (roadmap 7.2, docs/04 §7): enforcement, shadow logs, diagnostics, lessons."""

from __future__ import annotations

from decimal import Decimal as D
from typing import Any

import pytest

from aifund.domain.enums import Direction, RuleStatus
from aifund.rules import dsl
from aifund.rules.dsl import RuleContext
from aifund.rules.engine import EMPTY, BookRule, Rulebook, RuleEngine, lesson

EVIDENCE = {
    "n_matched": 23,
    "win_matched": 0.26,
    "win_unmatched": 0.48,
    "mean_matched": -0.41,
    "mean_unmatched": 0.12,
}


def rule(
    rid: str, action: dict[str, Any], feature: str = "h1.rsi14", value: float = 70, **scope: Any
) -> dsl.Rule:
    return dsl.parse(
        {
            "rule_id": rid,
            "scope": scope,
            "conditions": {"all": [{"feature": feature, "op": ">", "value": value}]},
            "action": action,
        }
    )


def book(*rules: tuple[dsl.Rule, RuleStatus]) -> Rulebook:
    return Rulebook(version=7, rules=tuple(BookRule(r, s, EVIDENCE) for r, s in rules))


def ctx(rsi: float | None = 75.0, **over: Any) -> RuleContext:
    features = {} if rsi is None else {"h1.rsi14": rsi, "m15.atr14_pct_rank100": 0.5}
    base: dict[str, Any] = {
        "symbol": "XAUUSD", "direction": Direction.LONG, "setup_tag": "mtf_trend_pullback",
        "trigger_tf": "M15", "features": features,
    }  # fmt: skip
    return RuleContext(**{**base, **over})


PEN = {"type": "penalty", "points": 25}


def test_penalties_sum_and_cap_scales_multiply_and_floor_blocks_win() -> None:
    engine = RuleEngine(
        book(
            (rule("R-0001", PEN), RuleStatus.ACTIVE),
            (rule("R-0002", {"type": "penalty", "points": 30}), RuleStatus.ACTIVE),
            (rule("R-0003", {"type": "risk_scale", "factor": "0.5"}), RuleStatus.ACTIVE),
            (rule("R-0004", {"type": "risk_scale", "factor": "0.4"}), RuleStatus.ACTIVE),
        ),
        max_total_penalty=40,
    )
    v = engine.evaluate(ctx())
    assert (v.rulebook_version, v.penalty_points, v.risk_factor, v.blocked_by) == (7, 40, D("0.25"), None)
    assert v.active == ("R-0001v1", "R-0002v1", "R-0003v1", "R-0004v1") and v.shadow == ()  # noqa: PT018
    assert [e.action_applied for e in v.evaluations][:1] == [{"type": "penalty", "points": 25}]
    blocked = RuleEngine(book((rule("R-0009", {"type": "block"}), RuleStatus.ACTIVE)), max_total_penalty=40)
    assert blocked.evaluate(ctx()).blocked_by == "R-0009v1"


def test_shadow_rules_are_logged_but_never_enforced() -> None:
    engine = RuleEngine(book((rule("R-0005", {"type": "block"}), RuleStatus.SHADOW)), max_total_penalty=40)
    v = engine.evaluate(ctx())
    assert (v.blocked_by, v.penalty_points, v.risk_factor) == (None, 0, D(1))
    assert v.shadow == ("R-0005v1",) and v.matched == ["R-0005v1 (shadow)"]  # noqa: PT018
    (e,) = v.evaluations
    assert (e.mode, e.matched, e.action_applied) == ("SHADOW", True, {"type": "block"})


def test_out_of_scope_and_unmatched_rules_leave_no_trace_but_missing_features_do() -> None:
    engine = RuleEngine(
        book(
            (rule("R-0001", PEN, symbols=["BTCUSD"]), RuleStatus.ACTIVE),
            (rule("R-0002", PEN, value=80), RuleStatus.ACTIVE),
            (rule("R-0003", PEN, feature="m15.rsi14"), RuleStatus.ACTIVE),
        ),
        max_total_penalty=40,
    )
    v = engine.evaluate(ctx())
    assert v.active == () and v.penalty_points == 0  # noqa: PT018
    (e,) = v.evaluations
    assert (e.rule_id, e.matched, e.action_applied) == ("R-0003", False, {"missing": ["m15.rsi14"]})


def test_an_empty_rulebook_changes_nothing() -> None:
    v = RuleEngine(EMPTY, max_total_penalty=40).evaluate(ctx())
    assert (v.rulebook_version, v.penalty_points, v.risk_factor, v.matched) == (0, 0, D(1), [])


def test_a_rulebook_holds_only_enforceable_rules() -> None:
    with pytest.raises(ValueError, match="ACTIVE and SHADOW"):
        book((rule("R-0001", PEN), RuleStatus.RETIRED))


def test_lessons_are_active_rules_matched_or_near_top_eight_by_severity() -> None:
    rules = [(rule(f"R-{i:04d}", {"type": "penalty", "points": 5 + i}), RuleStatus.ACTIVE) for i in range(10)]
    rules += [
        (rule("R-0100", {"type": "block"}, value=78), RuleStatus.ACTIVE),  # 75 is within 10% of 78
        (rule("R-0200", {"type": "block"}), RuleStatus.SHADOW),  # shadow: never a lesson
        (rule("R-0300", {"type": "block"}, value=90), RuleStatus.ACTIVE),  # far from matching
    ]
    engine = RuleEngine(book(*rules), max_total_penalty=40)
    shown = [lesson(br) for br in engine.lessons([ctx(), ctx(direction=Direction.SHORT)])]
    assert len(shown) == 8
    assert shown[0].startswith("[R-0100] LONG/SHORT any setup on any symbol when h1.rsi14 > 78: 23 trades")
    assert [s.split("]")[0] for s in shown[1:3]] == ["[R-0009", "[R-0008"]
    assert engine.lessons([]) == []


def test_the_lesson_wording_is_fixed() -> None:
    r = dsl.parse(
        {
            "rule_id": "R-0042",
            "scope": {"symbols": ["XAUUSD"], "directions": ["LONG"], "setup_tags": ["mtf_trend_pullback"]},
            "conditions": {"all": [{"feature": "h1.rsi14", "op": ">", "value": 70}]},
            "action": {"type": "penalty", "points": 20},
        }
    )
    assert lesson(BookRule(r, RuleStatus.ACTIVE, EVIDENCE)) == (
        "[R-0042] LONG mtf_trend_pullback on XAUUSD when h1.rsi14 > 70: 23 trades, win 26% vs 48% baseline, "
        "expectancy -0.41R vs +0.12R. Engine: -20 confidence."
    )
    assert lesson(BookRule(r, RuleStatus.ACTIVE)) == (
        "[R-0042] LONG mtf_trend_pullback on XAUUSD when h1.rsi14 > 70. Engine: -20 confidence."
    )
