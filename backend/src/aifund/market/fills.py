"""How a resting stop-loss / take-profit fills within one bar. Shared by the SimBroker (simulated real
trades) and the virtual-trade tracker (counterfactual trades), so both follow exactly the same rule.

Bars are bid prices. A BUY position exits by selling at the bid; a SELL position exits by buying at the ask
(bid + the bar's spread). When the stop and the target are both touched in one bar, the stop is assumed to
have filled first (the pessimistic reading of an unknown intra-bar path). A bar that opens beyond the stop
fills at its open (a gap: worse than the stop); a target fills at the target, never better.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from aifund.domain.enums import DealReason, Side


@dataclass(frozen=True)
class ExitFill:
    reason: DealReason  # SL or TP
    price: Decimal


def exit_on_bar(
    side: Side,
    sl: Decimal | None,
    tp: Decimal | None,
    *,
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    spread: Decimal,
) -> ExitFill | None:
    if side is Side.BUY:
        if sl is not None and low <= sl:
            return ExitFill(DealReason.SL, open_ if open_ <= sl else sl)
        if tp is not None and high >= tp:
            return ExitFill(DealReason.TP, tp)
        return None
    ask_open, ask_high, ask_low = open_ + spread, high + spread, low + spread
    if sl is not None and ask_high >= sl:
        return ExitFill(DealReason.SL, ask_open if ask_open >= sl else sl)
    if tp is not None and ask_low <= tp:
        return ExitFill(DealReason.TP, tp)
    return None
