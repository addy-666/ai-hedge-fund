"""Learned rules (docs/04 §5): scope + a condition over registry features + a penalty-only action.

    {"rule_id": "R-0042", "version": 1,
     "scope": {"symbols": ["XAUUSD"], "directions": ["LONG"], "setup_tags": ["mtf_trend_pullback"],
               "trigger_tfs": []},
     "conditions": {"all": [{"feature": "h1.rsi14", "op": ">", "value": 70}]},
     "action": {"type": "penalty", "points": 20},
     "hypothesis": "...", "cited_clusters": ["C-..."]}

- The condition grammar is ``market/conditions.py`` (shared with entry hypotheses): entry-time registry
  features only (invariant 9), values typed and inside the feature's declared range; ``any`` one level deep.
- Scope lists are ANDed; an empty list matches everything. Symbols are canonical names (``NAS100``, not the
  broker's ``NAS100.r``): a rule survives a broker change.
- Actions only ever reduce risk (§1.4): ``penalty`` 5–30 points, ``risk_scale`` 0.25–0.75, ``block``.
- ``dsl_sha256`` hashes what a rule *does* (scope, condition, action) for dedup; id, version, hypothesis and
  citations do not change it.
- Evaluation is pure; a missing feature → no match, reported by ``missing`` for the diagnostics.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aifund.domain.decision import FeatureValue
from aifund.domain.enums import Direction
from aifund.market import conditions as cond
from aifund.market.conditions import AllOf, AnyOf, Condition, ConditionError, Op, Predicate
from aifund.market.feature_registry import FeatureSpec

RULE_ID = re.compile(r"^R-\d{4,6}$")
PENALTY_RANGE = (5, 30)
SCALE_RANGE = (Decimal("0.25"), Decimal("0.75"))
NEAR = 0.10  # a lesson is shown when every predicate holds or misses its threshold by at most 10%
# known only after stage 10 plans the stop, i.e. after the rules ran: a rule can never see them
AFTER_RULES = frozenset({"prop.sl_atr_multiple", "prop.rr_target"})


class RuleError(ValueError):
    """A rule that does not validate (the message says what)."""


class ActionType(StrEnum):
    PENALTY = "penalty"
    RISK_SCALE = "risk_scale"
    BLOCK = "block"


class RuleAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ActionType
    points: int | None = None
    factor: Decimal | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        lo, hi = PENALTY_RANGE
        if self.type is ActionType.PENALTY:
            if self.points is None or not lo <= self.points <= hi or self.factor is not None:
                raise ValueError(f"penalty needs points {lo}..{hi} (and no factor)")
        elif self.type is ActionType.RISK_SCALE:
            if self.factor is None or not SCALE_RANGE[0] <= self.factor <= SCALE_RANGE[1] or self.points:
                raise ValueError(
                    f"risk_scale needs factor {SCALE_RANGE[0]}..{SCALE_RANGE[1]} (and no points)"
                )
        elif self.points is not None or self.factor is not None:
            raise ValueError("block takes no points or factor")
        return self

    @property
    def severity(self) -> int:
        """Ordering for lessons and reports: block > risk_scale > penalty (then by size)."""
        if self.type is ActionType.BLOCK:
            return 1000
        if self.type is ActionType.RISK_SCALE:
            return 500 + int((1 - (self.factor or Decimal(1))) * 100)
        return self.points or 0

    def describe(self) -> str:
        if self.type is ActionType.PENALTY:
            return f"-{self.points} confidence"
        if self.type is ActionType.RISK_SCALE:
            return f"risk x{self.factor}"
        return "block"


class RuleScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = ()
    directions: tuple[Direction, ...] = ()
    setup_tags: tuple[str, ...] = ()
    trigger_tfs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _directional(self) -> Self:
        if Direction.NONE in self.directions:
            raise ValueError("scope.directions: LONG and/or SHORT only")
        return self

    def describe(self) -> str:
        parts = [
            "/".join(d.value for d in self.directions) or "LONG/SHORT",
            "/".join(self.setup_tags) or "any setup",
            "on " + ("/".join(self.symbols) or "any symbol"),
        ]
        if self.trigger_tfs:
            parts.append("(" + "/".join(self.trigger_tfs) + ")")
        return " ".join(parts)


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    rule_id: str
    version: int = Field(default=1, ge=1)
    scope: RuleScope = RuleScope()
    conditions: AllOf | AnyOf
    action: RuleAction
    hypothesis: str = Field(default="", max_length=1000)
    cited_clusters: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _id(self) -> Self:
        if not RULE_ID.match(self.rule_id):
            raise ValueError(f"rule_id {self.rule_id!r} is not like R-0042")
        return self

    @property
    def key(self) -> str:
        return f"{self.rule_id}v{self.version}"

    def describe(self) -> str:
        return f"{self.scope.describe()} when {cond.describe(self.conditions)}"


def parse(data: Mapping[str, Any]) -> Rule:
    """A rule from its JSON form (RuleError, not ValidationError, on bad input)."""
    try:
        return Rule.model_validate(dict(data))
    except ValidationError as exc:
        raise RuleError(_first_error(exc)) from None


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err["loc"]) or "rule"
    return f"{where}: {err['msg']}"


def dump(rule: Rule) -> dict[str, Any]:
    data: dict[str, Any] = rule.model_dump(mode="json", by_alias=True, exclude_none=True)
    return data


def dsl_sha256(rule: Rule) -> str:
    body = dump(rule)
    return cond.sha256({k: body[k] for k in ("scope", "conditions", "action")})


# ------------------------------------------------------------------ validation


def validate(
    rule: Rule,
    *,
    max_conditions: int,
    symbols: Collection[str] | None = None,
    setup_tags: Collection[str] | None = None,
    observed: Mapping[str, tuple[float, float]] | None = None,
) -> None:
    """RuleError on the first problem: grammar and registry (via ``conditions.check``), scope values that do
    not exist, and numeric thresholds outside the range the data has actually shown (``observed``)."""
    try:
        cond.check(rule.conditions, max_predicates=max_conditions, allow=_before_rules)
    except ConditionError as exc:
        raise RuleError(str(exc)) from None
    for name, values, known in (
        ("symbols", rule.scope.symbols, symbols),
        ("setup_tags", rule.scope.setup_tags, setup_tags),
    ):
        unknown = sorted(set(values) - set(known)) if known is not None else []
        if unknown:
            raise RuleError(f"scope.{name}: unknown {unknown}")
    for p in cond.predicates(rule.conditions):
        span = (observed or {}).get(p.feature)
        if span is None or p.op not in cond.ORDERING:
            continue
        lo, hi = span
        for v in p.value if isinstance(p.value, list) else [p.value]:
            if not lo <= float(v) <= hi:  # numeric: check() passed
                raise RuleError(f"{p.feature} {p.op.value} {v}: outside the observed range {lo:g}..{hi:g}")


def _before_rules(spec: FeatureSpec) -> str | None:
    return "known only after the stop is planned (after the rules run)" if spec.name in AFTER_RULES else None


# ------------------------------------------------------------------ evaluation


def proposal_features(
    direction: Direction, setup_tag: str | None, confidence: int | None, features: Mapping[str, FeatureValue]
) -> dict[str, FeatureValue]:
    """The ``prop.*`` features a rule may use, from the candidate trade (docs/04 §5)."""
    score = features.get("ctx.htf_trend_score")
    sign = 1 if direction is Direction.LONG else -1
    return {
        "prop.direction": direction.value,
        "prop.setup_tag": setup_tag,
        "prop.llm_confidence": confidence,
        "prop.htf_alignment": score * sign
        if isinstance(score, int) and not isinstance(score, bool)
        else None,
    }


@dataclass(frozen=True)
class RuleContext:
    """What a rule sees at decision time: the candidate trade and its entry snapshot."""

    symbol: str  # canonical
    direction: Direction
    setup_tag: str | None
    trigger_tf: str
    features: Mapping[str, FeatureValue]


def in_scope(rule: Rule, ctx: RuleContext) -> bool:
    s = rule.scope
    return (
        (not s.symbols or ctx.symbol in s.symbols)
        and (not s.directions or ctx.direction in s.directions)
        and (not s.setup_tags or ctx.setup_tag in s.setup_tags)
        and (not s.trigger_tfs or ctx.trigger_tf in s.trigger_tfs)
    )


def matches(rule: Rule, ctx: RuleContext) -> bool:
    return in_scope(rule, ctx) and cond.evaluate(rule.conditions, ctx.features)


def missing(rule: Rule, ctx: RuleContext) -> set[str]:
    return cond.missing(rule.conditions, ctx.features)


def near(rule: Rule, ctx: RuleContext, tolerance: float = NEAR) -> bool:
    """In scope, and every predicate holds or a numeric one misses by at most ``tolerance`` of its threshold
    (docs/04 §7: lessons for conditions that are matched or close to it)."""
    if not in_scope(rule, ctx) or cond.missing(rule.conditions, ctx.features):
        return False
    features = {k: v for k, v in ctx.features.items() if v is not None}

    def close(p: Predicate) -> bool:
        if cond.evaluate(AllOf(all_=[p]), features):
            return True
        if p.op not in cond.ORDERING:
            return False
        x = float(features[p.feature])
        targets = [float(v) for v in p.value] if isinstance(p.value, list) else [float(p.value)]
        return any(abs(x - t) <= tolerance * max(abs(t), 1e-9) for t in targets)

    c: Condition = rule.conditions
    if isinstance(c, AnyOf):
        return any(close(p) for p in c.any_)
    return all(
        any(close(q) for q in item.any_) if isinstance(item, AnyOf) else close(item) for item in c.all_
    )


__all__ = [
    "ActionType",
    "AllOf",
    "AnyOf",
    "Op",
    "Predicate",
    "Rule",
    "RuleAction",
    "RuleContext",
    "RuleError",
    "RuleScope",
    "dsl_sha256",
    "dump",
    "in_scope",
    "matches",
    "missing",
    "near",
    "parse",
    "proposal_features",
    "validate",
]
