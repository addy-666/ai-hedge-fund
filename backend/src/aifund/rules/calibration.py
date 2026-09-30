"""Confidence calibration (roadmap 8.5, docs/04 §9): does an LLM's "80" mean an 80% chance of a win?

Samples are (raw confidence 0-100, win = the trade's net R > 0, decision time) for real and virtual trades of
one source (the analyst, or the committee). The fit is isotonic regression (pool-adjacent-violators) of the
win rate on the confidence, on the OLDEST ``1 - holdout`` of the samples; the newest ``holdout`` is kept out
of the fit to judge it by the Brier score (mean squared error of a probability):

    before = Brier of confidence / 100        after = Brier of the fitted map
    a model is a CANDIDATE only with >= ``min_samples`` samples and after <= before x (1 - min_improvement)

The fitted map interpolates linearly between the fitted points and maps back to 0-100. Outside the range of
confidences it was fitted on, it may only LOWER a confidence (``min(raw, value at the nearest end)``): a map
never raises a confidence where it has no data. Pure: no I/O. Identity until a model is active.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Any

IDENTITY = "IDENTITY"
ISOTONIC = "ISOTONIC"


@dataclass(frozen=True)
class Sample:
    confidence: int
    win: bool
    time: datetime


class FitResult(StrEnum):
    TOO_FEW = "TOO_FEW"  # below min_samples: identity stays
    NO_IMPROVEMENT = "NO_IMPROVEMENT"  # the fit does not beat the raw confidence on the held-out samples
    CANDIDATE = "CANDIDATE"


@dataclass(frozen=True)
class IsotonicMap:
    """Fitted (confidence, win probability) points, ascending in both."""

    points: tuple[tuple[int, float], ...]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("an isotonic map needs at least one point")
        xs = [x for x, _ in self.points]
        ys = [y for _, y in self.points]
        if xs != sorted(set(xs)) or ys != sorted(ys) or not all(0 <= y <= 1 for y in ys):
            raise ValueError("points must be strictly increasing in confidence, non-decreasing in [0, 1]")

    def probability(self, raw: float) -> float:
        pts = self.points
        if raw <= pts[0][0]:
            return pts[0][1]
        if raw >= pts[-1][0]:
            return pts[-1][1]
        for (x0, y0), (x1, y1) in pairwise(pts):
            if x0 <= raw <= x1:
                return y0 + (y1 - y0) * (raw - x0) / (x1 - x0)
        raise AssertionError("unreachable: raw lies between the first and last point")  # pragma: no cover

    def __call__(self, raw: int) -> int:
        mapped = round(100 * self.probability(raw))
        lo, hi = self.points[0][0], self.points[-1][0]
        if raw < lo or raw > hi:
            return min(raw, mapped)  # no data out here: never raise the confidence
        return mapped

    def params(self) -> dict[str, Any]:
        return {"points": [[x, y] for x, y in self.points]}

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> IsotonicMap:
        return cls(tuple((int(x), float(y)) for x, y in params["points"]))


@dataclass(frozen=True)
class Fit:
    result: FitResult
    n: int
    n_train: int
    n_test: int
    brier_before: float | None = None
    brier_after: float | None = None
    model: IsotonicMap | None = None

    @property
    def improvement(self) -> float | None:
        if self.brier_before is None or self.brier_after is None or self.brier_before == 0:
            return None
        return (self.brier_before - self.brier_after) / self.brier_before


@dataclass(frozen=True)
class Bin:
    lo: int
    hi: int
    n: int
    mean_confidence: float | None
    win_rate: float | None


@dataclass
class _Block:
    first: int
    last: int
    wins: int
    n: int

    @property
    def rate(self) -> float:
        return self.wins / self.n


def pav(samples: Sequence[Sample]) -> IsotonicMap:
    """Isotonic (non-decreasing) least-squares fit of win on confidence: pool adjacent violators."""
    by_x: dict[int, _Block] = {}
    for s in samples:
        block = by_x.setdefault(s.confidence, _Block(s.confidence, s.confidence, 0, 0))
        block.wins += int(s.win)
        block.n += 1
    blocks: list[_Block] = []
    for x in sorted(by_x):
        blocks.append(by_x[x])
        while len(blocks) > 1 and blocks[-2].rate > blocks[-1].rate:  # a violation: pool the two
            merged = blocks.pop()
            blocks[-1] = _Block(
                blocks[-1].first, merged.last, blocks[-1].wins + merged.wins, blocks[-1].n + merged.n
            )
    points: list[tuple[int, float]] = []
    for b in blocks:
        points.append((b.first, b.rate))
        if b.last != b.first:
            points.append((b.last, b.rate))
    return IsotonicMap(tuple(points))


def brier(probabilities: Sequence[float], wins: Sequence[bool]) -> float:
    return sum((p - float(w)) ** 2 for p, w in zip(probabilities, wins, strict=True)) / len(wins)


def fit(
    samples: Sequence[Sample], *, min_samples: int = 150, holdout: float = 0.30, min_improvement: float = 0.05
) -> Fit:
    ordered = sorted(samples, key=lambda s: s.time)
    n = len(ordered)
    n_test = round(n * holdout)
    if n < min_samples or n_test < 1 or n_test >= n:
        return Fit(FitResult.TOO_FEW, n, n - n_test, n_test)
    train, test = ordered[: n - n_test], ordered[n - n_test :]
    model = pav(train)
    wins = [s.win for s in test]
    before = brier([s.confidence / 100 for s in test], wins)
    after = brier([model.probability(s.confidence) for s in test], wins)
    result = FitResult.CANDIDATE if after <= before * (1 - min_improvement) else FitResult.NO_IMPROVEMENT
    return Fit(result, n, len(train), n_test, round(before, 6), round(after, 6), model)


def reliability(samples: Sequence[Sample], bins: int = 10) -> list[Bin]:
    """The reliability diagram: per confidence bin, how often the trades actually won."""
    width = 100 // bins
    out = []
    for i in range(bins):
        lo, hi = i * width, 100 if i == bins - 1 else (i + 1) * width - 1
        inside = [s for s in samples if lo <= s.confidence <= hi]
        out.append(
            Bin(
                lo=lo,
                hi=hi,
                n=len(inside),
                mean_confidence=round(sum(s.confidence for s in inside) / len(inside), 2) if inside else None,
                win_rate=round(sum(s.win for s in inside) / len(inside), 4) if inside else None,
            )
        )
    return out
