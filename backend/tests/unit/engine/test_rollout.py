"""Rollout gates (roadmap 9.6-9.7, docs/06 §10): the level from the config, every L2 gate, L3's additions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from aifund.config.loader import load_trading_config
from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import Mode
from aifund.domain.rollout import ClosedTrade, RolloutFacts
from aifund.engine.rollout import GateStatus, Level, demo_gates, evaluate, level_of

EXAMPLE = Path(__file__).resolve().parents[4] / "config" / "trading.example.yaml"
NOW = datetime(2026, 11, 30, 12, tzinfo=UTC)
START = NOW - timedelta(days=30)


def cfg(mode: Mode = Mode.DEMO, *, dry_run: bool = False, risk: str = "0.5") -> TradingConfig:
    base = load_trading_config(EXAMPLE).config
    return base.model_copy(update={
        "engine": base.engine.model_copy(update={"mode": mode}),
        "strategy": base.strategy.model_copy(update={"dry_run": dry_run}),
        "risk": base.risk.model_copy(update={"risk_per_trade_pct": D(risk)}),
    })  # fmt: skip


def trades(n: int, r: str = "0.2", costs: str = "-0.70", volume: str = "0.10") -> list[ClosedTrade]:
    return [ClosedTrade(D(r), D("10"), D(costs), D(volume), START + timedelta(hours=i)) for i in range(n)]


def facts(**over: Any) -> RolloutFacts:
    base: dict[str, Any] = dict(
        period_start=START, trades=trades(100), ledger_status="ok", ledger_at=NOW - timedelta(hours=10),
        flatten_tested=1, restarts_with_open=1,
    )  # fmt: skip
    return RolloutFacts(**{**base, **over})


@pytest.mark.parametrize(
    ("mode", "dry_run", "risk", "level"),
    [(Mode.SIM, False, "0.5", Level.L0), (Mode.DEMO, True, "0.5", Level.L1),
     (Mode.PAPER, False, "0.5", Level.L1), (Mode.DEMO, False, "0.5", Level.L2),
     (Mode.LIVE, False, "0.25", Level.L3), (Mode.LIVE, False, "0.5", Level.L4)],
)  # fmt: skip
def test_the_level_follows_the_config(mode: Mode, dry_run: bool, risk: str, level: Level) -> None:
    c = cfg(Mode.SIM, dry_run=dry_run, risk=risk)
    c = c.model_copy(update={"engine": c.engine.model_construct(**{**c.engine.__dict__, "mode": mode})})
    assert level_of(c) is level


def test_a_demo_period_that_meets_every_gate_is_ready() -> None:
    report = evaluate(cfg(), facts(), NOW)
    assert report.level is Level.L2
    assert [g.status for g in report.gates] == [GateStatus.PASS] * 9
    assert report.ready


def test_the_demo_override_is_said_but_does_not_block_l2() -> None:
    """Roadmap 10.11: L2 tests the order plumbing, so the override is a note there; L3 needs evidence."""
    c = cfg()
    c = c.model_copy(update={"engine": c.engine.model_copy(update={"demo_orders_without_evidence": True})})
    report = evaluate(c, facts(), NOW)
    (note,) = [g for g in report.gates if g.name == "evidence_override"]
    assert (note.status, note.value) == (GateStatus.MANUAL, "ON: DEMO orders without E1 / G-LLM evidence")
    assert "E1 + E2" in note.need
    assert report.ready


@pytest.mark.parametrize(
    ("over", "gate"),
    [
        ({"period_start": NOW - timedelta(days=27)}, "duration"),
        ({"period_start": None}, "duration"),
        ({"trades": trades(99)}, "closed_trades"),
        ({"duplicate_opens": 1}, "duplicate_orders"),
        ({"unknown_intents": 1}, "unreconciled_or_lost"),
        ({"ledger_status": "diff"}, "net_pnl_matches_mt5"),
        ({"ledger_at": NOW - timedelta(hours=37)}, "unreconciled_or_lost"),
        ({"ledger_status": None, "ledger_at": None}, "net_pnl_matches_mt5"),
        ({"sl_missing": 1}, "positions_without_sl"),
        ({"without_context": 2}, "trades_with_context"),
        ({"flatten_tested": 0}, "kill_switch_tested"),
        ({"restarts_with_open": 0}, "restart_with_open_positions"),
    ],
)
def test_each_demo_gate_can_fail(over: dict[str, Any], gate: str) -> None:
    gates = {g.name: g for g in demo_gates(facts(**over), NOW)}
    assert gates[gate].status is GateStatus.FAIL
    assert not evaluate(cfg(), facts(**over), NOW).ready


def test_live_micro_adds_risk_costs_and_expectancy() -> None:
    live = cfg(Mode.LIVE, risk="0.25")
    ok = evaluate(live, facts(baseline_trades=trades(50, costs="-0.65")), NOW)
    extra = {g.name: g for g in ok.gates[9:]}
    assert [g.status for g in ok.gates] == [GateStatus.PASS] * 12
    assert extra["costs_vs_demo"].value == "7.00 vs 6.50 per lot (8%)"
    assert extra["expectancy_net"].value.startswith("+0.200R, 90% CI")
    dear = evaluate(live, facts(baseline_trades=trades(50, costs="-0.50")), NOW)  # 40% dearer live
    assert {g.name: g.status for g in dear.gates}["costs_vs_demo"] is GateStatus.FAIL
    losing = evaluate(live, facts(trades=trades(100, r="-0.1"), baseline_trades=trades(5)), NOW)
    assert {g.name: g.status for g in losing.gates}["expectancy_net"] is GateStatus.FAIL
    empty = {g.name: g for g in evaluate(live, facts(trades=[], baseline_trades=[]), NOW).gates}
    assert (empty["costs_vs_demo"].value, empty["expectancy_net"].value) == (
        "no trades to compare",
        "no closed trades",
    )
    free = facts(trades=trades(100, costs="0"), baseline_trades=trades(5, costs="0"))
    assert {g.name: g.status for g in evaluate(live, free, NOW).gates}["costs_vs_demo"] is GateStatus.PASS
    paid_now = facts(trades=trades(100), baseline_trades=trades(5, costs="0"))
    assert {g.name: g.status for g in evaluate(live, paid_now, NOW).gates}["costs_vs_demo"] is GateStatus.FAIL
    no_r = facts(trades=[ClosedTrade(None, D(1), D(0), D("0.1"), START)] * 100, baseline_trades=trades(1))
    assert {g.name: g.status for g in evaluate(live, no_r, NOW).gates}["expectancy_net"] is GateStatus.FAIL


@pytest.mark.parametrize(("mode", "dry_run", "risk"), [(Mode.SIM, False, "0.5"), (Mode.DEMO, True, "0.5"),
                                                        (Mode.LIVE, False, "0.5")])  # fmt: skip
def test_the_other_levels_are_operator_judgements(mode: Mode, dry_run: bool, risk: str) -> None:
    report = evaluate(cfg(mode, dry_run=dry_run, risk=risk), RolloutFacts(period_start=None), NOW)
    assert {g.status for g in report.gates} == {GateStatus.MANUAL}
    assert report.ready is (report.level is not Level.L4)  # nothing above LIVE
