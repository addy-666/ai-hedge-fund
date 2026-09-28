"""Position loop against the SimBroker: SL repair after a broker drops it, time stop, foreign positions."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, _Pos
from aifund.config.loader import load_trading_config
from aifund.domain.enums import IntentStatus, Side, Timeframe
from aifund.engine.position_loop import PositionLoop
from aifund.execution.executor import Executor
from aifund.risk.position_manager import ActionKind, PositionManager
from tests.integration.test_executor import m1_bars, open_intent
from tests.unit.risk.test_stops import XAU

from .conftest import T0

CONFIG = Path(__file__).resolve().parents[3] / "config" / "trading.example.yaml"


def build(factory: sessionmaker[Session], clock: FakeClock):  # type: ignore[no-untyped-def]
    cfg = load_trading_config(CONFIG).config
    clock.set(T0 + timedelta(seconds=5))
    bars = m1_bars()
    m15 = [b.model_copy(update={"timeframe": Timeframe.M15, "time": T0 - timedelta(minutes=15 * (60 - i))})
           for i, b in enumerate(bars[:60])]  # fmt: skip
    feed = ReplayFeed(
        {("XAUUSD", Timeframe.M1): bars, ("XAUUSD", Timeframe.M15): m15}, {"XAUUSD": XAU}, clock
    )
    broker = SimBroker(feed=feed, clock=clock)
    executor = Executor(broker, feed, factory, clock, account_id="acc")
    manager = PositionManager(cfg.position_management, cfg.risk.stops, magic=cfg.engine.magic, account=1)
    loop = PositionLoop(
        cfg, manager, broker=broker, market=feed, executor=executor, factory=factory, clock=clock
    )
    return broker, executor, loop


async def test_dropped_stop_loss_is_restored(factory: sessionmaker[Session], clock: FakeClock) -> None:
    broker, executor, loop = build(factory, clock)
    opened = await executor.execute(open_intent(clock), XAU)
    assert opened.status is IntentStatus.FILLED
    broker._positions[opened.position_id].sl = None  # the broker dropped the stop on a slipped fill
    report = await loop.run_once()
    ((kind, result),) = report.actions
    assert (kind, result.status) == (ActionKind.REPAIR_SL, IntentStatus.FILLED)
    (pos,) = await broker.positions()
    assert pos.sl == D("4138.35")  # fill 4150.28 - planned 11.93
    assert (await loop.run_once()).actions == []  # healthy now: nothing more to do


async def test_time_stop_closes_a_stale_position(factory: sessionmaker[Session], clock: FakeClock) -> None:
    broker, executor, loop = build(factory, clock)
    await executor.execute(open_intent(clock), XAU)
    clock.advance(minutes=15 * 48 + 1)
    # keep the quote fresh for the close (the synthetic feed ends after 60 minutes)
    broker.feed._bars[("XAUUSD", Timeframe.M1)] += [
        b.model_copy(update={"time": b.time + timedelta(hours=12)})
        for b in broker.feed._bars[("XAUUSD", Timeframe.M1)]
    ]
    broker.feed.__init__(broker.feed._bars, broker.feed.specs, clock)  # type: ignore[misc]
    report = await loop.run_once()
    ((kind, result),) = report.actions
    assert (kind, result.status) == (ActionKind.TIME_STOP, IntentStatus.FILLED)
    assert await broker.positions() == []


async def test_foreign_positions_are_never_touched(factory: sessionmaker[Session], clock: FakeClock) -> None:
    broker, _, loop = build(factory, clock)
    broker._positions[999] = _Pos(
        ticket=999,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=D("1"),
        price_open=D("4150"),
        sl=None,
        tp=None,
        magic=0,
        comment="manual",
        time=clock.now(),
    )
    assert (await loop.run_once()).actions == []
    (pos,) = await broker.positions()
    assert pos.sl is None  # not ours: no repair, no close
