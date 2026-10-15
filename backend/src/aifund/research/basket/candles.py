"""Bars for the basket research (roadmap 11.2), in broker SERVER time, as plain float arrays.

The workshop EAs define their sessions on the broker's server clock (Vantage: New York + 7 h, so UTC+2 in
winter and UTC+3 in US summer). The export stores UTC computed with ONE offset for every bar (the manifest's
``server_offset_minutes``), so stored time + that offset is the server wall clock exactly, in every season.
Times here are integer minutes since 1970-01-01 on that clock; ``utc`` converts back for the ledger.

Indicators reproduce MetaTrader's: ``atr_sma`` is ``iATR`` (the simple mean of the true range), ``atr_wilder``
the Wilder smoothing some research engines used, ``ema`` MetaTrader's/pandas' recursive EMA seeded on the
first value. All are causal: element i uses bars 0..i only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import numpy.typing as npt

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]
DAY = 1440
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Candles:
    """One symbol and timeframe: open times (server minutes), bid OHLC and the spread in price units."""

    tf: Timeframe
    t: Ints
    o: Floats
    h: Floats
    low: Floats
    c: Floats
    spread: Floats

    def __len__(self) -> int:
        return len(self.t)

    @property
    def span(self) -> int:
        return self.tf.minutes

    def closed_count(self, minute: int) -> int:
        """How many bars had closed at ``minute`` (a bar closes at its open + span)."""
        return int(np.searchsorted(self.t, minute - self.span, side="right"))

    def first_open_at_or_after(self, minute: int) -> int:
        """Index of the first bar opening at or after ``minute`` (``len`` when none)."""
        return int(np.searchsorted(self.t, minute, side="left"))

    def high_low(self, start: int, end: int) -> tuple[float, float, int] | None:
        """(high, low, bars) of the bars opening in [start, end); None when there are none."""
        a, b = self.first_open_at_or_after(start), self.first_open_at_or_after(end)
        if b <= a:
            return None
        return float(self.h[a:b].max()), float(self.low[a:b].min()), b - a

    @classmethod
    def of(
        cls,
        tf: Timeframe,
        t: list[int] | Ints,
        o: list[float] | Floats,
        h: list[float] | Floats,
        low: list[float] | Floats,
        c: list[float] | Floats,
        spread: list[float] | Floats | None = None,
    ) -> Candles:
        times = np.asarray(t, dtype=np.int64)
        if len(times) > 1 and bool(np.any(np.diff(times) <= 0)):
            raise ValueError("candle times must be strictly increasing")
        arr = [np.asarray(x, dtype=np.float64) for x in (o, h, low, c)]
        sp = np.zeros(len(times)) if spread is None else np.asarray(spread, dtype=np.float64)
        if any(len(x) != len(times) for x in (*arr, sp)):
            raise ValueError("candle columns differ in length")
        return cls(tf, times, arr[0], arr[1], arr[2], arr[3], sp)

    def resample(self, tf: Timeframe) -> Candles:
        """Coarser bars anchored at server midnight (how MT5 builds them on the server clock)."""
        if tf.minutes % self.span or tf.minutes < self.span:
            raise ValueError(f"cannot build {tf} from {self.tf}")
        if len(self) == 0:
            return Candles.of(tf, [], [], [], [], [])
        keys = self.t - self.t % tf.minutes
        starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
        ends = np.r_[starts[1:], len(self.t)]
        return Candles.of(
            tf,
            keys[starts],
            self.o[starts],
            np.maximum.reduceat(self.h, starts),
            np.minimum.reduceat(self.low, starts),
            self.c[ends - 1],
            self.spread[starts],
        )


def server_minute(moment: datetime, offset_minutes: int) -> int:
    """UTC ``moment`` -> server minutes since the epoch."""
    return int((moment.astimezone(UTC) - _EPOCH) // timedelta(minutes=1)) + offset_minutes


def utc(minute: int, offset_minutes: int) -> datetime:
    """Server minutes -> the UTC instant the export stored."""
    return _EPOCH + timedelta(minutes=minute - offset_minutes)


def minute_of_day(minute: int) -> int:
    return minute % DAY


def day_of(minute: int) -> int:
    return minute // DAY


def weekday(minute: int) -> int:
    """Monday 0 … Sunday 6 (1970-01-01 was a Thursday)."""
    return (minute // DAY + 3) % 7


def load(root: Path, symbol: str, tf: Timeframe, point: float, offset_minutes: int) -> Candles:
    """One exported bar file as server-time candles."""
    cols = hs.read_columns(root, symbol, tf)
    t = [server_minute(x, offset_minutes) for x in cols["time"]]
    spread = np.asarray(cols["spread"], dtype=np.float64) * point
    return Candles.of(tf, t, cols["open"], cols["high"], cols["low"], cols["close"], spread)


# ---------------------------------------------------------------- indicators (causal)


def true_range(c: Candles) -> Floats:
    """High - low, widened to the previous close; the first bar's is its own high - low."""
    rng = c.h - c.low
    if len(c) < 2:
        return rng.copy()
    prev = c.c[:-1]
    gap = np.maximum(np.abs(c.h[1:] - prev), np.abs(c.low[1:] - prev))
    out: Floats = np.r_[rng[0], np.maximum(rng[1:], gap)]
    return out


def atr_sma(c: Candles, period: int = 14) -> Floats:
    """MetaTrader ``iATR``: the simple mean of the last ``period`` true ranges (NaN before ``period``
    bars)."""
    tr = true_range(c)
    out = np.full(len(tr), np.nan)
    if len(tr) >= period:
        csum = np.cumsum(np.r_[0.0, tr])
        out[period - 1 :] = (csum[period:] - csum[:-period]) / period
    return out


def atr_partial(c: Candles, period: int = 14) -> Floats:
    """Mean of up to ``period`` true ranges, partial at the start (the ICT EA's ComputeATR)."""
    tr = true_range(c)
    csum = np.cumsum(np.r_[0.0, tr])
    idx = np.arange(len(tr))
    lo = np.maximum(0, idx - period + 1)
    out: Floats = (csum[idx + 1] - csum[lo]) / (idx - lo + 1)
    return out


def atr_wilder(c: Candles, period: int = 14) -> Floats:
    """Wilder smoothing seeded on the first true range (alpha 1/period)."""
    tr = true_range(c)
    return _recursive(tr, 1.0 / period)


def ema(values: Floats, period: int) -> Floats:
    """Recursive EMA, alpha 2/(period+1), seeded on the first value."""
    return _recursive(values, 2.0 / (period + 1))


def _recursive(x: Floats, alpha: float) -> Floats:
    out = np.empty(len(x))
    acc = 0.0
    for i, v in enumerate(x):
        acc = float(v) if i == 0 else acc + alpha * (float(v) - acc)
        out[i] = acc
    return out


def last_closed_index(c: Candles, minute: int) -> int:
    """Index of the last bar closed at ``minute`` (-1 when none)."""
    return c.closed_count(minute) - 1
