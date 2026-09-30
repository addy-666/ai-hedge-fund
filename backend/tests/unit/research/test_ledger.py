"""Trial ledger (docs/09 §4): idempotent trials, whole-ledger BH-FDR, the single-use holdout."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aifund.research.ledger import (
    HoldoutSpent,
    Ledger,
    Origin,
    Split,
    Trial,
    digest,
)
from aifund.stats import Summary, benjamini_hochberg

START, END = datetime(2025, 7, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def summary(p: float, n: int = 100, mean: float = 0.1) -> Summary:
    return Summary(n, mean, 0.0, 0.5, 1.2, mean * n, 3.0, mean - 0.1, mean + 0.1, p)


def record(
    ledger: Ledger, hypothesis: dict[str, object], p: float, split: Split = Split.WALK_FORWARD, **kw: object
):  # type: ignore[no-untyped-def]
    args: dict[str, object] = dict(
        hypothesis=hypothesis, symbols=["XAUUSD"], start=START, end=END, split=split, origin=Origin.GRID,
        data_fingerprint="data-1", summary=summary(p), now=NOW,
    )  # fmt: skip
    args.update(kw)
    return ledger.record(**args)  # type: ignore[arg-type]


def test_hand_computed_benjamini_hochberg() -> None:
    p = {"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.20}
    assert benjamini_hochberg(p, 0.10) == {"a", "b", "c"}  # thresholds .025 .05 .075 .10
    assert benjamini_hochberg(p, 0.05) == {"a"}  # thresholds .0125 .025 .0375 .05
    assert benjamini_hochberg({}, 0.1) == set()
    with pytest.raises(ValueError, match="q must be"):
        benjamini_hochberg(p, 0)


def test_trials_are_idempotent_and_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "research" / "ledger.jsonl")
    assert ledger.trials() == []
    trial, added = record(
        ledger, {"detector": "mtf_trend_pullback", "v": "1", "params": {"oversold": 30}}, 0.02
    )
    assert added
    again, added_again = record(
        ledger, {"params": {"oversold": 30}, "v": "1", "detector": "mtf_trend_pullback"}, 0.9
    )
    assert (added_again, again) == (False, trial)  # key order does not matter; the first result stands
    assert ledger.trials() == [trial]
    assert Trial.from_json(trial.to_json()) == trial
    assert trial.hypothesis_id == digest(
        {"detector": "mtf_trend_pullback", "v": "1", "params": {"oversold": 30}}
    )
    hypothesis = {"detector": "mtf_trend_pullback", "v": "1", "params": {"oversold": 30}}
    _, other_data = record(ledger, hypothesis, 0.5, data_fingerprint="data-2")
    assert other_data  # new data is a new trial


def test_fdr_runs_over_the_whole_ledger(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    record(ledger, {"h": "good"}, 0.002)
    record(ledger, {"h": "lucky"}, 0.02)
    assert ledger.survivors(0.1) == {digest({"h": "good"}), digest({"h": "lucky"})}  # 2 tried: .05, .10
    for i in range(30):  # ...then 30 more ideas are tried, all noise
        record(ledger, {"h": f"noise-{i}"}, 0.5 + i / 100)
    # 32 tried: thresholds 0.1/32 = .003125 and 0.2/32 = .00625; "lucky" no longer survives its search
    assert ledger.survivors(0.1) == {digest({"h": "good"})}


def test_latest_walk_forward_per_hypothesis_counts(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    record(ledger, {"h": 1}, 0.9)
    record(ledger, {"h": 1}, 0.001, end=datetime(2026, 7, 1, tzinfo=UTC))  # re-evaluated on longer data
    record(
        ledger, {"h": 1}, 0.001, split=Split.IN_SAMPLE, end=datetime(2026, 8, 1, tzinfo=UTC)
    )  # not counted
    assert ledger.latest_walk_forward()[digest({"h": 1})].p_value == 0.001
    assert ledger.survivors(0.1) == {digest({"h": 1})}


def test_the_holdout_is_spent_once(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl")
    h = {"h": "candidate"}
    assert not ledger.holdout_spent(h)
    _, added = record(ledger, h, 0.03, split=Split.HOLDOUT)
    assert added
    assert ledger.holdout_spent(h)
    _, added = record(ledger, h, 0.03, split=Split.HOLDOUT)  # the identical trial: returned, not re-run
    assert not added
    with pytest.raises(HoldoutSpent):
        record(ledger, h, 0.01, split=Split.HOLDOUT, data_fingerprint="data-2")  # a second look is refused
