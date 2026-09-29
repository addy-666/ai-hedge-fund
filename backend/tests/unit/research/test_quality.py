"""Data-quality report (roadmap R.8): caps, gaps the calendar cannot explain, spreads, OHLC sanity."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aifund.adapters import history_store as hs
from aifund.config.trading_config import SessionConfig
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from aifund.market.sessions import SessionCalendar
from aifund.research.quality import HEADER, assess, likely_capped
from tests.unit.risk.test_stops import XAU

MON = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)  # Monday; New York is UTC-5 until 8 March
CAL = SessionCalendar.from_config(SessionConfig(closed_days=[date(2026, 3, 4)]))  # Wednesday closed


def bars(times: list[datetime], tf: Timeframe = Timeframe.H1, spread: int = 20) -> list[Bar]:
    return [
        Bar(
            symbol="XAUUSD",
            timeframe=tf,
            time=t,
            open=D("100"),
            high=D("101"),
            low=D("99"),
            close=D("100.5"),
            tick_volume=5,
            spread_points=spread,
        )
        for t in times
    ]


def hourly(start: datetime, hours: int) -> list[datetime]:
    return [start + timedelta(hours=h) for h in range(hours)]


def test_gaps_the_calendar_explains_are_not_reported() -> None:
    # Monday 00:00 -> Tuesday 21:00 UTC (16:00 NY), then nothing until Thursday 00:00 UTC: Tuesday's
    # 17:00-18:00 NY break and the closed Wednesday explain it all
    times = hourly(MON, 46) + hourly(MON + timedelta(days=3), 5)
    q = assess(
        "XAUUSD",
        Timeframe.H1,
        bars(times),
        calendar=CAL,
        trade_weekends=False,
        max_spread_points=50,
        capped=False,
    )
    assert q.unexpected_gaps == []
    assert q.verdict == "ok"


def test_a_gap_while_open_is_reported_and_weekend_traders_have_no_excuse() -> None:
    times = hourly(MON, 10) + hourly(MON + timedelta(hours=14), 4)  # 10:00-14:00 UTC missing on a Monday
    q = assess(
        "XAUUSD",
        Timeframe.H1,
        bars(times),
        calendar=CAL,
        trade_weekends=False,
        max_spread_points=None,
        capped=False,
    )
    ((gap),) = q.unexpected_gaps
    assert (gap.after, gap.missing) == (MON + timedelta(hours=9), timedelta(hours=4))
    assert q.missing_hours == 4.0
    closed = hourly(MON + timedelta(days=1), 20) + hourly(MON + timedelta(days=3), 2)
    q_btc = assess(
        "BTCUSD",
        Timeframe.H1,
        bars(closed),
        calendar=CAL,
        trade_weekends=True,
        max_spread_points=None,
        capped=False,
    )
    assert len(q_btc.unexpected_gaps) == 1  # a 24/7 market has no closed day to excuse it


def test_quiet_minutes_are_not_gaps() -> None:
    minutes = [MON + timedelta(minutes=m) for m in (0, 1, 2, 12, 13, 30)]  # 9 and 16 minutes missing
    q = assess(
        "XAUUSD",
        Timeframe.M1,
        bars(minutes, Timeframe.M1),
        calendar=None,
        trade_weekends=False,
        max_spread_points=None,
        capped=False,
    )
    assert [g.missing for g in q.unexpected_gaps] == [timedelta(minutes=16)]


def test_spreads_and_ohlc_sanity() -> None:
    series = bars(hourly(MON, 100))
    series[5] = series[5].model_copy(update={"spread_points": 400})
    series[6] = series[6].model_copy(update={"high": D("100.2")})  # below the close 100.5
    series[7] = series[7].model_copy(
        update={"open": D("100"), "high": D("100"), "low": D("100"), "close": D("100")}
    )
    q = assess(
        "XAUUSD", Timeframe.H1, series, calendar=None, trade_weekends=True, max_spread_points=50, capped=True
    )
    assert (q.spread_p50, q.spread_max, q.over_spread_gate) == (20.0, 400, 1)
    assert (q.bad_ohlc, q.zero_range) == (1, 1)
    assert q.verdict == "CAPPED, BAD OHLC"
    row = q.row()
    assert row.startswith("| XAUUSD | H1 | 100 | 2026-03-02 -> 2026-03-06 | yes | 0 (0.0 h) | 20 / ")
    assert row.count("|") == HEADER.splitlines()[0].count("|")


def test_an_empty_series_and_the_cap_heuristic() -> None:
    q = assess(
        "X", Timeframe.M1, [], calendar=None, trade_weekends=False, max_spread_points=None, capped=False
    )
    assert (q.bars, q.first, q.spread_p50) == (0, None, None)
    assert "| - | no | 0 (0.0 h) | - |" in q.row()
    start = datetime(2025, 7, 5, tzinfo=UTC)
    assert likely_capped(99_999, datetime(2026, 6, 17, tzinfo=UTC), start)
    assert not likely_capped(99_999, start + timedelta(days=2), start)  # the whole range fits
    assert not likely_capped(87_322, datetime(2026, 6, 17, tzinfo=UTC), start)
    assert not likely_capped(99_999, None, start)


def test_the_script_prints_and_saves_the_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "history"
    hs.write_specs(root, {"XAUUSD": XAU})
    hs.write_bars(root, "XAUUSD", Timeframe.H1, bars(hourly(MON, 10) + hourly(MON + timedelta(hours=14), 4)))
    hs.write_manifest(
        root,
        hs.Manifest(
            exported_at=MON + timedelta(days=1),
            server="Test",
            account_trade_mode="DEMO",
            server_offset_minutes=0,
            start=MON,
            end=MON + timedelta(days=1),
            rows={"XAUUSD": {"H1": 14}},
        ),
    )
    path = Path(__file__).resolve().parents[3] / "scripts" / "data_quality.py"
    spec = importlib.util.spec_from_file_location("data_quality_script", path)
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.main(["--history", str(root), "--out", str(tmp_path / "out"), "--details", "3"]) == 0
    printed = capsys.readouterr().out
    assert "terminal max bars unknown" in printed
    assert "| XAUUSD | H1 | 14 |" in printed and "1 gaps" in printed  # noqa: PT018
    assert "- XAUUSD H1: after 2026-03-02 09:00Z, 4:00:00 missing" in printed
    (saved,) = (tmp_path / "out").glob("quality_*.md")
    assert "| XAUUSD | H1 | 14 |" in saved.read_text()
