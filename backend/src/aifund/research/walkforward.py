"""Anchored walk-forward (docs/09 §3): parameters chosen in-sample, judged only out-of-sample.

Input: for each parameter variant (declared order matters: the first is the default), its signal outcomes over
the whole pre-holdout window. Features are causal (closed bars only), so a signal computed over the whole
window is the same signal a fold-by-fold run would produce; slicing by time is therefore exact.

The window [start, end) is cut into ``folds + 1`` equal segments. For fold k (1..folds) the test window is
segment k; the training data is every outcome that **exited** before the test window starts (an outcome still
open at the boundary carries information from inside the test window, so it is left out). The variant with
the best in-sample mean R among those with at least ``min_train_signals`` wins (ties: declared order); if none
qualifies, the default. Its outcomes that ENTER inside the test window are that fold's out-of-sample result.
The concatenated out-of-sample series is the walk-forward result; in-sample numbers are diagnostics only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aifund.research.signals import SignalOutcome
from aifund.stats import Summary, summarize


@dataclass(frozen=True)
class Fold:
    index: int
    test_start: datetime
    test_end: datetime
    chosen: str
    in_sample: Summary  # of the chosen variant, on its training outcomes
    out_of_sample: list[SignalOutcome] = field(repr=False)

    @property
    def oos_mean(self) -> float | None:
        if not self.out_of_sample:
            return None
        return sum(float(o.r_net) for o in self.out_of_sample) / len(self.out_of_sample)


@dataclass(frozen=True)
class WalkForwardResult:
    folds: list[Fold]
    out_of_sample: list[SignalOutcome]
    summary: Summary

    @property
    def positive_fold_share(self) -> float:
        judged = [f.oos_mean for f in self.folds if f.oos_mean is not None]
        return sum(1 for m in judged if m > 0) / len(judged) if judged else 0.0


def boundaries(start: datetime, end: datetime, folds: int) -> list[datetime]:
    """``folds + 2`` instants: the start, the start of each test window, and the end."""
    if folds < 1:
        raise ValueError(f"folds must be >= 1, got {folds}")
    if end <= start:
        raise ValueError("end must be after start")
    step = (end - start) / (folds + 1)
    return [start + step * i for i in range(folds + 1)] + [end]


def walk_forward(
    variants: Mapping[str, Sequence[SignalOutcome]],
    *,
    start: datetime,
    end: datetime,
    folds: int = 4,
    min_train_signals: int = 30,
    seed: int = 0,
) -> WalkForwardResult:
    if not variants:
        raise ValueError("walk-forward needs at least one variant")
    edges = boundaries(start, end, folds)
    out: list[Fold] = []
    for k in range(1, folds + 1):
        test_start, test_end = edges[k], edges[k + 1]
        chosen, train = choose(variants, start=start, before=test_start, min_train_signals=min_train_signals)
        test = [o for o in variants[chosen] if test_start <= o.entry_time < test_end]
        out.append(
            Fold(
                index=k,
                test_start=test_start,
                test_end=test_end,
                chosen=chosen,
                in_sample=summarize([o.r_net for o in train], seed=seed),
                out_of_sample=test,
            )
        )
    oos = [o for f in out for o in f.out_of_sample]
    summary = summarize([o.r_net for o in oos], times=[o.entry_time for o in oos], seed=seed)
    return WalkForwardResult(folds=out, out_of_sample=oos, summary=summary)


def choose(
    variants: Mapping[str, Sequence[SignalOutcome]],
    *,
    start: datetime,
    before: datetime,
    min_train_signals: int,
) -> tuple[str, list[SignalOutcome]]:
    """The selection rule: best mean R on outcomes that entered at/after ``start`` and EXITED before
    ``before``, among variants with at least ``min_train_signals`` of them (ties: declared order); else the
    first variant (the default). Returns the choice and its training outcomes."""
    names = list(variants)
    train = {n: [o for o in variants[n] if start <= o.entry_time and o.exit_time < before] for n in names}
    eligible = [n for n in names if len(train[n]) >= min_train_signals]
    chosen = max(eligible, key=lambda n: (_mean(train[n]), -names.index(n))) if eligible else names[0]
    return chosen, train[chosen]


def _mean(outcomes: Sequence[SignalOutcome]) -> float:
    return sum(float(o.r_net) for o in outcomes) / len(outcomes)
