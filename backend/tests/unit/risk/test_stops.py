"""Stops: hand-computed cases on the real Vantage XAUUSD spec, plus property tests of every guarantee."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aifund.config.trading_config import StopsConfig
from aifund.domain.enums import Side
from aifund.domain.market import SymbolSpec
from aifund.domain.values import is_multiple_of
from aifund.risk.stops import StopError, plan_stops

# Values from the Vantage demo smoke test (XAUUSD: stops_level 20 points)
XAU = SymbolSpec(
    symbol="XAUUSD", digits=2, point=D("0.01"), tick_size=D("0.01"), tick_value=D("1"),
    contract_size=D("100"),
    volume_min=D("0.01"), volume_max=D("100"), volume_step=D("0.01"), stops_level_points=20,
    freeze_level_points=0, filling_mode_flags=2, currency_profit="USD", currency_margin="XAU",
)  # fmt: skip
CFG = StopsConfig()  # k_sl 1.5 [1.0, 3.0], buffer 0.1 ATR, rr 2.0 [1.2, 4.0]
ATR = D("9.67")
SPREAD = D("0.28")


def test_invalidation_based_stop_and_target_by_hand() -> None:
    plan = plan_stops(
        side=Side.BUY,
        entry_ref=D("4150.68"),
        spread=SPREAD,
        atr=ATR,
        spec=XAU,
        cfg=CFG,
        invalidation=D("4140.00"),
        target=D("4180.00"),
    )
    # raw = (4150.68 - 4140.00) + 0.1 * 9.67 + 0.28 = 11.927 -> within [9.67, 29.01]
    # SL = 4150.68 - 11.927 = 4138.753 -> rounded DOWN (away) = 4138.75 ; distance 11.93
    assert (plan.sl, plan.sl_distance) == (D("4138.75"), D("11.93"))
    # target RR = 29.32 / 11.93 = 2.46 -> inside [1.2, 4] -> used exactly
    assert (plan.tp, plan.tp_distance, plan.used_target) == (D("4180.00"), D("29.32"), True)
    assert plan.used_invalidation
    assert plan.clamped == ""


def test_default_stop_without_levels() -> None:
    plan = plan_stops(side=Side.SELL, entry_ref=D("4150.40"), spread=SPREAD, atr=ATR, spec=XAU, cfg=CFG)
    # 1.5 * 9.67 = 14.505 -> SL = 4164.905 -> rounded UP (away) = 4164.91 ; distance 14.51
    # TP = 4150.40 - 2 * 14.51 = 4121.38 (exact on tick)
    assert (plan.sl, plan.sl_distance, plan.tp, plan.rr) == (D("4164.91"), D("14.51"), D("4121.38"), D("2"))
    assert not plan.used_invalidation


def test_tight_invalidation_is_widened_to_min_atr_and_far_one_capped() -> None:
    tight = plan_stops(
        side=Side.BUY,
        entry_ref=D("4150.68"),
        spread=SPREAD,
        atr=ATR,
        spec=XAU,
        cfg=CFG,
        invalidation=D("4150.00"),
    )
    assert (tight.sl_distance, tight.clamped) == (D("9.67"), "min_atr")
    far = plan_stops(
        side=Side.BUY,
        entry_ref=D("4150.68"),
        spread=SPREAD,
        atr=ATR,
        spec=XAU,
        cfg=CFG,
        invalidation=D("4000.00"),
    )
    assert far.clamped == "max_atr"
    assert far.sl_distance == D("29.01")  # 3 * 9.67


def test_wrong_side_levels_are_ignored() -> None:
    plan = plan_stops(
        side=Side.BUY,
        entry_ref=D("4150.68"),
        spread=SPREAD,
        atr=ATR,
        spec=XAU,
        cfg=CFG,
        invalidation=D("4160.00"),
        target=D("4100.00"),
    )
    assert not plan.used_invalidation
    assert not plan.used_target
    assert plan.rr == D("2")


def test_out_of_band_target_is_clamped() -> None:
    plan = plan_stops(
        side=Side.BUY,
        entry_ref=D("4150.68"),
        spread=SPREAD,
        atr=ATR,
        spec=XAU,
        cfg=CFG,
        invalidation=D("4140.00"),
        target=D("4152.00"),
    )  # RR ~0.11
    # 1.2 * 11.93 = 14.316 -> TP 4164.996: rounding toward entry (4164.99) would give RR 1.1995 < 1.2,
    # so it rounds away: 4165.00, RR 14.32 / 11.93 = 1.2003
    assert plan.tp == D("4165.00")
    assert plan.rr >= D("1.2")
    assert not plan.used_target


def test_broker_minimum_distance_wins_on_tiny_atr() -> None:
    plan = plan_stops(side=Side.BUY, entry_ref=D("4150.68"), spread=SPREAD, atr=D("0.05"), spec=XAU, cfg=CFG)
    # broker min = (20 + 2) * 0.01 + 0.28 = 0.50
    assert (plan.sl_distance, plan.clamped) == (D("0.50"), "broker_min")


def test_invalid_inputs_raise() -> None:
    with pytest.raises(StopError, match="ATR"):
        plan_stops(side=Side.BUY, entry_ref=D("100"), spread=D(0), atr=D(0), spec=XAU, cfg=CFG)


# ---------------------------------------------------------------- properties

prices = st.decimals(min_value=D("1"), max_value=D("100000"), places=2, allow_nan=False)
atrs = st.decimals(min_value=D("0.01"), max_value=D("2000"), places=2, allow_nan=False)
offsets = st.decimals(min_value=D("-5000"), max_value=D("5000"), places=2, allow_nan=False)


@settings(max_examples=400, deadline=None)
@given(side=st.sampled_from(list(Side)), entry=prices, atr=atrs,
       spread=st.decimals(D("0"), D("20"), places=2), inv=st.none() | offsets,
       tgt=st.none() | offsets)  # fmt: skip
def test_stop_plan_guarantees(side: Side, entry: D, atr: D, spread: D, inv: D | None, tgt: D | None) -> None:
    try:
        plan = plan_stops(
            side=side,
            entry_ref=entry,
            spread=spread,
            atr=atr,
            spec=XAU,
            cfg=CFG,
            invalidation=None if inv is None else entry + inv,
            target=None if tgt is None else entry + tgt,
        )
    except StopError:
        return  # e.g. the stop would be at or below zero on tiny prices
    sign = 1 if side is Side.BUY else -1
    assert sign * (entry - plan.sl) > 0  # SL on the loss side
    assert sign * (plan.tp - entry) > 0  # TP on the profit side
    assert is_multiple_of(plan.sl, XAU.tick_size)
    assert is_multiple_of(plan.tp, XAU.tick_size)
    assert plan.sl_distance == abs(entry - plan.sl)
    assert plan.tp_distance == abs(plan.tp - entry)
    broker_min = (XAU.stops_level_points + 2) * XAU.point + spread
    assert plan.sl_distance >= broker_min
    # within the ATR band (rounding may add < 1 tick) unless the broker minimum forced it wider
    if plan.clamped != "broker_min":
        assert CFG.k_sl_min * atr <= plan.sl_distance < CFG.k_sl_max * atr + XAU.tick_size
    # RR within band: the ceiling may be exceeded by < 1 tick only when the floor forced rounding away
    assert plan.tp_distance < CFG.rr_max * plan.sl_distance + XAU.tick_size
    assert plan.tp_distance >= CFG.rr_min * plan.sl_distance  # the floor holds exactly


@settings(max_examples=200, deadline=None)
@given(entry=prices, atr=atrs)
def test_wider_atr_never_tightens_the_stop(entry: D, atr: D) -> None:
    try:
        a = plan_stops(side=Side.BUY, entry_ref=entry, spread=D("0.2"), atr=atr, spec=XAU, cfg=CFG)
        b = plan_stops(side=Side.BUY, entry_ref=entry, spread=D("0.2"), atr=atr * 2, spec=XAU, cfg=CFG)
    except StopError:
        return
    assert b.sl_distance >= a.sl_distance
