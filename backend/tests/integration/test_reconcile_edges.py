"""Reconcile edge paths (R.0 coverage): history errors, lagging history, close-reason matching fallbacks."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.domain.enums import CloseReason, IntentKind, IntentStatus, Side, Timeframe
from aifund.domain.intent import COMMENT_PREFIX
from aifund.domain.market import Deal, FillingMode, OrderRequest, TradeAction
from aifund.execution.executor import Executor
from aifund.ports.broker import BrokerUnavailable
from aifund.reconcile.virtual import VirtualTracker
from tests.integration.test_enrichment import closed_by_sl, enricher
from tests.integration.test_reconciler import World, bars, close_intent, make_world, open_position, trade
from tests.integration.test_virtual_trades import ENTRY, pending
from tests.unit.risk.test_stops import XAU

from .conftest import T0

MAGIC = 26092801


class HistoryDown(SimBroker):
    """deals_for_position fails while ``history_down`` is set (terminal history not reachable)."""

    history_down = False

    async def deals_for_position(self, position_id: int) -> list[Deal]:
        if self.history_down:
            raise BrokerUnavailable("history unavailable")
        return await super().deals_for_position(position_id)


def world_with_history_down(factory: sessionmaker[Session], clock: FakeClock) -> tuple[World, HistoryDown]:
    clock.set(T0 + timedelta(seconds=5))
    feed = ReplayFeed({("XAUUSD", Timeframe.M1): bars()}, {"XAUUSD": XAU}, clock)
    broker = HistoryDown(feed=feed, clock=clock, config=SimConfig(commission_per_lot_side=D("3.5")))
    executor = Executor(broker, feed, factory, clock, account_id="acc")
    return World(clock, broker, executor, NullNotifier(), factory), broker


# ---------------------------------------------------------------------------------------------- reconciler


async def test_target_moves_are_tracked(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    w.broker._positions[pid].tp = D("4190.00")
    assert (await w.reconciler().run_once()).updated == [pid]
    assert trade(w, pid).current_tp == D("4190.00")


async def test_partial_close_waits_while_history_is_down(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w, broker = world_with_history_down(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    broker.close_externally(pid, volume=D("0.02"))
    broker.history_down = True
    report = await w.reconciler().run_once()
    assert report.errors
    assert report.partial == []
    assert trade(w, pid).volume_open_now == D("0.04")  # nothing half-written
    broker.history_down = False
    assert (await w.reconciler().run_once()).partial == [pid]


async def test_close_waits_while_history_is_down(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w, broker = world_with_history_down(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    broker.close_externally(pid)
    broker.history_down = True
    report = await w.reconciler().run_once()
    assert report.errors
    assert report.closed == []
    broker.history_down = False
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.MANUAL_EXTERNAL)]


async def test_last_engine_close_of_several_gives_the_reason(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    first = close_intent(w, pid, "0.02", CloseReason.TIME_STOP, IntentKind.CLOSE)
    assert (await w.executor.execute(first, XAU)).status is IntentStatus.FILLED
    await w.reconciler().run_once()
    second = close_intent(w, pid, "0.02", CloseReason.FLATTEN, IntentKind.FLATTEN)
    assert (await w.executor.execute(second, XAU)).status is IntentStatus.FILLED
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.FLATTEN)]


async def test_our_close_with_unmatched_tickets_takes_our_last_closes_reason(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    partial = close_intent(w, pid, "0.02", CloseReason.TIME_STOP, IntentKind.CLOSE)
    assert (await w.executor.execute(partial, XAU)).status is IntentStatus.FILLED
    await w.reconciler().run_once()
    # the rest is closed under our magic and comment prefix, but by no recorded intent (tickets rewritten)
    raw = OrderRequest(
        action=TradeAction.DEAL, symbol="XAUUSD", side=Side.SELL, volume=D("0.02"), price=D("4150.00"),
        deviation_points=20, magic=MAGIC, comment=f"{COMMENT_PREFIX}rewritten", filling=FillingMode.IOC,
        position_ticket=pid,
    )  # fmt: skip
    assert await w.broker.order_send(raw) is not None
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.TIME_STOP)]


# ---------------------------------------------------------------------------------------------- enrichment


async def test_enrichment_reports_broker_errors_and_retries(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await closed_by_sl(w)
    w.broker.connected = False
    report = await enricher(w).run_once()
    assert report.errors
    assert report.enriched == []
    w.broker.connected = True
    assert (await enricher(w).run_once()).enriched == [pid]


async def test_enrichment_without_visible_exit_deals_has_no_exit_slippage(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await closed_by_sl(w)
    w.broker.hide_deals(pid)  # the history lags behind the ledger
    assert (await enricher(w).run_once()).enriched == [pid]
    t = trade(w, pid)
    assert t.exit_slippage_points is None
    assert t.holding_minutes == 20


# ---------------------------------------------------------------------------------------------- virtual


async def test_virtual_tracker_reports_broker_errors_and_retries(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    vid = pending(w)
    clock.set(ENTRY + timedelta(minutes=1, seconds=5))
    w.broker.connected = False
    report = await VirtualTracker(w.broker, w.broker.feed, factory, clock).run_once()
    assert report.errors
    assert report.errors[0].startswith(vid)
    w.broker.connected = True
    assert (await VirtualTracker(w.broker, w.broker.feed, factory, clock).run_once()).entered == [vid]
