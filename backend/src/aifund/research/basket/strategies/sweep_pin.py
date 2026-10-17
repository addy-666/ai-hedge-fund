"""SessionSweepPinBar v1.0 in FOLLOW mode (workshop set "XAUUSD-Follow-Rolling8h-H1").

For every closed H1 bar that OPENED in the 15:00–19:00 server window: the range is the high/low of the 8 hours
before the bar's open (from the finest bars exported; at least 60% of those minutes must have data). The bar
is a pin at the range low when its low swept below it, it closed back above, its lower wick exceeds 2 × its
body and it closed in the top 30% of its range (mirror at the high). FOLLOW trades WITH the sweep: a pin at
the low gives a SELL, a pin at the high a BUY (the low side is checked first; one trade per bar). Market entry
at the next bar's open; stop one tick beyond the pin's other end, at least 1.5 × ATR(14, H1); target 2R;
closed after 240 minutes or at the 23:30 daily flat; at most two entries per server day, one position at a
time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import Candles, atr_sma, minute_of_day
from aifund.research.basket.harness import FlatRule, Signal
from aifund.research.basket.strategies.base import Params, finest, hhmm


@dataclass(frozen=True)
class PinParams(Params):
    rolling_hours: float = 8.0
    min_cover_pct: float = 60.0
    trade_start: str = "15:00"
    trade_end: str = "19:00"
    wick_body_ratio: float = 2.0
    close_zone: float = 0.30
    atr_period: int = 14
    stop_buffer_ticks: int = 1
    stop_floor_atr: float = 1.5
    target_r: float = 2.0
    max_hold_min: int = 240
    flat: str = "23:30"


def is_pin(o: float, h: float, low: float, c: float, *, at_low: bool, ratio: float, zone: float) -> bool:
    rng = h - low
    if rng <= 0:
        return False
    body = abs(c - o)
    if at_low:
        return (min(c, o) - low) > ratio * body and c > h - zone * rng
    return (h - max(c, o)) > ratio * body and c < low + zone * rng


class SweepPinFollow:
    name: ClassVar[str] = "sweep_pin_follow"
    version: ClassVar[str] = "1.0"
    setup_tag: ClassVar[str] = "sweep_pin_follow"
    symbols: ClassVar[tuple[str, ...]] = ("XAUUSD",)
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.H1,)
    max_trades_per_day: ClassVar[int | None] = 2

    def __init__(self, p: PinParams | None = None) -> None:
        self.p = p or PinParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        bars, fine = data.get(Timeframe.H1), finest(data, Timeframe.H1)
        if bars is None or fine is None:
            return []
        p = self.p
        atr = atr_sma(bars, p.atr_period)
        window = round(p.rolling_hours * 60)
        start, end = hhmm(p.trade_start), hhmm(p.trade_end)
        flats = (FlatRule(hhmm(p.flat)),)
        out = []
        for i in range(len(bars)):
            t = int(bars.t[i])
            if not start <= minute_of_day(t) < end:
                continue
            a = float(atr[i])
            hl = fine.high_low(t - window, t)
            if math.isnan(a) or a <= 0 or hl is None:
                continue
            rh, rl, n = hl
            if n * fine.span < p.min_cover_pct / 100.0 * window:
                continue
            o, h, low, c = float(bars.o[i]), float(bars.h[i]), float(bars.low[i]), float(bars.c[i])
            spring = low < rl < c and is_pin(
                o, h, low, c, at_low=True, ratio=p.wick_body_ratio, zone=p.close_zone
            )
            upthrust = c < rh < h and is_pin(
                o, h, low, c, at_low=False, ratio=p.wick_body_ratio, zone=p.close_zone
            )
            if not (spring or upthrust):
                continue
            side = Side.SELL if spring else Side.BUY  # FOLLOW: with the sweep; the spring is checked first
            buffer = p.stop_buffer_ticks * tick
            level = low - buffer if side is Side.BUY else h + buffer
            out.append(
                Signal(
                    decided_at=t + bars.span,
                    side=side,
                    setup_tag=self.setup_tag,
                    stop_level=level,
                    stop_floor=p.stop_floor_atr * a,
                    take_profit_r=p.target_r,
                    max_hold=p.max_hold_min,
                    flats=flats,
                    max_entry_delay=2,
                )
            )
        return out
