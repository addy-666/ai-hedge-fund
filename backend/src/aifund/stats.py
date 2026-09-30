"""Statistics of R-multiples (docs/09 §3, docs/04 §3/§6). Pure numpy, deterministic given the seed. Shared by
research (hypotheses) and rules (the learning loop), so it sits at the bottom of the layers.

- expectancy (mean R), median, win rate (R > 0), profit factor, total, max drawdown of the cumulative R curve
  in time order, mean R per calendar month;
- a percentile **bootstrap CI of the mean** (``confidence``, default 90%) and a **one-sided p-value** for
  mean > 0 from the bootstrap of the centred sample: p = (#{centred means >= observed mean} + 1) / (B + 1).

A 90% two-sided interval puts 5% in each tail, so "CI lower bound > 0" is a one-sided 5% test; with pure
noise it fires about 1 time in 20 — which is why the trial ledger corrects for how many hypotheses were tried.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import numpy as np

CHUNK = 250  # bootstrap resamples per batch (bounds memory for large samples)


@dataclass(frozen=True)
class Summary:
    n: int
    mean: float
    median: float
    win_rate: float
    profit_factor: float | None  # None when there are no losing trades
    total: float
    max_drawdown: float  # >= 0, in R
    ci_low: float | None  # None below 2 samples
    ci_high: float | None
    p_value: float  # one-sided, mean > 0
    monthly: dict[str, float] = field(default_factory=dict)  # "YYYY-MM" -> mean R

    @property
    def significant(self) -> bool:
        """CI lower bound above zero (a one-sided test at (1 - confidence) / 2)."""
        return self.ci_low is not None and self.ci_low > 0

    def render(self) -> str:
        ci = "n/a" if self.ci_low is None else f"[{self.ci_low:+.3f}, {self.ci_high:+.3f}]"
        pf = "inf" if self.profit_factor is None else f"{self.profit_factor:.2f}"
        return (
            f"n={self.n} mean={self.mean:+.3f}R CI90={ci} p={self.p_value:.3f} win={self.win_rate:.0%} "
            f"PF={pf} total={self.total:+.2f}R maxDD={self.max_drawdown:.2f}R"
        )


EMPTY = Summary(0, 0.0, 0.0, 0.0, None, 0.0, 0.0, None, None, 1.0)


def _bootstrap_means(x: np.ndarray, resamples: int, rng: np.random.Generator) -> np.ndarray:
    out = np.empty(resamples)
    done = 0
    while done < resamples:
        k = min(CHUNK, resamples - done)
        idx = rng.integers(0, len(x), size=(k, len(x)))
        out[done : done + k] = x[idx].mean(axis=1)
        done += k
    return out


def summarize(
    r: Sequence[float | Decimal],
    *,
    times: Sequence[datetime] | None = None,
    seed: int = 0,
    resamples: int = 2000,
    confidence: float = 0.90,
) -> Summary:
    if not 0 < confidence < 1:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if times is not None and len(times) != len(r):
        raise ValueError("times and r must have the same length")
    x = np.array([float(v) for v in r], dtype=float)
    n = len(x)
    if n == 0:
        return EMPTY
    order = np.argsort(np.array([t.timestamp() for t in times])) if times is not None else np.arange(n)
    curve = np.cumsum(x[order])
    drawdown = float(np.max(np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:] - curve))
    gains, losses = x[x > 0].sum(), -x[x < 0].sum()
    monthly: dict[str, float] = {}
    if times is not None:
        groups: defaultdict[str, list[float]] = defaultdict(list)
        for t, v in zip(times, x, strict=True):
            groups[f"{t:%Y-%m}"].append(float(v))
        monthly = {k: float(np.mean(v)) for k, v in sorted(groups.items())}
    mean = float(x.mean())
    ci_low = ci_high = None
    p_value = 1.0
    if n >= 2:
        rng = np.random.default_rng(seed)
        means = _bootstrap_means(x, resamples, rng)
        tail = (1 - confidence) / 2 * 100
        ci_low, ci_high = (float(v) for v in np.percentile(means, [tail, 100 - tail]))
        centred = _bootstrap_means(x - mean, resamples, rng)
        p_value = float((np.sum(centred >= mean) + 1) / (resamples + 1))
    return Summary(
        n=n,
        mean=mean,
        median=float(np.median(x)),
        win_rate=float(np.mean(x > 0)),
        profit_factor=float(gains / losses) if losses > 0 else None,
        total=float(x.sum()),
        max_drawdown=max(drawdown, 0.0),
        ci_low=ci_low,
        ci_high=ci_high,
        p_value=p_value,
        monthly=monthly,
    )


def benjamini_hochberg(p_values: Mapping[str, float], q: float) -> set[str]:
    """Keys whose null is rejected at false-discovery rate ``q``."""
    if not 0 < q < 1:
        raise ValueError(f"q must be in (0, 1), got {q}")
    ranked = sorted(p_values.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(ranked)
    cutoff = 0
    for k, (_, p) in enumerate(ranked, start=1):
        if p <= k / m * q:
            cutoff = k
    return {key for key, _ in ranked[:cutoff]}


def bootstrap_mean_ci(
    x: Sequence[float] | np.ndarray, *, seed: int = 0, resamples: int = 2000, confidence: float = 0.90
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean (None below 2 samples)."""
    arr = np.asarray(x, dtype=float)
    if len(arr) < 2:
        return None
    means = _bootstrap_means(arr, resamples, np.random.default_rng(seed))
    tail = (1 - confidence) / 2 * 100
    lo, hi = np.percentile(means, [tail, 100 - tail])
    return float(lo), float(hi)


