from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from aifund.adapters.mt5 import mapping as m
from aifund.domain.enums import (
    AccountTradeMode,
    DealEntry,
    DealReason,
    MarginMode,
    Side,
    Timeframe,
)
from aifund.domain.market import FillingMode, OrderRequest, TradeAction
from tests.fakes.fake_mt5 import make_account, make_symbol

OFFSET = timedelta(hours=3)
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)
SERVER_T0 = int((T0 + OFFSET).timestamp())


def test_prices_are_exact_and_quantized_to_digits() -> None:
    assert m.price(1.0847300000000001, 5) == Decimal("1.08473")
    assert m.price(2345.6, 2) == Decimal("2345.60")
    assert m.dec(0.1) == Decimal("0.1")


def test_account_mapping_and_unknown_enum() -> None:
    acc = m.account_from_mt5(make_account(trade_mode=2, margin_mode=0))
    assert acc.trade_mode is AccountTradeMode.REAL
    assert acc.margin_mode is MarginMode.NETTING
    assert acc.equity == Decimal("10050.25")
    with pytest.raises(m.MappingError):
        m.account_from_mt5(make_account(trade_mode=9))


@pytest.mark.parametrize(("trade_mode", "allowed"), [(4, True), (3, False), (1, False), (0, False)])
def test_only_full_trade_mode_allows_new_entries(trade_mode: int, allowed: bool) -> None:
    assert m.symbol_spec_from_mt5(make_symbol(trade_mode=trade_mode)).trade_allowed is allowed


def test_symbol_with_zero_tick_value_is_a_mapping_error() -> None:
    with pytest.raises(m.MappingError, match="XAUUSDm"):
        m.symbol_spec_from_mt5(make_symbol(trade_tick_value=0.0))


def test_bars_are_utc_sorted_and_exact() -> None:
    rows = [
        {
            "time": SERVER_T0 + 900,
            "open": 2350.1,
            "high": 2352.0,
            "low": 2349.5,
            "close": 2351.25,
            "tick_volume": 120,
            "spread": 18,
            "real_volume": 0,
        },
        {
            "time": SERVER_T0,
            "open": 2349.0,
            "high": 2350.5,
            "low": 2348.0,
            "close": 2350.1,
            "tick_volume": 100,
            "spread": 20,
            "real_volume": 0,
        },
    ]
    bars = m.bars_from_mt5(rows, "XAUUSDm", Timeframe.M15, 2, OFFSET)
    assert [b.time for b in bars] == [T0, T0 + timedelta(minutes=15)]
    assert bars[1].close == Decimal("2351.25")
    assert bars[0].spread_points == 20


def test_tick_uses_millisecond_time() -> None:
    raw = NS(time=SERVER_T0, time_msc=SERVER_T0 * 1000 + 250, bid=2350.1, ask=2350.3)
    tick = m.tick_from_mt5(raw, "XAUUSDm", 2, OFFSET)
    assert tick.time == T0 + timedelta(milliseconds=250)
    assert tick.spread == Decimal("0.20")


def test_position_without_stops_maps_to_none() -> None:
    raw = NS(
        ticket=5001,
        identifier=5001,
        symbol="XAUUSDm",
        type=1,
        volume=0.1,
        price_open=2350.1,
        sl=0.0,
        tp=2330.0,
        magic=26092801,
        comment="AF:ABCDEFGH",
        time=SERVER_T0,
        time_msc=0,
        profit=-3.2,
        swap=0.0,
    )
    pos = m.position_from_mt5(raw, 2, OFFSET)
    assert pos.side is Side.SELL
    assert pos.sl is None
    assert pos.tp == Decimal("2330.00")
    assert pos.time == T0


def _deal(**over: object) -> NS:
    base = dict(
        ticket=9,
        order=8,
        position_id=7,
        time=SERVER_T0,
        time_msc=0,
        symbol="XAUUSDm",
        type=0,
        entry=3,
        reason=4,
        magic=1,
        volume=0.1,
        price=2340.0,
        profit=-100.0,
        commission=-3.5,
        swap=-1.2,
        fee=0.0,
        comment="sl 2340.00",
    )
    return NS(**{**base, **over})


def test_deal_mapping_including_close_by_and_costs() -> None:
    deal = m.deal_from_mt5(_deal(), OFFSET)
    assert deal is not None
    assert deal.entry is DealEntry.OUT_BY
    assert deal.reason is DealReason.SL
    assert deal.net == Decimal("-104.7")
    assert deal.time == T0
    assert deal.time_server_epoch == SERVER_T0


def test_balance_deals_are_skipped_and_unknown_reasons_tolerated() -> None:
    assert m.deal_from_mt5(_deal(type=2), OFFSET) is None
    deal = m.deal_from_mt5(_deal(reason=42), OFFSET)
    assert deal is not None
    assert deal.reason is DealReason.OTHER


def test_request_mapping() -> None:
    deal_req = OrderRequest(
        action=TradeAction.DEAL,
        symbol="XAUUSDm",
        side=Side.SELL,
        volume=Decimal("0.10"),
        price=Decimal("2350.10"),
        sl=Decimal("2365.10"),
        tp=Decimal("2320.10"),
        deviation_points=20,
        magic=26092801,
        comment="AF:ABCDEFGH",
        filling=FillingMode.IOC,
    )
    out = m.request_to_mt5(deal_req)
    assert out == {
        "action": 1,
        "symbol": "XAUUSDm",
        "volume": 0.1,
        "type": 1,
        "price": 2350.1,
        "deviation": 20,
        "magic": 26092801,
        "comment": "AF:ABCDEFGH",
        "type_time": 0,
        "sl": 2365.1,
        "tp": 2320.1,
        "type_filling": 1,
    }
    sltp = OrderRequest(
        action=TradeAction.SLTP, symbol="XAUUSDm", position_ticket=5001, sl=Decimal("2340"), magic=26092801
    )
    assert m.request_to_mt5(sltp) == {
        "action": 6,
        "symbol": "XAUUSDm",
        "position": 5001,
        "sl": 2340.0,
        "tp": 0.0,
        "magic": 26092801,
    }


def test_order_result_mapping() -> None:
    res = m.order_result_from_mt5(
        NS(retcode=10030, order=0, deal=0, volume=0.0, price=0.0, comment="Unsupported")
    )
    assert res.retcode_name == "INVALID_FILL"
    assert m.order_result_from_mt5(NS(retcode=0, comment="Done")).retcode_name == "OK"
