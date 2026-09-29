"""Confidence pipeline (roadmap 4.5, docs/03 §8): calibration, capped penalties, blocks, risk factor floor."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal as D

from aifund.agents.portfolio_manager import PortfolioManager, RuleHit
from aifund.domain.decision import FeatureSnapshot, TradeProposal
from aifund.domain.enums import Direction
from tests.unit.agents.inputs import SNAPSHOT

PROPOSAL = TradeProposal(
    direction=Direction.LONG, confidence=74, setup_tag="mtf_trend_pullback", invalidation_price=D("4138.2"),
    target_price=D("4182.5"), thesis="t",
)  # fmt: skip


class Rules:
    def __init__(self, *hits: RuleHit) -> None:
        self.hits = hits

    def matches(self, symbol: str, proposal: TradeProposal, snapshot: FeatureSnapshot) -> Sequence[RuleHit]:
        return self.hits


def decide(pm: PortfolioManager):  # type: ignore[no-untyped-def]
    return pm.decide(
        decision_id="01JAXDECISION000000000000A", symbol="XAUUSD", proposal=PROPOSAL, snapshot=SNAPSHOT
    )


def test_without_rules_or_calibration_the_confidence_passes_through() -> None:
    out = decide(PortfolioManager(max_total_penalty=40))
    d = out.decision
    assert (d.llm_confidence, d.calibrated_confidence, d.penalty_points, d.final_confidence) == (
        74,
        74,
        0,
        74,
    )
    assert (d.risk_factor, d.invalidation_price, d.target_price) == (D(1), D("4138.2"), D("4182.5"))
    assert (out.blocked_by, out.rules_matched) == (None, ())


def test_calibration_then_capped_penalties() -> None:
    rules = Rules(RuleHit("R-1", penalty_points=25), RuleHit("R-2", penalty_points=30))
    d = decide(PortfolioManager(max_total_penalty=40, calibrator=lambda p: p - 10, rules=rules)).decision
    assert (d.calibrated_confidence, d.penalty_points, d.final_confidence) == (
        64,
        40,
        24,
    )  # 25+30 capped at 40


def test_calibrator_output_is_clamped() -> None:
    assert (
        decide(
            PortfolioManager(max_total_penalty=40, calibrator=lambda p: 140)
        ).decision.calibrated_confidence
        == 100
    )


def test_a_block_rule_blocks_and_risk_scales_multiply_with_a_floor() -> None:
    rules = Rules(
        RuleHit("R-7", risk_scale=D("0.5")), RuleHit("R-9", block=True), RuleHit("R-3", risk_scale=D("0.4"))
    )
    out = decide(PortfolioManager(max_total_penalty=40, rules=rules))
    assert out.blocked_by == "R-9"
    assert out.rules_matched == ("R-7", "R-9", "R-3")
    assert out.decision.risk_factor == D("0.25")  # 0.5 x 0.4 = 0.20, floored at 0.25


def test_rules_never_scale_risk_up() -> None:
    out = decide(PortfolioManager(max_total_penalty=40, rules=Rules(RuleHit("R-1", risk_scale=D("1.5")))))
    assert out.decision.risk_factor == D(1)
