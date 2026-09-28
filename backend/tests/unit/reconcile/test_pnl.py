"""P&L aggregation (docs/03 §14.2). Every expected value is derived by hand in the comments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.domain.enums import CloseReason, DealEntry, DealReason, Side, TradeOutcome
from aifund.domain.market import Deal
from aifund.reconcile import pnl

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
MAGIC = 26092801


def deal(
    ticket: int,
    entry: DealEntry,
    volume: str,
    price: str,
    *,
    profit: str = "0",
    commission: str = "0",
    swap: str = "0",
    fee: str = "0",
    reason: DealReason = DealReason.EXPERT,
    minutes: int = 0,
    position_id: int = 7,
    magic: int = MAGIC,
    comment: str = "",
) -> Deal:
    return Deal(
        ticket=ticket,
        order=ticket + 100,
        position_id=position_id,
        time=T0 + timedelta(minutes=minutes),
        time_server_epoch=0,
        symbol="XAUUSD",
        side=Side.BUY if entry is DealEntry.IN else Side.SELL,
        entry=entry,
        reason=reason,
        magic=magic,
        volume=D(volume),
        price=D(price),
        profit=D(profit),
        commission=D(commission),
        swap=D(swap),
        fee=D(fee),
        comment=comment,
    )


# BUY 0.04 XAUUSD @ 4150.28; half closed by the engine at 4160.00, the rest stopped out at 4138.35.
# XAU: 1.00 move on 1 lot = 100 USD, so profit = price move x 100 x lots.
ENTRY = deal(1, DealEntry.IN, "0.04", "4150.28", commission="-0.14")
HALF = deal(2, DealEntry.OUT, "0.02", "4160.00", profit="19.44", commission="-0.07", minutes=30)  # 9.72x2
REST = deal(
    3, DealEntry.OUT, "0.02", "4138.35", profit="-23.86", commission="-0.07", swap="-0.35",
    reason=DealReason.SL, minutes=90,
)  # -11.93 x 2  # fmt: skip


def test_sums_every_deal_including_entry_commission_and_swap() -> None:
    s = pnl.aggregate([REST, ENTRY, HALF])  # order of arrival must not matter
    assert (s.gross, s.commission, s.swap, s.fee) == (D("-4.42"), D("-0.28"), D("-0.35"), D("0"))
    assert s.net == D("-5.05")  # -4.42 - 0.28 - 0.35
    assert (s.volume_in, s.volume_out) == (D("0.04"), D("0.04"))
    assert s.close_time == T0 + timedelta(minutes=90)
    assert s.last_exit == REST
    # (4160.00 x 0.02 + 4138.35 x 0.02) / 0.04 = 4149.175, kept at two digits beyond the prices
    assert str(s.close_price_vwap) == "4149.1750"
    assert s.fully_closed(D("0.04"))


def test_a_partial_close_is_not_fully_closed() -> None:
    s = pnl.aggregate([ENTRY, HALF])
    assert not s.fully_closed(D("0.04"))
    assert s.net == D("19.23")  # 19.44 - 0.14 - 0.07: realised so far
    assert s.close_price_vwap == D("4160.00")  # one exit: its exact price


def test_nothing_closed_yet() -> None:
    s = pnl.aggregate([ENTRY])
    assert (s.last_exit, s.close_time, s.close_price_vwap) == (None, None, None)
    assert not s.fully_closed(D("0.04"))
    assert not pnl.aggregate([]).fully_closed(D("0.04"))


def test_missing_entry_deal_falls_back_to_the_opened_volume() -> None:
    # a history that no longer shows the entry deal: exits must still cover what the ledger opened
    assert pnl.aggregate([HALF, REST]).fully_closed(D("0.04"))
    assert not pnl.aggregate([HALF]).fully_closed(D("0.04"))


def test_a_deal_seen_twice_counts_once() -> None:
    assert pnl.aggregate([ENTRY, HALF, HALF, REST]).net == D("-5.05")


def test_deals_of_two_positions_are_refused() -> None:
    with pytest.raises(ValueError, match="more than one position"):
        pnl.aggregate([ENTRY, deal(9, DealEntry.OUT, "0.04", "4151", position_id=8)])


def test_fees_count() -> None:
    out = deal(4, DealEntry.OUT, "0.04", "4151.28", profit="4.00", fee="-0.50", reason=DealReason.TP)
    assert pnl.aggregate([ENTRY, out]).net == D("3.36")  # 4.00 - 0.14 - 0.50


@pytest.mark.parametrize(
    ("reason", "engine", "expected"),
    [
        (DealReason.SL, None, CloseReason.SL),
        (DealReason.TP, None, CloseReason.TP),
        (DealReason.SO, None, CloseReason.STOP_OUT),
        (DealReason.SL, CloseReason.TIME_STOP, CloseReason.SL),  # the broker's stop filled, not our close
        (DealReason.EXPERT, CloseReason.TIME_STOP, CloseReason.TIME_STOP),
        (DealReason.EXPERT, CloseReason.REVERSAL, CloseReason.REVERSAL),
        (DealReason.EXPERT, None, CloseReason.MANUAL_EXTERNAL),  # another EA or script
        (DealReason.CLIENT, None, CloseReason.MANUAL_EXTERNAL),
        (DealReason.MOBILE, None, CloseReason.MANUAL_EXTERNAL),
        (DealReason.WEB, None, CloseReason.MANUAL_EXTERNAL),
        (DealReason.CLIENT, CloseReason.ENGINE, CloseReason.MANUAL_EXTERNAL),  # a person, whatever we sent
    ],
)
def test_close_reason(reason: DealReason, engine: CloseReason | None, expected: CloseReason) -> None:
    last = deal(5, DealEntry.OUT, "0.04", "4150", reason=reason)
    assert pnl.close_reason(last, engine) is expected


def test_r_multiple_and_outcome() -> None:
    r = pnl.r_multiple(D("-5.05"), D("47.72"))
    assert r == D("-0.1058")  # -5.05 / 47.72 = -0.105825...
    assert pnl.outcome(r) is TradeOutcome.LOSS
    assert pnl.r_multiple(D("95.44"), D("47.72")) == D("2.0000")
    assert pnl.r_multiple(D("5"), None) is None  # orphan: no known risk
    assert pnl.r_multiple(D("5"), D("0")) is None


@pytest.mark.parametrize(
    ("r", "expected"),
    [
        ("0.1", TradeOutcome.WIN),
        ("0.0999", TradeOutcome.BREAKEVEN),
        ("0", TradeOutcome.BREAKEVEN),
        ("-0.0999", TradeOutcome.BREAKEVEN),
        ("-0.1", TradeOutcome.LOSS),
    ],
)
def test_outcome_thresholds(r: str, expected: TradeOutcome) -> None:
    assert pnl.outcome(D(r)) is expected


def test_no_outcome_without_r() -> None:
    assert pnl.outcome(None) is None
