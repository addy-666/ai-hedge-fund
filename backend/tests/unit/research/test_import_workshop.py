"""scripts/import_workshop_m1.py: workshop tick-built M1 joined onto an export, on the export's clock."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aifund.adapters import history_store as hs
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from tests.unit.risk.test_stops import XAU

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "import_workshop_m1.py"
OFFSET = 180


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_workshop_m1", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def workshop(root: Path, folder: str, stem: str, start: datetime, minutes: int) -> None:
    """M1 bars on the naive SERVER clock, as the workshop's converter writes them."""
    times = [start + timedelta(minutes=k) for k in range(minutes)]
    px = [100.0 + k for k in range(minutes)]
    n = len(times)
    table = pa.table(
        {
            "time": pa.array(times, pa.timestamp("ns")),
            "open": px,
            "high": [p + 0.5 for p in px],
            "low": [p - 0.5 for p in px],
            "close": [p + 0.25 for p in px],
            "ask_high": px,
            "ask_low": px,
            "spread_max": [0.3] * n,
            "ticks": [10] * n,
            "spread_avg": [0.2] * n,
        }
    )
    (root / folder).mkdir(parents=True, exist_ok=True)
    pq.write_table(table, root / folder / f"{stem}_M1.parquet")


def base_export(root: Path) -> None:
    spec = XAU.model_copy(update={"symbol": "NAS100.r"})
    hs.write_specs(root, {"NAS100.r": spec})
    start = datetime(2025, 9, 30, 21, tzinfo=UTC)  # 2025-10-01 00:00 server
    h1 = [
        Bar(
            symbol="NAS100.r",
            timeframe=Timeframe.H1,
            time=start + timedelta(hours=k),
            open=D(90),
            high=D(91),
            low=D(89),
            close=D(90),
            tick_volume=5,
            spread_points=18,
        )
        for k in range(30)  # into 2025-10-02 server: those are replaced by the workshop's bars
    ]
    hs.write_bars(root, "NAS100.r", Timeframe.H1, h1)
    hs.write_manifest(
        root,
        hs.Manifest(
            exported_at=start,
            server="S",
            account_trade_mode="DEMO",
            server_offset_minutes=OFFSET,
            start=start,
            end=start + timedelta(days=2),
            rows={},
            max_bars=1000,
        ),
    )


def test_joins_the_workshop_onto_the_export(tmp_path: Path) -> None:
    ws, base, out = tmp_path / "ws", tmp_path / "base", tmp_path / "out"
    workshop(ws, "nas100_data", "NAS100", datetime(2025, 10, 1, 22, 0), 180)  # 22:00 -> 01:00 next day
    workshop(ws, "ger40_data", "GER40", datetime(2025, 10, 2, 0, 0), 120)  # starts at midnight: a full day
    base_export(base)
    script = load()
    args = ["--workshop", str(ws), "--base", str(base), "--out", str(out), "--symbols", "NAS100.r,GER40.r"]
    assert script.main(args) == 0
    m1 = hs.read_columns(out, "NAS100.r", Timeframe.M1)
    assert m1["time"][0] == datetime(2025, 10, 1, 21, tzinfo=UTC)  # 2025-10-02 00:00 server - 3 h
    assert len(m1["time"]) == 60  # the partial first day is dropped
    assert m1["spread"][0] == 20  # 0.2 / point 0.01
    h1 = hs.read_columns(out, "NAS100.r", Timeframe.H1)
    assert len(h1["time"]) == 24 + 1  # the export's 2025-10-01 server day, then the workshop's 00:00 hour
    assert h1["open"][-1] == 220.0  # first workshop minute of the day (offset 120 minutes in)
    assert h1["high"][-1] == 279.5
    assert h1["tick_volume"][-1] == 600
    ger = hs.read_columns(out, "GER40.r", Timeframe.M1)
    assert ger["time"][0] == datetime(2025, 10, 1, 21, tzinfo=UTC)  # kept from its first minute
    specs = hs.read_specs(out)
    assert specs["NAS100.r"].symbol == "NAS100.r"
    assert specs["GER40.r"].point == D("0.01")
    manifest = hs.read_manifest(out)
    assert manifest.server_offset_minutes == OFFSET
    assert manifest.max_bars == 1000
    assert manifest.rows["NAS100.r"]["M1"] == 60
    assert script.main(args) == 0  # rebuilds the output from scratch


def test_offset_must_match_the_base(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tmp_path / "base"
    base_export(base)
    args = ["--workshop", str(tmp_path), "--base", str(base), "--out", str(tmp_path / "o"), "--offset", "120"]
    assert load().main(args) == 2
    assert "--offset" in capsys.readouterr().err


def test_without_a_base(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws, out = tmp_path / "ws", tmp_path / "out"
    workshop(ws, "eurusd_data", "EURUSD", datetime(2025, 10, 1, 23, 0), 120)
    workshop(ws, "nikkei225_data", "NIKKEI225", datetime(2025, 10, 1, 23, 0), 30)  # no full day
    script = load()
    assert script.main(["--workshop", str(ws), "--out", str(out), "--symbols", "EURUSD,Nikkei225"]) == 0
    assert "Nikkei225: no full server day" in capsys.readouterr().err
    assert set(hs.read_specs(out)) == {"EURUSD"}
    assert hs.read_specs(out)["EURUSD"].digits == 5
    assert hs.read_manifest(out).max_bars is None
    assert len(hs.read_columns(out, "EURUSD", Timeframe.D1)["time"]) == 1


def test_nothing_to_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workshop(tmp_path, "nikkei225_data", "NIKKEI225", datetime(2025, 10, 1, 23, 0), 30)
    args = ["--workshop", str(tmp_path), "--out", str(tmp_path / "o"), "--symbols", "Nikkei225"]
    assert load().main(args) == 2
    assert "nothing imported" in capsys.readouterr().err
