from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from aifund.adapters import history_store as hs
from aifund.adapters.clock import FakeClock
from aifund.adapters.mt5 import constants as c
from aifund.adapters.mt5.export import export_history
from aifund.adapters.mt5.gateway import MT5Credentials, MT5Gateway
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from tests.fakes.fake_mt5 import FakeMT5

NOW = datetime(2026, 9, 28, 12, 7, tzinfo=UTC)
OFFSET = timedelta(hours=3)
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "export_history.py"


def bar(t: datetime, close: str = "2350.25", tf: Timeframe = Timeframe.H1) -> Bar:
    c_ = Decimal(close)
    return Bar(
        symbol="XAUUSDm",
        timeframe=tf,
        time=t,
        open=c_,
        high=c_ + 1,
        low=c_ - 1,
        close=c_,
        tick_volume=10,
        spread_points=20,
    )


def test_store_round_trip_is_exact_and_sorted(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    bars = [
        bar(t0 + timedelta(hours=1), "2351.37"),
        bar(t0, "1.07"),
        bar(t0, "2350.10"),
    ]  # dup time: last wins
    hs.write_bars(tmp_path, "XAUUSDm", Timeframe.H1, bars)
    back = hs.read_bars(tmp_path, "XAUUSDm", Timeframe.H1, digits=2)
    assert [b.time for b in back] == [t0, t0 + timedelta(hours=1)]
    assert back[0].close == Decimal("2350.10")
    assert back[1].close == Decimal("2351.37")
    assert back[0].time.tzinfo is not None


def test_store_errors(tmp_path: Path) -> None:
    with pytest.raises(hs.HistoryError, match="no history file"):
        hs.read_bars(tmp_path, "X", Timeframe.M1, 2)
    with pytest.raises(hs.HistoryError, match="no manifest"):
        hs.read_manifest(tmp_path)


def _rows(tf: Timeframe, start: datetime, n: int) -> list[dict[str, object]]:
    step = timedelta(minutes=tf.minutes)
    return [
        {
            "time": int((start + i * step + OFFSET).timestamp()),
            "open": 2350.0,
            "high": 2351.0,
            "low": 2349.0,
            "close": 2350.5,
            "tick_volume": 5,
            "spread": 20,
        }
        for i in range(n)
    ]


async def test_export_writes_closed_bars_only_and_dedups_chunk_edges(tmp_path: Path) -> None:
    fake = FakeMT5()
    start = NOW - timedelta(days=60)
    # H1 bars from `start` up to and including the forming bar (12:00, closes 13:00 > NOW)
    n = int((NOW - start) / timedelta(hours=1)) + 1
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H1])] = _rows(Timeframe.H1, start.replace(minute=0), n)
    gw = MT5Gateway(
        MT5Credentials(login=12345678, password="x", server="Broker-Demo"),
        clock=FakeClock(NOW),
        module_loader=lambda: fake,
    )
    await gw.connect()
    gw.set_server_offset(OFFSET)
    result = await export_history(
        gw, symbols=["XAUUSDm"], timeframes=[Timeframe.H1], start=start, now=NOW, root=tmp_path
    )
    await gw.close()
    bars = hs.read_bars(tmp_path, "XAUUSDm", Timeframe.H1, digits=2)
    assert bars[-1].time == datetime(2026, 9, 28, 11, 0, tzinfo=UTC)  # 12:00 bar is still forming
    assert len({b.time for b in bars}) == len(bars) == result.rows["XAUUSDm"]["H1"]
    manifest = hs.read_manifest(tmp_path)
    assert manifest.server_offset_minutes == 180
    assert manifest.account_trade_mode == "DEMO"
    assert hs.read_specs(tmp_path)["XAUUSDm"].digits == 2
    assert not result.warnings


