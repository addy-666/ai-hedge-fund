"""CandleStreak v1.1 (workshop EA, set "XAUUSD-H4-StreakStop-TP2R-24h"): follow three same-colour H4
candles.

On the close of an H4 bar, if it and the two before it all closed up (down), buy (sell) at the next bar's
open.
Stop beyond the streak's lowest low (highest high) by 0.5 × ATR(14, H4) — a SELL's stop also carries the
spread, as the EA's — kept within [0.3, 3.0] × ATR; target 2R; close after 24 h or at the Friday 23:50 flat.
The entry is the first tick of the next bar however late it comes (the daily break, a weekend): the EA's
15-minute lateness check only skips a bar whose earlier minutes it missed, which a replay never does. ATR is
MetaTrader's iATR on the last closed bar. The H4 bars are the broker's own (anchored at server midnight):
the workshop found the edge only on that anchor.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import Candles, atr_sma, minute_of_day, weekday
from aifund.research.basket.harness import FlatRule, Signal
from aifund.research.basket.strategies.base import Params, hhmm


@dataclass(frozen=True)
class StreakParams(Params):
    streak: int = 3
    atr_period: int = 14
    stop_buffer_atr: float = 0.5
    stop_floor_atr: float = 0.3
    stop_cap_atr: float = 3.0
    target_r: float = 2.0
    max_hold_hours: int = 24
    friday_flat: str = "23:50"
    max_entry_delay_min: int = 7 * 24 * 60  # the first tick of the next bar, however late


class CandleStreak:
    name: ClassVar[str] = "candle_streak"
    version: ClassVar[str] = "1.1"
    setup_tag: ClassVar[str] = "candle_streak"
    symbols: ClassVar[tuple[str, ...]] = ("XAUUSD",)
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.H4,)
    max_trades_per_day: ClassVar[int | None] = None

    def __init__(self, p: StreakParams | None = None) -> None:
        self.p = p or StreakParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        bars = data.get(Timeframe.H4)
        if bars is None:
            return []
        p = self.p
        atr = atr_sma(bars, p.atr_period)
        up = bars.c > bars.o
        down = bars.c < bars.o
        flats = (FlatRule(hhmm(p.friday_flat), weekday=4),)
        out = []
        for i in range(p.streak - 1, len(bars)):
            a = float(atr[i])
            if math.isnan(a) or a <= 0:
                continue
            window = slice(i - p.streak + 1, i + 1)
            decided = int(bars.t[i]) + bars.span
            if weekday(decided) == 4 and minute_of_day(decided) >= hhmm(p.friday_flat):
                continue  # the EA's Friday-flat entry block (a later first tick is judged in the harness)
            if bool(up[window].all()):
                side, level = Side.BUY, float(bars.low[window].min()) - p.stop_buffer_atr * a
            elif bool(down[window].all()):
                side = Side.SELL
                level = float(bars.h[window].max()) + p.stop_buffer_atr * a + float(bars.spread[i])
            else:
                continue
            out.append(
                Signal(
                    decided_at=decided,
                    side=side,
                    setup_tag=self.setup_tag,
                    stop_level=level,
                    stop_floor=p.stop_floor_atr * a,
                    stop_cap=p.stop_cap_atr * a,
                    take_profit_r=p.target_r,
                    max_hold=p.max_hold_hours * 60,
                    flats=flats,
                    max_entry_delay=p.max_entry_delay_min,
                )
            )
        return out
