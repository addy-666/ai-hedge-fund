"""Rule lifecycle decisions (roadmap 7.7, docs/04 §8)."""

from __future__ import annotations

from decimal import Decimal

from aifund.domain.enums import RuleStatus
from aifund.rules.dsl import ActionType, RuleAction
from aifund.rules.lifecycle import (
    LifecycleConfig,
    ReviewVerdict,
    ShadowEvidence,
    ShadowVerdict,
    can_transition,
    needs_approval,
    over_limit,
    review_verdict,
    shadow_verdict,
)

CFG = LifecycleConfig()
SMALL = RuleAction(type=ActionType.PENALTY, points=10)
BIG = RuleAction(type=ActionType.PENALTY, points=20)
SCALE = RuleAction(type=ActionType.RISK_SCALE, factor=Decimal("0.5"))
BLOCK = RuleAction(type=ActionType.BLOCK)


def test_blocks_scales_and_big_penalties_need_the_operator() -> None:
    assert [needs_approval(a, CFG) for a in (SMALL, BIG, SCALE, BLOCK)] == [False, True, True, True]


def test_shadow_promotion_rejection_and_waiting() -> None:
    losing = ShadowEvidence(12, -0.4, 10)
    assert shadow_verdict(SMALL, losing, CFG)[0] is ShadowVerdict.PROMOTE
    assert shadow_verdict(BLOCK, losing, CFG)[0] is ShadowVerdict.AWAIT_APPROVAL
    assert shadow_verdict(BLOCK, losing, CFG, approved=True)[0] is ShadowVerdict.PROMOTE
    verdict, why = shadow_verdict(SMALL, ShadowEvidence(12, 0.2, 10), CFG)
    assert verdict is ShadowVerdict.REJECT and "win" in why  # noqa: PT018
    assert shadow_verdict(SMALL, ShadowEvidence(12, 0.05, 10), CFG)[0] is ShadowVerdict.KEEP
    verdict, why = shadow_verdict(SMALL, ShadowEvidence(12, 0.05, 31), CFG)
    assert verdict is ShadowVerdict.REJECT and "do not lose" in why  # noqa: PT018
    verdict, why = shadow_verdict(SMALL, ShadowEvidence(3, -1.0, 30), CFG)
    assert verdict is ShadowVerdict.REJECT and "3 shadow matches in 30 days" in why  # noqa: PT018
    assert shadow_verdict(SMALL, ShadowEvidence(0, None, 5), CFG)[0] is ShadowVerdict.KEEP


def test_reviews_retire_after_two_failures_in_a_row() -> None:
    assert review_verdict(True, 1) == (ReviewVerdict.RENEW, 0)
    assert review_verdict(False, 0) == (ReviewVerdict.STRIKE, 1)
    assert review_verdict(False, 1) == (ReviewVerdict.RETIRE, 2)


def test_the_weakest_rules_retire_over_the_cap() -> None:
    active = [("R-1", -0.9), ("R-2", -0.3), ("R-3", -0.5)]
    assert over_limit(active, 3) == []
    assert over_limit(active, 1) == ["R-2", "R-3"]


def test_transitions() -> None:
    assert can_transition(RuleStatus.CANDIDATE, RuleStatus.SHADOW)
    assert can_transition(RuleStatus.SHADOW, RuleStatus.ACTIVE)
    assert not can_transition(RuleStatus.ACTIVE, RuleStatus.SHADOW)
    assert not can_transition(RuleStatus.RETIRED, RuleStatus.ACTIVE)
