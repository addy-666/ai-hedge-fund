"""Indicators: hand-computed reference values (to 1e-8) and a no-lookahead property."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aifund.market import indicators as ind

NAN = math.nan


def close_to(actual: np.ndarray, expected: list[float]) -> None:
    np.testing.assert_allclose(actual, np.array(expected, dtype=float), rtol=0, atol=1e-8, equal_nan=True)


# ---------------------------------------------------------------- hand-computed references


def test_sma_and_ema() -> None:
    close_to(ind.sma(np.array([1.0, 2, 3, 4, 5]), 3), [NAN, NAN, 2, 3, 4])
    # EMA(3): alpha 0.5, seed = SMA(1,2,3) = 2 ; 0.5*4 + 0.5*2 = 3 ; 0.5*5 + 0.5*3 = 4
    close_to(ind.ema(np.array([1.0, 2, 3, 4, 5]), 3), [NAN, NAN, 2, 3, 4])
    # EMA(2): alpha 2/3, seed = 1.5 ; 1.5 + 2/3*(6-1.5) = 4.5
    close_to(ind.ema(np.array([1.0, 2, 6]), 2), [NAN, 1.5, 4.5])


def test_wilder_rma() -> None:
    # seed = (2+4)/2 = 3 ; 3 + (6-3)/2 = 4.5 ; 4.5 + (0-4.5)/2 = 2.25
    close_to(ind.rma(np.array([2.0, 4, 6, 0]), 2), [NAN, 3, 4.5, 2.25])


def test_rsi_by_hand() -> None:
    # changes +1 +1 -1 +1 ; n=2 seed: gain 1, loss 0 -> 100
    # then gain .5 / loss .5 -> 50 ; gain .75 / loss .25 -> 75
    close_to(ind.rsi(np.array([1.0, 2, 3, 2, 3]), 2), [NAN, NAN, 100, 50, 75])


def test_rsi_flat_series_is_50_and_rising_series_is_100() -> None:
    assert ind.rsi(np.full(20, 5.0), 14)[-1] == 50.0
    assert ind.rsi(np.arange(1.0, 21.0), 14)[-1] == 100.0


def test_rsi_14_on_wilders_classic_series_by_hand() -> None:
    # 14 changes: gains .06 .72 .50 .27 .32 .42 .24 .14 .67 (sum 3.34); losses .25 .54 .19 .42 (sum 1.40)
    # first RSI = 100 - 100 / (1 + 3.34/1.40). (Published tables show ~70.5 because they round the averages.)
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
              46.28, 46.28]  # fmt: skip
    values = ind.rsi(np.array(closes), 14)
    assert np.isnan(values[13])
    assert values[14] == pytest.approx(100 - 100 / (1 + 3.34 / 1.40), abs=1e-8)


def test_true_range_and_atr_by_hand() -> None:
    high, low, close = np.array([10.0, 11, 12]), np.array([9.0, 10, 10]), np.array([9.5, 10.5, 11])
    # TR: 1 ; max(1, |11-9.5|, |10-9.5|) = 1.5 ; max(2, |12-10.5|, |10-10.5|) = 2
    close_to(ind.true_range(high, low, close), [1, 1.5, 2])
    # ATR(2): seed (1+1.5)/2 = 1.25 ; 1.25 + (2-1.25)/2 = 1.625
    close_to(ind.atr(high, low, close, 2), [NAN, 1.25, 1.625])


def test_adx_by_hand() -> None:
    high = np.array([10.0, 12, 13, 12, 14])
    low = np.array([9.0, 10, 11, 11, 12])
    close = np.array([9.5, 11.5, 12.5, 11.5, 13.5])
    # diffs: up [2,1,-1,2], down [-1,-1,0,-1] -> +DM [2,1,0,2], -DM [0,0,0,0]; TR [2.5,2,1.5,2.5]
    # n=2: ATR [nan,2.25,1.875,2.1875]; +DM rma [nan,1.5,0.75,1.375]; -DI 0 -> DX 100 where defined
    # ADX = rma(DX, 2): seed at diff-index 2 = (100+100)/2 = 100 ; next 100
    close_to(ind.adx(high, low, close, 2), [NAN, NAN, NAN, 100, 100])


def test_adx_is_defined_from_bar_2n_minus_1_regardless_of_series_length() -> None:
    # Regression (found by the no-lookahead property): a 2n-bar series used to be all-NaN.
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(size=60))
    high, low = close + 0.5, close - 0.5
    full = ind.adx(high, low, close, 14)
    exact = ind.adx(high[:28], low[:28], close[:28], 14)
    assert np.isnan(full[26])
    assert not np.isnan(full[27])
    assert exact[27] == full[27]


def test_bollinger_width_by_hand() -> None:
    # SMA(1,2,3)=2, population sigma = sqrt(2/3) ; width = 2*2*sigma/2
    close_to(ind.bollinger_width(np.array([1.0, 2, 3]), 3, 2.0), [NAN, NAN, 2 * math.sqrt(2 / 3)])


def test_percentile_rank_and_zscore_by_hand() -> None:
    # window [3,1,2], current 2: one below, one equal -> (1 + .5)/3 ; window [1,2,2]: (1 + 1)/3
    close_to(ind.percentile_rank(np.array([3.0, 1, 2, 2]), 3), [NAN, NAN, 0.5, 2 / 3])
    close_to(
        ind.percentile_rank(np.full(5, 7.0), 3), [NAN, NAN, 0.5, 0.5, 0.5]
    )  # flat history is not extreme
    # window [1,2,3]: mean 2, sigma sqrt(2/3) -> z(3) = 1/sqrt(2/3)
    close_to(ind.zscore(np.array([1.0, 2, 3]), 3), [NAN, NAN, 1 / math.sqrt(2 / 3)])
    close_to(ind.zscore(np.array([4.0, 4, 4]), 3), [NAN, NAN, 0])


def test_zscore_and_rank_ignore_float_noise_on_flat_series() -> None:
    noise = 100 + np.array(
        [0, 1e-12, -1e-12, 2e-12, -2e-12] * 20
    )  # representable, but far below 1e-9 x price
    assert ind.zscore(noise, 50, min_std=1e-9 * 100)[-1] == 0.0
    assert abs(ind.zscore(noise, 50)[-1]) > 0.5  # without the floor, noise becomes a "signal"
    assert ind.percentile_rank(noise, 50)[-1] == 0.5


def test_macd_histogram_of_a_constant_is_zero_once_defined() -> None:
    hist = ind.macd_histogram(np.full(60, 100.0))
    assert np.isnan(hist[:33]).all()  # slow EMA from 25, signal EMA(9) of the line from 33
    close_to(hist[33:], [0.0] * 27)


def test_confirmed_swings_only_appear_after_confirmation() -> None:
    high = np.array([1.0, 2, 5, 3, 2, 2, 6, 4, 3])
    low = np.array([0.5, 1, 3, 1, 0.2, 1, 4, 2, 1])
    sh, sl = ind.confirmed_swings(high, low)
    # swing high 5 at bar 2, confirmed at bar 4 ; swing high 6 at bar 6 confirmed at bar 8
    close_to(sh, [NAN, NAN, NAN, NAN, 5, 5, 5, 5, 6])
    # swing low 0.2 at bar 4 (< 3,1 before; <= 1,4 after) confirmed at bar 6
    close_to(sl, [NAN, NAN, NAN, NAN, NAN, NAN, 0.2, 0.2, 0.2])


def test_nr7() -> None:
    high = np.array([10.0, 10, 10, 10, 10, 10, 10, 10])
    low = np.array([8.0, 7, 8.5, 8, 7.5, 8.2, 9.5, 9.5])
    # ranges 2,3,1.5,2,2.5,1.8,0.5,0.5 -> bar 6 narrower than all previous six ; bar 7 ties bar 6 -> False
    assert ind.nr7(high, low).tolist() == [False] * 6 + [True, False]


def test_short_inputs_are_all_nan() -> None:
    short = np.array([1.0, 2.0])
    for values in (ind.ema(short, 3), ind.rsi(short, 14), ind.atr(short, short, short, 14),
                   ind.adx(short, short, short, 14), ind.percentile_rank(short, 100)):  # fmt: skip
        assert np.isnan(values).all()


# ---------------------------------------------------------------- no lookahead

Series = tuple[np.ndarray, np.ndarray, np.ndarray]


@st.composite
def ohlc(draw: st.DrawFn) -> Series:
    n = draw(st.integers(min_value=40, max_value=120))
    steps = draw(st.lists(st.floats(-2, 2, allow_nan=False), min_size=n, max_size=n))
    spans = draw(st.lists(st.floats(0.01, 3, allow_nan=False), min_size=n, max_size=n))
    close = 100 + np.cumsum(steps)
    high = close + np.array(spans) / 2
    low = close - np.array(spans) / 2
    return high, low, close


INDICATORS: dict[str, Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]] = {
    "ema": lambda h, low, c: ind.ema(c, 20),
    "rsi": lambda h, low, c: ind.rsi(c, 14),
    "atr": lambda h, low, c: ind.atr(h, low, c, 14),
    "adx": lambda h, low, c: ind.adx(h, low, c, 14),
    "bbw": lambda h, low, c: ind.bollinger_width(c, 20),
    "macd": lambda h, low, c: ind.macd_histogram(c),
    "pct": lambda h, low, c: ind.percentile_rank(ind.atr(h, low, c, 14), 20),
    "zscore": lambda h, low, c: ind.zscore(c, 20),
    "swing_high": lambda h, low, c: ind.confirmed_swings(h, low)[0],
    "swing_low": lambda h, low, c: ind.confirmed_swings(h, low)[1],
    "nr7": lambda h, low, c: ind.nr7(h, low).astype(float),
}


@pytest.mark.parametrize("name", sorted(INDICATORS))
@settings(max_examples=200, deadline=None)
@given(data=ohlc(), cut=st.floats(0.3, 0.95))
def test_appending_future_bars_never_changes_past_values(name: str, data: Series, cut: float) -> None:
    high, low, close = data
    k = int(len(close) * cut)
    fn = INDICATORS[name]
    full = fn(high, low, close)
    prefix = fn(high[:k], low[:k], close[:k])
    np.testing.assert_allclose(prefix, full[:k], rtol=0, atol=1e-9, equal_nan=True)
