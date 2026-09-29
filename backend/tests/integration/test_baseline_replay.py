"""Scenario: the whole trading stack replayed over synthetic data.

bar clock → pipeline → risk → executor → SimBroker → reconciler, with an aggressive stub detector so
duplicates, reversals, cooldowns and SL/TP exits all occur. The invariants (one position per symbol, no
orphans, one decision per bar, nothing left mid-send, a ledger that matches the broker's deals) must hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM
from aifund.adapters.notify.null import NullNotifier
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.agents.analyst import Analyst
from aifund.config.loader import load_trading_config
from aifund.config.trading_config import ProfileConfig, StrategyConfig, SymbolConfig
from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import DecisionOutcome, Direction, ReasonCode, Timeframe
from aifund.engine.equity import EquitySnapshotter, EquityTracker
from aifund.engine.pipeline import DecisionPipeline
from aifund.engine.position_loop import PositionLoop
from aifund.engine.replay import ReplayReport, run_replay
from aifund.execution.executor import Executor
from aifund.market.bar_clock import BarClock
from aifund.persistence.repositories.cursors import DecisionCursorStore
from aifund.persistence.tables import DecisionRow, LLMCallRow, OrderIntentRow
from aifund.ports.llm import LLMRequest
from aifund.reconcile.enrichment import Enricher
from aifund.reconcile.reconciler import Reconciler
from aifund.reconcile.virtual import VirtualTracker
from aifund.risk.manager import RiskManager
from aifund.risk.position_manager import PositionManager
from aifund.strategies.base import TfRoles
from tests.fakes.synthetic import aggregate, random_walk_m1
from tests.unit.risk.test_stops import XAU

pytestmark = pytest.mark.scenario
CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"
PLAYBOOKS = Path(__file__).resolve().parents[1] / "fixtures" / "playbooks"
BASELINE = StrategyConfig(analyst_enabled=False, baseline_enabled=True)
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


def echo_candidate(request: LLMRequest) -> dict[str, Any]:
    """A FakeLLM analyst that takes the detected candidate, confidence 75: trades like the baseline."""
    user = request.messages[1].content
    line = next(x for x in user.splitlines() if x.startswith("- ") and "(strength" in x)
    tag, rest = line[2:].split(": ", 1)
    return {
        "direction": rest.split(" ")[0], "confidence": 75, "setup_tag": tag, "invalidation_price": None,
        "target_price": None, "thesis": "echo of the detected setup", "key_risks": [],
        "lessons_considered": [],
        "time_horizon_bars": None,
    }  # fmt: skip


async def replay(
    market: dict,  # type: ignore[type-arg]
    factory: sessionmaker[Session],
    *,
    strategy: StrategyConfig,
    llm: FakeLLM | None = None,
    days: int = REPLAY_DAYS,
) -> ReplayReport:
    cfg = load_trading_config(CONFIG).config
    cfg = cfg.model_copy(
        update={"symbols": [s for s in cfg.symbols if s.canonical == "XAUUSD"], "strategy": strategy}
    )
    profile = ProfileConfig(trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=[Timeframe.H4])
    clock = FakeClock(START + timedelta(days=WARMUP_DAYS, seconds=5))
    feed = ReplayFeed(market, {"XAUUSD": XAU}, clock)
    broker = SimBroker(feed=feed, clock=clock, config=SimConfig(starting_balance=Decimal("10000")))
    executor = Executor(broker, feed, factory, clock, account_id="acc")
    risk = RiskManager(cfg.risk, magic=cfg.engine.magic, broker=broker, clock=clock)

    def detectors(_s: SymbolConfig, _r: TfRoles) -> list[HourlyStub]:
        return [HourlyStub()]

    analyst = None
    if llm is not None:
        analyst = Analyst(llm, cfg.llm, playbooks=PLAYBOOKS)
    tracker = EquityTracker.for_engine(cfg.engine)  # shared by the pipeline and the snapshotter
    pipeline = DecisionPipeline(
        cfg, broker=broker, market=feed, factory=factory, clock=clock, executor=executor, risk=risk,
        detectors=detectors, equity=tracker, account_id="acc",
        profile_override={"intraday_m15": profile}, analyst=analyst,
    )  # fmt: skip
    bar_clock = BarClock(feed, clock, [("XAUUSD", Timeframe.M15)], DecisionCursorStore(factory))
    manager = PositionManager(cfg.position_management, cfg.risk.stops, magic=cfg.engine.magic, account=1)
    loop = PositionLoop(
        cfg, manager, broker=broker, market=feed, executor=executor, factory=factory, clock=clock
    )
    end = START + timedelta(days=WARMUP_DAYS + days)
    return await run_replay(
        pipeline=pipeline,
        bar_clock=bar_clock,
        position_loop=loop,
        equity_snapshotter=EquitySnapshotter(
            tracker, cfg.risk.limits, broker=broker, factory=factory, clock=clock, notifier=NullNotifier(),
            account_id="acc", magic=cfg.engine.magic, mode=cfg.engine.mode,
        ),
        virtual_tracker=VirtualTracker(broker, feed, factory, clock),
        enricher=Enricher(
            broker, feed, factory, clock, NullNotifier(),
            trigger_tfs={s.broker: cfg.profiles[s.profile].trigger_tf for s in cfg.symbols},
        ),
        reconciler=Reconciler(
            broker, factory, clock, NullNotifier(), account_id="acc", magic=cfg.engine.magic
        ),
        broker=broker,
        clock=clock,
        factory=factory,
        magic=cfg.engine.magic,
        end=end,
    )  # fmt: skip


async def test_baseline_stack_replay_holds_every_invariant(
    market: dict,  # type: ignore[type-arg]
    factory: sessionmaker[Session],
) -> None:
    report = await replay(market, factory, strategy=BASELINE)
    print(report.render())

    assert report.violations == []
    assert report.bar_events == REPLAY_DAYS * 96  # every M15 bar decided exactly once
    assert report.fills >= 3
    assert report.closed_trades >= 2
    assert report.outcomes[DecisionOutcome.ORDERED.value] >= 3
    # 3 of 4 bars per hour have no setup; the session calendar skips the two 21:00-22:00 UTC daily breaks
    # (8 bars), which also breaks the stub's LONG,LONG,SHORT,SHORT rhythm and sets off the flip-flop lock
    assert report.outcomes[DecisionOutcome.NO_SETUP.value] >= 50
    assert report.reasons[ReasonCode.MARKET_CLOSED.value] == 8
    assert report.outcomes[DecisionOutcome.RISK_REJECTED.value] >= 1  # guards fired
    assert report.max_positions_per_symbol == 1
    assert report.ledger_closed == report.closed_trades  # every closed position is a CLOSED trade
    assert sum(report.close_reasons.values()) == report.closed_trades
    assert report.enriched == report.closed_trades  # MAE/MFE etc. for every closed trade
    # guard rejections (flip-flop, cooldown, reversal rules...) get counterfactual trades; duplicates do not
    assert report.virtual is not None
    assert sum(report.virtual.values()) >= 1
    assert report.snapshots == REPLAY_DAYS * 24 * 60  # one per minute step
    assert report.final_equity is not None
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


async def test_analyst_replay_with_a_fake_llm(
    market: dict,  # type: ignore[type-arg]
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    """The full stack with the analyst deciding (roadmap 4.6); every LLM call recorded with its decision."""
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)
    report = await replay(market, factory, strategy=StrategyConfig(), llm=llm, days=1)
    print(report.render())
    assert report.violations == []
    assert report.outcomes[DecisionOutcome.ORDERED.value] >= 2
    with factory() as s:
        analysed = s.scalars(select(DecisionRow).where(DecisionRow.prompt_version == "analyst_v1")).all()
        calls = s.scalars(select(LLMCallRow)).all()
    assert len(analysed) == len(llm.requests) == len(calls) >= 12  # one call per bar with a setup
    assert {d.model for d in analysed} == {"fake-llm"}
    assert {c.decision_id for c in calls} == {d.id for d in analysed}
    assert all(d.llm_confidence == 75 and d.final_confidence == 75 for d in analysed)


async def test_dry_run_decides_but_sends_nothing(
    market: dict,  # type: ignore[type-arg]
    factory: sessionmaker[Session],
    clock: FakeClock,
) -> None:
    llm = FakeLLM([echo_candidate], factory=factory, clock=clock)
    report = await replay(market, factory, strategy=StrategyConfig(dry_run=True), llm=llm, days=1)
    assert report.violations == []
    assert report.outcomes[DecisionOutcome.DRY_RUN.value] >= 2
    assert report.fills == 0
    with factory() as s:
        assert s.scalars(select(OrderIntentRow)).all() == []  # zero intents
        dry = s.scalars(select(DecisionRow).where(DecisionRow.outcome == DecisionOutcome.DRY_RUN)).all()
    assert all(d.reason_detail and d.reason_detail.startswith("would send OPEN") for d in dry)
    assert all(d.risk_calc and "sl" in d.risk_calc for d in dry)  # fully risk-checked and sized
