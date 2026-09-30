"""Evidence gate E1 and throughput projection (docs/09 §7–§8). Pure: numbers in, verdicts out."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aifund.config.trading_config import LearningConfig, ResearchConfig
from aifund.research.walkforward import WalkForwardResult
from aifund.stats import Summary

DAYS_PER_MONTH = 30.44


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    detail: str


def e1_walk_forward_checks(
    wf: WalkForwardResult, *, survives_fdr: bool, cfg: ResearchConfig
) -> list[GateCheck]:
    s = wf.summary
    share = wf.positive_fold_share
    return [
        GateCheck(
            "oos_signals",
            s.n >= cfg.min_oos_signals,
            f"{s.n} out-of-sample signals (need {cfg.min_oos_signals})",
        ),
        GateCheck("oos_mean", s.n > 0 and s.mean > 0, f"mean {s.mean:+.3f}R net of costs"),
        GateCheck(
            "oos_ci",
            s.ci_low is not None and s.ci_low > 0,
            "90% CI lower bound " + ("n/a" if s.ci_low is None else f"{s.ci_low:+.3f}R") + " (need > 0)",
        ),
        GateCheck(
            "fold_stability",
            share >= float(cfg.min_positive_fold_share),
            f"{share:.0%} of folds positive (need {float(cfg.min_positive_fold_share):.0%})",
        ),
        GateCheck(
            "fdr",
            survives_fdr,
            f"{'survives' if survives_fdr else 'fails'} BH at q={cfg.fdr_q} over the ledger",
        ),
    ]


def e1_holdout_checks(holdout: Summary, cfg: ResearchConfig) -> list[GateCheck]:
    return [
        GateCheck(
            "holdout_signals",
            holdout.n >= cfg.min_holdout_signals,
            f"{holdout.n} holdout signals (need {cfg.min_holdout_signals})",
        ),
        GateCheck("holdout_mean", holdout.n > 0 and holdout.mean > 0, f"holdout mean {holdout.mean:+.3f}R"),
    ]


def passed(checks: list[GateCheck]) -> bool:
    return bool(checks) and all(c.passed for c in checks)


@dataclass(frozen=True)
class Throughput:
    signals: int
    months_observed: float
    signals_per_month: float
    months_to: dict[str, float | None]  # milestone -> months at the measured rate (None: rate is zero)
    too_slow: bool

    def render(self) -> str:
        parts = [f"{k} {'never' if v is None else f'{v:.1f} months'}" for k, v in self.months_to.items()]
        verdict = "TOO SLOW TO VALIDATE" if self.too_slow else "ok"
        return f"{self.signals_per_month:.1f} signals/month -> " + "; ".join(parts) + f" [{verdict}]"


def throughput(
    signals: int, start: datetime, end: datetime, research: ResearchConfig, learning: LearningConfig
) -> Throughput:
    months = max((end - start) / timedelta(days=1), 1.0) / DAYS_PER_MONTH
    rate = signals / months
    first_rule_trades = learning.min_matches_total / float(learning.max_rule_coverage)
    needs = {
        "E1 (OOS signals)": float(research.min_oos_signals),
        "E2 (forward signals)": float(research.min_forward_signals),
        "L2 (100 closed trades)": 100.0,
        "first learnable rule": first_rule_trades,
    }
    months_to = {k: (v / rate if rate > 0 else None) for k, v in needs.items()}
    e2 = months_to["E2 (forward signals)"]
    return Throughput(
        signals=signals,
        months_observed=months,
        signals_per_month=rate,
        months_to=months_to,
        too_slow=e2 is None or e2 > research.max_months_to_evidence,
    )
