"""SpikeFade v1.0 (workshop EA, LIVE settings): fade a 10-minute shock on XAUUSD M1 and exit on time.

When a new M1 bar opens, ``move`` = the last closed close minus the close 10 bars earlier; ``sigma`` = the
sample standard deviation of the same 10-bar move over the 1,440 bars before (at least 300). A move of at
least 5 sigma is faded at that bar's open: buy after a drop, sell after a rise. Stop 1 × |move| from entry (at
least $0.50); no target; closed after 30 minutes or at the Friday 23:30 flat. No entries in the rollover
(23:00–01:30), in the US-data blackouts (15:25–16:00, 16:55–17:15, 20:55–21:30), on Friday from 21:00, or with
a spread above $1.00 — all on the server clock, judged at the new bar's open as the EA does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import Candles, minute_of_day, weekday
from aifund.research.basket.harness import FlatRule, Signal
from aifund.research.basket.strategies.base import Params, hhmm


@dataclass(frozen=True)
class SpikeParams(Params):
    shock_bars: int = 10
    shock_sigma: float = 5.0
    vol_bars: int = 1440
    min_vol_bars: int = 300
    stop_mult: float = 1.0
    min_stop: float = 0.50
    hold_min: int = 30
    max_spread: float = 1.00
    blackouts: tuple[str, ...] = ("15:25-16:00", "16:55-17:15", "20:55-21:30")
    rollover: str = "23:00-01:30"
    friday_last_entry_hour: int = 21
    friday_flat: str = "23:30"


def _window(text: str) -> tuple[int, int]:
    a, b = text.split("-")
    return hhmm(a), hhmm(b)


def _inside(mod: int, window: tuple[int, int]) -> bool:
    a, b = window
    return a <= mod < b if a < b else (mod >= a or mod < b)


class SpikeFade:
    name: ClassVar[str] = "spike_fade"
    version: ClassVar[str] = "1.0"
    setup_tag: ClassVar[str] = "spike_fade"
    symbols: ClassVar[tuple[str, ...]] = ("XAUUSD",)
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.M1,)
    max_trades_per_day: ClassVar[int | None] = None

    def __init__(self, p: SpikeParams | None = None) -> None:
        self.p = p or SpikeParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def signals(self, data: Mapping[Timeframe, Candles], tick: float) -> list[Signal]:
        bars = data.get(Timeframe.M1)
        p = self.p
        if bars is None or len(bars) < p.min_vol_bars + p.shock_bars + 2:
            return []
        k = p.shock_bars
        c = bars.c
        r = np.full(len(c), np.nan)
        r[k:] = c[k:] - c[:-k]
        valid = np.nan_to_num(r)
        s1 = np.cumsum(np.r_[0.0, valid])
        s2 = np.cumsum(np.r_[0.0, valid * valid])
        blackouts = [_window(w) for w in p.blackouts]
        rollover = _window(p.rollover)
        flats = (FlatRule(hhmm(p.friday_flat), weekday=4),)
        out = []
        first = p.min_vol_bars + k  # i + 2 bars copied >= min_vol + shock + 2
        for i in range(first, len(c) - 1):
            n = min(p.vol_bars, i - k)
            a, b = i - n, i  # the moves r[i-n .. i-1]
            total, squares = s1[b] - s1[a], s2[b] - s2[a]
            var = (squares - total * total / n) / (n - 1)
            if var <= 0:
                continue
            move = float(r[i])
            z = move / float(np.sqrt(var))
            if abs(z) < p.shock_sigma:
                continue
            at = int(bars.t[i + 1])  # the new bar's open: the EA's decision moment
            mod = minute_of_day(at)
            friday = weekday(at) == 4
            if (
                _inside(mod, rollover)
                or (friday and mod >= p.friday_last_entry_hour * 60)
                or any(_inside(mod, w) for w in blackouts)
                or float(bars.spread[i]) > p.max_spread
            ):
                continue
            out.append(
                Signal(
                    decided_at=at,
                    side=Side.BUY if move < 0 else Side.SELL,
                    setup_tag=self.setup_tag,
                    stop_distance=max(p.stop_mult * abs(move), p.min_stop),
                    max_hold=p.hold_min,
                    flats=flats,
                    max_entry_delay=0,
                )
            )
        return out
