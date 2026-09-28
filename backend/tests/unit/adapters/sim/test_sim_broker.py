"""SimBroker + ReplayFeed: known bar sequences must produce exact fills, deals and balances."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.adapters.sim.sim_broker import Fault, RejectWith, SimBroker, SimConfig
from aifund.domain.enums import DealEntry, DealReason, Side, Timeframe
from aifund.domain.market import Bar, FillingMode, OrderRequest, SymbolSpec, TradeAction
from aifund.ports.broker import BrokerUnavailable

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
SYM = "XAUUSDm"
SPEC = SymbolSpec(
    symbol=SYM, digits=2, point=D("0.01"), tick_size=D("0.01"), tick_value=D("1"), contract_size=D("100"),
    volume_min=D("0.01"), volume_max=D("100"), volume_step=D("0.01"), stops_level_points=0,
    freeze_level_points=0, filling_mode_flags=2, currency_profit="USD", currency_margin="XAU",
)  # fmt: skip


def m1(minute: int, o: str, h: str, low: str, c: str, spread: int = 20) -> Bar:
    return Bar(
        symbol=SYM,
        timeframe=Timeframe.M1,
        time=T0 + timedelta(minutes=minute),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        tick_volume=10,
        spread_points=spread,
    )


# minute 0 closes at 10:01 with close 2350.00 → quote bid 2350.00 / ask 2350.20
BASE = [m1(0, "2349.50", "2350.50", "2349.00", "2350.00")]


def world(extra: list[Bar], *, spec: SymbolSpec = SPEC, **cfg: object) -> tuple[SimBroker, FakeClock]:
    clock = FakeClock(T0 + timedelta(minutes=1, seconds=5))
    bars = {(SYM, Timeframe.M1): [*BASE, *extra]}
    feed = ReplayFeed(bars, {SYM: spec}, clock)
    config = SimConfig(commission_per_lot_side=D("3.50"), **cfg)  # type: ignore[arg-type]
    return SimBroker(feed=feed, clock=clock, config=config), clock


def buy(
    volume: str = "0.10",
    sl: str | None = "2340.00",
    tp: str | None = "2360.00",
    price: str = "2350.20",
    **kw: object,
) -> OrderRequest:
    return OrderRequest(
        action=TradeAction.DEAL,
        symbol=SYM,
        side=Side.BUY,
        volume=D(volume),
        price=D(price),
        sl=D(sl) if sl else None,
        tp=D(tp) if tp else None,
        deviation_points=20,
        magic=7,
        comment="AF:TEST0001",
        **kw,
    )  # type: ignore[arg-type]


def sell(
    volume: str = "0.10",
    sl: str | None = "2360.00",
    tp: str | None = "2340.00",
    price: str = "2350.00",
    **kw: object,
) -> OrderRequest:
    return OrderRequest(
        action=TradeAction.DEAL,
        symbol=SYM,
        side=Side.SELL,
        volume=D(volume),
        price=D(price),
        sl=D(sl) if sl else None,
        tp=D(tp) if tp else None,
        deviation_points=20,
        magic=7,
        comment="AF:TEST0002",
        **kw,
    )  # type: ignore[arg-type]


# ---------------------------------------------------------------- replay feed


async def test_feed_shows_only_closed_bars_and_quotes_from_the_last_one() -> None:
    clock = FakeClock(T0 + timedelta(seconds=30))
    feed = ReplayFeed(
        {(SYM, Timeframe.M1): [*BASE, m1(1, "2350", "2351", "2349", "2350.50")]}, {SYM: SPEC}, clock
    )
    assert await feed.closed_bars(SYM, Timeframe.M1, 5) == []  # minute-0 bar still forming
    assert await feed.tick(SYM) is None
    clock.set(T0 + timedelta(minutes=1))
    (only,) = await feed.closed_bars(SYM, Timeframe.M1, 5)
    assert only.time == T0
    tick = await feed.tick(SYM)
    assert tick is not None
    assert (tick.bid, tick.ask, tick.time) == (D("2350.00"), D("2350.20"), T0 + timedelta(minutes=1))
    clock.set(T0 + timedelta(minutes=2))
    assert [b.close for b in await feed.closed_bars(SYM, Timeframe.M1, 1)] == [D("2350.50")]


# ---------------------------------------------------------------- fills and exits


async def test_buy_fills_at_ask_with_slippage_and_commission() -> None:
    broker, _ = world([], slippage_points=5)
    res = await broker.order_send(buy())
    assert res is not None
    assert res.retcode_name == "DONE"
    assert res.price == D("2350.25")  # ask 2350.20 + 5 points adverse
    (pos,) = await broker.positions()
    assert (pos.side, pos.volume, pos.price_open, pos.position_id) == (
        Side.BUY,
        D("0.10"),
        D("2350.25"),
        res.order,
    )
    (deal,) = await broker.deals_for_position(pos.position_id)
    assert (deal.entry, deal.reason, deal.commission) == (DealEntry.IN, DealReason.EXPERT, D("-0.35"))
    assert broker.balance == D("9999.65")


async def test_take_profit_fills_at_tp_with_exact_profit() -> None:
    broker, clock = world([m1(1, "2351", "2355", "2350", "2354"), m1(2, "2354", "2361", "2353", "2359")])
    res = await broker.order_send(buy())
    assert res is not None
    clock.set(T0 + timedelta(minutes=3))
    assert await broker.positions() == []
    out = [d for d in await broker.deals_for_position(res.order) if d.entry is DealEntry.OUT]
    assert len(out) == 1
    assert (out[0].reason, out[0].price, out[0].side) == (DealReason.TP, D("2360.00"), Side.SELL)
    assert out[0].profit == D("98.00")  # (2360.00-2350.20)/0.01 ticks x $1 x 0.10 lot
    assert out[0].time == T0 + timedelta(minutes=2)  # stamped in the minute the TP was touched
    assert broker.balance == D("10000") + D("98.00") - D("0.70")


async def test_sl_wins_when_both_levels_are_touched_in_one_bar() -> None:
    broker, clock = world([m1(1, "2350", "2365", "2335", "2350")])
    res = await broker.order_send(buy())
    assert res is not None
    clock.set(T0 + timedelta(minutes=2))
    (out,) = [d for d in await broker.deals_for_position(res.order) if d.entry is DealEntry.OUT]
    assert (out.reason, out.price, out.profit) == (DealReason.SL, D("2340.00"), D("-102.00"))


async def test_gap_through_stop_fills_at_bar_open() -> None:
    broker, clock = world([m1(1, "2330", "2332", "2328", "2331")])
    res = await broker.order_send(buy())
    assert res is not None
    clock.set(T0 + timedelta(minutes=2))
    (out,) = [d for d in await broker.deals_for_position(res.order) if d.entry is DealEntry.OUT]
    assert (out.reason, out.price) == (DealReason.SL, D("2330.00"))


async def test_sell_exits_are_checked_against_the_ask() -> None:
    # bid high 2359.85 never reaches SL 2360, but ask = bid + 0.20 = 2360.05 does
    broker, clock = world([m1(1, "2350", "2359.85", "2349", "2355")])
    res = await broker.order_send(sell())
    assert res is not None
    assert res.price == D("2350.00")
    clock.set(T0 + timedelta(minutes=2))
    (out,) = [d for d in await broker.deals_for_position(res.order) if d.entry is DealEntry.OUT]
    assert (out.reason, out.price, out.side, out.profit) == (
        DealReason.SL,
        D("2360.00"),
        Side.BUY,
        D("-100.00"),
    )


async def test_positions_opened_later_ignore_earlier_bars() -> None:
    broker, clock = world([m1(1, "2350", "2351", "2330", "2350"), m1(2, "2350", "2351", "2349", "2350")])
    clock.set(T0 + timedelta(minutes=2, seconds=5))  # the crash bar (minute 1) is already history
    res = await broker.order_send(buy(price="2350.20"))
    assert res is not None
    clock.set(T0 + timedelta(minutes=3))
    assert len(await broker.positions()) == 1


async def test_partial_then_full_close_and_equity() -> None:
    broker, clock = world([m1(1, "2352", "2353", "2351", "2352")])
    res = await broker.order_send(buy(volume="0.30", sl=None, tp=None))
    assert res is not None
    clock.set(T0 + timedelta(minutes=2))
    account = await broker.account_info()
    # floating at bid 2352.00: (2352.00-2350.20)/0.01 x 1 x 0.30 = 54.00 ; balance after 0.30 lot commission
    assert account.equity == account.balance + D("54.00")
    close_part = sell(volume="0.10", sl=None, tp=None, price="2352.00", position_ticket=res.order)
    part = await broker.order_send(close_part)
    assert part is not None
    assert part.retcode_name == "DONE"
    (pos,) = await broker.positions()
    assert pos.volume == D("0.20")
    await broker.order_send(sell(volume="0.20", sl=None, tp=None, price="2352.00", position_ticket=res.order))
    assert await broker.positions() == []
    outs = [d for d in await broker.deals_for_position(res.order) if d.entry is DealEntry.OUT]
    assert [d.volume for d in outs] == [D("0.10"), D("0.20")]
    assert sum(d.profit for d in outs) == D("54.00")


async def test_sltp_modification() -> None:
    broker, _ = world([])
    res = await broker.order_send(buy(sl=None, tp=None))
    assert res is not None
    mod = OrderRequest(
        action=TradeAction.SLTP,
        symbol=SYM,
        position_ticket=res.order,
        sl=D("2345.00"),
        tp=D("2370.00"),
        magic=7,
    )
    done = await broker.order_send(mod)
    assert done is not None
    assert done.retcode_name == "DONE"
    (pos,) = await broker.positions()
    assert (pos.sl, pos.tp) == (D("2345.00"), D("2370.00"))


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (buy(volume="0.005"), "INVALID_VOLUME"),
        (buy(volume="0.015"), "INVALID_VOLUME"),
        (buy(volume="101"), "INVALID_VOLUME"),
        (buy(sl="2355.00"), "INVALID_STOPS"),  # SL above price for a BUY
        (buy(tp="2345.00"), "INVALID_STOPS"),
        (buy(volume="30"), "NO_MONEY"),  # 30 lot x 100 oz x 2350 / 100 = $70,500 margin
        (buy(filling=FillingMode.FOK), "INVALID_FILL"),
        (sell(position_ticket=424242, sl=None, tp=None), "POSITION_CLOSED"),
    ],
)
async def test_invalid_requests_are_rejected_by_check_and_send(request_: OrderRequest, expected: str) -> None:
    broker, _ = world([])
    assert (await broker.order_check(request_)).retcode_name == expected
    res = await broker.order_send(request_)
    assert res is not None
    assert res.retcode_name == expected
    assert await broker.positions() == []


async def test_stops_level_is_enforced() -> None:
    broker, _ = world([], spec=SPEC.model_copy(update={"stops_level_points": 500}))
    assert (await broker.order_check(buy(sl="2346.00"))).retcode_name == "INVALID_STOPS"  # 400 points < 500
    assert (await broker.order_check(buy(sl="2344.00", tp="2356.00"))).retcode_name == "OK"


async def test_feed_bars_range_is_inclusive_and_never_the_future() -> None:
    bars = [m1(i, "2350", "2351", "2349", "2350") for i in range(10)]
    clock = FakeClock(T0 + timedelta(minutes=6, seconds=30))  # bars 0..5 have closed; 6 is forming
    feed = ReplayFeed({(SYM, Timeframe.M1): bars}, {SYM: SPEC}, clock)
    got = await feed.bars_range(SYM, Timeframe.M1, T0 + timedelta(minutes=2), T0 + timedelta(minutes=4))
    assert [b.time.minute for b in got] == [2, 3, 4]  # open times in [start, end], both ends included
    future = await feed.bars_range(SYM, Timeframe.M1, T0 + timedelta(minutes=4), T0 + timedelta(minutes=9))
    assert [b.time.minute for b in future] == [4, 5]  # 6 is still forming: never returned
    assert (
        await feed.bars_range(
            SYM, Timeframe.M1, T0 + timedelta(minutes=2, seconds=1), T0 + timedelta(minutes=2, seconds=59)
        )
        == []
    )


async def test_stale_quote_means_market_closed() -> None:
    broker, clock = world([])
    clock.set(T0 + timedelta(hours=2))  # no bars since 10:01 (weekend / feed stall)
    assert (await broker.order_check(buy())).retcode_name == "MARKET_CLOSED"


async def test_requote_when_price_moved_beyond_deviation() -> None:
    broker, _ = world([])
    res = await broker.order_send(buy(price="2349.00"))  # 120 points away, deviation 20
    assert res is not None
    assert res.retcode_name == "REQUOTE"
    assert res.price == D("2350.20")
    assert await broker.positions() == []


async def test_close_only_symbol_rejects_new_entries() -> None:
    broker, _ = world([], spec=SPEC.model_copy(update={"trade_allowed": False}))
    assert (await broker.order_check(buy())).retcode_name == "TRADE_DISABLED"


# ---------------------------------------------------------------- fault injection


async def test_lost_ack_executes_but_returns_unknown() -> None:
    broker, clock = world([])
    broker.inject(Fault.LOST_ACK)
    assert await broker.order_send(buy()) is None
    (pos,) = await broker.positions()  # the fill happened: the UNKNOWN resolver must find it
    assert pos.comment == "AF:TEST0001"
    deals = await broker.deals_between(T0, clock.now())
    assert [d.comment for d in deals] == ["AF:TEST0001"]


async def test_dropped_order_and_forced_rejection_execute_nothing() -> None:
    broker, _ = world([])
    broker.inject(Fault.DROPPED, RejectWith(10027))
    assert await broker.order_send(buy()) is None
    res = await broker.order_send(buy())
    assert res is not None
    assert res.retcode == 10027
    assert await broker.positions() == []
    ok = await broker.order_send(buy())  # queue drained: normal behaviour again
    assert ok is not None
    assert ok.retcode_name == "DONE"


async def test_disconnected_broker_raises_the_port_error() -> None:
    broker, _ = world([])
    broker.connected = False
    with pytest.raises(BrokerUnavailable):
        await broker.account_info()
    with pytest.raises(BrokerUnavailable):
        await broker.order_send(buy())


async def test_replays_are_deterministic() -> None:
    async def run() -> list[tuple[object, ...]]:
        broker, clock = world([m1(1, "2351", "2355", "2350", "2354"), m1(2, "2354", "2361", "2353", "2359")])
        await broker.order_send(buy())
        await broker.order_send(sell(sl="2370.00", tp="2300.00"))
        clock.set(T0 + timedelta(minutes=3))
        return [(d.ticket, d.entry, d.price, d.profit) for d in await broker.deals_between(T0, clock.now())]

    assert await run() == await run()


async def test_feed_loads_exported_history(tmp_path: object) -> None:
    from pathlib import Path

    from aifund.adapters import history_store as hs

    root = Path(str(tmp_path))
    hs.write_bars(root, SYM, Timeframe.M1, [*BASE, m1(1, "2350", "2351", "2349", "2350.75")])
    hs.write_bars(
        root,
        SYM,
        Timeframe.M15,
        [
            Bar(
                symbol=SYM,
                timeframe=Timeframe.M15,
                time=T0,
                open=D("2349.5"),
                high=D("2352"),
                low=D("2349"),
                close=D("2351"),
                tick_volume=150,
            )
        ],
    )
    hs.write_specs(root, {SYM: SPEC})
    clock = FakeClock(T0 + timedelta(minutes=15))
    feed = ReplayFeed.from_history(root, [SYM], [Timeframe.M15], clock)
    (bar,) = await feed.closed_bars(SYM, Timeframe.M15, 10)
    assert bar.close == D("2351.00")
    tick = await feed.tick(SYM)
    assert tick is not None
    assert tick.bid == D("2350.75")


def test_adapters_satisfy_the_ports() -> None:
    from aifund.adapters.mt5.gateway import MT5Credentials, MT5Gateway
    from aifund.ports.broker import BrokerPort, MarketDataPort

    broker, _ = world([])
    gateway = MT5Gateway(MT5Credentials(login=1, password="x", server="s"), clock=FakeClock(T0))
    assert isinstance(broker, BrokerPort)
    assert isinstance(broker.feed, MarketDataPort)
    assert isinstance(gateway, BrokerPort)
    assert isinstance(gateway, MarketDataPort)
