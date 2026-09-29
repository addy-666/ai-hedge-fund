"""Portfolio manager: the confidence pipeline between the analyst and the Risk Manager (docs/03 §8).

    p_raw       = proposal.confidence
    p_cal       = calibrator(p_raw)                                   identity until calibration (Phase 8)
    penalty     = min(Σ active-rule penalty points, max_total_penalty)  no rules until Phase 7
    final       = p_cal − penalty
    blocked     = a BLOCK rule matched                                → RULE_BLOCKED
    risk_factor = Π rule risk_scale, floored at 0.25, never above 1

The confidence threshold itself is enforced by the pipeline and again by the Risk Manager; regime, volatility
and drawdown scaling already happen in position sizing (risk/sizing.py), so they are not applied twice here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from aifund.domain.decision import FeatureSnapshot, FinalDecision, TradeProposal

RISK_FACTOR_FLOOR = Decimal("0.25")


class Calibrator(Protocol):
    def __call__(self, p_raw: int) -> int: ...


def identity(p_raw: int) -> int:
    return p_raw


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    penalty_points: int = 0
    risk_scale: Decimal = Decimal(1)
    block: bool = False


class RuleEvaluator(Protocol):
    """Deterministic learned rules (Phase 7, rules/engine.py): which active rules match this decision."""

    def matches(
        self, symbol: str, proposal: TradeProposal, snapshot: FeatureSnapshot
    ) -> Sequence[RuleHit]: ...


class NoRules:
    def matches(self, symbol: str, proposal: TradeProposal, snapshot: FeatureSnapshot) -> Sequence[RuleHit]:
        return ()


@dataclass(frozen=True)
class PortfolioDecision:
    decision: FinalDecision
    blocked_by: str | None = None
    rules_matched: tuple[str, ...] = field(default_factory=tuple)


class PortfolioManager:
    def __init__(
        self,
        *,
        max_total_penalty: int,
        calibrator: Calibrator = identity,
        rules: RuleEvaluator | None = None,
    ) -> None:
        self._max_penalty = max_total_penalty
        self._calibrate = calibrator
        self._rules = rules or NoRules()

    def decide(
        self, *, decision_id: str, symbol: str, proposal: TradeProposal, snapshot: FeatureSnapshot
    ) -> PortfolioDecision:
        p_raw = proposal.confidence
        p_cal = min(max(int(self._calibrate(p_raw)), 0), 100)
        hits = list(self._rules.matches(symbol, proposal, snapshot))
        penalty = min(sum(max(h.penalty_points, 0) for h in hits), self._max_penalty, 100)
        scale = Decimal(1)
        for h in hits:
            scale *= min(max(h.risk_scale, Decimal(0)), Decimal(1))  # rules only ever scale down
        blocking = next((h.rule_id for h in hits if h.block), None)
        decision = FinalDecision(
            decision_id=decision_id,
            symbol=symbol,
            direction=proposal.direction,
            setup_tag=proposal.setup_tag,
            llm_confidence=p_raw,
            calibrated_confidence=p_cal,
            penalty_points=penalty,
            final_confidence=p_cal - penalty,
            risk_factor=max(scale, RISK_FACTOR_FLOOR),
            invalidation_price=proposal.invalidation_price,
            target_price=proposal.target_price,
        )
        return PortfolioDecision(decision, blocking, tuple(h.rule_id for h in hits))
