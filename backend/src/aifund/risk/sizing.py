"""Position sizing (docs/03 §11). Pure Decimal arithmetic; broker figures come in as inputs.

Because the stop distance scales with ATR, a fixed money risk already gives smaller positions in
volatile markets. On top of that, explicit factors scale the risk DOWN for low confidence, an extreme
volatility regime, drawdown and learned-rule penalties. Nothing here can scale risk up beyond
``risk_per_trade_pct``, and the result is capped at ``max_risk_per_trade_pct``.

Volume is always floored to the broker's step. If even the minimum lot risks more than the budget
(plus ``min_lot_overshoot_pct``), the trade is REJECTED — never silently upsized (the prototype's
min-lot clamp risked 3x the budget on a small account).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from aifund.config.trading_config import RiskConfig
from aifund.domain.enums import ReasonCode
from aifund.domain.market import SymbolSpec
from aifund.domain.values import floor_volume


class SizingRejected(Exception):
    def __init__(self, reason: ReasonCode, detail: str, worksheet: dict[str, str]) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.worksheet = worksheet


@dataclass(frozen=True)
class SizingInputs:
    equity: Decimal
    free_margin: Decimal
    final_confidence: int
    loss_per_lot: Decimal  # account-currency loss of 1.0 lot if the stop is hit (broker calculator)
    margin_per_lot: Decimal  # broker margin for 1.0 lot at the entry price
    notional_per_lot: Decimal  # account-currency exposure of 1.0 lot
    commission_per_lot: Decimal = Decimal(0)  # round-turn
    atr_pct_rank: float | None = None
    drawdown_pct: Decimal = Decimal(0)
    rule_risk_factor: Decimal = Decimal(1)


@dataclass(frozen=True)
class SizingResult:
    lots: Decimal
    risk_pct: Decimal
    risk_money: Decimal
    initial_risk_money: Decimal
    margin: Decimal
    notional: Decimal
    worksheet: dict[str, str] = field(default_factory=dict)


def confidence_factor(confidence: int, cfg: RiskConfig) -> Decimal:
    """Linear from ``at_threshold`` (confidence == threshold) to ``at_90`` (confidence >= 90)."""
    lo, hi = cfg.confidence_risk_scaling.at_threshold, cfg.confidence_risk_scaling.at_90
    threshold = cfg.confidence_threshold
    if confidence >= 90 or threshold >= 90:
        return hi
    if confidence <= threshold:
        return lo
    return lo + (hi - lo) * Decimal(confidence - threshold) / Decimal(90 - threshold)


def size_position(inputs: SizingInputs, spec: SymbolSpec, cfg: RiskConfig) -> SizingResult:
    ws: dict[str, str] = {}

    def note(key: str, value: object) -> None:
        ws[key] = format(value, "f") if isinstance(value, Decimal) else str(value)

    def reject(reason: ReasonCode, detail: str) -> SizingRejected:
        note("rejected", f"{reason}: {detail}")
        return SizingRejected(reason, detail, ws)

    if inputs.equity <= 0:
        raise reject(ReasonCode.RISK_BELOW_MIN_LOT, f"non-positive equity {inputs.equity}")
    if inputs.loss_per_lot <= 0 or inputs.margin_per_lot < 0 or inputs.commission_per_lot < 0:
        raise reject(ReasonCode.INTERNAL_ERROR, "invalid broker figures for sizing")

    conf_f = confidence_factor(inputs.final_confidence, cfg)
    vol = cfg.vol_regime_scaling
    vol_f = (
        vol.factor
        if inputs.atr_pct_rank is not None and inputs.atr_pct_rank > float(vol.atr_pct_rank_above)
        else Decimal(1)
    )
    dd = cfg.drawdown_scaling
    dd_f = dd.factor if inputs.drawdown_pct > dd.dd_pct_above else Decimal(1)
    rule_f = min(max(inputs.rule_risk_factor, Decimal("0.25")), Decimal(1))
    risk_pct = min(cfg.risk_per_trade_pct * conf_f * vol_f * dd_f * rule_f, cfg.max_risk_per_trade_pct)
    risk_money = inputs.equity * risk_pct / 100
    per_lot = inputs.loss_per_lot + inputs.commission_per_lot
    lots_raw = risk_money / per_lot
    worksheet_items: tuple[tuple[str, object], ...] = (
        ("equity", inputs.equity),
        ("base_risk_pct", cfg.risk_per_trade_pct),
        ("confidence", inputs.final_confidence),
        ("confidence_factor", conf_f),
        ("vol_factor", vol_f),
        ("drawdown_factor", dd_f),
        ("rule_factor", rule_f),
        ("risk_pct", risk_pct),
        ("risk_money", risk_money),
        ("loss_per_lot", inputs.loss_per_lot),
        ("commission_per_lot", inputs.commission_per_lot),
        ("lots_raw", lots_raw),
    )
    for key, value in worksheet_items:
        note(key, value)

    lots = floor_volume(lots_raw, spec.volume_step)
    cap = min(spec.volume_max, floor_volume(cfg.max_lots_per_symbol, spec.volume_step))
    if lots > cap:
        lots = cap
        note("capped_at", cap)
    if lots < spec.volume_min:
        min_lot_risk = spec.volume_min * per_lot
        allowed = risk_money * (1 + cfg.min_lot_overshoot_pct / 100)
        note("min_lot_risk", min_lot_risk)
        if min_lot_risk > allowed:
            raise reject(
                ReasonCode.RISK_BELOW_MIN_LOT,
                f"minimum lot {spec.volume_min} risks {min_lot_risk} > budget {risk_money:.2f}",
            )
        lots = spec.volume_min

    margin = lots * inputs.margin_per_lot
    margin_cap = inputs.free_margin * cfg.max_margin_utilisation
    note("margin", margin)
    note("margin_cap", margin_cap)
    if margin > margin_cap:
        raise reject(ReasonCode.MARGIN, f"margin {margin} > {margin_cap}")

    initial_risk = lots * per_lot
    notional = lots * inputs.notional_per_lot
    note("lots", lots)
    note("initial_risk_money", initial_risk)
    note("notional", notional)
    return SizingResult(
        lots=lots,
        risk_pct=risk_pct,
        risk_money=risk_money,
        initial_risk_money=initial_risk,
        margin=margin,
        notional=notional,
        worksheet=ws,
    )
