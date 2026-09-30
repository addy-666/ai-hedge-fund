"""Rule validator (roadmap 7.6, docs/04 §6): the only gate between a proposed rule and the rulebook.

The window splits by time: the oldest ``1 − holdout_fraction`` is discovery, the newest the holdout (the miner
never saw it). A candidate passes only if ALL hold:

    matches in the window           ≥ min_matches_total          (× 1.5 when > 50% of matches are virtual)
    matches in the holdout          ≥ min_matches_holdout        (× 1.5 likewise)
    effect in discovery             ≤ −min_effect_r              (mean R matched − mean R unmatched)
    effect in the holdout           ≤ −0.5 × min_effect_r        (same sign, at least half the size)
    90% CI upper of mean R matched  < 0                          (whole window)
    coverage                        ≤ max_rule_coverage          (above: a strategy-level finding, escalated)
    counterfactual                  removing matches raises holdout total R and removes ≤ 25% of the
                                    window's gross winning R
    complexity                      ≤ max_rule_conditions predicates
    dedup                           Jaccard of matched sets with every live rule < 0.8 (else: a new version
                                    of that rule; the stronger one is kept)

The validator also sets the action (the auditor's is advisory):

    mean R matched ≤ −0.6, CI upper < −0.2, n ≥ 30   → block        (always needs operator approval)
    mean R matched ≤ −0.3                              → risk_scale 0.5 (approval required)
    otherwise                                          → penalty clamp(round(−effect × 40), 5, 30)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np

from aifund.market import conditions as cond
from aifund.rules.dsl import ActionType, Rule, RuleAction, matches
from aifund.rules.miner import Sample, split
from aifund.stats import bootstrap_mean_ci

VIRTUAL_FACTOR = 1.5
MAX_WINNER_SHARE = 0.25
DUPLICATE_JACCARD = 0.8
BLOCK_MEAN, BLOCK_CI, BLOCK_N = -0.6, -0.2, 30
SCALE_MEAN = -0.3


@dataclass(frozen=True)
class ValidatorConfig:
    min_matches_total: int = 20
    min_matches_holdout: int = 6
    min_effect_r: float = 0.30
    holdout_fraction: float = 0.30
    max_rule_coverage: float = 0.30
    max_rule_conditions: int = 3
    seed: int = 0
    resamples: int = 2000


@dataclass(frozen=True)
class Validation:
    rule_id: str
    passed: bool
    failures: tuple[str, ...]
    action: RuleAction
    evidence: dict[str, Any]
    strategy_finding: str | None = None  # coverage too broad: a human decision, not a rule
    duplicate_of: str | None = None  # a live rule matching (almost) the same trades
    matched: frozenset[str] = field(default_factory=frozenset)  # sample keys

    @property
    def needs_approval(self) -> bool:
        return self.action.type in (ActionType.BLOCK, ActionType.RISK_SCALE)


def _mean(x: np.ndarray) -> float:
    return float(x.mean()) if len(x) else 0.0


def _effect(r: np.ndarray, mask: np.ndarray) -> float | None:
    if mask.sum() == 0 or (~mask).sum() == 0:
        return None
    return float(r[mask].mean() - r[~mask].mean())


def severity(mean_matched: float, ci_high: float | None, n: int, effect: float) -> RuleAction:
    if mean_matched <= BLOCK_MEAN and ci_high is not None and ci_high < BLOCK_CI and n >= BLOCK_N:
        return RuleAction(type=ActionType.BLOCK)
    if mean_matched <= SCALE_MEAN:
        return RuleAction(type=ActionType.RISK_SCALE, factor=Decimal("0.5"))
    return RuleAction(type=ActionType.PENALTY, points=min(max(round(-effect * 40), 5), 30))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def validate(
    rule: Rule,
    samples: Sequence[Sample],
    cfg: ValidatorConfig,
    *,
    live: Mapping[str, frozenset[str]] | None = None,
) -> Validation:
    """``live``: matched-sample sets of the ACTIVE/SHADOW rules (by rule id) for the dedup check; a rule being
    re-validated is left out of it by the caller."""
    discovery, holdout = split(samples, cfg.holdout_fraction)
    ordered = [*discovery, *holdout]
    r = np.asarray([s.r for s in ordered], dtype=float)
    mask = np.asarray([matches(rule, s.context()) for s in ordered], dtype=bool)
    virtual = np.asarray([s.virtual for s in ordered], dtype=bool)
    in_holdout = np.zeros(len(ordered), dtype=bool)
    in_holdout[len(discovery) :] = True
    matched_keys = frozenset(s.key for s, m in zip(ordered, mask, strict=True) if m)

    n = int(mask.sum())
    n_hold = int((mask & in_holdout).sum())
    virtual_share = float(virtual[mask].mean()) if n else 0.0
    factor = VIRTUAL_FACTOR if virtual_share > 0.5 else 1.0
    need_total = int(np.ceil(cfg.min_matches_total * factor))
    need_hold = int(np.ceil(cfg.min_matches_holdout * factor))
    matched_r = r[mask]
    mean_matched, mean_unmatched = _mean(matched_r), _mean(r[~mask])
    effect = mean_matched - mean_unmatched if n and n < len(r) else 0.0
    d = ~in_holdout
    effect_disc = _effect(r[d], mask[d])
    effect_hold = _effect(r[in_holdout], mask[in_holdout])
    ci = bootstrap_mean_ci(matched_r, seed=cfg.seed, resamples=cfg.resamples)
    coverage = n / len(r) if len(r) else 0.0
    hold_r = r[in_holdout]
    hold_total, hold_without = float(hold_r.sum()), float(hold_r[~mask[in_holdout]].sum())
    gross_wins = float(r[r > 0].sum())
    removed_wins = float(matched_r[matched_r > 0].sum())
    winner_share = removed_wins / gross_wins if gross_wins > 0 else 0.0
    predicates = len(list(cond.predicates(rule.conditions)))

    failures = []
    if n < need_total:
        failures.append(f"{n} matches < {need_total}")
    if n_hold < need_hold:
        failures.append(f"{n_hold} holdout matches < {need_hold}")
    if effect_disc is None or effect_disc > -cfg.min_effect_r:
        failures.append(f"discovery effect {_fmt(effect_disc)} > -{cfg.min_effect_r}")
    if effect_hold is None or effect_hold > -0.5 * cfg.min_effect_r:
        failures.append(f"holdout effect {_fmt(effect_hold)} > -{0.5 * cfg.min_effect_r:g}")
    if ci is None or ci[1] >= 0:
        failures.append(f"CI90 upper of mean R {_fmt(ci[1] if ci else None)} >= 0")
    if not hold_without > hold_total:
        failures.append(
            f"removing matches does not improve holdout R ({hold_total:+.2f} -> {hold_without:+.2f})"
        )
    if winner_share > MAX_WINNER_SHARE:
        failures.append(f"removes {winner_share:.0%} of gross winning R (> {MAX_WINNER_SHARE:.0%})")
    if predicates > cfg.max_rule_conditions:
        failures.append(f"{predicates} predicates > {cfg.max_rule_conditions}")
    finding = None
    if coverage > cfg.max_rule_coverage:
        failures.append(f"coverage {coverage:.0%} > {cfg.max_rule_coverage:.0%}: strategy-level finding")
        finding = (
            f"{rule.describe()} matches {coverage:.0%} of trades with mean {mean_matched:+.2f}R vs "
            f"{mean_unmatched:+.2f}R: too broad for a rule, review the setup itself"
        )
    duplicate = None
    for other, keys in sorted((live or {}).items()):
        if other != rule.rule_id and jaccard(matched_keys, keys) >= DUPLICATE_JACCARD:
            duplicate = other
            failures.append(f"matches the same trades as {other} (Jaccard >= {DUPLICATE_JACCARD})")
            break

    evidence = {
        "n_matched": n,
        "n_holdout": n_hold,
        "n_window": len(r),
        "n_virtual_matched": int((mask & virtual).sum()),
        "virtual_share": round(virtual_share, 4),
        "win_matched": round(float((matched_r > 0).mean()) if n else 0.0, 4),
        "win_unmatched": round(float((r[~mask] > 0).mean()) if n < len(r) else 0.0, 4),
        "mean_matched": round(mean_matched, 4),
        "mean_unmatched": round(mean_unmatched, 4),
        "effect": round(effect, 4),
        "effect_discovery": None if effect_disc is None else round(effect_disc, 4),
        "effect_holdout": None if effect_hold is None else round(effect_hold, 4),
        "ci_mean": None if ci is None else [round(ci[0], 4), round(ci[1], 4)],
        "coverage": round(coverage, 4),
        "holdout_total_r": round(hold_total, 4),
        "holdout_total_r_without": round(hold_without, 4),
        "winner_share_removed": round(winner_share, 4),
        "predicates": predicates,
        "window": [ordered[0].time.isoformat(), ordered[-1].time.isoformat()] if ordered else None,
        "holdout_from": holdout[0].time.isoformat() if holdout else None,
    }
    return Validation(
        rule_id=rule.rule_id,
        passed=not failures,
        failures=tuple(failures),
        action=severity(mean_matched, ci[1] if ci else None, n, effect),
        evidence=evidence,
        strategy_finding=finding,
        duplicate_of=duplicate,
        matched=matched_keys,
    )


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.3f}"
