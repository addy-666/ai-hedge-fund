from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from aifund.adapters.clock import FakeClock
from aifund.adapters.mt5.gateway import (
    AccountMismatch,
    ConnectionState,
    GatewayDisconnected,
    GatewayError,
    GatewayTimeout,
    MT5Credentials,
    MT5Gateway,
)
from aifund.adapters.mt5.server_time import OffsetUnavailable
from aifund.domain.enums import Mode, Side, Timeframe
from aifund.domain.market import OrderRequest, TradeAction
from tests.fakes.fake_mt5 import IPC_FAIL, FakeMT5, make_account, make_symbol

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
OFFSET = timedelta(hours=3)
CREDS = MT5Credentials(login=12345678, password="pw", server="Broker-Demo")
REQ = OrderRequest(
    action=TradeAction.DEAL,
    symbol="XAUUSDm",
    side=Side.BUY,
    volume=Decimal("0.10"),
    price=Decimal("2350.00"),
    magic=1,
    comment="AF:ABCDEFGH",
)


def server_epoch(moment: datetime) -> int:
    return int((moment + OFFSET).timestamp())


def fresh_tick(age_s: int = 20) -> NS:
    epoch = server_epoch(T0 - timedelta(seconds=age_s))
    return NS(time=epoch, time_msc=epoch * 1000, bid=2350.10, ask=2350.30)


@pytest.fixture
def fake() -> FakeMT5:
    f = FakeMT5(tick_advance_ms=500)  # live feed
    f.ticks["XAUUSDm"] = fresh_tick()
    return f


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
async def gw(fake: FakeMT5, clock: FakeClock):  # type: ignore[no-untyped-def]
    gateway = MT5Gateway(
        CREDS, clock=clock, module_loader=lambda: fake, call_timeout_s=0.5, order_timeout_s=0.5
    )
    await gateway.connect()
    await gateway.refresh_server_offset(["XAUUSDm"])
    yield gateway
    await gateway.close()


# ---------------------------------------------------------------- connection


async def test_connect_verifies_the_account_identity(gw: MT5Gateway, fake: FakeMT5) -> None:
    assert gw.state is ConnectionState.CONNECTED
    init_kwargs = next(c[3] for c in fake.calls if c[0] == "initialize")
    assert init_kwargs["login"] == 12345678
    assert init_kwargs["server"] == "Broker-Demo"


@pytest.mark.parametrize("account", [make_account(login=99999999), make_account(server="Other-Live")])
async def test_wrong_account_or_server_is_refused_and_disconnected(clock: FakeClock, account: NS) -> None:
    fake = FakeMT5(account=account)
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    with pytest.raises(AccountMismatch):
        await gateway.connect()
    assert gateway.state is ConnectionState.DISCONNECTED
    assert fake.names()[-1] == "shutdown"
    await gateway.close()


async def test_initialize_failure_is_a_disconnect(clock: FakeClock) -> None:
    fake = FakeMT5(initialize_failures=1)
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    with pytest.raises(GatewayDisconnected, match="initialize failed"):
        await gateway.connect()
    await gateway.close()


async def test_reconnect_backs_off_exponentially(clock: FakeClock) -> None:
    fake = FakeMT5(initialize_failures=3)
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    assert await gateway.reconnect(max_attempts=5)
    assert clock.now() == T0 + timedelta(seconds=1 + 2 + 4)
    assert gateway.state is ConnectionState.CONNECTED
    await gateway.close()


async def test_reconnect_gives_up_after_max_attempts(clock: FakeClock) -> None:
    fake = FakeMT5(initialize_failures=10)
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    assert not await gateway.reconnect(max_attempts=3)
    assert clock.now() == T0 + timedelta(seconds=1 + 2)
    await gateway.close()


async def test_every_call_runs_on_the_single_worker_thread(gw: MT5Gateway, fake: FakeMT5) -> None:
    await gw.account_info()
    await gw.positions()
    await gw.closed_bars("XAUUSDm", Timeframe.M15, 3)
    threads = {c[1] for c in fake.calls}
    assert len(threads) == 1
    assert threads.pop().startswith("mt5-gateway")


# ---------------------------------------------------------------- failures


