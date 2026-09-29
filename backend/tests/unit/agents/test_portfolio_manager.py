"""Confidence pipeline (roadmap 4.5 + 7.2, docs/03 §8): calibration, then the rule engine's verdict."""

from __future__ import annotations

from decimal import Decimal as D

from aifund.agents.portfolio_manager import PortfolioDecision, PortfolioManager, with_rules
from aifund.domain.decision import TradeProposal
from aifund.domain.enums import Direction
from aifund.rules.engine import NO_RULES, RuleVerdict

PROPOSAL = TradeProposal(
    direction=Direction.LONG, confidence=74, setup_tag="mtf_trend_pullback", invalidation_price=D("4138.2"),
    target_price=D("4182.5"), thesis="t",
)  # fmt: skip


def decide(pm: PortfolioManager, rules: RuleVerdict = NO_RULES) -> PortfolioDecision:
    return pm.decide(
        decision_id="01JAXDECISION000000000000A", symbol="XAUUSD", proposal=PROPOSAL, rules=rules
    )


def test_without_rules_or_calibration_the_confidence_passes_through() -> None:
    out = decide(PortfolioManager())
    d = out.decision
    assert (d.llm_confidence, d.calibrated_confidence, d.penalty_points, d.final_confidence) == (
        74,
        74,
        0,
        74,
    )
    assert (d.risk_factor, d.invalidation_price, d.target_price) == (D(1), D("4138.2"), D("4182.5"))
    assert (out.blocked_by, out.rules_matched) == (None, ())


def test_calibration_then_the_rule_penalty() -> None:
    verdict = RuleVerdict(rulebook_version=3, penalty_points=40, active=("R-0001v1", "R-0002v1"))
    out = decide(PortfolioManager(calibrator=lambda p: p - 10), verdict)
    d = out.decision
    assert (d.calibrated_confidence, d.penalty_points, d.final_confidence) == (64, 40, 24)
    assert out.rules_matched == ("R-0001v1", "R-0002v1")


def test_calibrator_output_is_clamped() -> None:
    assert decide(PortfolioManager(calibrator=lambda p: 140)).decision.calibrated_confidence == 100


def test_a_block_is_passed_on_and_risk_is_never_scaled_up_or_below_the_floor() -> None:
    verdict = RuleVerdict(
        blocked_by="R-0009v1", risk_factor=D("0.1"), active=("R-0009v1",), shadow=("R-0004v2",)
    )
    out = decide(PortfolioManager(), verdict)
    assert out.blocked_by == "R-0009v1"
    assert out.rules_matched == ("R-0009v1", "R-0004v2 (shadow)")
    assert out.decision.risk_factor == D("0.25")
    assert decide(PortfolioManager(), RuleVerdict(risk_factor=D("1.5"))).decision.risk_factor == D(1)


def test_with_rules_applies_a_verdict_to_any_decision() -> None:
    base = decide(PortfolioManager()).decision
    ruled = with_rules(base, RuleVerdict(penalty_points=15, risk_factor=D("0.5")))
    assert (ruled.penalty_points, ruled.final_confidence, ruled.risk_factor) == (15, 59, D("0.5"))
    assert with_rules(base, RuleVerdict(penalty_points=-5)).penalty_points == 0
