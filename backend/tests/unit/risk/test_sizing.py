"""Sizing: hand-computed cases with the real Vantage XAUUSD figures, and property tests of the guarantees."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aifund.config.trading_config import RiskConfig
from aifund.domain.enums import ReasonCode
from aifund.domain.values import is_multiple_of
from aifund.risk.sizing import SizingInputs, SizingRejected, confidence_factor, size_position
from tests.unit.risk.test_stops import XAU

CFG = RiskConfig()  # 0.5% base, 1% max, threshold 65, conf 0.5..1.0, vol 0.5 above rank .9, dd 0.5 above 5%
# Smoke test: 0.01 lot XAUUSD margin $8.30 at 1:500 -> $830 per lot; notional = margin x leverage.
MARGIN_PER_LOT, NOTIONAL_PER_LOT = D("830"), D("415000")
# 11.93 stop on gold: 1 lot = 100 oz -> $1,193 loss per lot
LOSS_PER_LOT = D("1193")


def inputs(**over: object) -> SizingInputs:
    base: dict[str, object] = dict(
        equity=D("10000"),
        free_margin=D("10000"),
        final_confidence=90,
        loss_per_lot=LOSS_PER_LOT,
        margin_per_lot=MARGIN_PER_LOT,
        notional_per_lot=NOTIONAL_PER_LOT,
    )
    return SizingInputs(**{**base, **over})  # type: ignore[arg-type]


def test_full_confidence_sizing_by_hand() -> None:
    r = size_position(inputs(), XAU, CFG)
    # risk = 10,000 x 0.5% = $50 ; 50 / 1193 = 0.0419 lot -> floor 0.04 ; initial risk 0.04 x 1193 = $47.72
    assert (r.risk_money, r.lots, r.initial_risk_money) == (D("50.0"), D("0.04"), D("47.72"))
    assert (r.margin, r.notional) == (D("33.20"), D("16600.00"))
    assert r.worksheet["lots"] == "0.04"


def test_confidence_scales_risk_down_linearly() -> None:
    assert confidence_factor(65, CFG) == D("0.5")
    assert confidence_factor(90, CFG) == D("1.0")
    assert confidence_factor(77, CFG) == D("0.5") + D("0.5") * D(12) / D(25)
    assert confidence_factor(40, CFG) == D("0.5")
    r = size_position(inputs(final_confidence=65), XAU, CFG)
    assert (r.risk_money, r.lots) == (D("25.00"), D("0.02"))  # $25 / 1193 = 0.021 -> 0.02


def test_volatility_drawdown_and_rule_factors_multiply() -> None:
    r = size_position(
        inputs(equity=D("100000"), atr_pct_rank=0.95, drawdown_pct=D("6"), rule_risk_factor=D("0.5")),
        XAU,
        CFG,
    )
    # 100k x 0.5% x 0.5 (vol) x 0.5 (dd) x 0.5 (rule) = $62.50 -> 0.0523 -> 0.05 lot
    assert (r.risk_money, r.lots) == (D("62.5000"), D("0.05"))
    assert r.worksheet["vol_factor"] == "0.5"
    assert r.worksheet["drawdown_factor"] == "0.5"


def test_rule_factor_is_floored_and_can_never_scale_up() -> None:
    low = size_position(inputs(equity=D("100000"), rule_risk_factor=D("0.01")), XAU, CFG)
    high = size_position(inputs(equity=D("100000"), rule_risk_factor=D("5")), XAU, CFG)
    assert low.worksheet["rule_factor"] == "0.25"
    assert high.worksheet["rule_factor"] == "1"


def test_small_account_is_rejected_instead_of_upsized() -> None:
    # The prototype audit case: $500 account -> $2.50 budget, but 0.01 lot risks $11.93 (4.8x)
    with pytest.raises(SizingRejected) as exc:
        size_position(inputs(equity=D("500"), free_margin=D("500")), XAU, CFG)
    assert exc.value.reason is ReasonCode.RISK_BELOW_MIN_LOT
    assert exc.value.worksheet["min_lot_risk"] == "11.93"


def test_min_lot_overshoot_is_honoured_when_configured() -> None:
    cfg = CFG.model_copy(update={"min_lot_overshoot_pct": D("20")})
    # budget $10 (equity 2000), min lot risks $11.93 -> 19.3% over: allowed with a 20% overshoot
    assert size_position(inputs(equity=D("2000"), free_margin=D("2000")), XAU, cfg).lots == D("0.01")


def test_margin_limit_rejects() -> None:
    with pytest.raises(SizingRejected) as exc:
        size_position(inputs(free_margin=D("100")), XAU, CFG)  # 0.04 x 830 = 33.2 > 100 x 0.3
    assert exc.value.reason is ReasonCode.MARGIN


def test_volume_is_capped_by_config_and_broker() -> None:
    r = size_position(inputs(equity=D("100000000"), free_margin=D("100000000")), XAU, CFG)
    assert r.lots == D("5.0")  # max_lots_per_symbol


def test_risk_pct_never_exceeds_the_ceiling() -> None:
    cfg = CFG.model_copy(update={"risk_per_trade_pct": D("1.0"), "max_risk_per_trade_pct": D("1.0")})
    assert size_position(inputs(), XAU, cfg).risk_pct == D("1.0")


def test_invalid_broker_figures_are_rejected() -> None:
    with pytest.raises(SizingRejected) as exc:
        size_position(inputs(loss_per_lot=D("0")), XAU, CFG)
    assert exc.value.reason is ReasonCode.INTERNAL_ERROR


# ---------------------------------------------------------------- properties

money = st.decimals(min_value=D("50"), max_value=D("5000000"), places=2)
loss = st.decimals(min_value=D("1"), max_value=D("100000"), places=2)


@settings(max_examples=400, deadline=None)
@given(equity=money, per_lot=loss, conf=st.integers(0, 100), rank=st.none() | st.floats(0, 1),
       dd=st.decimals(D("0"), D("50"), places=1),
       overshoot=st.sampled_from([D("0"), D("10"), D("50")]))  # fmt: skip
def test_sizing_guarantees(equity: D, per_lot: D, conf: int, rank: float | None, dd: D, overshoot: D) -> None:
    cfg = CFG.model_copy(update={"min_lot_overshoot_pct": overshoot})
    args = inputs(
        equity=equity,
        free_margin=equity * 1000,
        loss_per_lot=per_lot,
        final_confidence=conf,
        atr_pct_rank=rank,
        drawdown_pct=dd,
    )
    rejection: ReasonCode | None = None
    try:
        r = size_position(args, XAU, cfg)
    except SizingRejected as exc:
        rejection = exc.reason
    if rejection is not None:
        assert rejection is ReasonCode.RISK_BELOW_MIN_LOT  # the only possible rejection here
        return
    assert is_multiple_of(r.lots, XAU.volume_step)
    assert XAU.volume_min <= r.lots <= min(XAU.volume_max, cfg.max_lots_per_symbol)
    assert r.risk_pct <= cfg.max_risk_per_trade_pct
    assert r.initial_risk_money <= r.risk_money * (1 + overshoot / 100)
    assert r.initial_risk_money == r.lots * per_lot


@settings(max_examples=200, deadline=None)
@given(equity=money, per_lot=loss)
def test_bigger_stop_never_means_bigger_position(equity: D, per_lot: D) -> None:
    def lots(p: D) -> D:
        try:
            return size_position(
                inputs(equity=equity, free_margin=equity * 1000, loss_per_lot=p), XAU, CFG
            ).lots
        except SizingRejected:
            return D(0)

    assert lots(per_lot * 2) <= lots(per_lot)
