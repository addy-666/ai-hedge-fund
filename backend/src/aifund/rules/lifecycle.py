"""Rule lifecycle decisions (roadmap 7.7, docs/04 §8). Pure: the engine's learning loop gathers the evidence,
asks here, and records the transition (new rulebook version, event, notice).

    CANDIDATE → SHADOW     the validator passed
    CANDIDATE → REJECTED   the validator failed (reasons and numbers kept: rejected ideas are knowledge)
    SHADOW → ACTIVE        ≥ shadow_min_matches live matches (real + virtual) with mean R < 0, and either a
                           penalty ≤ auto_promote_max_penalty or the operator's approval (blocks and risk
                           scales always need it)
    SHADOW → REJECTED      shadow_max_days without enough matches, or shadow mean R ≥ +0.1, or the shadow
                           period ends with enough matches that do not lose
    ACTIVE → ACTIVE        re-validation at review_at passes → review_at and expires_at move forward
    ACTIVE → RETIRED       re-validation fails twice in a row; expires_at passes without renewal; the operator
                           retires it; or max_active_rules is exceeded (the weakest retires)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from aifund.domain.enums import RuleStatus
from aifund.rules.dsl import ActionType, RuleAction

SHADOW_WINNING = 0.1  # shadow mean R at or above this rejects the rule
RETIRE_AFTER_FAILURES = 2

TRANSITIONS = {
    RuleStatus.CANDIDATE: {
        RuleStatus.SHADOW,
        RuleStatus.REJECTED,
        RuleStatus.ACTIVE,
    },  # ACTIVE: force (audited)
    RuleStatus.SHADOW: {RuleStatus.ACTIVE, RuleStatus.REJECTED, RuleStatus.RETIRED},
    RuleStatus.ACTIVE: {RuleStatus.RETIRED},
    RuleStatus.REJECTED: set(),
    RuleStatus.RETIRED: set(),
}


def can_transition(old: RuleStatus, new: RuleStatus) -> bool:
    return new in TRANSITIONS[old]


@dataclass(frozen=True)
class LifecycleConfig:
    shadow_min_matches: int = 10
    shadow_max_days: int = 30
    auto_promote_max_penalty: int = 15
    review_after_days: int = 30
    expire_after_days: int = 90
    max_active_rules: int = 25


class ShadowVerdict(StrEnum):
    KEEP = "KEEP"
    PROMOTE = "PROMOTE"
    AWAIT_APPROVAL = "AWAIT_APPROVAL"
    REJECT = "REJECT"


@dataclass(frozen=True)
class ShadowEvidence:
    n: int  # live matches with a known outcome (real or virtual)
    mean_r: float | None
    days: float  # in shadow so far


def needs_approval(action: RuleAction, cfg: LifecycleConfig) -> bool:
    if action.type in (ActionType.BLOCK, ActionType.RISK_SCALE):
        return True
    return (action.points or 0) > cfg.auto_promote_max_penalty


def shadow_verdict(
    action: RuleAction, ev: ShadowEvidence, cfg: LifecycleConfig, *, approved: bool = False
) -> tuple[ShadowVerdict, str]:
    enough = ev.n >= cfg.shadow_min_matches and ev.mean_r is not None
    if enough and ev.mean_r is not None and ev.mean_r < 0:
        if needs_approval(action, cfg) and not approved:
            return (
                ShadowVerdict.AWAIT_APPROVAL,
                f"{ev.n} shadow matches, mean {ev.mean_r:+.2f}R: needs approval",
            )
        return ShadowVerdict.PROMOTE, f"{ev.n} shadow matches, mean {ev.mean_r:+.2f}R"
    if enough and ev.mean_r is not None and ev.mean_r >= SHADOW_WINNING:
        return ShadowVerdict.REJECT, f"shadow matches win: {ev.n} with mean {ev.mean_r:+.2f}R"
    if ev.days >= cfg.shadow_max_days:
        if enough:
            return ShadowVerdict.REJECT, f"after {ev.days:.0f} days the {ev.n} shadow matches do not lose"
        return ShadowVerdict.REJECT, f"{ev.n} shadow matches in {ev.days:.0f} days < {cfg.shadow_min_matches}"
    return ShadowVerdict.KEEP, f"{ev.n} shadow matches so far"


class ReviewVerdict(StrEnum):
    RENEW = "RENEW"
    STRIKE = "STRIKE"  # the first failure: kept, reviewed again at the next review
    RETIRE = "RETIRE"


def review_verdict(passed: bool, failures_before: int) -> tuple[ReviewVerdict, int]:
    """(verdict, consecutive failures after this review)."""
    if passed:
        return ReviewVerdict.RENEW, 0
    failures = failures_before + 1
    return (ReviewVerdict.RETIRE if failures >= RETIRE_AFTER_FAILURES else ReviewVerdict.STRIKE), failures


def over_limit(active: Sequence[tuple[str, float]], max_active: int) -> list[str]:
    """Keys to retire when more than ``max_active`` rules are ACTIVE: the weakest first (the highest, i.e.
    least negative, mean R of their matches)."""
    excess = len(active) - max_active
    if excess <= 0:
        return []
    weakest = sorted(active, key=lambda kv: (-kv[1], kv[0]))
    return [key for key, _ in weakest[:excess]]
