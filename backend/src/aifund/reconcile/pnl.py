"""P&L aggregation over all deals of one position (docs/03 §14.2). Pure: no I/O, exact Decimal.

The prototype used only the last exit deal's ``profit`` and its sign, ignoring commission, swap, fees and
partial closes. Here every deal of the position counts: entry deals carry commission on many brokers, and a
position closed in parts has several exit deals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from aifund.domain.enums import CloseReason, DealEntry, DealReason, TradeOutcome
from aifund.domain.market import Deal

R_PLACES = Decimal("0.0001")
WIN_R = Decimal("0.1")  # |R| below this is BREAKEVEN
_BROKER_REASONS = {
    DealReason.SL: CloseReason.SL,
    DealReason.TP: CloseReason.TP,
    DealReason.SO: CloseReason.STOP_OUT,
}


@dataclass(frozen=True)
class PositionPnl:
    gross: Decimal
    commission: Decimal
    swap: Decimal
    fee: Decimal
    net: Decimal
    volume_in: Decimal
    volume_out: Decimal
    close_price_vwap: Decimal | None  # None while nothing has been closed
    close_time: datetime | None
    last_exit: Deal | None

    def fully_closed(self, volume_opened: Decimal) -> bool:
        """Exit deals cover the whole position (entry deals may be missing from a truncated history)."""
        return self.last_exit is not None and self.volume_out >= max(self.volume_in, volume_opened)


def _ordered(deals: Sequence[Deal]) -> list[Deal]:
    return sorted(deals, key=lambda d: (d.time, d.ticket))


def aggregate(deals: Sequence[Deal]) -> PositionPnl:
    """Sum every deal of ONE position. Duplicate tickets (a deal seen twice) are counted once."""
    unique = _ordered(list({d.ticket: d for d in deals}.values()))
    if len({d.position_id for d in unique}) > 1:
        raise ValueError("deals of more than one position")
    exits = [d for d in unique if d.entry.reduces_position]
    volume_out = sum((d.volume for d in exits), Decimal(0))
    vwap = None
    if len(exits) == 1:
        vwap = exits[0].price
    elif exits and volume_out > 0:
        # an average of several fills: two digits more than the prices carry, never scientific notation
        places = max(-int(d.price.as_tuple().exponent) for d in exits) + 2
        raw = sum((d.price * d.volume for d in exits), Decimal(0)) / volume_out
        vwap = raw.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    gross = sum((d.profit for d in unique), Decimal(0))
    commission = sum((d.commission for d in unique), Decimal(0))
    swap = sum((d.swap for d in unique), Decimal(0))
    fee = sum((d.fee for d in unique), Decimal(0))
    return PositionPnl(
        gross=gross,
        commission=commission,
        swap=swap,
        fee=fee,
        net=gross + commission + swap + fee,
        volume_in=sum((d.volume for d in unique if d.entry is DealEntry.IN), Decimal(0)),
        volume_out=volume_out,
        close_price_vwap=vwap,
        close_time=max(d.time for d in exits) if exits else None,
        last_exit=exits[-1] if exits else None,
    )


def close_reason(last_exit: Deal, engine_reason: CloseReason | None) -> CloseReason:
    """Map the last exit deal's DEAL_REASON to a close reason.

    ``engine_reason`` is the reason recorded on the engine's closing intent that produced this deal (None if
    no intent of ours matches it). An EXPERT deal we did not send came from another EA or script:
    MANUAL_EXTERNAL. CLIENT / MOBILE / WEB are a person closing it by hand.
    """
    if last_exit.reason in _BROKER_REASONS:
        return _BROKER_REASONS[last_exit.reason]
    if last_exit.reason is DealReason.EXPERT and engine_reason is not None:
        return engine_reason
    return CloseReason.MANUAL_EXTERNAL


def r_multiple(net: Decimal, initial_risk_money: Decimal | None) -> Decimal | None:
    """net P&L in units of the money at risk at entry; None for trades without a known risk (orphans)."""
    if initial_risk_money is None or initial_risk_money <= 0:
        return None
    return (net / initial_risk_money).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)


def outcome(r: Decimal | None) -> TradeOutcome | None:
    if r is None:
        return None
    if r >= WIN_R:
        return TradeOutcome.WIN
    if r <= -WIN_R:
        return TradeOutcome.LOSS
    return TradeOutcome.BREAKEVEN
