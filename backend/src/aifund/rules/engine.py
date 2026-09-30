"""The rule engine at decision time (docs/04 §7). Pure: the rulebook is handed in, nothing is read or written.

    in_scope  = rules whose scope matches (symbol, direction, setup_tag, trigger_tf)
    matched   = in-scope rules whose condition holds on the entry snapshot (+ prop.* features)
    ACTIVE    → enforced: any block → RULE_BLOCKED; penalty = min(Σ points, max_total_penalty);
                risk_factor = max(Π factors, 0.25)
    SHADOW    → logged only (their live evidence decides promotion, §8)

Every match, and every in-scope rule that could not be evaluated because a feature was missing, becomes a
``rule_evaluations`` row. Lessons for the analyst's prompt are the ACTIVE rules in scope whose condition holds
or is within 10% of its thresholds, top 8 by severity, in the fixed wording of docs/03 §7.1 — the LLM is told
the engine enforces them, so it never double-penalises.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from aifund.domain.enums import RuleStatus
from aifund.rules.dsl import ActionType, Rule, RuleContext, in_scope, matches, missing, near

RISK_FACTOR_FLOOR = Decimal("0.25")
MAX_LESSONS = 8
ENFORCED = (RuleStatus.ACTIVE, RuleStatus.SHADOW)


@dataclass(frozen=True)
class BookRule:
    """A rule as the rulebook holds it: the DSL, its status and the evidence behind it (for lessons)."""

    rule: Rule
    status: RuleStatus
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return "ACTIVE" if self.status is RuleStatus.ACTIVE else "SHADOW"


@dataclass(frozen=True)
class Rulebook:
    version: int
    rules: tuple[BookRule, ...] = ()

    def __post_init__(self) -> None:
        wrong = [r.rule.key for r in self.rules if r.status not in ENFORCED]
        if wrong:
            raise ValueError(f"a rulebook holds ACTIVE and SHADOW rules only, not {wrong}")


EMPTY = Rulebook(version=0)


@dataclass(frozen=True)
class Evaluation:
    """One ``rule_evaluations`` row."""

    rule_id: str
    rule_version: int
    mode: str  # ACTIVE | SHADOW
    matched: bool
    action_applied: dict[str, Any] | None  # the action for a match; {"missing": [...]} when unevaluable


@dataclass(frozen=True)
class RuleVerdict:
    rulebook_version: int = 0
    penalty_points: int = 0
    risk_factor: Decimal = Decimal(1)
    blocked_by: str | None = None
    active: tuple[str, ...] = ()  # matched ACTIVE rule keys (R-0042v1)
    shadow: tuple[str, ...] = ()  # matched SHADOW rule keys
    evaluations: tuple[Evaluation, ...] = ()

    @property
    def matched(self) -> list[str]:
        """What ``decisions.rules_matched`` records: active first, shadow marked."""
        return [*self.active, *(f"{k} (shadow)" for k in self.shadow)]


NO_RULES = RuleVerdict()


class RuleEngine:
    def __init__(self, rulebook: Rulebook, *, max_total_penalty: int) -> None:
        self.rulebook = rulebook
        self._max_penalty = max_total_penalty

    @property
    def version(self) -> int:
        return self.rulebook.version

    def evaluate(self, ctx: RuleContext) -> RuleVerdict:
        evaluations: list[Evaluation] = []
        active: list[BookRule] = []
        shadow: list[BookRule] = []
        for br in self.rulebook.rules:
            r = br.rule
            if not in_scope(r, ctx):
                continue
            absent = missing(r, ctx)
            if absent:
                evaluations.append(
                    Evaluation(r.rule_id, r.version, br.mode, False, {"missing": sorted(absent)})
                )
                continue
            if not matches(r, ctx):
                continue
            (active if br.status is RuleStatus.ACTIVE else shadow).append(br)
            evaluations.append(
                Evaluation(
                    r.rule_id, r.version, br.mode, True, r.action.model_dump(mode="json", exclude_none=True)
                )
            )
        penalty = sum(
            br.rule.action.points or 0 for br in active if br.rule.action.type is ActionType.PENALTY
        )
        factor = Decimal(1)
        for br in active:
            if br.rule.action.type is ActionType.RISK_SCALE and br.rule.action.factor is not None:
                factor *= br.rule.action.factor
        blocking = next((br.rule.key for br in active if br.rule.action.type is ActionType.BLOCK), None)
        return RuleVerdict(
            rulebook_version=self.version,
            penalty_points=min(penalty, self._max_penalty),
            risk_factor=max(factor, RISK_FACTOR_FLOOR),
            blocked_by=blocking,
            active=tuple(br.rule.key for br in active),
            shadow=tuple(br.rule.key for br in shadow),
            evaluations=tuple(evaluations),
        )

    def lessons(self, contexts: Sequence[RuleContext]) -> list[BookRule]:
        """The analyst's lessons: ACTIVE rules matched or nearly matched by any candidate, top 8 by severity
        (render each with ``lesson``)."""
        shown: dict[str, BookRule] = {}
        for br in self.rulebook.rules:
            if br.status is RuleStatus.ACTIVE and any(near(br.rule, c) for c in contexts):
                shown[br.rule.key] = br
        ranked = sorted(shown.values(), key=lambda br: (-br.rule.action.severity, br.rule.rule_id))
        return ranked[:MAX_LESSONS]


def lesson(br: BookRule) -> str:
    """``[R-0042] LONG mtf_trend_pullback on XAUUSD when h1.rsi14 > 70: 23 trades, win 26% vs 48% baseline,
    expectancy -0.41R vs +0.12R. Engine: -20 confidence.``"""
    r, e = br.rule, br.evidence
    text = f"[{r.rule_id}] {r.describe()}"
    if "n_matched" in e:
        text += (
            f": {e['n_matched']} trades, win {float(e['win_matched']):.0%} vs {float(e['win_unmatched']):.0%}"
            f" baseline, expectancy {float(e['mean_matched']):+.2f}R vs {float(e['mean_unmatched']):+.2f}R"
        )
    return f"{text}. Engine: {r.action.describe()}."
