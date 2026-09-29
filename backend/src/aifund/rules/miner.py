"""Pattern miner (roadmap 7.4, docs/04 §3): ranked loss clusters with effect sizes, CIs and FDR control.

Deterministic given the seed. It only ever sees the DISCOVERY part of the window (the oldest
``1 - holdout_fraction`` by entry time): bin edges and every statistic come from it, so the validator's
holdout stays unseen.

    for each scope (ALL; per symbol; per setup; per symbol × direction; per setup × direction):
        for each rule-usable feature:  numeric → quintile bins; flag / category (or few-valued int) → values
            n, win rate, mean R of the bin vs the rest; effect = mean(bin) − mean(rest)
        the 10 strongest single bins (n ≥ min/2) → pairwise conjunctions (different features)
        a bin is a candidate when effect ≤ −min_effect_r and n ≥ min_matches_total:
            90% bootstrap CI of its mean R and of the effect; one-sided permutation p of the effect (exact
            permutation variance, normal tail: 1,000 Monte Carlo permutations cannot resolve the p <= q/m BH
            needs over thousands of bins)
    Benjamini-Hochberg over EVERY bin tested (untested bins count with p = 1) at q = fdr_q
    → the 20 strongest survivors, plus the 10 strongest non-survivors marked weak

A bin is a condition in the rule DSL (``market/conditions.py``), so a cluster can become a rule unchanged and
the validator re-evaluates exactly what was mined. Samples missing a feature never match its bins.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from aifund.domain.decision import FeatureValue
from aifund.domain.enums import Direction
from aifund.market import conditions as cond
from aifund.market import feature_registry as reg
from aifund.market.conditions import AllOf, Op, Predicate
from aifund.market.feature_registry import FeatureType
from aifund.rules.dsl import AFTER_RULES, RuleContext, RuleScope
from aifund.stats import benjamini_hochberg, bootstrap_diff_ci, bootstrap_mean_ci, permutation_z_p_lower

QUINTILES = (0.2, 0.4, 0.6, 0.8)
FEW_VALUES = 5  # an int feature with at most this many distinct values is binned by value
TOP_SINGLES = 10


@dataclass(frozen=True)
class Sample:
    """One finished trade or virtual trade: entry-time facts and the outcome in R."""

    key: str
    time: datetime
    symbol: str  # canonical
    direction: Direction
    setup_tag: str | None
    trigger_tf: str
    r: float
    virtual: bool
    features: Mapping[str, FeatureValue]  # the snapshot plus prop.* features
    tags: tuple[str, ...] = ()  # reviewer tags (explanations only, never conditions)

    def context(self) -> RuleContext:
        return RuleContext(self.symbol, self.direction, self.setup_tag, self.trigger_tf, self.features)


@dataclass(frozen=True)
class MinerConfig:
    min_matches_total: int = 20
    min_effect_r: float = 0.30
    fdr_q: float = 0.10
    holdout_fraction: float = 0.30
    seed: int = 0
    resamples: int = 2000
    permutations: int = 1000
    max_clusters: int = 20
    max_weak: int = 10


@dataclass(frozen=True)
class Cluster:
    id: str
    scope: RuleScope
    condition: AllOf
    n: int
    n_virtual: int
    win_rate: float
    mean_r: float
    mean_r_rest: float
    effect: float
    ci_mean: tuple[float, float] | None
    ci_effect: tuple[float, float] | None
    p_value: float
    survived: bool

    def describe(self) -> str:
        return f"{self.scope.describe()} when {cond.describe(self.condition)}"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope.model_dump(mode="json"),
            "conditions": self.condition.model_dump(mode="json", by_alias=True),
            "text": self.describe(),
            "n": self.n,
            "n_virtual": self.n_virtual,
            "win_rate": round(self.win_rate, 4),
            "mean_r": round(self.mean_r, 4),
            "mean_r_rest": round(self.mean_r_rest, 4),
            "effect": round(self.effect, 4),
            "ci_mean": _round_pair(self.ci_mean),
            "ci_effect": _round_pair(self.ci_effect),
            "p_value": round(self.p_value, 5),
            "survived": self.survived,
        }


@dataclass(frozen=True)
class Row:
    key: str
    n: int
    win_rate: float
    mean_r: float
    total_r: float


@dataclass
class MinerResult:
    n_samples: int
    n_discovery: int
    discovery_end: datetime | None
    baseline_mean_r: float
    tested: int
    clusters: list[Cluster] = field(default_factory=list)  # survivors
    weak: list[Cluster] = field(default_factory=list)
    tables: dict[str, list[Row]] = field(default_factory=dict)
    tag_frequency: dict[str, dict[str, float]] = field(default_factory=dict)  # tag -> {losses, wins}

    def to_json(self) -> dict[str, Any]:
        return {
            "n_samples": self.n_samples,
            "n_discovery": self.n_discovery,
            "discovery_end": self.discovery_end.isoformat() if self.discovery_end else None,
            "baseline_mean_r": round(self.baseline_mean_r, 4),
            "tested": self.tested,
            "clusters": [c.to_json() for c in self.clusters],
            "weak": [c.to_json() for c in self.weak],
            "tables": {k: [r.__dict__ for r in rows] for k, rows in self.tables.items()},
            "tag_frequency": self.tag_frequency,
        }


def _round_pair(p: tuple[float, float] | None) -> list[float] | None:
    return None if p is None else [round(p[0], 4), round(p[1], 4)]


def split(samples: Sequence[Sample], holdout_fraction: float) -> tuple[list[Sample], list[Sample]]:
    """(discovery, holdout) by entry time: the newest ``holdout_fraction`` is the holdout."""
    ordered = sorted(samples, key=lambda s: (s.time, s.key))
    cut = len(ordered) - math.ceil(len(ordered) * holdout_fraction)
    return ordered[:cut], ordered[cut:]


# ------------------------------------------------------------------ bins


def minable(name: str) -> bool:
    try:
        spec = reg.get(name)
    except KeyError:
        return False
    return (
        spec.available_at_entry
        and name not in AFTER_RULES
        and name not in ("prop.direction", "prop.setup_tag")
    )


def _nice(x: float) -> float:
    return float(f"{x:.4g}") + 0.0


def feature_bins(name: str, values: Sequence[FeatureValue]) -> list[Predicate]:
    """Candidate predicates for one feature over the scope's discovery values (missing values ignored)."""
    present = [v for v in values if v is not None]
    if not present:
        return []
    spec = reg.get(name)
    distinct = sorted({v for v in present}, key=str)
    if spec.dtype in (FeatureType.BOOL, FeatureType.CATEGORY) or (
        spec.dtype is FeatureType.INT and len(distinct) <= FEW_VALUES
    ):
        if len(distinct) < 2:
            return []
        return [Predicate(feature=name, op=Op.EQ, value=v) for v in distinct]
    nums = np.asarray([float(v) for v in present if not isinstance(v, str)], dtype=float)
    if len(np.unique(nums)) < 2:
        return []
    edges = sorted({_nice(float(e)) for e in np.quantile(nums, QUINTILES)})
    lo, hi = _nice(float(nums.min())), _nice(float(nums.max()))
    edges = [e for e in edges if lo < e < hi] or []
    if not edges:
        return []
    preds = [Predicate(feature=name, op=Op.LE, value=edges[0])]
    preds += [Predicate(feature=name, op=Op.BETWEEN, value=[a, b]) for a, b in itertools.pairwise(edges)]
    preds.append(Predicate(feature=name, op=Op.GE, value=edges[-1]))
    return preds


