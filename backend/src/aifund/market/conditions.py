"""Condition grammar over registry features (docs/04 §5, docs/09 §5).

One grammar for two users: entry hypotheses (``strategies/dsl_detector.py``, roadmap R.5) and learned rules
(``rules/dsl.py``, roadmap 7.1). It lives in ``market`` because both layers sit above it and may not import
each other.

    {"all": [{"feature": "h4.ema50_above_ema200", "op": "==", "value": true},
             {"any": [{"feature": "m15.rsi14", "op": "<", "value": 35},
                      {"feature": "m15.stoch_cross_up", "op": "==", "value": true}]}]}

- ``all`` (AND) of predicates and ``any`` groups; ``any`` (OR) holds predicates only (one level deep).
- ops: ``<  <=  >  >=  ==  !=  in  not_in  between`` (``value: [lo, hi]``, inclusive).
- features must exist in the registry with ``available_at_entry=True`` (invariant 9); values must match the
  feature's type (numbers for numeric features, booleans for flags, declared categories), and thresholds
  must lie inside the feature's declared range.
- evaluation is pure; a referenced feature that is missing (absent or null) → the condition is False.
- ``canonical``/``sha256``: sorted keys, numbers normalised (35 == 35.0), for dedup and evidence hashes.
- ``mirror``: the SHORT-side condition of a LONG condition, via the registry's declared feature mirrors.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aifund.domain.decision import FeatureValue
from aifund.market import feature_registry as reg
from aifund.market.feature_registry import CATEGORY_MIRROR, FeatureSpec, FeatureType, MirrorKind

Scalar = bool | int | float | str


class ConditionError(ValueError):
    """A condition that does not validate against the registry (the message says what and where)."""


class Op(StrEnum):
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    EQ = "=="
    NE = "!="
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"


ORDERING = {Op.LT, Op.LE, Op.GT, Op.GE, Op.BETWEEN}
FLIPPED = {Op.LT: Op.GT, Op.LE: Op.GE, Op.GT: Op.LT, Op.GE: Op.LE}


class Predicate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    feature: str
    op: Op
    value: Scalar | list[Scalar]


class AnyOf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    any_: list[Predicate] = Field(alias="any", min_length=1)


class AllOf(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    all_: list[Predicate | AnyOf] = Field(alias="all", min_length=1)


Condition = AllOf | AnyOf


def predicates(cond: Condition) -> Iterator[Predicate]:
    items = cond.all_ if isinstance(cond, AllOf) else cond.any_
    for item in items:
        if isinstance(item, AnyOf):
            yield from item.any_
        else:
            yield item


def features_of(cond: Condition) -> set[str]:
    return {p.feature for p in predicates(cond)}


# ------------------------------------------------------------------ validation


FeatureFilter = Callable[[FeatureSpec], str | None]  # extra restriction: a reason to refuse, or None


def check(cond: Condition, *, max_predicates: int, allow: FeatureFilter | None = None) -> None:
    """Raise ConditionError on the first problem; the message names the predicate."""
    preds = list(predicates(cond))
    if len(preds) > max_predicates:
        raise ConditionError(f"{len(preds)} predicates > max {max_predicates}")
    for p in preds:
        _check_predicate(p, allow)


def _check_predicate(p: Predicate, allow: FeatureFilter | None) -> None:
    where = f"{p.feature} {p.op.value} {p.value!r}"
    try:
        spec = reg.get(p.feature)
    except KeyError:
        raise ConditionError(f"{where}: unknown feature {p.feature!r}") from None
    if not spec.available_at_entry:
        raise ConditionError(f"{where}: {p.feature} is not available at entry time")
    reason = allow(spec) if allow is not None else None
    if reason is not None:
        raise ConditionError(f"{where}: {reason}")
    values = _operands(p, where)
    if spec.dtype in (FeatureType.BOOL, FeatureType.CATEGORY) and p.op in ORDERING:
        raise ConditionError(f"{where}: {p.op.value} needs a numeric feature ({p.feature} is {spec.dtype})")
    for v in values:
        _check_value(spec, v, where)
    if p.op is Op.BETWEEN and float(values[0]) > float(values[1]):
        raise ConditionError(f"{where}: between needs lo <= hi")


def _operands(p: Predicate, where: str) -> list[Scalar]:
    listed = isinstance(p.value, list)
    if p.op is Op.BETWEEN:
        if not listed or len(p.value) != 2:  # type: ignore[arg-type]
            raise ConditionError(f"{where}: between needs [lo, hi]")
    elif p.op in (Op.IN, Op.NOT_IN):
        if not listed or not p.value:
            raise ConditionError(f"{where}: {p.op.value} needs a non-empty list")
    elif listed:
        raise ConditionError(f"{where}: {p.op.value} needs a single value")
    return list(p.value) if isinstance(p.value, list) else [p.value]


def _check_value(spec: FeatureSpec, v: Scalar, where: str) -> None:
    if spec.dtype is FeatureType.BOOL:
        if not isinstance(v, bool):
            raise ConditionError(f"{where}: {spec.name} is a flag; use true/false")
        return
    if spec.dtype is FeatureType.CATEGORY:
        if spec.categories is not None and v not in spec.categories:
            raise ConditionError(f"{where}: {v!r} is not one of {list(spec.categories)}")
        if not isinstance(v, str):
            raise ConditionError(f"{where}: {spec.name} is a category; use one of its names")
        return
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise ConditionError(f"{where}: {spec.name} is numeric; use a finite number")
    bounds = spec.bounds
    if bounds is not None and not bounds[0] <= v <= bounds[1]:
        raise ConditionError(f"{where}: {v} is outside {spec.name}'s range {bounds[0]:g}..{bounds[1]:g}")


# ------------------------------------------------------------------ evaluation


def missing(cond: Condition, features: Mapping[str, FeatureValue]) -> set[str]:
    return {name for name in features_of(cond) if features.get(name) is None}


def evaluate(cond: Condition, features: Mapping[str, FeatureValue]) -> bool:
    """Pure. Any referenced feature missing → False (no signal / no rule match, never a guess)."""
    if missing(cond, features):
        return False
    if isinstance(cond, AnyOf):
        return any(_holds(p, features[p.feature]) for p in cond.any_)
    return all(
        any(_holds(q, features[q.feature]) for q in item.any_)
        if isinstance(item, AnyOf)
        else _holds(item, features[item.feature])
        for item in cond.all_
    )


def _holds(p: Predicate, x: FeatureValue) -> bool:
    if p.op in ORDERING:
        v = float(x)  # type: ignore[arg-type]  # validated: numeric feature
        if p.op is Op.BETWEEN:
            lo, hi = _pair(p.value)
            return float(lo) <= v <= float(hi)
        t = float(p.value)  # type: ignore[arg-type]
        return {Op.LT: v < t, Op.LE: v <= t, Op.GT: v > t, Op.GE: v >= t}[p.op]
    if p.op in (Op.IN, Op.NOT_IN):
        inside = any(_equal(x, v) for v in p.value)  # type: ignore[union-attr]
        return inside if p.op is Op.IN else not inside
    same = _equal(x, p.value)  # type: ignore[arg-type]
    return same if p.op is Op.EQ else not same


def _pair(value: Scalar | list[Scalar]) -> tuple[Scalar, Scalar]:
    assert isinstance(value, list) and len(value) == 2  # noqa: PT018 - validated by check()
    return value[0], value[1]


def _equal(x: FeatureValue, v: Scalar) -> bool:
    if isinstance(x, bool) or isinstance(v, bool):
        return isinstance(x, bool) and isinstance(v, bool) and x is v  # True never equals 1
    if isinstance(x, str) or isinstance(v, str):
        return x == v
    return float(x) == float(v)  # type: ignore[arg-type]


# ------------------------------------------------------------------ canonical form


def _normal(obj: Any) -> Any:
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int | float):
        return float(obj)  # 35 and 35.0 are the same threshold
    if isinstance(obj, Mapping):
        return {str(k): _normal(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_normal(v) for v in obj]
    raise TypeError(f"not canonicalisable: {type(obj).__name__}")


def canonical(obj: Any) -> str:
    """JSON with sorted keys and float numbers; conditions are dumped by alias (``all``/``any``)."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json", by_alias=True)
    return json.dumps(_normal(obj), sort_keys=True, separators=(",", ":"))


