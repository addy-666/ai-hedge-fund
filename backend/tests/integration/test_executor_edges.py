"""Executor edge paths (R.0 coverage): missing quotes, broker errors mid-flow, resolution fallbacks."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import Fault, RejectWith, SimBroker
from aifund.domain._issuance import issue_order_intent
from aifund.domain.enums import IntentKind, IntentStatus, ReasonCode, Side, Timeframe
from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.domain.intent import OrderIntent, intent_comment
from aifund.domain.market import Deal, OrderRequest, OrderResult, Position, Tick
from aifund.execution.executor import Executor, ExecutorConfig
from aifund.ports.broker import BrokerUnavailable
from tests.unit.risk.test_stops import XAU

from .conftest import T0
from .test_executor import KEY, m1_bars, open_intent, row


@pytest.fixture
def world(clock: FakeClock) -> tuple[SimBroker, ReplayFeed]:
    clock.set(T0 + timedelta(seconds=5))  # quote: bid 4150.00 / ask 4150.28, as in test_executor
    feed = ReplayFeed({("XAUUSD", Timeframe.M1): m1_bars()}, {"XAUUSD": XAU}, clock)
    return SimBroker(feed=feed, clock=clock), feed


class QuoteGoes(ReplayFeed):
    """A feed whose quote disappears after ``quotes`` ticks (market closed, symbol halted)."""

    quotes = 0

    async def tick(self, symbol: str) -> Tick | None:
        self.quotes -= 1
        return await super().tick(symbol) if self.quotes >= 0 else None


class Brittle(SimBroker):
    """A SimBroker whose chosen calls fail, and which can rewrite position comments."""

    fail: frozenset[str] = frozenset()
    blank_comments = False

    def _maybe(self, name: str) -> None:
        if name in self.fail:
            raise BrokerUnavailable(f"{name} failed")

    async def order_check(self, request: OrderRequest) -> OrderResult:
        self._maybe("order_check")
        return await super().order_check(request)

    async def deals_between(self, start: datetime, end: datetime) -> list[Deal]:
        self._maybe("deals_between")
        return [] if "no_deals" in self.fail else await super().deals_between(start, end)

    async def positions(self, symbol: str | None = None) -> list[Position]:
        self._maybe("positions")
        found = await super().positions(symbol)
        return [p.model_copy(update={"comment": ""}) for p in found] if self.blank_comments else found


def make(
    world: tuple[SimBroker, ReplayFeed], clock: FakeClock, **broker_attrs: Any
) -> tuple[Brittle, QuoteGoes]:
    base, _ = world
    quoting = QuoteGoes({("XAUUSD", Timeframe.M1): m1_bars()}, {"XAUUSD": XAU}, clock)
    quoting.quotes = broker_attrs.pop("quotes", 10**6)
    broker = Brittle(feed=quoting, clock=clock, config=base.config)
    for name, value in broker_attrs.items():
        setattr(broker, name, value)
    return broker, quoting


def close_intent(clock: FakeClock, ticket: int) -> OrderIntent:
    cid = new_id()
    return issue_order_intent(
        id=cid,
        idempotency_key="c" * 24,
        decision_id=None,
        kind=IntentKind.CLOSE,
        symbol="XAUUSD",
        side=Side.SELL,
        volume=D("0.04"),
        price_ref=D("4150.00"),
        risk_money=D(0),
        risk_pct=D(0),
        magic=26092801,
        comment=intent_comment(cid),
        position_ticket=ticket,
        created_at=clock.now(),
    )


# ---------------------------------------------------------------- refusals before sending


async def test_no_quote_rejects_without_sending(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock, quotes=0)
    result = await Executor(broker, feed, factory, clock, account_id="acc").execute(open_intent(clock), XAU)
    assert (result.status, result.reason) == (IntentStatus.REJECTED, ReasonCode.MARKET_CLOSED)
    assert broker.sent == []


async def test_order_check_error_rejects_and_pauses(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock, fail=frozenset({"order_check"}))
    intent = open_intent(clock)
    result = await Executor(broker, feed, factory, clock, account_id="acc").execute(intent, XAU)
    assert (result.status, result.reason, result.pause_engine) == (
        IntentStatus.REJECTED,
        ReasonCode.BROKER_REJECTED,
        True,
    )
    assert "order_check failed" in (row(factory, intent.id).broker_comment or "")
    assert broker.sent == []


async def test_quote_lost_between_retries_rejects_without_resending(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock, quotes=1)  # the first build has a quote, the rebuild does not
    broker.inject(RejectWith(10004))
    result = await Executor(broker, feed, factory, clock, account_id="acc").execute(open_intent(clock), XAU)
    assert (result.status, result.reason) == (IntentStatus.REJECTED, ReasonCode.MARKET_CLOSED)
    assert len(broker.sent) == 1


def test_config_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        ExecutorConfig(max_attempts=0)


# ---------------------------------------------------------------- finding the position of a fill


async def test_position_id_skips_other_deals_in_the_window(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, _ = world
    ex = Executor(broker, world[1], factory, clock, account_id="acc")
    first = await ex.execute(open_intent(clock), XAU)
    second = await ex.execute(open_intent(clock, key="b" * 24), XAU)  # its search sees the first deal too
    assert first.position_id != second.position_id
    assert {p.position_id for p in await broker.positions()} == {first.position_id, second.position_id}


async def test_position_id_from_open_positions_when_deals_lag(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock, fail=frozenset({"no_deals"}))
    result = await Executor(broker, feed, factory, clock, account_id="acc").execute(open_intent(clock), XAU)
    (pos,) = await broker.positions()
    assert result.position_id == pos.position_id


async def test_position_id_falls_back_to_the_order_ticket_when_history_fails(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock, fail=frozenset({"deals_between"}))
    result = await Executor(broker, feed, factory, clock, account_id="acc").execute(open_intent(clock), XAU)
    (pos,) = await broker.positions()
    assert result.status is IntentStatus.FILLED
    assert result.position_id == pos.position_id  # MT5: position id == opening order ticket


# ---------------------------------------------------------------- resolution


async def test_resolve_unknown_id_is_an_invariant_violation(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    ex = Executor(world[0], world[1], factory, clock, account_id="acc")
    with pytest.raises(InvariantViolation, match="unknown intent"):
        await ex.resolve("missing")


async def test_resolve_terminal_intent_is_a_no_op(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = world
    ex = Executor(broker, feed, factory, clock, account_id="acc")
    filled = await ex.execute(open_intent(clock), XAU)
    again = await ex.resolve(filled.intent_id)
    assert (again.status, again.position_id) == (IntentStatus.FILLED, filled.position_id)
    assert len(broker.sent) == 1


async def test_lost_ack_with_rewritten_comment_resolves_structurally(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock, blank_comments=True)
    ex = Executor(broker, feed, factory, clock, account_id="acc")
    other = await ex.execute(open_intent(clock, key="b" * 24, volume="0.02"), XAU)  # a different position
    broker.inject(Fault.LOST_ACK)
    result = await ex.execute(open_intent(clock), XAU)
    positions = {p.volume: p.position_id for p in await broker.positions()}
    assert (result.status, result.position_id) == (IntentStatus.FILLED, positions[D("0.04")])
    assert result.position_id != other.position_id


async def test_broker_down_during_resolution_keeps_the_intent_unknown(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = make(world, clock)
    broker.inject(Fault.LOST_ACK)
    broker.fail = frozenset({"positions"})
    intent = open_intent(clock)
    result = await Executor(broker, feed, factory, clock, account_id="acc").execute(intent, XAU)
    assert result.status is IntentStatus.UNKNOWN
    assert "broker unavailable" in result.detail
    assert row(factory, intent.id).status is IntentStatus.UNKNOWN  # symbol stays locked
    assert len(broker.sent) == 1


async def test_lost_ack_on_a_close_resolves_from_its_deal(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = world
    ex = Executor(broker, feed, factory, clock, account_id="acc")
    other = await ex.execute(open_intent(clock, key="b" * 24), XAU)  # its deals share the search window
    opened = await ex.execute(open_intent(clock), XAU)
    assert opened.position_id is not None
    assert other.position_id is not None
    clock.advance(seconds=10)
    broker.inject(Fault.LOST_ACK)
    closed = await ex.execute(close_intent(clock, opened.position_id), XAU)
    assert (closed.status, closed.position_id) == (IntentStatus.FILLED, opened.position_id)
    assert [p.position_id for p in await broker.positions()] == [other.position_id]


async def test_unknown_within_grace_stays_locked(world, factory, clock) -> None:  # type: ignore[no-untyped-def]
    broker, feed = world
    broker.inject(Fault.DROPPED)
    ex = Executor(broker, feed, factory, clock, account_id="acc")
    first = await ex.execute(open_intent(clock, key=KEY), XAU)
    clock.advance(seconds=30)
    assert (await ex.resolve(first.intent_id)).status is IntentStatus.UNKNOWN
