"""Equity snapshotter against the SimBroker and a migrated database (roadmap 3.5).

Account 10,000.00, commission 3.50 per lot per side. A BUY of 0.04 XAUUSD fills at the ask 4150.28 with the
bid at 4150.00: balance 10000 - 0.14 = 9999.86, floating (4150.00 - 4150.28) x 100 x 0.04 = -1.12, so equity
9998.74. Planned risk 47.72.
"""

from __future__ import annotations

from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.config.trading_config import LimitsConfig
from aifund.domain.enums import EngineState, EventType, Mode
from aifund.engine.equity import EquitySnapshotter, EquityTracker
from aifund.persistence.tables import EngineStateRow, EquitySnapshotRow, EventRow
from aifund.ports.system import Severity
from tests.integration.test_reconciler import MAGIC, World, make_world, open_position

from .conftest import T0


def snapshotter(w: World, limits: LimitsConfig | None = None) -> tuple[EquitySnapshotter, EquityTracker]:
    tracker = EquityTracker("17:00", "America/New_York")
    s = EquitySnapshotter(
        tracker, limits or LimitsConfig(), broker=w.broker, factory=w.factory, clock=w.clock,
        notifier=w.notifier, account_id="acc", magic=MAGIC, mode=Mode.SIM,
    )  # fmt: skip
    return s, tracker


def snapshots(w: World) -> list[EquitySnapshotRow]:
    with w.factory() as s:
        return list(s.scalars(select(EquitySnapshotRow).order_by(EquitySnapshotRow.id)).all())


def engine_row(w: World) -> EngineStateRow:
    with w.factory() as s:
        row = s.get(EngineStateRow, "acc")
        assert row is not None
        s.expunge(row)
        return row


async def test_snapshot_rows_and_persisted_references(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    snap, _ = snapshotter(w)
    await snap.start()
    await snap.run_once()  # flat: 10,000.00
    await open_position(w)
    clock.advance(seconds=60)
    report = await snap.run_once()
    assert report.breach is None
    first, second = snapshots(w)
    assert (first.equity, first.open_positions, first.day_pnl, first.drawdown_pct) == (
        D("10000"),
        0,
        D("0"),
        D("0"),
    )
    assert (second.balance, second.equity) == (D("9999.86"), D("9998.74"))
    assert (second.open_positions, second.open_risk_money) == (1, D("47.72"))
    assert second.day_pnl == D("-1.26")  # 9998.74 - 10000.00
    assert second.drawdown_pct == D("0.01")  # 1.26 / 10000 = 0.0126% -> 0.01
    assert second.margin == second.open_notional / 100  # notional = broker margin x leverage (1:100)
    row = engine_row(w)
    # trading day of Mon 2026-09-28 09:00 UTC started Sun 17:00 New York = 21:00 UTC
    assert (row.day_start_equity, row.peak_equity, row.week_start_equity) == (
        D("10000"),
        D("10000"),
        D("10000"),
    )
    assert row.day_start_at == T0.replace(day=27, hour=21)


async def test_a_restart_keeps_peak_and_day_start(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    snap, _ = snapshotter(w)
    await snap.start()
    await snap.run_once()
    await open_position(w)
    # the engine restarts: a new tracker, restored from engine_state and the last snapshot
    restarted, tracker = snapshotter(w)
    await restarted.start()
    assert (tracker.refs.peak_equity, tracker.refs.day_start_equity) == (D("10000"), D("10000"))
    clock.advance(seconds=60)
    report = await restarted.run_once()
    assert report.state is not None
    assert report.state.day_start_equity == D("10000")  # not reset to the post-restart equity 9998.74


async def test_a_breach_halts_once_and_alerts(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    tight = LimitsConfig(daily_loss_limit_pct=D("0.01"))  # 0.01%: the 1.26 floating loss breaches it
    snap, _ = snapshotter(w, tight)
    await snap.start()
    await snap.run_once()
    await open_position(w)
    clock.advance(seconds=60)
    report = await snap.run_once()
    assert report.breach is not None
    assert report.alerted
    assert (report.breach.kind.value, report.breach.loss_pct) == ("DAILY_LOSS", D("0.0126"))
    row = engine_row(w)
    assert row.state is EngineState.HALTED
    assert row.halt_reason is not None
    assert row.halt_reason.startswith("DAILY_LOSS")
    ((severity, title, _),) = w.notifier.sent
    assert (severity, title) == (Severity.CRITICAL, "Loss limit breached: DAILY_LOSS")
    clock.advance(seconds=60)
    assert not (await snap.run_once()).alerted  # once per limit and trading day
    with factory() as s:
        assert s.scalars(select(EventRow.type)).all() == [EventType.RISK_LIMIT_BREACH]


async def test_broker_down_writes_nothing(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    snap, _ = snapshotter(w)
    await snap.start()
    w.broker.connected = False
    report = await snap.run_once()
    assert report.errors
    assert report.state is None
    assert snapshots(w) == []
