"""On-disk bar history (Parquet), shared by the MT5 exporter (writes) and the replay feed (reads).

Layout::

    <root>/manifest.json              export metadata (server, account type, offset, range, row counts)
    <root>/specs.json                 SymbolSpec per broker symbol
    <root>/<SYMBOL>/<TF>.parquet      bars, oldest first

Columns: ``time`` (UTC microsecond timestamp = bar OPEN time), ``open/high/low/close`` (float64),
``tick_volume`` (int64), ``spread`` (int32, points). Prices are stored as float64 and re-quantized to the
symbol's digits on read, which restores the exact broker price (every price has at most ``digits``
decimals, well within float64 precision).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar, SymbolSpec
from aifund.domain.values import to_decimal

SCHEMA = pa.schema(
    [
        ("time", pa.timestamp("us", tz="UTC")),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("tick_volume", pa.int64()),
        ("spread", pa.int32()),
    ]
)
FORMAT_VERSION = 1


class HistoryError(Exception):
    """History files are missing, malformed or inconsistent."""


def bars_path(root: Path, symbol: str, timeframe: Timeframe) -> Path:
    return root / symbol / f"{timeframe.value}.parquet"


def write_bars(root: Path, symbol: str, timeframe: Timeframe, bars: list[Bar]) -> Path:
    """Write bars (any order, duplicates by time dropped, keeping the last) sorted by time."""
    unique = {b.time: b for b in bars}
    ordered = [unique[t] for t in sorted(unique)]
    table = pa.table(
        {
            "time": [b.time for b in ordered],
            "open": [float(b.open) for b in ordered],
            "high": [float(b.high) for b in ordered],
            "low": [float(b.low) for b in ordered],
            "close": [float(b.close) for b in ordered],
            "tick_volume": [b.tick_volume for b in ordered],
            "spread": [b.spread_points for b in ordered],
        },
        schema=SCHEMA.with_metadata(
            {"symbol": symbol, "timeframe": timeframe.value, "format_version": str(FORMAT_VERSION)}
        ),
    )
    path = bars_path(root, symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return path


def read_bars(root: Path, symbol: str, timeframe: Timeframe, digits: int) -> list[Bar]:
    path = bars_path(root, symbol, timeframe)
    if not path.is_file():
        raise HistoryError(f"no history file {path}")
    table = pq.read_table(path)
    if not table.schema.remove_metadata().equals(SCHEMA):
        raise HistoryError(f"{path}: unexpected schema {table.schema}")
    quantum = to_decimal(1).scaleb(-digits)
    cols = table.to_pydict()
    bars: list[Bar] = []
    for i, t in enumerate(cols["time"]):
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                time=t,
                open=to_decimal(cols["open"][i]).quantize(quantum),
                high=to_decimal(cols["high"][i]).quantize(quantum),
                low=to_decimal(cols["low"][i]).quantize(quantum),
                close=to_decimal(cols["close"][i]).quantize(quantum),
                tick_volume=cols["tick_volume"][i],
                spread_points=cols["spread"][i],
            )
        )
    for prev, cur in pairwise(bars):
        if cur.time <= prev.time:
            raise HistoryError(f"{path}: bars not strictly increasing at {cur.time}")
    return bars


def write_specs(root: Path, specs: dict[str, SymbolSpec]) -> Path:
    path = root / "specs.json"
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: v.model_dump(mode="json") for k, v in sorted(specs.items())}, indent=2))
    return path


def read_specs(root: Path) -> dict[str, SymbolSpec]:
    path = root / "specs.json"
    if not path.is_file():
        raise HistoryError(f"no specs file {path}")
    raw = json.loads(path.read_text())
    return {k: SymbolSpec.model_validate(v) for k, v in raw.items()}


@dataclass(frozen=True)
class Manifest:
    exported_at: datetime
    server: str
    account_trade_mode: str
    server_offset_minutes: int
    start: datetime
    end: datetime
    rows: dict[str, dict[str, int]]
    format_version: int = FORMAT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "exported_at": self.exported_at.isoformat(),
            "server": self.server,
            "account_trade_mode": self.account_trade_mode,
            "server_offset_minutes": self.server_offset_minutes,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rows": self.rows,
        }


def write_manifest(root: Path, manifest: Manifest) -> Path:
    path = root / "manifest.json"
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_json(), indent=2))
    return path


def read_manifest(root: Path) -> Manifest:
    path = root / "manifest.json"
    if not path.is_file():
        raise HistoryError(f"no manifest {path}")
    raw = json.loads(path.read_text())
    if raw.get("format_version") != FORMAT_VERSION:
        raise HistoryError(f"{path}: unsupported format_version {raw.get('format_version')}")
    return Manifest(
        exported_at=datetime.fromisoformat(raw["exported_at"]),
        server=raw["server"],
        account_trade_mode=raw["account_trade_mode"],
        server_offset_minutes=int(raw["server_offset_minutes"]),
        start=datetime.fromisoformat(raw["start"]),
        end=datetime.fromisoformat(raw["end"]),
        rows=raw["rows"],
    )
