"""Reconciler against the SimBroker and a migrated database (roadmap 3.1).

Scenarios: SL hit, TP hit, manual close, partial close, stop-out, orphan, engine down during a close, a crash
mid-send (no false orphan), a position that vanishes without closing deals, and idempotency.

Money is derived by hand. Fill: BUY 0.04 XAUUSD at the ask 4150.28 (bid 4150.00 + 28-point spread), SL
4138.35 (11.93 below), TP 4179.60 (29.32 above), commission 3.50 per lot per side = 0.14 per 0.04-lot deal,
risk budget 47.72. XAU: a 1.00 move on 1 lot is 100 USD.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import SimBroker, SimConfig
from aifund.domain._issuance import issue_order_intent
from aifund.domain.enums import (
    CloseReason,
    DealReason,
    EventType,
    IntentKind,
    IntentStatus,
    Side,
    Timeframe,
    TradeOutcome,
    TradeStatus,
)
from aifund.domain.ids import new_id
from aifund.domain.intent import intent_comment
from aifund.domain.market import Bar, FillingMode, OrderRequest, TradeAction
from aifund.execution.executor import Executor
from aifund.persistence.tables import DealRow, EventRow, OrderIntentRow, TradeRow
from aifund.ports.system import Severity
from aifund.reconcile.reconciler import Reconciler, ReconcileReport
from tests.integration.test_executor import m1_bars, open_intent
from tests.unit.risk.test_stops import XAU

from .conftest import T0

MAGIC = 26092801


@dataclass
class World:
    clock: FakeClock
    broker: SimBroker
    executor: Executor
    notifier: NullNotifier
    factory: sessionmaker[Session]

    def reconciler(self) -> Reconciler:
        """A fresh instance is a restarted engine: nothing may depend on in-memory state."""
        return Reconciler(self.broker, self.factory, self.clock, self.notifier, account_id="acc", magic=MAGIC)


def bars(*, shock: tuple[str, str] | None = None) -> list[Bar]:
    """Flat 4149-4151 minute bars; ``shock`` = ("low"|"high", price) on the bar opening at T0+20."""
    out = m1_bars()
    if shock is not None:
        field, price = shock
        i = next(i for i, b in enumerate(out) if b.time == T0 + timedelta(minutes=20))
        out[i] = out[i].model_copy(update={field: D(price)})
    return out


def make_world(
    factory: sessionmaker[Session], clock: FakeClock, shock: tuple[str, str] | None = None
) -> World:
    clock.set(T0 + timedelta(seconds=5))
    feed = ReplayFeed({("XAUUSD", Timeframe.M1): bars(shock=shock)}, {"XAUUSD": XAU}, clock)
    broker = SimBroker(feed=feed, clock=clock, config=SimConfig(commission_per_lot_side=D("3.5")))
    return World(
        clock, broker, Executor(broker, feed, factory, clock, account_id="acc"), NullNotifier(), factory
    )


async def open_position(w: World) -> int:
    result = await w.executor.execute(open_intent(w.clock), XAU)
    assert result.status is IntentStatus.FILLED
    assert result.position_id is not None
    return result.position_id


def trade(w: World, position_id: int) -> TradeRow:
    with w.factory() as s:
        row = s.scalars(select(TradeRow).where(TradeRow.position_id == position_id)).one()
        s.expunge(row)
        return row


def events(w: World) -> list[str]:
    with w.factory() as s:
        return list(s.scalars(select(EventRow.type).order_by(EventRow.seq)).all())


def deal_count(w: World, position_id: int) -> int:
    with w.factory() as s:
        return len(s.scalars(select(DealRow.ticket).where(DealRow.position_id == position_id)).all())


def close_intent(w: World, position_id: int, volume: str, reason: CloseReason | None, kind: IntentKind):  # type: ignore[no-untyped-def]
    intent_id = new_id()
    return issue_order_intent(
        id=intent_id, idempotency_key=new_id()[:24], decision_id=None, kind=kind, symbol="XAUUSD",
        side=Side.SELL, volume=D(volume), price_ref=D("4150.00"), risk_money=D(0), risk_pct=D(0),
        magic=MAGIC, comment=intent_comment(intent_id), position_ticket=position_id, close_reason=reason,
        created_at=w.clock.now(),
    )  # fmt: skip


# ---------------------------------------------------------------------------------------------- opening


async def test_a_fill_becomes_an_open_trade_linked_to_its_intent(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    report = await w.reconciler().run_once()
    assert report.opened == [pid]
    t = trade(w, pid)
    assert t.status is TradeStatus.OPEN
    assert (t.side, t.open_price, t.volume_opened, t.volume_open_now) == (
        Side.BUY,
        D("4150.28"),
        D("0.04"),
        D("0.04"),
    )
    assert (t.initial_sl, t.initial_tp) == (D("4138.35"), D("4179.60"))
    assert t.initial_risk_money == D("47.72")
    with factory() as s:
        (intent,) = s.scalars(select(OrderIntentRow)).all()
    assert t.intent_id == intent.id
    assert events(w) == [EventType.TRADE_OPENED]
    # nothing changed: a second cycle writes nothing
    assert await w.reconciler().run_once() == ReconcileReport()


async def test_stop_moves_are_tracked(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    w.broker._positions[pid].sl = D("4150.28")  # e.g. the position manager moved it to break-even
    report = await w.reconciler().run_once()
    assert report.updated == [pid]
    t = trade(w, pid)
    assert (t.initial_sl, t.current_sl) == (D("4138.35"), D("4150.28"))  # the initial stop stays the R basis


# ---------------------------------------------------------------------------------------------- closing


async def test_sl_hit(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await open_position(w)
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(minutes=25))
    report = await w.reconciler().run_once()
    assert report.closed == [(pid, CloseReason.SL)]
    t = trade(w, pid)
    assert t.status is TradeStatus.CLOSED
    assert t.close_price_vwap == D("4138.35")
    assert t.close_time == T0 + timedelta(minutes=20)  # stamped in the minute the stop was touched
    assert t.gross_profit == D("-47.72")  # -11.93 x 100 x 0.04
    assert t.commission == D("-0.28")  # 0.14 in + 0.14 out
    assert t.net_pnl == D("-48.00")
    assert t.r_multiple == D("-1.0059")  # -48.00 / 47.72 = -1.005867...
    assert t.outcome is TradeOutcome.LOSS
    assert t.volume_open_now == D("0")
    assert deal_count(w, pid) == 2
    assert events(w) == [EventType.TRADE_OPENED, EventType.TRADE_CLOSED]


async def test_tp_hit(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock, shock=("high", "4185.00"))
    pid = await open_position(w)
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(minutes=25))
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.TP)]
    t = trade(w, pid)
    assert (t.gross_profit, t.net_pnl) == (D("117.28"), D("117.00"))  # 29.32 x 4; less 0.28 commission
    assert t.r_multiple == D("2.4518")  # 117.00 / 47.72 = 2.451802...
    assert t.outcome is TradeOutcome.WIN


async def test_manual_close(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    w.broker.close_externally(pid)  # someone closes it in the terminal at the bid 4150.00
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.MANUAL_EXTERNAL)]
    t = trade(w, pid)
    assert t.net_pnl == D("-1.40")  # -0.28 x 100 x 0.04 = -1.12, less 0.28 commission
    assert t.r_multiple == D("-0.0293")  # -1.40 / 47.72
    assert t.outcome is TradeOutcome.BREAKEVEN


async def test_stop_out(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    w.broker.close_externally(pid, reason=DealReason.SO)
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.STOP_OUT)]


async def test_partial_close_then_engine_close(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()

    w.broker.close_externally(pid, volume=D("0.02"))  # half closed by hand at 4150.00
    report = await w.reconciler().run_once()
    assert report.partial == [pid]
    assert report.closed == []
    assert trade(w, pid).volume_open_now == D("0.02")
    assert deal_count(w, pid) == 2  # the partial's deals are mirrored as they happen

    # the engine closes the rest (a time stop): its intent's reason wins over DEAL_REASON_EXPERT
    closing = close_intent(w, pid, "0.02", CloseReason.TIME_STOP, IntentKind.CLOSE)
    assert (await w.executor.execute(closing, XAU)).status is IntentStatus.FILLED
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.TIME_STOP)]
    t = trade(w, pid)
    # each half: -0.28 x 100 x 0.02 = -0.56 and 0.07 commission; entry commission 0.14
    assert (t.gross_profit, t.commission, t.net_pnl) == (D("-1.12"), D("-0.28"), D("-1.40"))
    assert str(t.close_price_vwap) == "4150.0000"  # average of two exits at 4150.00
    assert deal_count(w, pid) == 3
    assert EventType.TRADE_PARTIAL_CLOSE in events(w)


async def test_reversal_close_without_explicit_reason(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    closing = close_intent(w, pid, "0.04", None, IntentKind.REVERSE_CLOSE)
    await w.executor.execute(closing, XAU)
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.REVERSAL)]


async def test_engine_down_during_the_whole_trade(factory: sessionmaker[Session], clock: FakeClock) -> None:
    """Filled, then stopped out, and no reconcile ran in between: the trade is built from its deals."""
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await open_position(w)
    clock.set(T0 + timedelta(minutes=25))
    report = await w.reconciler().run_once()
    assert report.opened == []
    assert report.closed == [(pid, CloseReason.SL)]
    t = trade(w, pid)
    assert t.status is TradeStatus.CLOSED
    assert t.intent_id is not None
    assert (t.open_price, t.volume_opened) == (D("4150.28"), D("0.04"))
    assert (t.initial_sl, t.initial_tp) == (D("4138.35"), D("4179.60"))  # from the intent's distances
    assert t.net_pnl == D("-48.00")
    assert events(w) == [EventType.TRADE_CLOSED]


async def test_engine_down_while_an_open_trade_closes(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock, shock=("high", "4185.00"))
    pid = await open_position(w)
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(hours=6))  # engine was down for hours; the TP filled meanwhile
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.TP)]


# ---------------------------------------------------------------------------------------------- orphans


def raw_request(magic: int, comment: str) -> OrderRequest:
    return OrderRequest(
        action=TradeAction.DEAL, symbol="XAUUSD", side=Side.SELL, volume=D("0.10"), price=D("4150.00"),
        deviation_points=20, magic=magic, comment=comment, filling=FillingMode.IOC,
    )  # fmt: skip


async def test_orphan_is_tracked_and_alerted(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    sent = await w.broker.order_send(raw_request(MAGIC, "another script"))  # our magic, not our intent
    assert sent is not None
    report = await w.reconciler().run_once()
    (pid,) = report.orphans
    assert len(report.alerts) == 1
    ((severity, title, _),) = w.notifier.sent
    assert (severity, title) == (Severity.WARN, "Orphan position")
    t = trade(w, pid)
    assert (t.status, t.intent_id, t.initial_risk_money) == (TradeStatus.ORPHAN_OPEN, None, None)
    assert (await w.reconciler().run_once()).orphans == []  # alerted once, tracked from now on

    w.broker.close_externally(pid)
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.MANUAL_EXTERNAL)]
    t = trade(w, pid)
    assert t.status is TradeStatus.ORPHAN_CLOSED
    # sold 0.10 at the bid 4150.00, bought back at the ask 4150.28: -0.28 x 100 x 0.10 = -2.80; 0.35 a side
    assert (t.gross_profit, t.commission, t.net_pnl) == (D("-2.80"), D("-0.70"), D("-3.50"))
    assert (t.r_multiple, t.outcome) == (None, None)  # no known risk: excluded from learning


async def test_foreign_magic_is_ignored(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    await w.broker.order_send(raw_request(12345, "another EA"))
    assert await w.reconciler().run_once() == ReconcileReport()
    with factory() as s:
        assert s.scalars(select(TradeRow)).all() == []


async def test_crash_mid_send_is_not_a_false_orphan(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)

    def crash(stage: str) -> None:
        if stage == "after_send":
            raise RuntimeError("process killed")

    crashing = Executor(w.broker, w.broker.feed, factory, clock, account_id="acc", checkpoint=crash)
    with pytest.raises(RuntimeError):
        await crashing.execute(open_intent(clock), XAU)
    (pos,) = await w.broker.positions()  # executed at the broker, intent still SENT in the database
    report = await w.reconciler().run_once()
    assert report.deferred == [pos.position_id]
    assert (report.orphans, w.notifier.sent) == ([], [])
    await w.executor.recover()  # startup recovery settles the intent: FILLED
    assert (await w.reconciler().run_once()).opened == [pos.position_id]
    assert trade(w, pos.position_id).status is TradeStatus.OPEN


# ---------------------------------------------------------------------------------------------- vanished


async def test_a_transient_gap_restarts_the_vanish_timer(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    reconciler = w.reconciler()
    await reconciler.run_once()
    hidden = w.broker._positions.pop(pid)  # positions_get briefly omits it
    assert (await reconciler.run_once()).waiting == [pid]
    w.broker._positions[pid] = hidden
    await reconciler.run_once()  # back: the timer resets
    clock.advance(minutes=4)
    w.broker._positions.pop(pid)
    assert (await reconciler.run_once()).waiting == [pid]
    clock.advance(minutes=2)  # 6 min since the first gap, but only 2 since this one
    assert (await reconciler.run_once()).alerts == []


async def test_vanished_position_alerts_once_then_closes(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    reconciler = w.reconciler()  # one running engine: the alert timer lives in it
    w.broker.hide_deals(pid)
    w.broker.close_externally(pid)  # gone, but the history does not show the closing deal yet

    report = await reconciler.run_once()
    assert (report.waiting, report.closed, report.alerts) == ([pid], [], [])
    clock.advance(minutes=4)
    assert (await reconciler.run_once()).alerts == []
    clock.advance(minutes=1, seconds=1)
    report = await reconciler.run_once()
    assert len(report.alerts) == 1
    assert w.notifier.sent[-1][:2] == (Severity.CRITICAL, "Position vanished")
    assert (await reconciler.run_once()).alerts == []  # alerted once; still retrying
    assert trade(w, pid).status is TradeStatus.OPEN

    w.broker.hide_deals(pid, hidden=False)
    assert (await reconciler.run_once()).closed == [(pid, CloseReason.MANUAL_EXTERNAL)]
    assert events(w).count(EventType.TRADE_VANISHED) == 1


# ---------------------------------------------------------------------------------------------- idempotency


async def test_closing_is_idempotent(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await open_position(w)
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(minutes=25))
    await w.reconciler().run_once()
    for _ in range(3):
        assert await w.reconciler().run_once() == ReconcileReport()
    with factory() as s:
        assert len(s.scalars(select(TradeRow)).all()) == 1
    assert deal_count(w, pid) == 2


async def test_broker_down_changes_nothing(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    await open_position(w)
    w.broker.connected = False
    report = await w.reconciler().run_once()
    assert report.errors
    assert (report.opened, report.orphans) == ([], [])