def _holds(p: Predicate, features: Mapping[str, FeatureValue]) -> bool:
    return cond.evaluate(AllOf(all_=[p]), features)


# ------------------------------------------------------------------ scopes


def scopes(samples: Sequence[Sample], min_n: int) -> list[RuleScope]:
    """ALL, per symbol, per setup, per (symbol, direction), per (setup, direction) with at least ``min_n``;
    a scope holding exactly the samples of a broader one is dropped (it would only repeat its tests)."""
    out = [RuleScope()]
    count: Counter[tuple[str, str, str]] = Counter()
    for s in samples:
        d = s.direction.value
        count[("sym", s.symbol, "")] += 1
        if s.setup_tag:
            count[("setup", s.setup_tag, "")] += 1
            count[("setup", s.setup_tag, d)] += 1
        count[("sym", s.symbol, d)] += 1
    for (kind, value, d), n in sorted(count.items()):
        if n < min_n:
            continue
        directions = (Direction(d),) if d else ()
        if kind == "sym":
            out.append(RuleScope(symbols=(value,), directions=directions))
        else:
            out.append(RuleScope(setup_tags=(value,), directions=directions))
    seen: set[frozenset[str]] = set()
    unique = []
    for scope in sorted(
        out, key=lambda sc: (len(sc.symbols) + len(sc.setup_tags) + len(sc.directions), sc.describe())
    ):
        members = frozenset(s.key for s in samples if _in(scope, s))
        if members not in seen:
            seen.add(members)
            unique.append(scope)
    return unique


