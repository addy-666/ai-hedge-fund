"""Trial ledger (docs/09 §4): every hypothesis ever evaluated, so significance reflects the whole search.

Append-only JSON lines. A hypothesis is identified by the hash of its canonical JSON (detector id, version
and parameters, a parameter grid with its selection rule, or an entry-rule DSL document); a trial by the hash
of hypothesis + symbols + window + split + data fingerprint, so re-running the same evaluation adds nothing.

- ``survivors(q)``: Benjamini–Hochberg at level q over ONE p-value per distinct hypothesis (its latest
  walk-forward trial) across the WHOLE ledger. A grid whose parameters are chosen inside the walk-forward is
  one hypothesis (its out-of-sample result already pays for the choice); every LLM idea is one more.
- The holdout is single-use: ``record`` refuses a second ``holdout`` trial of a hypothesis (``HoldoutSpent``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from aifund.research.stats import Summary


class Split(StrEnum):
    IN_SAMPLE = "in_sample"
    WALK_FORWARD = "walk_forward"
    HOLDOUT = "holdout"


class Origin(StrEnum):
    GRID = "grid"
    LLM = "llm"
    OPERATOR = "operator"


class HoldoutSpent(Exception):
    """Already evaluated on the holdout: a second look would turn the holdout into a training set."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Trial:
    trial_id: str
    hypothesis_id: str
    hypothesis: Mapping[str, Any]
    symbols: list[str]
    window: tuple[str, str]  # ISO start, end
    split: Split
    origin: Origin
    data_fingerprint: str
    n: int
    mean: float
    ci_low: float | None
    ci_high: float | None
    p_value: float
    created_at: str

    def to_json(self) -> str:
        return canonical({**asdict(self), "window": list(self.window)})

    @classmethod
    def from_json(cls, line: str) -> Trial:
        raw = json.loads(line)
        return cls(
            **{
                **raw,
                "window": tuple(raw["window"]),
                "split": Split(raw["split"]),
                "origin": Origin(raw["origin"]),
            }
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


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def trials(self) -> list[Trial]:
        if not self.path.is_file():
            return []
        return [Trial.from_json(line) for line in self.path.read_text().splitlines() if line.strip()]

    def record(
        self,
        *,
        hypothesis: Mapping[str, Any],
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        split: Split,
        origin: Origin,
        data_fingerprint: str,
        summary: Summary,
        now: datetime,
    ) -> tuple[Trial, bool]:
        """Append a trial; returns (trial, added). An identical earlier trial is returned instead."""
        hypothesis_id = digest(hypothesis)
        window = (start.isoformat(), end.isoformat())
        trial_id = digest([hypothesis_id, sorted(symbols), window, split.value, data_fingerprint])
        existing = self.trials()
        for t in existing:
            if t.trial_id == trial_id:
                return t, False
        if split is Split.HOLDOUT and any(
            t.hypothesis_id == hypothesis_id and t.split is Split.HOLDOUT for t in existing
        ):
            raise HoldoutSpent(f"hypothesis {hypothesis_id} was already evaluated on the holdout")
        trial = Trial(
            trial_id=trial_id,
            hypothesis_id=hypothesis_id,
            hypothesis=dict(hypothesis),
            symbols=sorted(symbols),
            window=window,
            split=split,
            origin=origin,
            data_fingerprint=data_fingerprint,
            n=summary.n,
            mean=summary.mean,
            ci_low=summary.ci_low,
            ci_high=summary.ci_high,
            p_value=summary.p_value,
            created_at=now.isoformat(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(trial.to_json() + "\n")
        return trial, True

    def holdout_spent(self, hypothesis: Mapping[str, Any]) -> bool:
        hid = digest(hypothesis)
        return any(t.hypothesis_id == hid and t.split is Split.HOLDOUT for t in self.trials())

    def latest_walk_forward(self) -> dict[str, Trial]:
        latest: dict[str, Trial] = {}
        for t in self.trials():  # file order is time order
            if t.split is Split.WALK_FORWARD:
                latest[t.hypothesis_id] = t
        return latest

    def survivors(self, q: float) -> set[str]:
        """Hypothesis ids that survive BH at ``q`` against every hypothesis in the ledger."""
        return benjamini_hochberg({h: t.p_value for h, t in self.latest_walk_forward().items()}, q)
