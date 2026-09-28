"""Virtual-trade tracker against the replay feed and a migrated database (roadmap 3.4).

Minute bars are flat between 4149.00 and 4151.00 (spread 0.28) except a shock bar at T0+20. A BUY signal
blocked on the bar closing at T0+15 enters at the T0+15 bar's open: ask 4150.28; stop 11.93 below, target
29.32 above. Expected values by hand.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.domain.enums import CloseReason, DecisionOutcome, Side, VirtualStatus
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.persistence.tables import VirtualTradeRow
from aifund.reconcile.virtual import VirtualReport, VirtualTracker
from tests.integration.test_reconciler import World, make_world

from .conftest import T0

ENTRY = T0 + timedelta(minutes=15)


def pending(w: World, *, entry=ENTRY, expires=ENTRY + timedelta(hours=12)) -> str:  # type: ignore[no-untyped-def]
    with unit_of_work(w.factory) as s:
        decision = DecisionRepository(s, w.clock).add(
            account_id="acc", symbol="XAUUSD", trigger_tf="M15", bar_time=T0, stage_reached="RISK",
            outcome=DecisionOutcome.RISK_REJECTED, reason_code="COOLDOWN",
        )  # fmt: skip
        row = VirtualTradeRepository(s, w.clock).add_pending(
            account_id="acc", decision_id=decision.id, symbol="XAUUSD", side=Side.BUY, setup_tag="stub",
            entry_time=entry, sl_distance=D("11.93"), tp_distance=D("29.32"), expires_at=expires,
            expire_reason=CloseReason.TIME_STOP,
        )  # fmt: skip
        return row.id


def row(w: World, virtual_id: str) -> VirtualTradeRow:
    with w.factory() as s:
        r = s.get(VirtualTradeRow, virtual_id)
        assert r is not None
        s.expunge(r)
        return r


def tracker(w: World) -> VirtualTracker:
    return VirtualTracker(w.broker, w.broker.feed, w.factory, w.clock)


async def test_pending_then_open_then_stopped_out(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock, shock=("low", "4130.00"))
    vid = pending(w)
    t = tracker(w)
    clock.set(ENTRY + timedelta(seconds=30))  # the entry bar is still forming
    assert await t.run_once() == VirtualReport()
    assert row(w, vid).status is VirtualStatus.PENDING

    clock.set(ENTRY + timedelta(minutes=1, seconds=5))
    assert (await t.run_once()).entered == [vid]
    v = row(w, vid)
    assert (v.status, v.entry_price, v.sl, v.tp) == (
        VirtualStatus.OPEN,
        D("4150.28"),
        D("4138.35"),
        D("4179.60"),
    )
    assert (await t.run_once()).entered == []  # nothing new while it runs

    clock.set(T0 + timedelta(minutes=25))
    assert (await t.run_once()).finished == [(vid, VirtualStatus.CLOSED)]
    v = row(w, vid)
    assert (v.exit_reason, v.exit_price, v.exit_time) == (
        CloseReason.SL,
        D("4138.35"),
        T0 + timedelta(minutes=20),
    )
    assert (v.r_multiple, v.mae_r, v.mfe_r) == (D("-1.0000"), D("-1.0000"), D("0.0604"))
    assert await t.run_once() == VirtualReport()  # final: never touched again


async def test_expiry(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    vid = pending(w, expires=ENTRY + timedelta(minutes=10))
    clock.set(ENTRY + timedelta(minutes=12))
    assert (await tracker(w).run_once()).finished == [(vid, VirtualStatus.EXPIRED)]
    v = row(w, vid)
    # the last bar before expiry (T0+24) closes at the bid 4150.00: -0.28 / 11.93 = -0.023470
    assert (v.exit_reason, v.exit_time, v.exit_price, v.r_multiple) == (
        CloseReason.TIME_STOP, ENTRY + timedelta(minutes=10), D("4150.00"), D("-0.0235"),
    )  # fmt: skip


async def test_no_entry_when_there_is_no_next_bar(factory: sessionmaker[Session], clock: FakeClock) -> None:
    w = make_world(factory, clock)
    vid = pending(w, entry=T0 + timedelta(hours=3))  # the feed has no data then (market closed, say)
    clock.set(T0 + timedelta(hours=3, minutes=10))
    assert (await tracker(w).run_once()).finished == []  # may still arrive
    clock.set(T0 + timedelta(days=1, hours=3, minutes=1))
    assert (await tracker(w).run_once()).finished == [(vid, VirtualStatus.NO_ENTRY)]
    with factory() as s:
        assert s.scalars(select(VirtualTradeRow.entry_price)).one() is None
