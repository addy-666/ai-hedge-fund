"""Executor against the SimBroker and a migrated database: fills, retries, unknown outcomes, crashes."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import Fault, RejectWith, SimBroker
from aifund.domain._issuance import issue_order_intent
from aifund.domain.enums import IntentKind, IntentStatus, ReasonCode, Side, Timeframe
from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.domain.intent import OrderIntent, intent_comment
from aifund.domain.market import Bar
from aifund.execution.executor import Executor
from aifund.persistence.tables import OrderIntentRow
from aifund.ports.broker import BrokerUnavailable
from tests.unit.risk.test_stops import XAU

from .conftest import T0

KEY = "a" * 24


def m1_bars() -> list[Bar]:
    return [
        Bar(
            symbol="XAUUSD",
            timeframe=Timeframe.M1,
            time=T0 + timedelta(minutes=i),
            open=D("4150.00"),
            high=D("4151.00"),
            low=D("4149.00"),
            close=D("4150.00"),
            tick_volume=10,
            spread_points=28,
        )
        for i in range(-5, 60)
    ]


@pytest.fixture
def world(clock: FakeClock) -> tuple[SimBroker, ReplayFeed]:
    clock.set(T0 + timedelta(seconds=5))  # quote: bid 4150.00 / ask 4150.28 (bar closed at T0)
    feed = ReplayFeed({("XAUUSD", Timeframe.M1): m1_bars()}, {"XAUUSD": XAU}, clock)
    return SimBroker(feed=feed, clock=clock), feed


def executor(
    world: tuple[SimBroker, ReplayFeed], factory: sessionmaker[Session], clock: FakeClock, **kw: Any
) -> Executor:
    broker, feed = world
    return Executor(broker, feed, factory, clock, account_id="acc", **kw)


def open_intent(
    clock: FakeClock, *, key: str = KEY, volume: str = "0.04", price_ref: str = "4150.28"
) -> OrderIntent:
    intent_id = new_id()
    ref = D(price_ref)
    return issue_order_intent(
        id=intent_id,
        idempotency_key=key,
        decision_id=None,
        kind=IntentKind.OPEN,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=D(volume),
        price_ref=ref,
        sl=ref - D("11.93"),
        tp=ref + D("29.32"),
        sl_distance=D("11.93"),
        tp_distance=D("29.32"),
        risk_money=D("47.72"),
        risk_pct=D("0.4772"),
        magic=26092801,
        comment=intent_comment(intent_id),
        created_at=clock.now(),
    )


def row(factory: sessionmaker[Session], intent_id: str) -> OrderIntentRow:
    with factory() as s:
        r = s.get(OrderIntentRow, intent_id)
        assert r is not None
        return r


# ---------------------------------------------------------------- happy path & refusals


async def test_open_fills_and_is_recorded(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    intent = open_intent(clock)
    result = await executor(world, factory, clock).execute(intent, XAU)
    assert result.status is IntentStatus.FILLED
    (pos,) = await broker.positions()
    assert result.position_id == pos.position_id
    assert (pos.price_open, pos.sl, pos.tp, pos.volume) == (
        D("4150.28"),
        D("4138.35"),
        D("4179.60"),
        D("0.04"),
    )
    assert pos.comment == intent.comment
    r = row(factory, intent.id)
    assert (r.status, r.position_id, r.fill_price, r.attempts, r.slippage_points) == (
        IntentStatus.FILLED,
        pos.position_id,
        D("4150.28"),
        1,
        0,
    )
    assert r.sent_at is not None


async def test_unissued_intents_are_refused_before_anything_happens(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    forged = OrderIntent.model_validate(open_intent(clock).model_dump())  # e.g. built from LLM output
    with pytest.raises(InvariantViolation, match="not issued"):
        await executor(world, factory, clock).execute(forged, XAU)
    assert broker.sent == []
    with factory() as s:
        assert s.get(OrderIntentRow, forged.id) is None


async def test_duplicate_key_never_reaches_the_broker(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    ex = executor(world, factory, clock)
    await ex.execute(open_intent(clock), XAU)
    again = await ex.execute(open_intent(clock), XAU)  # same bar/direction key, new intent id
    assert again.duplicate
    assert again.reason is ReasonCode.DUPLICATE_IDEMPOTENCY
    assert len(broker.sent) == 1


async def test_failed_check_sends_nothing(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    result = await executor(world, factory, clock).execute(open_intent(clock, volume="0.015"), XAU)
    assert (result.status, result.retcode_name) == (IntentStatus.CHECK_FAILED, "INVALID_VOLUME")
    assert broker.sent == []


async def test_price_moved_since_risk_check_is_rejected(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    result = await executor(world, factory, clock).execute(open_intent(clock, price_ref="4140.00"), XAU)
    assert (result.status, result.reason) == (IntentStatus.REJECTED, ReasonCode.PRICE_MOVED)
    assert broker.sent == []


# ---------------------------------------------------------------- retcodes


async def test_requote_is_retried_then_filled(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    broker.inject(RejectWith(10004))
    intent = open_intent(clock)
    result = await executor(world, factory, clock).execute(intent, XAU)
    assert result.status is IntentStatus.FILLED
    assert row(factory, intent.id).attempts == 2
    assert len(broker.sent) == 2


async def test_retries_are_bounded(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    broker.inject(RejectWith(10004), RejectWith(10020), RejectWith(10004))
    result = await executor(world, factory, clock).execute(open_intent(clock), XAU)
    assert (result.status, result.retcode_name) == (IntentStatus.REJECTED, "REQUOTE")
    assert len(broker.sent) == 3
    assert await broker.positions() == []


async def test_autotrading_off_rejects_and_asks_to_pause(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    broker.inject(RejectWith(10027))
    result = await executor(world, factory, clock).execute(open_intent(clock), XAU)
    assert result.status is IntentStatus.REJECTED
    assert result.pause_engine
    assert len(broker.sent) == 1  # a PAUSE code is never retried


# ---------------------------------------------------------------- unknown outcomes


async def test_lost_ack_is_resolved_to_the_real_fill(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    broker.inject(Fault.LOST_ACK)
    intent = open_intent(clock)
    result = await executor(world, factory, clock).execute(intent, XAU)
    (pos,) = await broker.positions()
    assert (result.status, result.position_id) == (IntentStatus.FILLED, pos.position_id)
    assert len(broker.sent) == 1  # never resent


async def test_dropped_order_stays_unknown_then_is_rejected_after_grace(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    broker.inject(Fault.DROPPED)
    ex = executor(world, factory, clock)
    intent = open_intent(clock)
    first = await ex.execute(intent, XAU)
    assert first.status is IntentStatus.UNKNOWN
    clock.advance(minutes=3)
    later = await ex.resolve(intent.id)
    assert (later.status, later.reason) == (IntentStatus.REJECTED, ReasonCode.UNKNOWN_NOT_EXECUTED)
    assert len(broker.sent) == 1


async def test_disconnect_during_send_is_unknown_not_failure(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    _, feed = world

    class Flaky(SimBroker):
        async def order_send(self, request):  # type: ignore[no-untyped-def]
            raise BrokerUnavailable("connection lost")

    flaky = Flaky(feed=feed, clock=clock)
    result = await Executor(flaky, feed, factory, clock, account_id="acc").execute(open_intent(clock), XAU)
    assert result.status is IntentStatus.UNKNOWN


# ---------------------------------------------------------------- crash at every step


class Crash(Exception):
    pass


@pytest.mark.parametrize(
    "stage", ["after_pending", "after_sent", "after_send", "between_retries", "after_filled"]
)
async def test_crash_at_every_step_never_duplicates_or_loses_a_trade(
    world, factory, clock, stage: str
) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    if stage == "between_retries":
        broker.inject(RejectWith(10004))  # first send is requoted, the process dies before resending

    def die(at: str) -> None:
        if at == stage:
            raise Crash(at)

    with pytest.raises(Crash):
        await executor(world, factory, clock, checkpoint=die).execute(open_intent(clock), XAU)

    # restart: recover first (twice: within and after the grace period), then the same bar fires again
    restarted = executor(world, factory, clock)
    await restarted.recover()
    clock.advance(minutes=3)
    await restarted.recover()
    retry = await restarted.execute(open_intent(clock), XAU)

    positions = await broker.positions()
    assert retry.duplicate  # the bar/direction was already acted on
    assert len(positions) <= 1  # zero duplicates
    with factory() as s:
        rows = s.query(OrderIntentRow).all()
    assert len(rows) == 1
    assert rows[0].status.is_terminal  # nothing left dangling
    if positions:  # zero lost trades: a real fill is always recorded against its position
        assert (rows[0].status, rows[0].position_id) == (IntentStatus.FILLED, positions[0].position_id)
    else:
        assert rows[0].status is IntentStatus.REJECTED
    expected_sends = 0 if stage in ("after_pending", "after_sent") else 1
    assert len(broker.sent) == expected_sends


# ---------------------------------------------------------------- close & modify


async def test_close_and_modify_intents(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    ex = executor(world, factory, clock)
    opened = await ex.execute(open_intent(clock), XAU)
    assert opened.position_id is not None
    common: dict[str, Any] = dict(
        decision_id=None,
        symbol="XAUUSD",
        risk_money=D(0),
        risk_pct=D(0),
        magic=26092801,
        position_ticket=opened.position_id,
        created_at=clock.now(),
    )
    mid = new_id()
    modify = issue_order_intent(
        id=mid,
        idempotency_key="m" * 24,
        kind=IntentKind.MODIFY_SLTP,
        side=Side.BUY,
        volume=D("0.04"),
        price_ref=D("4150.28"),
        sl=D("4145.00"),
        tp=D("4190.00"),
        comment=intent_comment(mid),
        **common,
    )
    assert (await ex.execute(modify, XAU)).status is IntentStatus.FILLED
    (pos,) = await broker.positions()
    assert (pos.sl, pos.tp) == (D("4145.00"), D("4190.00"))
    cid = new_id()
    close = issue_order_intent(
        id=cid,
        idempotency_key="c" * 24,
        kind=IntentKind.REVERSE_CLOSE,
        side=Side.SELL,
        volume=D("0.04"),
        price_ref=D("4150.00"),
        comment=intent_comment(cid),
        **common,
    )
    closed = await ex.execute(close, XAU)
    assert (closed.status, closed.position_id) == (IntentStatus.FILLED, opened.position_id)
    assert await broker.positions() == []