async def test_export_warns_when_the_terminal_has_less_history(tmp_path: Path) -> None:
    fake = FakeMT5()
    start = NOW - timedelta(days=90)
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.D1])] = _rows(Timeframe.D1, NOW - timedelta(days=20), 10)
    gw = MT5Gateway(
        MT5Credentials(login=12345678, password="x", server="Broker-Demo"),
        clock=FakeClock(NOW),
        module_loader=lambda: fake,
    )
    await gw.connect()
    gw.set_server_offset(OFFSET)
    result = await export_history(
        gw, symbols=["XAUUSDm"], timeframes=[Timeframe.D1, Timeframe.M5], start=start, now=NOW, root=tmp_path
    )
    await gw.close()
    assert any("Max bars in chart" in w for w in result.warnings)
    assert any("M5: no bars returned" in w for w in result.warnings)


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_history_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_script_end_to_end_with_explicit_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeMT5()
    real_now = datetime.now(UTC)
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H4])] = _rows(
        Timeframe.H4, real_now - timedelta(days=20), 100
    )
    script = load_script()

    class _Gw(MT5Gateway):
        def __init__(self, creds: MT5Credentials, *, clock: object) -> None:
            super().__init__(creds, clock=FakeClock(real_now), module_loader=lambda: fake)

    monkeypatch.setattr(script, "MT5Gateway", _Gw)
    for k, v in {"MT5_LOGIN": "12345678", "MT5_PASSWORD": "pw", "MT5_SERVER": "Broker-Demo"}.items():
        monkeypatch.setenv(k, v)
    code = await script.main(
        [
            "--months",
            "1",
            "--timeframes",
            "H4",
            "--symbols",
            "XAUUSDm",
            "--out",
            str(tmp_path),
            "--server-offset-hours",
            "3",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "XAUUSDm H4:" in out
    assert (tmp_path / "XAUUSDm" / "H4.parquet").is_file()
    assert not {"order_send", "order_check"} & set(fake.names())


async def test_export_warns_when_history_ends_long_before_now(tmp_path: Path) -> None:
    fake = FakeMT5()
    start = NOW - timedelta(days=30)
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H1])] = _rows(
        Timeframe.H1, start, 24 * 20
    )  # ends 10 days ago
    gw = MT5Gateway(
        MT5Credentials(login=12345678, password="x", server="Broker-Demo"),
        clock=FakeClock(NOW),
        module_loader=lambda: fake,
    )
    await gw.connect()
    gw.set_server_offset(OFFSET)
    result = await export_history(
        gw, symbols=["XAUUSDm"], timeframes=[Timeframe.H1], start=start, now=NOW, root=tmp_path
    )
    await gw.close()
    assert any("looks unsynchronised" in w for w in result.warnings)


def gateway(fake: FakeMT5) -> MT5Gateway:
    return MT5Gateway(
        MT5Credentials(login=12345678, password="x", server="Broker-Demo"),
        clock=FakeClock(NOW),
        module_loader=lambda: fake,
    )


async def test_a_timeframe_at_the_terminal_bar_cap_is_flagged(tmp_path: Path) -> None:
    fake = FakeMT5()
    fake.terminal = SimpleNamespace(connected=True, trade_allowed=True, maxbars=100)
    start = NOW - timedelta(days=30)
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H1])] = _rows(Timeframe.H1, NOW - timedelta(hours=100), 100)
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H4])] = _rows(Timeframe.H4, start, 30)
    gw = gateway(fake)
    await gw.connect()
    gw.set_server_offset(OFFSET)
    result = await export_history(
        gw, symbols=["XAUUSDm"], timeframes=[Timeframe.H1, Timeframe.H4], start=start, now=NOW, root=tmp_path
    )
    await gw.close()
    assert any("XAUUSDm H1: CAPPED" in w and "Unlimited" in w for w in result.warnings)
    manifest = hs.read_manifest(tmp_path)
    assert (manifest.max_bars, manifest.capped) == (100, ["XAUUSDm H1"])


async def test_a_date_range_export_merges_into_the_existing_one(tmp_path: Path) -> None:
    fake = FakeMT5()
    old_start = NOW - timedelta(days=60)
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H4])] = _rows(Timeframe.H4, old_start, 6 * 60)
    gw = gateway(fake)
    await gw.connect()
    gw.set_server_offset(OFFSET)
    recent = await export_history(gw, symbols=["XAUUSDm"], timeframes=[Timeframe.H4],
                                  start=NOW - timedelta(days=20), now=NOW, root=tmp_path)  # fmt: skip
    older = await export_history(
        gw, symbols=["XAUUSDm"], timeframes=[Timeframe.H4], start=old_start, end=NOW - timedelta(days=20),
        now=NOW, root=tmp_path, merge=True,
    )  # fmt: skip
    await gw.close()
    bars = hs.read_bars(tmp_path, "XAUUSDm", Timeframe.H4, digits=2)
    assert bars[0].time == old_start and len(bars) == len({b.time for b in bars})  # noqa: PT018
    assert older.rows["XAUUSDm"]["H4"] == len(bars) > recent.rows["XAUUSDm"]["H4"]
    manifest = hs.read_manifest(tmp_path)
    assert (manifest.start, manifest.end, manifest.max_bars) == (old_start, NOW, None)
    assert not older.warnings  # the range ends 20 days ago by request, not because history is stale


async def test_merging_exports_from_different_servers_is_refused(tmp_path: Path) -> None:
    fake = FakeMT5()
    fake.rates[("XAUUSDm", c.TIMEFRAMES[Timeframe.H4])] = _rows(Timeframe.H4, NOW - timedelta(days=5), 30)
    gw = gateway(fake)
    await gw.connect()
    gw.set_server_offset(OFFSET)
    kw = dict(
        symbols=["XAUUSDm"], timeframes=[Timeframe.H4], start=NOW - timedelta(days=5), now=NOW, root=tmp_path
    )
    await export_history(gw, **kw)  # type: ignore[arg-type]
    manifest = hs.read_manifest(tmp_path)
    hs.write_manifest(tmp_path, hs.Manifest(**{**manifest.__dict__, "server": "Other-Live"}))
    with pytest.raises(ValueError, match="cannot merge"):
        await export_history(gw, merge=True, **kw)  # type: ignore[arg-type]
    await gw.close()