def sha256(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


# ------------------------------------------------------------------ mirroring


def mirror(cond: Condition) -> Condition:
    """The same condition for the opposite direction: holds on a market iff ``cond`` holds on its price
    reflection. ConditionError if a feature has no declared mirror (write that side explicitly)."""
    if isinstance(cond, AnyOf):
        return AnyOf(any_=[mirror_predicate(p) for p in cond.any_])
    return AllOf(
        all_=[
            AnyOf(any_=[mirror_predicate(q) for q in item.any_])
            if isinstance(item, AnyOf)
            else mirror_predicate(item)
            for item in cond.all_
        ]
    )


def mirror_predicate(p: Predicate) -> Predicate:
    found = reg.mirror_of(p.feature)
    if found is None:
        raise ConditionError(f"{p.feature} has no declared mirror: write the short side explicitly")
    partner, kind = found
    op, value = p.op, p.value
    if kind in (MirrorKind.NEGATE, MirrorKind.COMPLEMENT):
        # the short side's partner value y satisfies T(y) op x, T decreasing and self-inverse → y op' T(x)
        def t(v: Scalar) -> Scalar:
            n = -float(v) if kind is MirrorKind.NEGATE else 100.0 - float(v)
            return n + 0.0  # no negative zero in canonical forms

        if op is Op.BETWEEN:
            lo, hi = _pair(value)
            value = [t(hi), t(lo)]
        elif isinstance(value, list):
            value = [t(v) for v in value]
        else:
            value = t(value)
        op = FLIPPED.get(op, op)
    elif kind is MirrorKind.NOT:
        value = [not v for v in value] if isinstance(value, list) else not value
    elif kind is MirrorKind.CATEGORY:

        def swap(v: Scalar) -> Scalar:
            return CATEGORY_MIRROR.get(v, v) if isinstance(v, str) else v

        value = [swap(v) for v in value] if isinstance(value, list) else swap(value)
    return Predicate(feature=partner, op=op, value=value)


# ------------------------------------------------------------------ text


def describe(cond: Condition) -> str:
    """Compact one-line form for prompts and reports: ``m15.rsi14 in 35..50 AND (m15.nr7 == true OR ...)``."""

    def pred(p: Predicate) -> str:
        if p.op is Op.BETWEEN:
            lo, hi = _pair(p.value)
            return f"{p.feature} in {_text(lo)}..{_text(hi)}"
        if isinstance(p.value, list):
            return f"{p.feature} {p.op.value} [{', '.join(_text(v) for v in p.value)}]"
        return f"{p.feature} {p.op.value} {_text(p.value)}"

    if isinstance(cond, AnyOf):
        return " OR ".join(pred(p) for p in cond.any_)
    parts = [
        "(" + " OR ".join(pred(q) for q in item.any_) + ")" if isinstance(item, AnyOf) else pred(item)
        for item in cond.all_
    ]
    return " AND ".join(parts)


def _text(v: Scalar) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
