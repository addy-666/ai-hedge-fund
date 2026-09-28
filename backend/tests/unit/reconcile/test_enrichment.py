"""Trade enrichment maths (docs/03 §14.3) on synthetic paths. Expected values derived by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.domain.enums import DealEntry, DealReason, Side, Timeframe
from aifund.domain.market import Bar, Deal
from aifund.reconcile import enrichment as en

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
POINT = D("0.01")


def bar(minute: int, low: str, high: str, spread: int = 28) -> Bar:
    lo, hi = D(low), D(high)
    return Bar(
        symbol="XAUUSD", timeframe=Timeframe.M1, time=T0 + timedelta(minutes=minute),
        open=lo, high=hi, low=lo, close=hi, tick_volume=1, spread_points=spread,
    )  # fmt: skip


PATH = [bar(5, "4146.00", "4152.00"), bar(6, "4148.50", "4158.40")]


def test_buy_excursions_use_bid_prices() -> None:
    ex = en.excursion(Side.BUY, D("4150.28"), PATH, POINT)
    assert ex == en.Excursion(mae_price=D("4.28"), mfe_price=D("8.12"))  # 4150.28-4146.00, 4158.40-4150.28
    # 4.28 / 11.93 = 0.358759..., 8.12 / 11.93 = 0.680637...
    assert en.excursion_r(ex, D("11.93")) == (D("-0.3588"), D("0.6806"))


def test_sell_excursions_use_ask_prices() -> None:
    path = [bar(5, "4146.00", "4152.00", spread=28), bar(6, "4140.10", "4149.00", spread=30)]
    ex = en.excursion(Side.SELL, D("4150.00"), path, POINT)
    # ask highs 4152.28 / 4149.30 -> worst 4152.28; ask lows 4146.28 / 4140.40 -> best 4140.40
    assert ex == en.Excursion(mae_price=D("2.28"), mfe_price=D("9.60"))


def test_excursions_are_never_negative() -> None:
    ex = en.excursion(Side.BUY, D("4140.00"), PATH, POINT)  # every bar above the entry
    assert ex is not None
    assert ex.mae_price == D("0")
    assert ex.mfe_price == D("18.40")


def test_exit_fills_count_even_beyond_the_bars() -> None:
    # stopped at 4138.35 inside a minute whose bar is left out: the fill itself is the worst price seen
    ex = en.excursion(Side.BUY, D("4150.28"), PATH, POINT, fills=[D("4138.35")])
    assert ex == en.Excursion(mae_price=D("11.93"), mfe_price=D("8.12"))
    assert en.excursion_r(ex, D("11.93")) == (D("-1.0000"), D("0.6806"))  # exactly -1R, not beyond
    # a trade shorter than a full minute: only its exit
    only = en.excursion(Side.SELL, D("4150.00"), [], POINT, fills=[D("4151.00")])
    assert only == en.Excursion(mae_price=D("1.00"), mfe_price=D("0"))


def test_no_bars_no_excursion_and_no_stop_no_r() -> None:
    assert en.excursion(Side.BUY, D("4150"), [], POINT) is None
    ex = en.Excursion(D("1"), D("2"))
    assert en.excursion_r(ex, None) == (None, None)  # orphan without a stop
    assert en.excursion_r(ex, D("0")) == (None, None)


def test_bars_during_the_trade_are_the_full_minutes_only() -> None:
    bars = [bar(m, "1", "2") for m in range(3, 24)]
    during = en.bars_during(
        bars, T0 + timedelta(minutes=5, seconds=30), T0 + timedelta(minutes=21, seconds=40)
    )
    # entry at 10:05:30 and exit at 10:21:40: 10:06 .. 10:20 were held in full; 10:05 and 10:21 were not
    assert [b.time.minute for b in during] == list(range(6, 21))
    exact = en.bars_during(bars, T0 + timedelta(minutes=5), T0 + timedelta(minutes=21))
    assert [b.time.minute for b in exact] == list(range(5, 21))  # on minute boundaries both ends count


def test_holding_time() -> None:
    opened, closed = T0 + timedelta(minutes=5, seconds=30), T0 + timedelta(hours=2, minutes=20)
    assert en.holding(opened, closed, Timeframe.M15) == (8, 134)  # 134.5 min -> 8 whole M15 bars
    assert en.holding(opened, closed, None) == (None, 134)


@pytest.mark.parametrize(
    ("comment", "level"),
    [
        ("[sl 4155.00]", D("4155.00")),
        ("[tp 111500.00]", D("111500.00")),
        ("[sl 0.98765]", D("0.98765")),
        ("closed by hand", None),
        ("sl 4155.00", None),
        ("", None),
    ],
)
def test_stop_level_from_the_broker_comment(comment: str, level: D | None) -> None:
    assert en.level_from_comment(comment) == level


def exit_deal(price: str, reason: DealReason, side: Side = Side.SELL, comment: str = "") -> Deal:
    return Deal(
        ticket=9, order=9, position_id=7, time=T0, time_server_epoch=0, symbol="XAUUSD", side=side,
        entry=DealEntry.OUT, reason=reason, magic=0, volume=D("0.04"), price=D(price), comment=comment,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("position_side", "deal", "level", "points"),
    [
        (Side.BUY, exit_deal("4136.00", DealReason.SL), D("4138.35"), 235),  # stopped 2.35 below the stop
        (Side.BUY, exit_deal("4139.00", DealReason.SL), D("4138.35"), -65),  # filled better than the stop
        (Side.BUY, exit_deal("4179.60", DealReason.TP), D("4179.60"), 0),
        (Side.SELL, exit_deal("4162.50", DealReason.SL, Side.BUY), D("4161.93"), 57),  # buy-back 0.57 higher
        (
            Side.BUY,
            exit_deal("4136.00", DealReason.SL, comment="[sl 4137.00]"),
            D("4138.35"),
            100,
        ),  # comment wins
        (
            Side.BUY,
            exit_deal("4136.00", DealReason.CLIENT),
            D("4138.35"),
            None,
        ),  # no level for a manual close
        (Side.BUY, exit_deal("4136.00", DealReason.SL), None, None),
    ],
)
def test_stop_and_target_exit_slippage(
    position_side: Side, deal: Deal, level: D | None, points: int | None
) -> None:
    assert en.level_exit_slippage(position_side, deal, level, POINT) == points
