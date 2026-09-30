"""Portfolio manager: the confidence pipeline between a proposal and the Risk Manager (docs/03 §8).

    p_raw       = proposal.confidence
    p_cal       = calibrator(p_raw)                              identity until calibration (Phase 8)
    penalty     = the rule engine's capped Σ of ACTIVE penalty points (rules/engine.py)
    final       = p_cal − penalty
    blocked     = an ACTIVE block rule matched                   → RULE_BLOCKED
    risk_factor = the rule engine's Π risk_scale, floored at 0.25, never above 1

With the Phase 8 committee (``decide_committee``, docs/03 §8) ``p_raw`` is the weighted mean confidence of the
specialists that proposed a trade — all in one direction, or the committee holds — minus the risk critic's
penalties (HIGH 15, MEDIUM 7 by default); the invalidation, target and setup are the most confident
proposal's. The committee has its own calibrator (a separate instance of this class).

The rules are evaluated by the caller (the pipeline holds the current rulebook) and handed in as a verdict;
``with_rules`` applies one to any decision, so the deterministic baseline is ruled exactly like the analyst.
The confidence threshold itself is enforced by the pipeline and again by the Risk Manager; regime, volatility
and drawdown scaling already happen in position sizing (risk/sizing.py), so they are not applied twice here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from aifund.domain.decision import Critique, FinalDecision, TradeProposal
from aifund.domain.enums import Direction, ObjectionSeverity, ReasonCode
from aifund.rules.engine import NO_RULES, RISK_FACTOR_FLOOR, RuleVerdict


class Calibrator(Protocol):
    def __call__(self, p_raw: int, /) -> int: ...


def identity(p_raw: int) -> int:
    return p_raw


@dataclass(frozen=True)
class PortfolioDecision:
    decision: FinalDecision
    blocked_by: str | None = None
    rules_matched: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Vote:
    """One specialist's validated trade proposal and its family's weight."""

    family: str
    weight: Decimal
    proposal: TradeProposal


@dataclass(frozen=True)
class Ballot:
    """The committee's agreed direction and weighted confidence, before the critic."""

    direction: Direction
    combined: int
    best: Vote  # the most confident vote: its setup, invalidation and target are the committee's


def ballot(votes: Sequence[Vote]) -> Ballot | ReasonCode:
    """Specialists that proposed a trade vote; none → COMMITTEE_HOLD, opposite sides → COMMITTEE_SPLIT."""
    trades = [v for v in votes if v.proposal.is_trade]
    if not trades:
        return ReasonCode.COMMITTEE_HOLD
    if len({v.proposal.direction for v in trades}) > 1:
        return ReasonCode.COMMITTEE_SPLIT
    total = sum((v.weight for v in trades), Decimal(0))
    mean = sum((v.weight * v.proposal.confidence for v in trades), Decimal(0)) / total
    best = max(trades, key=lambda v: (v.proposal.confidence, v.weight, v.family))
    return Ballot(best.proposal.direction, int(mean.quantize(Decimal(1), ROUND_HALF_UP)), best)


def critic_penalty(critique: Critique, points: Mapping[ObjectionSeverity, int]) -> int:
    return sum(points[o.severity] for o in critique.objections)


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
        return self._decide(decision_id, symbol, proposal, proposal.confidence, rules)

    def decide_committee(
        self, *, decision_id: str, symbol: str, ballot: Ballot, penalty: int, rules: RuleVerdict = NO_RULES
    ) -> PortfolioDecision:
        """The committee's decision: the ballot's confidence less the critic's ``penalty``, then decided."""
        p_raw = min(max(ballot.combined - max(penalty, 0), 0), 100)
        return self._decide(decision_id, symbol, ballot.best.proposal, p_raw, rules)

    def _decide(
        self, decision_id: str, symbol: str, proposal: TradeProposal, p_raw: int, rules: RuleVerdict
    ) -> PortfolioDecision:
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
