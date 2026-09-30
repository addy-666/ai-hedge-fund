"""Confidence calibration (roadmap 8.5, docs/04 §9): PAV, the time-ordered holdout, the Brier gate, and a map
that never raises a confidence where it has no data. DoD: synthetic miscalibrated data is corrected."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from aifund.rules.calibration import FitResult, IsotonicMap, Sample, brier, fit, pav, reliability

T = datetime(2026, 1, 1, tzinfo=UTC)


def samples(n: int, true_p: object, seed: int = 7) -> list[Sample]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        c = rng.randint(40, 95)
        out.append(Sample(c, rng.random() < true_p(c), T + timedelta(hours=i)))  # type: ignore[operator]
    return out


def test_pav_pools_violators() -> None:
    data = [Sample(50, w, T) for w in (True, True, True, False, False)]  # 0.6
    data += [Sample(60, w, T) for w in (True, True, False, False, False)]  # 0.4: violates -> pooled 0.5
    data += [Sample(70, w, T) for w in (True, True, True, True, False)]  # 0.8
    model = pav(data)
    assert model.points == ((50, 0.5), (60, 0.5), (70, 0.8))
    assert model(65) == 65  # halfway between 0.5 and 0.8
    assert model.probability(55) == 0.5


def test_an_overconfident_llm_is_corrected() -> None:
    data = samples(600, lambda c: max(0.05, c / 100 - 0.30))  # says 80, wins 50%
    result = fit(data)
    assert result.result is FitResult.CANDIDATE
    assert (result.n, result.n_train, result.n_test) == (600, 420, 180)
    assert result.brier_after is not None and result.brier_before is not None  # noqa: PT018
    assert result.improvement is not None and result.improvement >= 0.05  # noqa: PT018
    model = result.model
    assert model is not None
    assert 40 <= model(80) <= 60
    assert model(50) <= 30


def test_a_well_calibrated_llm_is_left_alone() -> None:
    result = fit(samples(600, lambda c: c / 100))
    assert result.result is FitResult.NO_IMPROVEMENT
    assert result.model is not None  # fitted, reported, not used


def test_too_few_samples_keep_the_identity() -> None:
    result = fit(samples(149, lambda c: 0.5))
    assert (result.result, result.model, result.improvement) == (FitResult.TOO_FEW, None, None)
    assert fit([], min_samples=0).result is FitResult.TOO_FEW


def test_the_holdout_is_the_newest_samples() -> None:
    old = [Sample(90, False, T + timedelta(hours=i)) for i in range(70)]  # the fit learns "90 loses"
    new = [Sample(90, True, T + timedelta(days=10, hours=i)) for i in range(30)]  # ...and is judged on wins
    result = fit(list(reversed(old + new)), min_samples=100)
    assert result.result is FitResult.NO_IMPROVEMENT
    assert (result.brier_before, result.brier_after) == (0.01, 1.0)


def test_no_data_no_raise() -> None:
    model = IsotonicMap(((60, 0.8), (80, 0.9)))
    assert model(70) == 85
    assert (model(50), model(55), model(90), model(59)) == (50, 55, 90, 59)  # below: min(raw, 80)
    low = IsotonicMap(((60, 0.3), (80, 0.6)))
    assert (low(50), low(95)) == (30, 60)  # lowering is allowed anywhere
    assert IsotonicMap.from_params(model.params()) == model


@pytest.mark.parametrize(
    "points", [(), ((60, 0.8), (50, 0.9)), ((50, 0.8), (60, 0.7)), ((50, 1.2),), ((50, 0.1), (50, 0.2))]
)
def test_a_map_must_be_monotone(points: tuple[tuple[int, float], ...]) -> None:
    with pytest.raises(ValueError, match="point"):
        IsotonicMap(points)


def test_brier_and_reliability() -> None:
    assert brier([0.8, 0.2], [True, False]) == pytest.approx(0.04)
    bins = reliability([Sample(5, False, T), Sample(95, True, T), Sample(100, False, T)], bins=10)
    assert len(bins) == 10
    assert (bins[0].lo, bins[0].hi, bins[0].n, bins[0].win_rate) == (0, 9, 1, 0.0)
    assert (bins[9].lo, bins[9].hi, bins[9].n, bins[9].mean_confidence, bins[9].win_rate) == (
        90,
        100,
        2,
        97.5,
        0.5,
    )
    assert (bins[5].n, bins[5].mean_confidence, bins[5].win_rate) == (0, None, None)
