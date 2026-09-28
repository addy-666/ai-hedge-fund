"""Scenario: the whole Phase 2 stack replayed over synthetic data.

bar clock → pipeline → risk → executor → SimBroker, with an aggressive stub detector so duplicates,
reversals, cooldowns and SL/TP exits all occur. The invariants (one position per symbol, no orphans,
one decision per bar, nothing left mid-send) must hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.config.loader import load_trading_config
from aifund.config.trading_config import ProfileConfig, SymbolConfig
from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import DecisionOutcome, Direction, Timeframe
from aifund.engine.equity import EquityTracker
from aifund.engine.pipeline import DecisionPipeline
from aifund.engine.replay import run_replay
from aifund.execution.executor import Executor
from aifund.market.bar_clock import BarClock
from aifund.persistence.repositories.cursors import DecisionCursorStore
from aifund.persistence.tables import DecisionRow, OrderIntentRow
from aifund.risk.manager import RiskManager
from aifund.strategies.base import TfRoles
from tests.fakes.synthetic import aggregate, random_walk_m1
from tests.unit.risk.test_stops import XAU

pytestmark = pytest.mark.scenario
CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"
START = datetime(2026, 6, 1, tzinfo=UTC)  # a Monday
WARMUP_DAYS, REPLAY_DAYS = 51, 2


class HourlyStub:
    """Fires at every full hour; LONG in hours 0-1 mod 4, SHORT in hours 2-3 mod 4. No key levels."""

    setup_tag = "stub_hourly"
    playbook_id = "stub"
    version = "test"

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        closed_at = snapshot.bar_time + timedelta(minutes=15)
        if closed_at.minute != 0:
            return []
        direction = Direction.LONG if closed_at.hour % 4 < 2 else Direction.SHORT
        return [
            SetupCandidate(
                setup_tag="stub_hourly", playbook_id="stub", direction_hint=direction, strength=0.5
            )
        ]


@pytest.fixture(scope="module")
def market() -> dict[tuple[str, Timeframe], list]:  # type: ignore[type-arg]
    m1 = random_walk_m1("XAUUSD", START, (WARMUP_DAYS + REPLAY_DAYS + 1) * 1440)
    series = {("XAUUSD", Timeframe.M1): m1}
    for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        series[("XAUUSD", tf)] = aggregate(m1, tf)
    return series


async def test_baseline_stack_replay_holds_every_invariant(
    market: dict,
    factory: sessionmaker[Session],  # type: ignore[type-arg]
) -> None:
    cfg = load_trading_config(CONFIG).config
    cfg = cfg.model_copy(update={"symbols": [s for s in cfg.symbols if s.canonical == "XAUUSD"]})
    profile = ProfileConfig(trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=[Timeframe.H4])
    clock = FakeClock(START + timedelta(days=WARMUP_DAYS, seconds=5))
    feed = ReplayFeed(market, {"XAUUSD": XAU}, clock)
    broker = SimBroker(feed=feed, clock=clock, config=SimConfig(starting_balance=Decimal("10000")))
    executor = Executor(broker, feed, factory, clock, account_id="acc")
    risk = RiskManager(cfg.risk, magic=cfg.engine.magic, broker=broker, clock=clock)

    def detectors(_s: SymbolConfig, _r: TfRoles) -> list[HourlyStub]:
        return [HourlyStub()]

    pipeline = DecisionPipeline(
        cfg, broker=broker, market=feed, factory=factory, clock=clock, executor=executor, risk=risk,
        detectors=detectors, equity=EquityTracker(cfg.engine.trading_day_boundary_utc), account_id="acc",
        profile_override={"intraday_m15": profile},
    )  # fmt: skip
    bar_clock = BarClock(feed, clock, [("XAUUSD", Timeframe.M15)], DecisionCursorStore(factory))
    end = START + timedelta(days=WARMUP_DAYS + REPLAY_DAYS)
    report = await run_replay(
        pipeline=pipeline,
        bar_clock=bar_clock,
        broker=broker,
        clock=clock,
        factory=factory,
        magic=cfg.engine.magic,
        end=end,
    )
    print(report.render())

    assert report.violations == []
    assert report.bar_events == REPLAY_DAYS * 96  # every M15 bar decided exactly once
    assert report.fills >= 3
    assert report.closed_trades >= 2
    assert report.outcomes[DecisionOutcome.ORDERED.value] >= 3
    assert report.outcomes[DecisionOutcome.NO_SETUP.value] >= 100  # 3 of 4 bars per hour have no setup
    assert report.outcomes[DecisionOutcome.RISK_REJECTED.value] >= 1  # guards fired
    assert report.max_positions_per_symbol == 1
    with factory() as s:
        ordered = s.scalars(
            select(DecisionRow.id).where(DecisionRow.outcome == DecisionOutcome.ORDERED)
        ).all()
        linked = set(s.scalars(select(OrderIntentRow.decision_id)).all())
        in_progress = s.scalars(
            select(DecisionRow.id).where(DecisionRow.reason_detail == "in progress")
        ).all()
    assert set(ordered) <= linked  # every order traces back to its decision
    assert in_progress == []  # every run completed its record