async def test_slow_call_times_out(gw: MT5Gateway, fake: FakeMT5) -> None:
    fake.hang["account_info"] = 1.0
    with pytest.raises(GatewayTimeout):
        await gw.account_info()


async def test_none_result_while_disconnected_marks_gateway_disconnected(
    gw: MT5Gateway, fake: FakeMT5
) -> None:
    fake.none_for.add("account_info")
    fake.terminal = NS(connected=False, trade_allowed=True)
    fake.error = IPC_FAIL
    with pytest.raises(GatewayDisconnected):
        await gw.account_info()
    assert gw.state is ConnectionState.DISCONNECTED
    assert not await gw.is_connected()


async def test_none_result_while_connected_is_a_plain_error(gw: MT5Gateway, fake: FakeMT5) -> None:
    fake.none_for.add("order_calc_profit")
    fake.error = (-2, "Invalid arguments")
    with pytest.raises(GatewayError, match="Invalid arguments"):
        await gw.calc_profit(Side.BUY, "XAUUSDm", Decimal("0.1"), Decimal(2350), Decimal(2340))


async def test_order_send_none_or_timeout_is_unknown_not_an_error(gw: MT5Gateway, fake: FakeMT5) -> None:
    fake.none_for.add("order_send")
    assert await gw.order_send(REQ) is None
    fake.none_for.clear()
    fake.hang["order_send"] = 1.0
    assert await gw.order_send(REQ) is None
    assert fake.names().count("order_send") == 2  # never resent by the gateway


async def test_order_send_and_check_results(gw: MT5Gateway, fake: FakeMT5) -> None:
    res = await gw.order_send(REQ)
    assert res is not None
    assert res.retcode_name == "DONE"
    assert res.price == Decimal("2350.12")
    assert (await gw.order_check(REQ)).retcode_name == "OK"
    fake.none_for.add("order_check")
    with pytest.raises(GatewayError, match="order_check"):
        await gw.order_check(REQ)


# ---------------------------------------------------------------- data


async def test_closed_bars_never_include_the_forming_bar(gw: MT5Gateway, fake: FakeMT5) -> None:
    rows = [
        {
            "time": server_epoch(T0 + timedelta(minutes=15 * i)),
            "open": 2350.0,
            "high": 2360.0,
            "low": 2349.0,
            "close": 2350.5 + i,
            "tick_volume": 10,
            "spread": 20,
        }
        for i in range(5)
    ]  # rows[4] is the forming bar
    fake.rates[("XAUUSDm", 15)] = rows
    bars = await gw.closed_bars("XAUUSDm", Timeframe.M15, 3)
    assert [b.time for b in bars] == [T0 + timedelta(minutes=15 * i) for i in (1, 2, 3)]
    start_pos = next(c[2][2] for c in fake.calls if c[0] == "copy_rates_from_pos")
    assert start_pos == 1


async def test_tick_is_utc_and_missing_tick_is_none(gw: MT5Gateway, fake: FakeMT5) -> None:
    tick = await gw.tick("XAUUSDm")
    assert tick is not None
    # fixture sampled the feed twice (+1.0 s), this call advances it once more (+0.5 s)
    assert tick.time == T0 - timedelta(seconds=20) + timedelta(milliseconds=1500)
    fake.ticks["XAUUSDm"] = NS(time=0, time_msc=0, bid=0.0, ask=0.0)
    assert await gw.tick("XAUUSDm") is None


async def test_positions_empty_and_mapped(gw: MT5Gateway, fake: FakeMT5) -> None:
    assert await gw.positions() == []
    fake.positions.append(
        NS(
            ticket=7,
            identifier=7,
            symbol="XAUUSDm",
            type=0,
            volume=0.1,
            price_open=2350.123,
            sl=2335.0,
            tp=0.0,
            magic=1,
            comment="",
            time=server_epoch(T0),
            time_msc=0,
            profit=1.0,
            swap=0.0,
        )
    )
    (pos,) = await gw.positions("XAUUSDm")
    assert pos.price_open == Decimal("2350.12")
    assert pos.tp is None


