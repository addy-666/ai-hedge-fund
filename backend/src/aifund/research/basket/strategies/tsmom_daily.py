"""Daily time-series momentum (roadmap 11.13, docs/09 §9.1). Pre-registered 2026-10-16, before any run.

At every D1 close: the sign of ln(close / close L calendar days earlier) — the close of the last D1 bar that
had closed at least L days before — is the side to hold until the next D1 close. The stop is 1.5 x ATR20(D1)
(the MetaTrader ``iATR`` mean of the last 20 true ranges, D1 bars closed at the decision) from the segment's
entry, and is also the R unit: every symbol risks the same per period, the inverse-volatility weighting the
literature uses. A zero return, a missing ATR or too little history = no position. Calendar-day lookbacks make
BTCUSD's 7-day weeks comparable with the 5-day markets. Decisions read only closed D1 bars.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import DAY, Candles, atr_sma
from aifund.research.basket.segments import Hold
from aifund.research.basket.strategies.base import Params

ENTRY_DELAY = 3 * DAY  # Friday's decision enters at Monday's first bar


@dataclass(frozen=True)
class TsmomParams(Params):
    lookback_days: int = 28
    atr_period: int = 20
    stop_atr: float = 1.5


class TsmomDaily:
    name: ClassVar[str] = "tsmom_daily"
    version: ClassVar[str] = "1"
    setup_tag: ClassVar[str] = "tsmom_daily"
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.D1,)

    def __init__(self, params: TsmomParams | None = None) -> None:
        self.p = params or TsmomParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def holds(self, data: Mapping[Timeframe, Candles], *, force: Side | None = None) -> list[Hold]:
        """One hold per D1 close (``force``: that side on every decision with enough history — a control)."""
        d1 = data.get(Timeframe.D1)
        if d1 is None or len(d1) < 2:
            return []
        p = self.p
        atr = atr_sma(d1, p.atr_period)
        closed_at = d1.t + d1.span
        out = []
        for i in range(len(d1) - 1):  # the last bar has no next decision to hold until
            j = int(np.searchsorted(closed_at, closed_at[i] - p.lookback_days * DAY, side="right")) - 1
            if j < 0 or math.isnan(atr[i]) or atr[i] <= 0:
                continue
            ret = math.log(float(d1.c[i]) / float(d1.c[j]))
            side = force or (Side.BUY if ret > 0 else Side.SELL if ret < 0 else None)
            if side is None:
                continue
            out.append(
                Hold(
                    decided_at=int(closed_at[i]),
                    side=side,
                    stop_distance=p.stop_atr * float(atr[i]),
                    end_at=int(closed_at[i + 1]),
                    setup_tag=self.setup_tag,
                    max_entry_delay=ENTRY_DELAY,
                )
            )
        return out