def _in(scope: RuleScope, s: Sample) -> bool:
    return (
        (not scope.symbols or s.symbol in scope.symbols)
        and (not scope.directions or s.direction in scope.directions)
        and (not scope.setup_tags or s.setup_tag in scope.setup_tags)
    )


# ------------------------------------------------------------------ mining


@dataclass
class _Bin:
    scope: RuleScope
    preds: tuple[Predicate, ...]
    mask: np.ndarray
    r: np.ndarray
    virtual: np.ndarray
    effect: float
    n: int

    @property
    def condition(self) -> AllOf:
        return AllOf(all_=list(self.preds))

    @property
    def features(self) -> set[str]:
        return {p.feature for p in self.preds}


def _bin(
    scope: RuleScope, preds: tuple[Predicate, ...], mask: np.ndarray, r: np.ndarray, virtual: np.ndarray
) -> _Bin:
    n = int(mask.sum())
    effect = float(r[mask].mean() - r[~mask].mean()) if 0 < n < len(r) else 0.0
    return _Bin(scope, preds, mask, r, virtual, effect, n)


def mine(samples: Sequence[Sample], cfg: MinerConfig, *, label: str = "C") -> MinerResult:
    discovery, _ = split(samples, cfg.holdout_fraction)
    r_all = np.asarray([s.r for s in discovery], dtype=float)
    result = MinerResult(
        n_samples=len(samples),
        n_discovery=len(discovery),
        discovery_end=discovery[-1].time if discovery else None,
        baseline_mean_r=float(r_all.mean()) if len(r_all) else 0.0,
        tested=0,
    )
    result.tables, result.tag_frequency = describe(discovery)
    if len(discovery) < cfg.min_matches_total:
        return result
    names = sorted({k for s in discovery for k in s.features if minable(k)})
    bins: list[_Bin] = []
    for scope in scopes(discovery, cfg.min_matches_total):
        inside = [s for s in discovery if _in(scope, s)]
        r = np.asarray([s.r for s in inside], dtype=float)
        virtual = np.asarray([s.virtual for s in inside], dtype=bool)
        singles: list[_Bin] = []
        for name in names:
            for p in feature_bins(name, [s.features.get(name) for s in inside]):
                mask = np.asarray([_holds(p, s.features) for s in inside], dtype=bool)
                if 0 < mask.sum() < len(inside):
                    singles.append(_bin(scope, (p,), mask, r, virtual))
        strong = sorted(
            (b for b in singles if b.n >= cfg.min_matches_total / 2), key=lambda b: (-abs(b.effect), _key(b))
        )[:TOP_SINGLES]
        pairs = []
        for i, a in enumerate(strong):
            for b in strong[i + 1 :]:
                if a.features & b.features:
                    continue
                mask = a.mask & b.mask
                if 0 < mask.sum() < len(inside):
                    pairs.append(_bin(scope, a.preds + b.preds, mask, r, virtual))
        bins += singles + pairs
    result.tested = len(bins)
    candidates = {
        i for i, b in enumerate(bins) if b.effect <= -cfg.min_effect_r and b.n >= cfg.min_matches_total
    }
    p_values = {str(i): 1.0 for i in range(len(bins))}
    stats: dict[int, tuple[Any, Any, float]] = {}
    for i in sorted(candidates):
        b = bins[i]
        seed = cfg.seed + i
        ci_mean = bootstrap_mean_ci(b.r[b.mask], seed=seed, resamples=cfg.resamples)
        ci_effect = bootstrap_diff_ci(b.r[b.mask], b.r[~b.mask], seed=seed, resamples=cfg.resamples)
        p_value = permutation_z_p_lower(b.r, b.mask)
        stats[i] = (ci_mean, ci_effect, p_value)
        p_values[str(i)] = p_value
    survivors = {int(k) for k in benjamini_hochberg(p_values, cfg.fdr_q)} if bins else set()
    ranked = sorted(stats, key=lambda i: (bins[i].effect, _key(bins[i])))
    kept = [i for i in ranked if i in survivors][: cfg.max_clusters]
    weak_ids = [i for i in ranked if i not in survivors]
    weak_ids += sorted(
        (
            i
            for i, b in enumerate(bins)
            if i not in stats and b.effect < 0 and b.n >= cfg.min_matches_total / 2
        ),
        key=lambda i: (bins[i].effect, _key(bins[i])),
    )
    for n, i in enumerate(kept, start=1):
        result.clusters.append(_cluster(f"{label}-{n:02d}", bins[i], stats.get(i), survived=True))
    for n, i in enumerate(weak_ids[: cfg.max_weak], start=1):
        result.weak.append(_cluster(f"{label}-W{n:02d}", bins[i], stats.get(i), survived=False))
    return result


