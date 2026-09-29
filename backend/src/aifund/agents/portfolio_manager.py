"""Portfolio manager: the confidence pipeline between a proposal and the Risk Manager (docs/03 §8).

    p_raw       = proposal.confidence
    p_cal       = calibrator(p_raw)                              identity until calibration (Phase 8)
    penalty     = the rule engine's capped Σ of ACTIVE penalty points (rules/engine.py)
    final       = p_cal − penalty
    blocked     = an ACTIVE block rule matched                   → RULE_BLOCKED
    risk_factor = the rule engine's Π risk_scale, floored at 0.25, never above 1

The rules are evaluated by the caller (the pipeline holds the current rulebook) and handed in as a verdict;
``with_rules`` applies one to any decision, so the deterministic baseline is ruled exactly like the analyst.
The confidence threshold itself is enforced by the pipeline and again by the Risk Manager; regime, volatility
and drawdown scaling already happen in position sizing (risk/sizing.py), so they are not applied twice here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from aifund.domain.decision import FinalDecision, TradeProposal
from aifund.rules.engine import NO_RULES, RISK_FACTOR_FLOOR, RuleVerdict


class Calibrator(Protocol):
    def __call__(self, p_raw: int) -> int: ...


def identity(p_raw: int) -> int:
    return p_raw


@dataclass(frozen=True)
class PortfolioDecision:
    decision: FinalDecision
    blocked_by: str | None = None
    rules_matched: tuple[str, ...] = field(default_factory=tuple)


def with_rules(decision: FinalDecision, verdict: RuleVerdict) -> FinalDecision:
    """The decision after its rules: penalty off the calibrated confidence, risk only ever scaled down."""
    penalty = min(max(verdict.penalty_points, 0), 100)
    factor = min(max(verdict.risk_factor, RISK_FACTOR_FLOOR), Decimal(1))
    return decision.model_copy(
        update={
            "penalty_points": penalty,
            "final_confidence": decision.calibrated_confidence - penalty,
            "risk_factor": factor,
        }
    )


class PortfolioManager:
    def __init__(self, *, calibrator: Calibrator = identity) -> None:
        self._calibrate = calibrator

    def decide(
        self, *, decision_id: str, symbol: str, proposal: TradeProposal, rules: RuleVerdict = NO_RULES
    ) -> PortfolioDecision:
        p_raw = proposal.confidence
        p_cal = min(max(int(self._calibrate(p_raw)), 0), 100)
        decision = FinalDecision(
            decision_id=decision_id,
            symbol=symbol,
            direction=proposal.direction,
            setup_tag=proposal.setup_tag,
            llm_confidence=p_raw,
            calibrated_confidence=p_cal,
            penalty_points=0,
            final_confidence=p_cal,
            risk_factor=Decimal(1),
            invalidation_price=proposal.invalidation_price,
            target_price=proposal.target_price,
        )
        return PortfolioDecision(with_rules(decision, rules), rules.blocked_by, tuple(rules.matched))
