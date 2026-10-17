"""IndexOpeningPlaybook v1.0.0 setup S1 with its BOS filter (workshop set "S1-BOS-Only_JPN225-GER40").

Pre-registered in ``docs/research/basket_batch2.md`` (H2), rule for rule from the workshop's
``replay_index_playbook.py`` with ``s1_bos=True`` and S2/S3 off. The cash open in exchange time is converted
to the broker's server clock (New York + 7 h) with each zone's own DST, so Xetra opens at 10:00 server most
of the year and 11:00 in the US/EU DST-mismatch weeks. A day needs an M1 bar exactly at the open and
``min_bars`` bars in [open - 60, open + 400). One of the first ``signal_bars`` closed M1 bars from the open
with body >= ``body`` of its range, range in [``min_range_pct``, ``max_range_pct``] % of its close and
>= ``min_range_spread`` x the spread, closing beyond the high (bull) / low (bear) of the ``bos_lookback`` bars
before it, places a stop order beyond it (a buy at its high + spread, stop at its low; a sell at its low,
stop at its high + spread), skipped when the spread is above ``max_spread_frac`` of the risk or the next bar
opens more than ``max_chase_r`` past the entry; it lives ``expiry_bars`` bars and never past open +
``entry_minutes``. Target ``target_r``; whatever is open at open + ``close_minutes`` is closed; one fill a
day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from aifund.domain.enums import Side, Timeframe
from aifund.research.basket.candles import DAY, Candles
from aifund.research.basket.harness import FlatRule, Signal
from aifund.research.basket.strategies.base import Params

NEW_YORK = ZoneInfo("America/New_York")
SERVER_AFTER_NY = timedelta(hours=7)  # Vantage server clock
_EPOCH = datetime(1970, 1, 1)  # noqa: DTZ001 - the server clock is naive by definition

# broker symbol -> (exchange zone, cash open hour, minute)
OPENS: dict[str, tuple[str, int, int]] = {
    "GER40.r": ("Europe/Berlin", 9, 0),
    "Nikkei225": ("Asia/Tokyo", 9, 0),
    "NAS100.r": ("America/New_York", 9, 30),
}


def server_open(symbol: str, day: date) -> int:
    """The cash open of ``day`` (exchange date) in server minutes."""
    zone, hour, minute = OPENS[symbol]
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(zone))
    server = local.astimezone(NEW_YORK).replace(tzinfo=None) + SERVER_AFTER_NY
    return int((server - _EPOCH) // timedelta(minutes=1))


@dataclass(frozen=True)
class OpeningParams(Params):
    signal_bars: int = 5
    entry_minutes: int = 8
    body: float = 0.85
    min_range_pct: float = 0.03
    max_range_pct: float = 0.50
    min_range_spread: float = 3.0
    bos_lookback: int = 15
    max_spread_frac: float = 0.25
    max_chase_r: float = 0.30
    expiry_bars: int = 5
    target_r: float = 1.5
    close_minutes: int = 330
    min_bars: int = 70


class IndexOpeningS1:
    name: ClassVar[str] = "index_opening_s1"
    version: ClassVar[str] = "1.0"
    setup_tag: ClassVar[str] = "index_opening_s1"
    symbols: ClassVar[tuple[str, ...]] = ("GER40.r", "Nikkei225")
    needs: ClassVar[tuple[Timeframe, ...]] = (Timeframe.M1,)
    max_trades_per_day: ClassVar[int | None] = 1

    def __init__(self, p: OpeningParams | None = None) -> None:
        self.p = p or OpeningParams()

    def params(self) -> dict[str, Any]:
        return self.p.asdict()

    def signals(self, data: Mapping[Timeframe, Candles], tick: float, symbol: str = "") -> list[Signal]:
        bars = data.get(Timeframe.M1)
        if bars is None or len(bars) == 0 or symbol not in OPENS:
            return []
        first, last = int(bars.t[0]) // DAY, int(bars.t[-1]) // DAY
        out: list[Signal] = []
        for d in range(first, last + 1):
            day = (_EPOCH + timedelta(days=d)).date()
            if day.weekday() < 5:
                out += self._day(bars, server_open(symbol, day))
        return out

    def _day(self, bars: Candles, t_open: int) -> list[Signal]:
        p = self.p
        i0 = bars.first_open_at_or_after(t_open)
        if i0 >= len(bars) or int(bars.t[i0]) != t_open:
            return []  # holiday / closed
        a, b = bars.first_open_at_or_after(t_open - 60), bars.first_open_at_or_after(t_open + 400)
        if b - a < p.min_bars:
            return []
        out: list[Signal] = []
        for j in range(i0, min(i0 + p.signal_bars, len(bars) - 1)):
            o, h, low, c = (float(x[j]) for x in (bars.o, bars.h, bars.low, bars.c))
            rng = h - low
            if rng <= 0 or abs(c - o) / rng < p.body:
                continue
            spread = float(bars.spread[j + 1])  # at the first tick of the next bar, as the replay
            if not (
                p.min_range_pct / 100 * c <= rng <= p.max_range_pct / 100 * c
                and rng >= p.min_range_spread * spread
            ):
                continue
            lo = max(0, j - p.bos_lookback)
            up = c > o
            if up and not c > float(bars.h[lo:j].max(initial=-float("inf"))):
                continue
            if not up and not c < float(bars.low[lo:j].min(initial=float("inf"))):
                continue
            entry, stop = (h + spread, low) if up else (low, h + spread)
            risk = abs(entry - stop)
            if risk <= 0 or spread > p.max_spread_frac * risk:
                continue
            placed = int(bars.t[j + 1])
            out.append(
                Signal(
                    decided_at=placed,
                    side=Side.BUY if up else Side.SELL,
                    setup_tag=self.setup_tag,
                    stop_price=entry,
                    max_chase=p.max_chase_r * risk,
                    stop_level=stop,
                    take_profit_r=p.target_r,
                    limit_expires_at=min(placed + p.expiry_bars, t_open + p.entry_minutes),
                    flats=(FlatRule((t_open + p.close_minutes) % DAY),),
                    max_entry_delay=1,
                )
            )
        return out