def bootstrap_diff_ci(
    a: Sequence[float] | np.ndarray,
    b: Sequence[float] | np.ndarray,
    *,
    seed: int = 0,
    resamples: int = 2000,
    confidence: float = 0.90,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of mean(a) − mean(b), each group resampled on its own (None if either < 2)."""
    xa, xb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(xa) < 2 or len(xb) < 2:
        return None
    rng = np.random.default_rng(seed)
    diffs = _bootstrap_means(xa, resamples, rng) - _bootstrap_means(xb, resamples, rng)
    tail = (1 - confidence) / 2 * 100
    lo, hi = np.percentile(diffs, [tail, 100 - tail])
    return float(lo), float(hi)


def permutation_z_p_lower(x: Sequence[float] | np.ndarray, mask: Sequence[bool] | np.ndarray) -> float:
    """One-sided p that mean(x[mask]) − mean(x[~mask]) is this low under random relabelling, from the exact
    permutation variance of the difference, σ²·n² / (k·(n−k)·(n−1)) (σ² with divisor n), and a normal tail
    with a continuity correction of half the smallest step the difference can take (trade outcomes cluster
    at a few R values; without it the tail is anti-conservative by a third at k = 20).

    Why not Monte Carlo: B permutations cannot resolve p below 1/(B+1), while Benjamini-Hochberg over m tests
    needs p <= q/m for its first discovery — with thousands of mined bins no pattern could ever survive.
    """
    arr = np.asarray(x, dtype=float)
    m = np.asarray(mask, dtype=bool)
    k, n = int(m.sum()), len(arr)
    if k == 0 or k == n or n < 3:
        return 1.0
    var_pop = float(arr.var())
    if var_pop == 0:
        return 1.0
    effect = float(arr[m].mean() - arr[~m].mean())
    scale = n / (k * (n - k))  # swapping two members of different value v, w moves the effect by |v−w|·scale
    sd = math.sqrt(var_pop * n * scale / (n - 1))
    gaps = np.diff(np.unique(arr))
    half_step = float(gaps.min()) * scale / 2  # continuity correction: R outcomes cluster at -1R, +rr...
    return 0.5 * math.erfc(-(effect + half_step) / sd / math.sqrt(2))  # Φ((effect + step/2) / sd)
