"""Committee vs analyst vs baseline in shadow (roadmap 8.4): the comparison behind the Analytics report.

Over the bars where the committee deliberated beside the analyst and every shadow arm has finished, each arm
earned the R of its shadow virtual trade (same stop planner for all three; an arm that stayed out earned 0).
The committee's uplift is paired per bar and net of LLM cost in R (the spend over the money one trade risks):

    vs analyst:  (committee R - committee cost) - (analyst R - analyst cost)
    vs baseline: (committee R - committee cost) - baseline R

A seeded bootstrap 90% CI says whether a difference is more than noise. Pure: numbers in, report out. This is
a comparison, not a gate: nothing lets the committee trade (docs/03 §8, committee.mode is off or shadow).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aifund.domain.trade import CommitteeBar
from aifund.stats import Summary, summarize


@dataclass(frozen=True)
class Arm:
    name: str
    trades: int
    total_r: Decimal
    mean_r_per_trade: float | None
    win_rate: float | None
    cost_usd: Decimal


@dataclass(frozen=True)
class CommitteeReport:
    bars: int
    first: datetime | None
    last: datetime | None
    arms: list[Arm]
    agreement: float | None  # bars where both LLM arms traded the same direction / bars where either traded
    risk_usd: Decimal | None
    vs_analyst: Summary | None  # None without risk_usd (cost cannot be put in R) or without bars
    vs_baseline: Summary | None


def _arm(name: str, rs: Sequence[Decimal], traded: Sequence[bool], cost: Decimal) -> Arm:
    taken = [r for r, t in zip(rs, traded, strict=True) if t]
    return Arm(
        name=name,
        trades=len(taken),
        total_r=sum(taken, Decimal(0)),
        mean_r_per_trade=float(sum(taken, Decimal(0)) / len(taken)) if taken else None,
        win_rate=round(sum(1 for r in taken if r > 0) / len(taken), 4) if taken else None,
        cost_usd=cost,
    )


def compare(bars: Sequence[CommitteeBar], *, risk_usd: Decimal | None, seed: int = 0) -> CommitteeReport:
    if risk_usd is not None and risk_usd <= 0:
        raise ValueError("risk_usd must be positive (the money one trade risks)")
    arms = [
        _arm("baseline", [b.baseline_r for b in bars], [b.baseline_traded for b in bars], Decimal(0)),
        _arm("analyst", [b.analyst_r for b in bars], [b.analyst_traded for b in bars],
             sum((b.analyst_cost_usd for b in bars), Decimal(0))),
        _arm("committee", [b.committee_r for b in bars], [b.committee_traded for b in bars],
             sum((b.committee_cost_usd for b in bars), Decimal(0))),
    ]  # fmt: skip
    either = [b for b in bars if b.analyst_traded or b.committee_traded]
    same = [
        b
        for b in either
        if b.analyst_traded and b.committee_traded and b.analyst_direction == b.committee_direction
    ]
    vs_analyst = vs_baseline = None
    if risk_usd is not None and bars:
        net = [b.committee_r - b.committee_cost_usd / risk_usd for b in bars]
        vs_analyst = summarize(
            [n - (b.analyst_r - b.analyst_cost_usd / risk_usd) for n, b in zip(net, bars, strict=True)],
            seed=seed,
        )
        vs_baseline = summarize([n - b.baseline_r for n, b in zip(net, bars, strict=True)], seed=seed)
    return CommitteeReport(
        bars=len(bars),
        first=min((b.bar_time for b in bars), default=None),
        last=max((b.bar_time for b in bars), default=None),
        arms=arms,
        agreement=round(len(same) / len(either), 4) if either else None,
        risk_usd=risk_usd,
        vs_analyst=vs_analyst,
        vs_baseline=vs_baseline,
    )
