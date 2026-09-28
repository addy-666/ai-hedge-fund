"""Technical indicators on numpy float64 arrays (oldest first). Pure functions, no I/O.

Conventions:
- Output has the input's length; values that are not yet defined (warm-up) are NaN.
- Every value at index i depends only on inputs at indices <= i (no lookahead) — enforced by a property
  test. Fractal swings are only reported once *confirmed* (two bars after the pivot).
- EMA is seeded with the SMA of its first ``n`` values. Wilder smoothing (RSI, ATR, ADX) uses
  ``alpha = 1/n``, seeded with a simple average. Note: MT5's built-in iATR is a *simple* average of true
  range; this ATR is Wilder's, so values differ slightly from the terminal's indicator by design.
- Bollinger width uses the population standard deviation (ddof=0), as Bollinger defined it.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]


def _as_float(x: Arr | list[float]) -> Arr:
    return np.asarray(x, dtype=np.float64)


def _nan(n: int) -> Arr:
    return np.full(n, np.nan, dtype=np.float64)


def sma(x: Arr, n: int) -> Arr:
    x = _as_float(x)
    out = _nan(len(x))
    if n <= 0 or len(x) < n:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1 :] = (csum[n:] - csum[:-n]) / n
    return out


def _recursive(x: Arr, n: int, alpha: float) -> Arr:
    """Seed with the SMA of the first n valid values, then y = y_prev + alpha * (x - y_prev).

    Leading NaNs in ``x`` are skipped (so indicators can be chained, e.g. an EMA of MACD)."""
    x = _as_float(x)
    out = _nan(len(x))
    valid = np.flatnonzero(~np.isnan(x))
    if n <= 0 or len(valid) < n:
        return out
    start = int(valid[0])
    seed_end = start + n - 1
    if np.isnan(x[start : seed_end + 1]).any():
        return out
    prev = float(np.mean(x[start : seed_end + 1]))
    out[seed_end] = prev
    for i in range(seed_end + 1, len(x)):
        prev = prev + alpha * (x[i] - prev)
        out[i] = prev
    return out


def ema(x: Arr, n: int) -> Arr:
    return _recursive(x, n, 2.0 / (n + 1))


def rma(x: Arr, n: int) -> Arr:
    """Wilder's moving average (alpha = 1/n)."""
    return _recursive(x, n, 1.0 / n)


def true_range(high: Arr, low: Arr, close: Arr) -> Arr:
    high, low, close = _as_float(high), _as_float(low), _as_float(close)
    tr = high - low
    if len(close) > 1:
        prev = close[:-1]
        tr[1:] = np.maximum.reduce([high[1:] - low[1:], np.abs(high[1:] - prev), np.abs(low[1:] - prev)])
    return tr


def atr(high: Arr, low: Arr, close: Arr, n: int = 14) -> Arr:
    return rma(true_range(high, low, close), n)


def rsi(close: Arr, n: int = 14) -> Arr:
    close = _as_float(close)
    out = _nan(len(close))
    if len(close) <= n:
        return out
    change = np.diff(close)
    gain = np.where(change > 0, change, 0.0)
    loss = np.where(change < 0, -change, 0.0)
    avg_gain = rma(gain, n)
    avg_loss = rma(loss, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        values = 100.0 - 100.0 / (1.0 + rs)
    values = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, values)
    values = np.where((avg_loss == 0) & (avg_gain == 0), 50.0, values)
    out[1:] = values
    return out


def adx(high: Arr, low: Arr, close: Arr, n: int = 14) -> Arr:
    high, low, close = _as_float(high), _as_float(low), _as_float(close)
    size = len(close)
    if size < 2 * n + 1:
        return _nan(size)
    up = np.diff(high)
    down = -np.diff(low)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(high, low, close)[1:]
    atr_ = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * rma(plus_dm, n) / atr_
        minus_di = 100.0 * rma(minus_dm, n) / atr_
        denom = plus_di + minus_di
        dx = np.where(denom > 0, 100.0 * np.abs(plus_di - minus_di) / denom, 0.0)
    dx = np.where(np.isnan(plus_di), np.nan, dx)
    out = _nan(size)
    out[1:] = rma(dx, n)
    return out


def bollinger_width(close: Arr, n: int = 20, k: float = 2.0) -> Arr:
    """(upper - lower) / middle = 2k·σ / SMA."""
    close = _as_float(close)
    out = _nan(len(close))
    if len(close) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(close, n)
    mean = windows.mean(axis=1)
    std = windows.std(axis=1, ddof=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[n - 1 :] = 2.0 * k * std / mean
    return out


def macd_histogram(close: Arr, fast: int = 12, slow: int = 26, signal: int = 9) -> Arr:
    close = _as_float(close)
    line = ema(close, fast) - ema(close, slow)
    return line - ema(line, signal)


def zscore(x: Arr, window: int) -> Arr:
    x = _as_float(x)
    out = _nan(len(x))
    if len(x) < window:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(x, window)
    mean = windows.mean(axis=1)
    std = windows.std(axis=1, ddof=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[window - 1 :] = np.where(std > 0, (x[window - 1 :] - mean) / std, 0.0)
    out[window - 1 :] = np.where(np.isnan(windows).any(axis=1), np.nan, out[window - 1 :])
    return out


def percentile_rank(x: Arr, window: int = 100) -> Arr:
    """Fraction of the last ``window`` values (including the current one) that are <= the current value."""
    x = _as_float(x)
    out = _nan(len(x))
    if len(x) < window:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(x, window)
    current = x[window - 1 :]
    ranks = (windows <= current[:, None]).sum(axis=1) / window
    out[window - 1 :] = np.where(np.isnan(windows).any(axis=1), np.nan, ranks)
    return out


def confirmed_swings(high: Arr, low: Arr, side: int = 2) -> tuple[Arr, Arr]:
    """Most recent CONFIRMED fractal swing high / low as of each bar.

    A swing high at pivot p requires high[p] > the ``side`` highs before it and >= the ``side`` highs
    after it; it becomes known at bar p + side. Returns (last_swing_high, last_swing_low) arrays whose
    value at index i uses only bars <= i.
    """
    high, low = _as_float(high), _as_float(low)
    size = len(high)
    last_high, last_low = _nan(size), _nan(size)
    current_high = current_low = np.nan
    for i in range(size):
        p = i - side
        if p - side >= 0:
            if high[p] > high[p - side : p].max() and high[p] >= high[p + 1 : i + 1].max():
                current_high = high[p]
            if low[p] < low[p - side : p].min() and low[p] <= low[p + 1 : i + 1].min():
                current_low = low[p]
        last_high[i], last_low[i] = current_high, current_low
    return last_high, last_low


def nr7(high: Arr, low: Arr) -> NDArray[np.bool_]:
    """True where the bar's range is strictly narrower than each of the previous six bars' ranges."""
    rng = _as_float(high) - _as_float(low)
    out = np.zeros(len(rng), dtype=bool)
    if len(rng) >= 7:
        prev_min = np.lib.stride_tricks.sliding_window_view(rng[:-1], 6).min(axis=1)
        out[6:] = rng[6:] < prev_min
    return out
