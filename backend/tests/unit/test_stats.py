"""R-multiple statistics and the anchored walk-forward (docs/09 §3). Hand-derived values where possible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import numpy as np
import pytest

from aifund.domain.enums import CloseReason, Direction, Timeframe, VirtualStatus
from aifund.research.signals import SignalOutcome
from aifund.research.walkforward import boundaries, walk_forward
from aifund.stats import EMPTY, summarize

T0 = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------- summarize


def test_hand_computed_summary() -> None:
    times = [T0 + timedelta(days=d) for d in (0, 1, 40, 41)]
    s = summarize([1, -1, 2, -0.5], times=times)
    assert (s.n, s.mean, s.median, s.win_rate, s.total) == (4, 0.375, 0.25, 0.5, 1.5)
    assert s.profit_factor == 2.0  # 3 / 1.5
    assert s.max_drawdown == 1.0  # curve 1, 0, 2, 1.5
    assert s.monthly == {"2026-01": 0.0, "2026-02": 0.75}
    assert s.ci_low is not None and s.ci_high is not None  # noqa: PT018
    assert s.ci_low <= s.mean <= s.ci_high


def test_drawdown_follows_time_order_not_input_order() -> None:
    times = [T0 + timedelta(days=d) for d in (3, 0, 1, 2)]  # in time: +1, -1, -1, -1 -> curve 1, 0, -1, -2
    assert summarize([-1, 1, -1, -1], times=times).max_drawdown == 3.0
    assert summarize([-1, 1, -1, -1]).max_drawdown == 2.0  # as given: -1, 0, -1, -2 from the 0 start


def test_degenerate_samples() -> None:
    assert summarize([]) == EMPTY
    one = summarize([D("0.5")])
    assert (one.ci_low, one.ci_high, one.p_value, one.profit_factor) == (None, None, 1.0, None)
    assert not one.significant
    with pytest.raises(ValueError, match="confidence"):
        summarize([1, 2], confidence=1.0)
    with pytest.raises(ValueError, match="same length"):
        summarize([1, 2], times=[T0])


def test_deterministic_given_the_seed() -> None:
    x = list(np.random.default_rng(7).normal(0.1, 1, 100))
    assert summarize(x, seed=3) == summarize(x, seed=3)
    assert summarize(x, seed=3).ci_low != summarize(x, seed=4).ci_low


def test_a_planted_edge_is_found() -> None:
    x = np.random.default_rng(1).normal(0.3, 1.0, 300)
    s = summarize(list(x))
    assert s.significant
    assert s.p_value < 0.01
    assert "CI90=[+" in s.render()


def test_pure_noise_rarely_passes() -> None:
    passed = covered = 0
    for seed in range(200):
        s = summarize(
            list(np.random.default_rng(1000 + seed).normal(0.0, 1.0, 200)), seed=seed, resamples=500
        )
        passed += s.significant
        covered += s.ci_low is not None and s.ci_high is not None and s.ci_low <= 0 <= s.ci_high
    assert passed / 200 <= 0.08  # nominal 5% (one-sided)
    assert covered / 200 >= 0.85  # nominal 90%


# ---------------------------------------------------------------- walk-forward


def outcome(day: float, r: float, *, held_days: float = 0.1) -> SignalOutcome:
    entry = T0 + timedelta(days=day)
    return SignalOutcome(
        symbol="X", bar_time=entry, entry_time=entry, direction=Direction.LONG, setup_tag="s", detector="d@1",
        status=VirtualStatus.CLOSED, exit_reason=CloseReason.TP if r > 0 else CloseReason.SL,
        exit_time=entry + timedelta(days=held_days), entry_price=D(1), sl_distance=D(1), tp_distance=D(2),
        r_gross=D(str(r)), r_net=D(str(r)), mae_r=None, mfe_r=None, resolution=Timeframe.M1, features={},
    )  # fmt: skip


END = T0 + timedelta(days=100)  # 4 folds: segments of 20 days; tests start at days 20, 40, 60, 80


def test_boundaries() -> None:
    assert boundaries(T0, END, 4) == [T0 + timedelta(days=d) for d in (0, 20, 40, 60, 80, 100)]
    with pytest.raises(ValueError, match="folds"):
        boundaries(T0, END, 0)
    with pytest.raises(ValueError, match="after start"):
        boundaries(END, T0, 2)


def test_variant_chosen_in_sample_and_judged_out_of_sample() -> None:
    a = [outcome(d, 0.1) for d in range(100)]
    b = [outcome(d, 0.5 if d < 20 else -1.0) for d in range(100)]  # great early, terrible afterwards
    wf = walk_forward({"a": a, "b": b}, start=T0, end=END, folds=4, min_train_signals=10)
    assert wf.folds[0].chosen == "b"  # only days 0-19 were known
    assert wf.folds[0].oos_mean == -1.0  # ...and it failed out of sample
    assert [f.chosen for f in wf.folds[1:]] == ["a", "a", "a"]  # by day 40 b's mean is -0.25 < a's 0.1
    assert len(wf.out_of_sample) == 80
    assert wf.summary.n == 80
    assert wf.positive_fold_share == 0.75


def test_the_test_window_never_influences_its_own_choice() -> None:
    a = [outcome(d, 0.1) for d in range(100)]
    b = [outcome(d, -0.2) for d in range(100)]
    base = walk_forward({"a": a, "b": b}, start=T0, end=END, folds=4, min_train_signals=5)
    for k, fold in enumerate(base.folds):
        lo, hi = 20 * (k + 1), 20 * (k + 2)
        # make b spectacular inside this fold's test window only: the choice for this fold must not change
        b_future = [outcome(d, 5.0) if lo <= d < hi else o for d, o in zip(range(100), b, strict=True)]
        again = walk_forward({"a": a, "b": b_future}, start=T0, end=END, folds=4, min_train_signals=5)
        assert again.folds[k].chosen == fold.chosen == "a"


def test_outcomes_still_open_at_the_boundary_are_not_training_data() -> None:
    # b's only good trade starts before day 20 but exits after it: it must not count for fold 1
    b = [outcome(d, -0.1) for d in range(15)] + [outcome(19, 50.0, held_days=2)]
    a = [outcome(d, 0.0) for d in range(16)]
    wf = walk_forward({"a": a, "b": b}, start=T0, end=END, folds=4, min_train_signals=10)
    assert wf.folds[0].chosen == "a"
    assert wf.folds[0].in_sample.n == 16


def test_too_little_training_data_falls_back_to_the_default() -> None:
    wf = walk_forward({"default": [outcome(50, 1.0)], "other": [outcome(5, 9.0)]}, start=T0, end=END, folds=4)
    assert {f.chosen for f in wf.folds} == {"default"}
    assert [f.oos_mean for f in wf.folds] == [None, 1.0, None, None]
    assert wf.positive_fold_share == 1.0
    with pytest.raises(ValueError, match="at least one variant"):
        walk_forward({}, start=T0, end=END)


def test_no_out_of_sample_signals_at_all() -> None:
    wf = walk_forward({"a": [outcome(1, 1.0)]}, start=T0, end=END, folds=2)
    assert wf.out_of_sample == []
    assert wf.positive_fold_share == 0.0


def test_choose_uses_only_finished_training_outcomes() -> None:
    from aifund.research.walkforward import choose

    a = [outcome(d, 0.2) for d in range(40)]
    b = [outcome(d, 1.0, held_days=30) for d in range(40)]  # still open at the cut for days >= 20
    chosen, train = choose({"a": a, "b": b}, start=T0, before=T0 + timedelta(days=50), min_train_signals=25)
    assert chosen == "a"  # b has only 20 finished outcomes: not eligible
    assert len(train) == 40


def test_bootstrap_cis_of_a_mean_and_a_difference() -> None:
    from aifund.stats import bootstrap_diff_ci, bootstrap_mean_ci

    lo, hi = bootstrap_mean_ci([-1.0, -1.0, 1.5, -1.0, 2.0, -1.0], seed=1)  # type: ignore[misc]
    assert lo < -1 / 12 < hi  # the sample mean sits inside its own interval
    assert bootstrap_mean_ci([1.0]) is None
    lo, hi = bootstrap_diff_ci([-1.0] * 10 + [1.5], [1.5] * 10 + [-1.0], seed=2)  # type: ignore[misc]
    assert hi < 0
    assert bootstrap_diff_ci([1.0], [1.0, 2.0]) is None


def test_the_permutation_z_p_matches_brute_force_relabelling() -> None:
    import itertools

    import numpy as np

    from aifund.stats import permutation_z_p_lower

    rng = np.random.default_rng(7)
    x = rng.choice([-1.0, 1.5], size=16, p=[0.55, 0.45])
    mask = np.zeros(16, dtype=bool)
    mask[:5] = True
    x[:5] = -1.0  # the masked group loses
    observed = x[mask].mean() - x[~mask].mean()
    effects = []
    for idx in itertools.combinations(range(16), 5):  # every relabelling: the exact permutation distribution
        m = np.zeros(16, dtype=bool)
        m[list(idx)] = True
        effects.append(x[m].mean() - x[~m].mean())
    exact = np.mean(np.asarray(effects) <= observed + 1e-12)
    approx = permutation_z_p_lower(x, mask)
    assert abs(approx - exact) < 0.03, (approx, exact)
    assert np.isclose(np.var(effects), (x.var() * 16 * 16 / (5 * 11 * 15)))  # the variance used is exact
    assert permutation_z_p_lower([1.0, 2.0, 3.0], [False, False, False]) == 1.0
    assert permutation_z_p_lower([1.0, 1.0, 1.0], [True, False, False]) == 1.0
    assert permutation_z_p_lower([1.0, 2.0], [True, False]) == 1.0
    assert permutation_z_p_lower([3.0, 3.0, 1.0, 1.0], [True, True, False, False]) > 0.9  # a HIGHER group
