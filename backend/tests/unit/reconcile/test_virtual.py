"""Virtual trades (docs/03 §14.4): entry, SL-first, gaps, expiry, R/MAE/MFE — same maths as real trades.

XAUUSD, point 0.01, 28-point spread (0.28). A signal on the bar closing at T0+15 enters at the open of the
next bar (T0+15): a BUY pays the ask = bid open + spread, a SELL gets the bid. Stop 11.93 away (1R), target
29.32 away. Expected values by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from aifund.config.trading_config import PositionManagementConfig, SessionConfig
from aifund.domain.enums import CloseReason, Side, Timeframe, VirtualStatus
from aifund.domain.market import Bar
from aifund.market.sessions import SessionCalendar
from aifund.reconcile.virtual import MAX_VIRTUAL_BARS, VirtualPath, simulate, virtual_expiry

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
ENTRY = T0 + timedelta(minutes=15)
POINT = D("0.01")
SL, TP = D("11.93"), D("29.32")


def bar(minute: int, o: str = "4150.00", h: str = "4151.00", lo: str = "4149.00", c: str = "4150.50") -> Bar:
    return Bar(
        symbol="XAUUSD", timeframe=Timeframe.M1, time=T0 + timedelta(minutes=minute), open=D(o), high=D(h),
        low=D(lo), close=D(c), tick_volume=1, spread_points=28,
    )  # fmt: skip


def run(
    bars: list[Bar], *, side: Side = Side.BUY, now_minute: int = 60, expires_minute: int = 15 + 720
) -> VirtualPath:
    return simulate(
        side=side,
        entry_time=ENTRY,
        sl_distance=SL,
        tp_distance=TP,
        expires_at=T0 + timedelta(minutes=expires_minute),
        expire_reason=CloseReason.TIME_STOP,
        bars=bars,
        point=POINT,
        now=T0 + timedelta(minutes=now_minute),
    )


def test_buy_enters_at_the_ask_of_the_next_bar_open_and_stops_out() -> None:
    v = run([bar(15), bar(16, lo="4138.00")])
    assert (v.entry_price, v.sl, v.tp) == (D("4150.28"), D("4138.35"), D("4179.60"))
    assert (v.status, v.exit_reason, v.exit_price) == (VirtualStatus.CLOSED, CloseReason.SL, D("4138.35"))
    assert v.exit_time == T0 + timedelta(minutes=16)
    assert v.r_multiple == D("-1.0000")
    # MAE is the stop fill (the 4138.00 dip happened in the exit minute); MFE 4151.00 - 4150.28 = 0.72
    assert (v.mae_r, v.mfe_r) == (D("-1.0000"), D("0.0604"))


def test_take_profit() -> None:
    v = run([bar(15), bar(16), bar(17, h="4180.00")])
    assert (v.exit_reason, v.exit_price, v.r_multiple) == (
        CloseReason.TP,
        D("4179.60"),
        D("2.4577"),
    )  # 29.32/11.93


def test_stop_first_when_both_touched_in_one_bar() -> None:
    v = run([bar(15), bar(16, h="4185.00", lo="4130.00")])
    assert (v.exit_reason, v.r_multiple) == (CloseReason.SL, D("-1.0000"))


def test_a_gap_through_the_stop_fills_at_the_open() -> None:
    v = run([bar(15), bar(16, o="4135.00", h="4136.00", lo="4134.00", c="4135.50")])
    assert (v.exit_price, v.r_multiple) == (D("4135.00"), D("-1.2808"))  # (4135.00-4150.28)/11.93 = -1.280805


def test_sell_mirrors_on_ask_prices() -> None:
    v = run([bar(15), bar(16, h="4161.70")], side=Side.SELL)  # ask high 4161.98 >= stop 4161.93
    assert (v.entry_price, v.sl, v.tp) == (D("4150.00"), D("4161.93"), D("4120.68"))
    assert (v.exit_reason, v.exit_price, v.r_multiple) == (CloseReason.SL, D("4161.93"), D("-1.0000"))


def test_expiry_exits_at_the_market() -> None:
    v = run([bar(m) for m in range(15, 25)], now_minute=25, expires_minute=20)
    assert (v.status, v.exit_reason, v.exit_time) == (
        VirtualStatus.EXPIRED,
        CloseReason.TIME_STOP,
        T0 + timedelta(minutes=20),
    )
    assert v.exit_price == D("4150.50")  # the bid close of the last bar before expiry (T0+19)
    assert v.r_multiple == D("0.0184")  # 0.22 / 11.93 = 0.018440
    assert (v.mae_r, v.mfe_r) == (D("-0.1073"), D("0.0604"))  # 1.28 and 0.72 over five full minutes


def test_a_sell_expires_at_the_ask() -> None:
    v = run([bar(m) for m in range(15, 25)], side=Side.SELL, now_minute=25, expires_minute=20)
    assert v.exit_price == D("4150.78")  # bid close 4150.50 + 0.28
    assert v.r_multiple == D("-0.0654")  # -0.78 / 11.93 = -0.065381


def test_pending_until_the_entry_bar_has_closed() -> None:
    assert run([], now_minute=15).status is VirtualStatus.PENDING
    v = run([bar(15)], now_minute=16)
    assert (v.status, v.entry_price, v.exit_reason) == (VirtualStatus.OPEN, D("4150.28"), None)


def test_open_while_nothing_is_hit() -> None:
    v = run([bar(m) for m in range(15, 30)], now_minute=30)
    assert v.status is VirtualStatus.OPEN
    assert v.r_multiple is None


def test_no_entry_when_the_next_bar_never_comes() -> None:
    assert run([], now_minute=15 + 60).status is VirtualStatus.PENDING  # history may still sync
    assert run([], now_minute=15 + 24 * 60 + 1).status is VirtualStatus.NO_ENTRY
    late = run([bar(15 + 30), bar(15 + 31)], now_minute=120)  # the market reopened much later
    assert late.status is VirtualStatus.NO_ENTRY


def test_the_entry_bar_itself_can_stop_out() -> None:
    v = run([bar(15, lo="4130.00")])  # entered at the open, stopped within the same minute
    assert (v.exit_reason, v.exit_time, v.r_multiple) == (CloseReason.SL, ENTRY, D("-1.0000"))
    assert (v.mae_r, v.mfe_r) == (D("-1.0000"), D("0.0000"))  # no full minute held: only the fill counts


# ---------------------------------------------------------------- expiry (pipeline and research)

US = SessionCalendar.from_config(SessionConfig())  # New York 17:00 close, 18:00 open; weekends shut
MON_10 = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
FRI_20 = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)  # the weekend close is 21:00 UTC (17:00 New York, EDT)


def test_expiry_is_the_time_stop_on_a_normal_day() -> None:
    assert virtual_expiry(MON_10, Timeframe.M15, PositionManagementConfig(), US, trade_weekends=False) == (
        MON_10 + timedelta(minutes=15 * 48),
        CloseReason.TIME_STOP,
    )


def test_expiry_without_a_time_stop_uses_the_cap() -> None:
    pm = PositionManagementConfig(time_stop_bars=None)
    assert virtual_expiry(MON_10, Timeframe.M15, pm, None, trade_weekends=True) == (
        MON_10 + timedelta(minutes=15 * MAX_VIRTUAL_BARS),
        CloseReason.TIME_STOP,
    )


def test_expiry_at_the_pre_close_flatten_when_it_comes_first() -> None:
    assert virtual_expiry(FRI_20, Timeframe.M15, PositionManagementConfig(), US, trade_weekends=False) == (
        datetime(2026, 10, 2, 20, 30, tzinfo=UTC),
        CloseReason.FLATTEN,
    )
    # weekend symbols, no calendar or flatten disabled: the time stop
    no_flatten = PositionManagementConfig(flatten_before_close_minutes=None)
    for session, weekends, pm in [
        (US, True, PositionManagementConfig()),
        (None, False, PositionManagementConfig()),
        (US, False, no_flatten),
    ]:
        assert virtual_expiry(FRI_20, Timeframe.M15, pm, session, trade_weekends=weekends) == (
            FRI_20 + timedelta(minutes=15 * 48),
            CloseReason.TIME_STOP,
        )


def test_no_trade_when_it_would_be_flattened_at_once() -> None:
    late = datetime(2026, 10, 2, 20, 40, tzinfo=UTC)  # after the 20:30 flatten
    assert virtual_expiry(late, Timeframe.M15, PositionManagementConfig(), US, trade_weekends=False) is None
