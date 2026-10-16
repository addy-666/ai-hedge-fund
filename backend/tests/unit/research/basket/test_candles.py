"""Server-time candles and MetaTrader-style indicators (roadmap 11.2), values derived by hand."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from aifund.research.basket.candles import (
    Candles,
    atr_partial,
    atr_sma,
    atr_wilder,
    day_of,
    ema,
    last_closed_index,
    load,
    minute_of_day,
    server_minute,
    true_range,
    utc,
    weekday,
)

# TR by hand: bar0 2 (its own range), bar1 4 (12-8), bar2 6 (13-7); closes stay at 10
THREE = Candles.of(Timeframe.H1, [0, 60, 120], [10, 10, 10], [11, 12, 13], [9, 8, 7], [10, 10, 10])


def test_true_range_uses_the_previous_close_across_a_gap() -> None:
    c = Candles.of(Timeframe.H1, [0, 60], [10, 11.5], [10.5, 12], [9.5, 11], [8, 11.5])
    assert list(true_range(c)) == [1.0, 4.0]  # bar0: its range; bar1: max(1, |12-8|, |11-8|)
    assert list(true_range(Candles.of(Timeframe.H1, [0], [1], [3], [1], [2]))) == [2.0]


def test_atr_variants_by_hand() -> None:
    sma = atr_sma(THREE, 2)
    assert math.isnan(sma[0])
    assert list(sma[1:]) == [3.0, 5.0]
    assert list(atr_partial(THREE, 2)) == [2.0, 3.0, 5.0]
    assert list(atr_wilder(THREE, 2)) == [2.0, 3.0, 4.5]  # alpha 1/2: 2 -> 3 -> 4.5
    assert all(math.isnan(x) for x in atr_sma(THREE, 5))  # fewer bars than the period


def test_ema_is_seeded_on_the_first_value() -> None:
    assert list(ema(np.array([1.0, 2.0, 3.0]), 3)) == [1.0, 1.5, 2.25]  # alpha 2/(3+1)


def test_candles_reject_bad_input() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        Candles.of(Timeframe.M1, [1, 1], [1, 1], [1, 1], [1, 1], [1, 1])
    with pytest.raises(ValueError, match="length"):
        Candles.of(Timeframe.M1, [1, 2], [1], [1, 1], [1, 1], [1, 1])


def test_windows_and_lookups() -> None:
    assert len(THREE) == 3
    assert THREE.span == 60
    assert THREE.closed_count(119) == 1  # bar0 closed at 60, bar1 closes at 120
    assert last_closed_index(THREE, 120) == 1
    assert THREE.first_open_at_or_after(61) == 2
    assert THREE.high_low(0, 120) == (12.0, 8.0, 2)
    assert THREE.high_low(200, 300) is None


def test_resample_anchors_at_midnight_and_aggregates() -> None:
    m15 = Candles.of(
        Timeframe.M15, [0, 15, 30, 45, 60], [1, 2, 3, 4, 5], [2, 9, 4, 5, 6], [0.5, 1, 2, 3, 4],
        [2, 3, 4, 4.5, 6],
        [0.1, 0.2, 0.3, 0.4, 0.5],
    )  # fmt: skip
    h1 = m15.resample(Timeframe.H1)
    assert list(h1.t) == [0, 60]
    assert (h1.o[0], h1.h[0], h1.low[0], h1.c[0], h1.spread[0]) == (1.0, 9.0, 0.5, 4.5, 0.1)
    assert (h1.o[1], h1.c[1]) == (5.0, 6.0)
    assert len(Candles.of(Timeframe.M15, [], [], [], [], []).resample(Timeframe.H1)) == 0
    with pytest.raises(ValueError, match="cannot build"):
        h1.resample(Timeframe.M15)


def test_server_clock_round_trip_and_calendar() -> None:
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    m = server_minute(moment, 180)
    assert m == 29_453_760 + 180
    assert utc(m, 180) == moment
    assert minute_of_day(m) == 180  # 03:00 on the server
    assert day_of(m) == 20_454
    assert weekday(0) == 3  # 1970-01-01 was a Thursday
    assert weekday(m) == 3  # 2026-01-01 too


def _bar(time: datetime, spread: int) -> Bar:
    return Bar(
        symbol="XAUUSD", timeframe=Timeframe.H1, time=time, open=D("100.00"), high=D("101.00"),
        low=D("99.00"), close=D("100.50"), tick_volume=1, spread_points=spread,
    )  # fmt: skip


def test_load_reads_an_export_on_the_server_clock(tmp_path: Path) -> None:
    t0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
    hs.write_bars(tmp_path, "XAUUSD", Timeframe.H1, [_bar(t0, 20)])
    c = load(tmp_path, "XAUUSD", Timeframe.H1, 0.01, 120)
    assert list(c.t) == [server_minute(t0, 120)]
    assert minute_of_day(int(c.t[0])) == 12 * 60  # 10:00 UTC = 12:00 on a UTC+2 server
    assert c.spread[0] == pytest.approx(0.20)
    assert (c.o[0], c.h[0], c.low[0], c.c[0]) == (100.0, 101.0, 99.0, 100.5)


def test_read_columns_refuses_missing_bad_or_unordered_files(tmp_path: Path) -> None:
    with pytest.raises(hs.HistoryError, match="no history file"):
        hs.read_columns(tmp_path, "XAUUSD", Timeframe.H1)
    path = hs.bars_path(tmp_path, "XAUUSD", Timeframe.H1)
    path.parent.mkdir(parents=True)
    pq.write_table(pa.table({"x": [1]}), path)
    with pytest.raises(hs.HistoryError, match="unexpected schema"):
        hs.read_columns(tmp_path, "XAUUSD", Timeframe.H1)
    t = datetime(2026, 3, 2, tzinfo=UTC)
    rows = {"time": [t, t], "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0], "close": [1.0, 1.0],
            "tick_volume": [1, 1], "spread": [0, 0]}  # fmt: skip
    pq.write_table(pa.Table.from_pydict(rows, schema=hs.SCHEMA), path)
    with pytest.raises(hs.HistoryError, match="not strictly increasing"):
        hs.read_columns(tmp_path, "XAUUSD", Timeframe.H1)
