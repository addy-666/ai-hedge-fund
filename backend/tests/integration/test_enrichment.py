"""Trade enrichment against the SimBroker, the reconciler and a migrated database (roadmap 3.3).

Same world as test_reconciler: BUY 0.04 XAUUSD filled at 4150.28, SL 4138.35 (11.93 = 1R), TP 4179.60, minute
bars flat between 4149.00 and 4151.00 except a shock bar at T0+20. The position opens at T0 exactly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.domain.enums import CloseReason, IntentKind, Timeframe
from aifund.persistence.tables import TradeRow
from aifund.ports.system import Severity
from aifund.reconcile.enrichment import Enricher
from tests.integration.test_reconciler import (
    World,
    close_intent,
    make_world,
    open_position,
    raw_request,
    trade,
)
from tests.unit.risk.test_stops import XAU

from .conftest import T0

MAGIC = 26092801


def enricher(w: World) -> Enricher:
    return Enricher(
        w.broker, w.broker.feed, w.factory, w.clock, w.notifier, trigger_tfs={"XAUUSD": Timeframe.M15}
    )


async def closed_by_sl(w: World) -> int:
    pid = await open_position(w)
    await w.reconciler().run_once()
    w.clock.set(T0 + timedelta(minutes=25))
    assert (await w.reconciler().run_once()).closed == [(pid, CloseReason.SL)]
    return pid


async def test_stop_loss_trade(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await closed_by_sl(w)
    assert (await enricher(w).run_once()).enriched == [pid]
    t = trade(w, pid)
    # the shock minute dipped to 4130.00, but the trade left at its stop: MAE is the fill, exactly 1R
    assert (t.mae_price, t.mfe_price) == (D("11.93"), D("0.72"))  # 4150.28-4138.35, 4151.00-4150.28
    assert (t.mae_r, t.mfe_r) == (D("-1.0000"), D("0.0604"))  # 0.72 / 11.93 = 0.060352
    assert (t.holding_minutes, t.bars_held) == (20, 1)  # T0 -> T0+20: one whole M15 bar
    assert (t.entry_slippage_points, t.exit_slippage_points) == (0, 0)  # filled at the ask, stop at the stop
    assert t.enriched_at is not None
    ((severity, title, body),) = w.notifier.sent
    assert (severity, title) == (Severity.INFO, "Trade closed: XAUUSD BUY")
    assert body.startswith("SL, net -48.00, -1.0059R, MAE -1.0000R / MFE 0.0604R, held 20 min")


async def test_take_profit_trade(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock, shock=("high", "4185.00"))
    pid = await open_position(w)
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(minutes=25))
    await w.reconciler().run_once()
    await enricher(w).run_once()
    t = trade(w, pid)
    assert (t.mae_price, t.mfe_price) == (D("1.28"), D("29.32"))  # 4150.28-4149.00; the TP fill 4179.60
    assert (t.mae_r, t.mfe_r) == (D("-0.1073"), D("2.4577"))  # 1.28/11.93 = 0.107292, 29.32/11.93 = 2.457669


async def test_engine_close_slippage(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    w.broker.config = replace(w.broker.config, slippage_points=3)  # every fill 0.03 worse
    pid = await open_position(w)  # asked 4150.28, filled 4150.31
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(minutes=10, seconds=5))
    closing = close_intent(w, pid, "0.04", CloseReason.TIME_STOP, IntentKind.CLOSE)  # bid 4150.00 -> 4149.97
    await w.executor.execute(closing, XAU)
    await w.reconciler().run_once()
    clock.advance(minutes=2)
    assert (await enricher(w).run_once()).enriched == [pid]
    t = trade(w, pid)
    assert (t.entry_slippage_points, t.exit_slippage_points) == (3, 3)
    assert t.holding_minutes == 10


async def test_orphan_gets_excursions_in_price_only(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    await w.broker.order_send(raw_request(MAGIC, "another script"))  # SELL 0.10 at 4150.00, no stop
    (pid,) = (await w.reconciler().run_once()).orphans
    clock.set(T0 + timedelta(minutes=30, seconds=5))
    w.broker.close_externally(pid)  # bought back at the ask 4150.28
    await w.reconciler().run_once()
    clock.advance(minutes=2)
    await enricher(w).run_once()
    t = trade(w, pid)
    # asks over the full minutes: 4149.28 .. 4151.28 (bid 4149-4151 + 0.28); exit 4150.28
    assert (t.mae_price, t.mfe_price) == (D("1.28"), D("0.72"))
    assert (t.mae_r, t.mfe_r) == (None, None)  # no stop: no R
    assert (t.bars_held, t.holding_minutes) == (2, 30)  # trigger timeframe from the symbol's profile
    assert (t.entry_slippage_points, t.exit_slippage_points) == (None, None)  # nothing was requested by us


async def test_waits_for_the_closing_minute_and_runs_once(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock)
    pid = await open_position(w)
    await w.reconciler().run_once()
    clock.set(T0 + timedelta(minutes=10, seconds=30))
    w.broker.close_externally(pid)  # closed at 10:10:30: the 10:10 minute bar is still forming
    await w.reconciler().run_once()
    e = enricher(w)
    assert (await e.run_once()).waiting == [pid]
    clock.advance(minutes=1)
    assert (await e.run_once()).enriched == [pid]
    assert (await e.run_once()).enriched == []  # once
    with factory() as s:
        assert s.scalars(select(TradeRow.enriched_at)).one() is not None


async def test_missing_history_is_retried_then_given_up(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    pid = await closed_by_sl(w)
    w.broker.feed._bars[("XAUUSD", Timeframe.M1)] = []  # the terminal has not synced the history
    e = enricher(w)
    assert (await e.run_once()).waiting == [pid]
    clock.advance(days=1, minutes=1)
    assert (await e.run_once()).enriched == [pid]  # enrich from what is known: the exit fill
    t = trade(w, pid)
    assert (t.mae_price, t.mfe_price, t.mae_r) == (D("11.93"), D("0"), D("-1.0000"))
