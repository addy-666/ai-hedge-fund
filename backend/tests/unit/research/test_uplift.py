"""Gate G-LLM arithmetic (roadmap R.9) against a hand-computed fixture."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from aifund.config.trading_config import ResearchConfig
from aifund.research.uplift import Pair, uplift

# baseline, analyst, LLM cost; one trade risks $10, so each $0.10 call costs 0.01R
PAIRS = [
    Pair(D(1), D(2), D("0.10")),  # the analyst did better: +1 - 0.01 = +0.99
    Pair(D(-1), D(0), D("0.10")),  # the analyst held through a loser: +0.99
    Pair(D(2), D(2), D("0.10")),  # same trade: -0.01
    Pair(D(-1), D(-1), D("0.10")),  # same loser: -0.01
]


def test_the_paired_report_matches_the_hand_computed_fixture() -> None:
    r = uplift(PAIRS, risk_usd=D(10), cfg=ResearchConfig())
    assert r.n == 4
    assert (r.baseline_mean, r.analyst_mean, r.cost_mean_r) == (0.25, 0.75, 0.01)
    assert r.uplift.mean == pytest.approx(0.49)  # (0.99 + 0.99 - 0.01 - 0.01) / 4
    assert r.uplift.n == 4
    assert not r.passed  # 4 pairs, far below min_paired_signals
    assert next(c.name for c in r.checks if not c.passed) == "paired_signals"
    assert r.render().startswith(
        "4 paired bars: baseline +0.250R, analyst +0.750R, LLM cost 0.0100R -> uplift"
    )


def test_a_consistent_uplift_over_enough_pairs_passes_and_its_cost_can_sink_it() -> None:
    cfg = ResearchConfig(min_paired_signals=30)
    pairs = [Pair(D(0), D("0.5") if i % 2 else D("0.1"), D("0.10")) for i in range(40)]
    assert uplift(pairs, risk_usd=D(10), cfg=cfg).passed  # +0.29R a bar net of 0.01R cost
    assert not uplift(pairs, risk_usd=D("0.2"), cfg=cfg).passed  # the LLM costs 0.5R a bar: no uplift left


def test_no_pairs_and_bad_risk() -> None:
    r = uplift([], risk_usd=D(10), cfg=ResearchConfig())
    assert (r.n, r.baseline_mean, r.passed) == (0, 0.0, False)
    assert "n/a" in r.checks[1].detail
    with pytest.raises(ValueError, match="risk_usd"):
        uplift(PAIRS, risk_usd=D(0), cfg=ResearchConfig())
