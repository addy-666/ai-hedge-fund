"""Committee vs analyst vs baseline (roadmap 8.4): per-arm stats, agreement, paired uplift net of LLM cost."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.domain.trade import CommitteeBar
from aifund.research.committee import compare

T = datetime(2026, 10, 1, tzinfo=UTC)


def bar(i: int, base: str, analyst: str | None, committee: str | None, a_dir: str = "BUY",
        c_dir: str = "BUY", a_cost: str = "0.01", c_cost: str = "0.03") -> CommitteeBar:  # fmt: skip
    return CommitteeBar(
        decision_id=f"d{i}", symbol="XAUUSD", bar_time=T + timedelta(hours=i),
        baseline_r=D(base), analyst_r=D(analyst or 0), committee_r=D(committee or 0),
        baseline_traded=True, analyst_traded=analyst is not None, committee_traded=committee is not None,
        analyst_direction=a_dir if analyst is not None else None,
        committee_direction=c_dir if committee is not None else None,
        analyst_cost_usd=D(a_cost), committee_cost_usd=D(c_cost),
    )  # fmt: skip


def test_the_comparison() -> None:
    bars = [
        bar(0, "-1", "-1", None),  # the committee stayed out of a loser
        bar(1, "2", "2", "2"),  # both took the winner
        bar(2, "1", "-1", "1", a_dir="SELL"),  # they disagreed
        bar(3, "0.5", None, None),  # only the baseline traded
    ]
    out = compare(bars, risk_usd=D(10))
    base, analyst, comm = out.arms
    assert (base.trades, base.total_r, base.win_rate) == (4, D("2.5"), 0.75)
    assert (analyst.trades, analyst.total_r, analyst.cost_usd) == (3, D(0), D("0.04"))
    assert (comm.trades, comm.total_r, comm.mean_r_per_trade, comm.cost_usd) == (2, D(3), 1.5, D("0.12"))
    assert out.agreement == 0.3333  # of the 3 bars where either LLM arm traded, 1 agreed
    assert (out.bars, out.first, out.last) == (4, T, T + timedelta(hours=3))
    # per bar: committee - 0.003 - (analyst - 0.001) = committee - analyst - 0.002
    assert out.vs_analyst is not None
    assert out.vs_analyst.mean == pytest.approx((1 + 0 + 2 + 0) / 4 - 0.002)
    assert out.vs_baseline is not None
    assert out.vs_baseline.mean == pytest.approx((1 + 0 + 0 - 0.5) / 4 - 0.003)


def test_without_risk_or_bars_there_is_no_uplift() -> None:
    out = compare([bar(0, "1", "1", "1")], risk_usd=None)
    assert (out.vs_analyst, out.vs_baseline, out.agreement) == (None, None, 1.0)
    empty = compare([], risk_usd=D(10))
    assert (empty.bars, empty.first, empty.agreement, empty.vs_analyst) == (0, None, None, None)
    assert empty.arms[2].mean_r_per_trade is None
    with pytest.raises(ValueError, match="positive"):
        compare([], risk_usd=D(0))