async def test_deals_between_queries_server_time(gw: MT5Gateway, fake: FakeMT5) -> None:
    fake.deals.append(
        NS(
            ticket=9,
            order=8,
            position_id=7,
            time=server_epoch(T0),
            time_msc=0,
            symbol="XAUUSDm",
            type=1,
            entry=1,
            reason=5,
            magic=1,
            volume=0.1,
            price=2360.0,
            profit=100.0,
            commission=-3.5,
            swap=0.0,
            fee=0.0,
            comment="tp",
        )
    )
    deals = await gw.deals_between(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert [d.ticket for d in deals] == [9]
    assert deals[0].time == T0
    args = next(c[2] for c in fake.calls if c[0] == "history_deals_get")
    assert args == (server_epoch(T0 - timedelta(hours=1)), server_epoch(T0 + timedelta(hours=1)))
    assert [d.ticket for d in await gw.deals_for_position(7)] == [9]


@pytest.mark.parametrize("age_min", [60, 15, 7])
async def test_stale_ticks_never_yield_an_offset_even_at_15_minute_multiples(
    clock: FakeClock, age_min: int
) -> None:
    # Regression: a tick exactly 60 min old used to be read as a fresh tick with a 1h-smaller offset.
    fake = FakeMT5()  # static feed: market closed
    fake.ticks["XAUUSDm"] = fresh_tick(age_s=age_min * 60)
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    await gateway.connect()
    with pytest.raises(OffsetUnavailable, match="no symbol ticked"):
        await gateway.refresh_server_offset(["XAUUSDm"])
    await gateway.close()


async def test_server_offset_detects_changes(gw: MT5Gateway, fake: FakeMT5) -> None:
    assert gw.server_offset == OFFSET
    fake.ticks["XAUUSDm"] = NS(
        time=0,
        time_msc=int((T0 + timedelta(hours=2) - timedelta(seconds=5)).timestamp()) * 1000,
        bid=1.0,
        ask=1.1,
    )
    offset, changed = await gw.refresh_server_offset(["XAUUSDm"])
    assert offset == timedelta(hours=2)
    assert changed


# ---------------------------------------------------------------- startup checks


async def test_startup_checks_pass_on_a_healthy_demo_terminal(gw: MT5Gateway) -> None:
    report = await gw.startup_checks(["XAUUSDm"], mode=Mode.DEMO, require_hedging=True)
    assert report.ok, report.problems
    assert report.server_offset == OFFSET
    assert report.specs["XAUUSDm"].contract_size == Decimal("100.0")


async def test_startup_checks_catch_every_unsafe_condition(clock: FakeClock) -> None:
    fake = FakeMT5(
        account=make_account(trade_mode=2, margin_mode=0, trade_expert=False),
        terminal=NS(connected=True, trade_allowed=False),
        symbols={"XAUUSDm": make_symbol(trade_mode=3)},
    )
    fake.ticks["XAUUSDm"] = fresh_tick(age_s=3600)  # weekend: stale tick
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    await gateway.connect()
    report = await gateway.startup_checks(["XAUUSDm", "NOPE"], mode=Mode.DEMO, require_hedging=True)
    text = " | ".join(report.problems)
    assert not report.ok
    assert "AutoTrading is disabled" in text
    assert "expert/algorithmic trading is disabled" in text
    assert "mode DEMO but the account is REAL" in text
    assert "hedging account required" in text
    assert "symbol NOPE" in text
    assert any("not open for new entries" in w for w in report.warnings)
    assert any("server UTC offset unknown" in w for w in report.warnings)
    await gateway.close()


async def test_live_mode_requires_a_real_account(gw: MT5Gateway) -> None:
    report = await gw.startup_checks(["XAUUSDm"], mode=Mode.LIVE, require_hedging=False)
    assert "mode LIVE but the account is DEMO" in report.problems


async def test_data_calls_before_offset_is_known_fail_loudly(clock: FakeClock, fake: FakeMT5) -> None:
    gateway = MT5Gateway(CREDS, clock=clock, module_loader=lambda: fake)
    await gateway.connect()
    with pytest.raises(GatewayError, match="offset unknown"):
        await gateway.tick("XAUUSDm")
    gateway.set_server_offset(OFFSET)
    assert await gateway.tick("XAUUSDm") is not None
    await gateway.close()
