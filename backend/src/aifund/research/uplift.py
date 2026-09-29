"""Gate G-LLM (docs/09 §7, roadmap R.9): does the analyst beat the deterministic baseline, net of its cost?

Paired comparison over the bars where both decided (the analyst ran on a bar with a detected candidate):
each bar contributes ``analyst R - baseline R - LLM cost in R``, where both R come from shadow virtual trades
with the SAME stop planner (an arm that did not trade earned 0) and the cost is the decision's LLM spend over
the money one trade risks. Passes when there are at least ``min_paired_signals`` pairs and the seeded
bootstrap 90% CI of the mean uplift lies above zero. Pure: numbers in, verdict out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from aifund.config.trading_config import ResearchConfig
from aifund.research.gates import GateCheck, passed
from aifund.research.stats import Summary, summarize


@dataclass(frozen=True)
class Pair:
    baseline_r: Decimal
    analyst_r: Decimal
    cost_usd: Decimal


@dataclass(frozen=True)
class UpliftReport:
    n: int
    baseline_mean: float
    analyst_mean: float
    cost_mean_r: float
    uplift: Summary  # of analyst - baseline - cost, per pair
    checks: list[GateCheck]

    @property
    def passed(self) -> bool:
        return passed(self.checks)

    def render(self) -> str:
        return (
            f"{self.n} paired bars: baseline {self.baseline_mean:+.3f}R, analyst {self.analyst_mean:+.3f}R, "
            f"LLM cost {self.cost_mean_r:.4f}R -> uplift {self.uplift.render()}"
        )


def uplift(pairs: Sequence[Pair], *, risk_usd: Decimal, cfg: ResearchConfig, seed: int = 0) -> UpliftReport:
    if risk_usd <= 0:
        raise ValueError("risk_usd must be positive (the money one trade risks)")
    costs = [p.cost_usd / risk_usd for p in pairs]
    diffs = [p.analyst_r - p.baseline_r - c for p, c in zip(pairs, costs, strict=True)]
    n = len(pairs)
    summary = summarize(diffs, seed=seed)

    def mean(values: Sequence[Decimal]) -> float:
        return float(sum(values, Decimal(0)) / len(values)) if values else 0.0

    checks = [
        GateCheck(
            "paired_signals", n >= cfg.min_paired_signals, f"{n} paired bars (need {cfg.min_paired_signals})"
        ),
        GateCheck(
            "uplift_ci",
            summary.ci_low is not None and summary.ci_low > 0,
            "90% CI lower bound of the uplift "
            + ("n/a" if summary.ci_low is None else f"{summary.ci_low:+.3f}R")
            + " (need > 0)",
        ),
    ]
    return UpliftReport(
        n=n,
        baseline_mean=mean([p.baseline_r for p in pairs]),
        analyst_mean=mean([p.analyst_r for p in pairs]),
        cost_mean_r=mean(costs),
        uplift=summary,
        checks=checks,
    )