def _key(b: _Bin) -> str:
    return cond.canonical(
        {"scope": b.scope.model_dump(mode="json"), "c": b.condition.model_dump(mode="json", by_alias=True)}
    )


def _cluster(cid: str, b: _Bin, stats: tuple[Any, Any, float] | None, *, survived: bool) -> Cluster:
    matched = b.r[b.mask]
    ci_mean, ci_effect, p = stats if stats is not None else (None, None, 1.0)
    return Cluster(
        id=cid,
        scope=b.scope,
        condition=b.condition,
        n=b.n,
        n_virtual=int(b.virtual[b.mask].sum()),
        win_rate=float((matched > 0).mean()),
        mean_r=float(matched.mean()),
        mean_r_rest=float(b.r[~b.mask].mean()),
        effect=b.effect,
        ci_mean=ci_mean,
        ci_effect=ci_effect,
        p_value=p,
        survived=survived,
    )


# ------------------------------------------------------------------ descriptive tables


def _rows(groups: Mapping[str, list[float]]) -> list[Row]:
    return [
        Row(
            k,
            len(v),
            round(float(np.mean(np.asarray(v) > 0)), 4),
            round(float(np.mean(v)), 4),
            round(sum(v), 4),
        )
        for k, v in sorted(groups.items())
    ]


def _group(samples: Iterable[Sample], key: Any) -> list[Row]:
    groups: defaultdict[str, list[float]] = defaultdict(list)
    for s in samples:
        groups[str(key(s))].append(s.r)
    return _rows(groups)


def confidence_bucket(value: FeatureValue) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        return "unknown"
    lo = value // 10 * 10
    return f"{lo}-{lo + 9}"


def describe(samples: Sequence[Sample]) -> tuple[dict[str, list[Row]], dict[str, dict[str, float]]]:
    """Expectancy tables for the auditor and the dashboard, and reviewer-tag frequency in losses vs wins."""
    tables = {
        "symbol": _group(samples, lambda s: s.symbol),
        "setup_tag": _group(samples, lambda s: s.setup_tag),
        "direction": _group(samples, lambda s: s.direction.value),
        "session": _group(samples, lambda s: s.features.get("ctx.session")),
        "regime": _group(samples, lambda s: s.features.get("ctx.regime")),
        "htf_alignment": _group(samples, lambda s: s.features.get("prop.htf_alignment")),
        "confidence_bucket": _group(
            samples, lambda s: confidence_bucket(s.features.get("prop.llm_confidence"))
        ),
        "virtual": _group(samples, lambda s: "virtual" if s.virtual else "real"),
    }
    losses = [s for s in samples if s.r < 0]
    wins = [s for s in samples if s.r > 0]
    tags: dict[str, dict[str, float]] = {}
    for tag in sorted({t for s in samples for t in s.tags}):
        tags[tag] = {
            "losses": round(sum(tag in s.tags for s in losses) / len(losses), 4) if losses else 0.0,
            "wins": round(sum(tag in s.tags for s in wins) / len(wins), 4) if wins else 0.0,
        }
    return tables, tags
