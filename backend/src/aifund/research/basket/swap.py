"""Overnight swap as a price distance (roadmap 11.11, docs/09 §9.2). Research only.

A position open at a server rollover (00:00 on the server clock) pays the broker's swap; the rollover of the
symbol's ``swap_rollover3days`` counts three nights (it carries the weekend). Today's rates are applied to all
history, and a positive swap counts as 0: research never books a financing credit. Charges are price distances
per unit of volume, so they turn into R with the trade's own stop distance, like the cost model.

Modes (ENUM_SYMBOL_SWAP_MODE): DISABLED (nothing), POINTS (swap x point), CURRENCY_DEPOSIT (account money per
lot, converted with tick_size / tick_value like the commission), INTEREST_CURRENT (annual percent of the price
over 360 days). Any other mode, or a spec exported before the swap fields existed, raises ``SwapUnavailable``:
a multi-day hold without its financing overstates the edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from aifund.domain.enums import Side, SwapMode
from aifund.domain.market import SymbolSpec
from aifund.research.basket.candles import DAY, weekday

INTEREST_DAYS = 360


class SwapUnavailable(Exception):
    """The export cannot say what holding a position overnight costs."""


@dataclass(frozen=True)
class SwapModel:
    mode: SwapMode
    long: float  # the broker's figure for each side, sign as stated (negative = the trader pays)
    short: float
    point: float
    tick_size: float
    tick_value: float
    triple_weekday: int  # server weekday (Monday 0) whose rollover counts three nights

    @classmethod
    def of(cls, spec: SymbolSpec) -> SwapModel:
        long, short, mode, triple = spec.swap_long, spec.swap_short, spec.swap_mode, spec.swap_rollover3days
        if long is None or short is None or mode is None or triple is None:
            raise SwapUnavailable(f"{spec.symbol}: no swap in the export (re-export with roadmap 11.11)")
        if mode is SwapMode.UNSUPPORTED:
            raise SwapUnavailable(f"{spec.symbol}: its swap mode is not implemented (docs/09 §9.2)")
        return cls(
            mode=mode,
            long=float(long),
            short=float(short),
            point=float(spec.point),
            tick_size=float(spec.tick_size),
            tick_value=float(spec.tick_value),
            triple_weekday=(triple - 1) % 7,  # MT5 counts from Sunday 0
        )

    def night(self, side: Side, price: float) -> float:
        """One night's charge as a price distance (>= 0) for one unit of volume at ``price``."""
        rate = self.long if side is Side.BUY else self.short
        if self.mode is SwapMode.DISABLED or rate >= 0:
            return 0.0
        if self.mode is SwapMode.POINTS:
            return -rate * self.point
        if self.mode is SwapMode.CURRENCY_DEPOSIT:
            return -rate * self.tick_size / self.tick_value
        return price * -rate / 100 / INTEREST_DAYS  # INTEREST_CURRENT

    def nights(self, entry_minute: int, exit_minute: int) -> int:
        """Nights charged on a position held over ``(entry_minute, exit_minute]`` (server minutes)."""
        first = entry_minute // DAY + 1  # the first rollover strictly after the entry
        last = exit_minute // DAY  # the last one at or before the exit
        total = 0
        for day in range(first, last + 1):
            total += 3 if weekday(day * DAY - 1) == self.triple_weekday else 1
        return total

    def charge(self, side: Side, entry_minute: int, exit_minute: int, price: float) -> float:
        """The price distance this position pays in swap (>= 0)."""
        return self.night(side, price) * self.nights(entry_minute, exit_minute)
