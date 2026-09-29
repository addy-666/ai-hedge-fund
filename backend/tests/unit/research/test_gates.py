"""Evidence gate E1 checks and the throughput projection (docs/09 §7–§8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aifund.config.trading_config import LearningConfig, ResearchConfig
from aifund.research.gates import (
    e1_holdout_checks,
    e1_walk_forward_checks,
    passed,
    throughput,
)
from aifund.research.stats import EMPTY, Summary
from aifund.research.walkforward import WalkForwardResult
from tests.unit.research.test_stats import outcome

CFG = ResearchConfig()  # 200 OOS signals, 60% positive folds, q 0.10, 60 holdout signals


def summary(n: int, mean: float, ci_low: float | None) -> Summary:
    return Summary(
        n, mean, mean, 0.5, 1.5, mean * n, 5.0, ci_low, None if ci_low is None else mean + 0.1, 0.01
    )


def result(s: Summary, fold_means: list[float]) -> WalkForwardResult:
    from aifund.research.walkforward import Fold

    t = datetime(2026, 1, 1, tzinfo=UTC)
    folds = [
        Fold(i, t, t, "default", EMPTY, [outcome(i, m)] if m is not None else [])
        for i, m in enumerate(fold_means)
    ]
    return WalkForwardResult(folds=folds, out_of_sample=[], summary=s)


def names(checks) -> dict[str, bool]:  # type: ignore[no-untyped-def]
    return {c.name: c.passed for c in checks}


def test_a_strong_walk_forward_passes_every_check() -> None:
    checks = e1_walk_forward_checks(
        result(summary(250, 0.2, 0.05), [0.1, 0.3, -0.1, 0.2]), survives_fdr=True, cfg=CFG
    )
    assert passed(checks)
    assert all(c.detail for c in checks)


def test_each_check_fails_on_its_own() -> None:
    good = result(summary(250, 0.2, 0.05), [0.1, 0.3, -0.1, 0.2])
    assert not names(
        e1_walk_forward_checks(result(summary(150, 0.2, 0.05), [0.1] * 4), survives_fdr=True, cfg=CFG)
    )["oos_signals"]
    assert not names(
        e1_walk_forward_checks(result(summary(250, -0.1, 0.05), [0.1] * 4), survives_fdr=True, cfg=CFG)
    )["oos_mean"]
    assert not names(
        e1_walk_forward_checks(result(summary(250, 0.2, -0.01), [0.1] * 4), survives_fdr=True, cfg=CFG)
    )["oos_ci"]
    assert not names(
        e1_walk_forward_checks(result(summary(250, 0.2, None), [0.1] * 4), survives_fdr=True, cfg=CFG)
    )["oos_ci"]
    assert not names(
        e1_walk_forward_checks(
            result(summary(250, 0.2, 0.05), [0.1, -0.2, -0.1, 0.2]), survives_fdr=True, cfg=CFG
        )
    )["fold_stability"]
    assert not names(e1_walk_forward_checks(good, survives_fdr=False, cfg=CFG))["fdr"]
    assert not passed([])


def test_holdout_checks() -> None:
    assert passed(e1_holdout_checks(summary(80, 0.1, None), CFG))
    assert names(e1_holdout_checks(summary(40, 0.1, None), CFG)) == {
        "holdout_signals": False,
        "holdout_mean": True,
    }
    assert names(e1_holdout_checks(summary(80, -0.1, None), CFG))["holdout_mean"] is False


def test_throughput_in_signals_not_weeks() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    tp = throughput(20, start, start + timedelta(days=304.4), CFG, LearningConfig())  # 10 months
    assert round(tp.signals_per_month, 9) == 2.0
    assert round(tp.months_to["E2 (forward signals)"] or 0, 9) == 50.0  # 100 / 2
    assert round(tp.months_to["L2 (100 closed trades)"] or 0, 9) == 50.0
    assert round(tp.months_to["first learnable rule"] or 0, 1) == 33.3  # 20 matches at <= 30% coverage
    assert tp.too_slow
    assert "TOO SLOW" in tp.render()
    fast = throughput(600, start, start + timedelta(days=304.4), CFG, LearningConfig())
    assert not fast.too_slow
    none = throughput(0, start, start + timedelta(days=30), CFG, LearningConfig())
    assert none.months_to["E1 (OOS signals)"] is None
    assert none.too_slow
    assert "never" in none.render()
