"""Join the workshop's tick-built M1 bars with an MT5 export into one research history root (roadmap 11.5).

    cd backend
    uv run python scripts/import_workshop_m1.py --workshop "<path>/claude dev workshop" \
        --base "<path>/max history export/history" --out ../data/research/history

The workshop keeps a year of Vantage ticks per symbol as M1 bars (``<name>_data/<NAME>_M1.parquet``: SERVER time,
bid OHLC, ``spread_avg`` in price). The MT5 export (``export_history.py``) has older intraday bars but its M1 is
capped. Each output timeframe = the export's bars before the first full server day of the workshop data, then the
workshop M1 from that day on (resampled at server midnight, as MT5 builds bars). Symbols without an export (GER40,
Nikkei 225, EURUSD here) come from the workshop alone.

Times are written as the export writes them: server time - ``--offset`` minutes, so the basket loader's
"stored + offset" is the server clock exactly, in every season (``research/basket/candles.py``).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from aifund.adapters import history_store as hs
from aifund.config.settings import PROJECT_ROOT
from aifund.domain.enums import Timeframe
from aifund.domain.market import SymbolSpec

# broker symbol -> (workshop folder, file stem)
WORKSHOP = {
    "XAUUSD": ("gold_data", "XAUUSD"),
    "NAS100.r": ("nas100_data", "NAS100"),
    "BTCUSD": ("btcusd_data", "BTCUSD"),
    "EURUSD": ("eurusd_data", "EURUSD"),
    "GER40.r": ("ger40_data", "GER40"),
    "Nikkei225": ("nikkei225_data", "NIKKEI225"),
}
TFS = (
    Timeframe.M1,
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
)  # what export_history writes (M30 is resampled on load)
DAY = 1440


def _spec(symbol: str, digits: int, tick_value: str, contract: str, profit: str) -> SymbolSpec:
    point = Decimal(1).scaleb(-digits)
    return SymbolSpec(
        symbol=symbol, digits=digits, point=point, tick_size=point, tick_value=Decimal(tick_value),
        contract_size=Decimal(contract), volume_min=Decimal("0.01"), volume_max=Decimal("100"),
        volume_step=Decimal("0.01"), stops_level_points=0, freeze_level_points=0, filling_mode_flags=2,
        currency_profit=profit, currency_margin=profit, trade_allowed=True,
    )  # fmt: skip


# specs for symbols the export does not have (only point/tick size matter to the research: no commission)
EXTRA_SPECS = {
    "EURUSD": _spec("EURUSD", 5, "1.0", "100000", "USD"),
    "GER40.r": _spec("GER40.r", 2, "0.01", "1", "EUR"),
    "Nikkei225": _spec("Nikkei225", 2, "0.01", "1", "JPY"),
}


def server_minutes(times: np.ndarray) -> np.ndarray:
    """Naive server datetimes (ns) -> minutes since the epoch on the server clock."""
    return times.astype("datetime64[m]").astype(np.int64)


def load_workshop(path: Path, point: float) -> dict[str, np.ndarray]:
    t = pq.read_table(path, columns=["time", "open", "high", "low", "close", "ticks", "spread_avg"])
    cols = {k: t.column(k).to_numpy() for k in t.column_names}
    return {
        "t": server_minutes(cols["time"]),
        "o": cols["open"], "h": cols["high"], "l": cols["low"], "c": cols["close"],
        "v": cols["ticks"].astype(np.int64),
        "s": np.rint(cols["spread_avg"] / point).astype(np.int32),
    }  # fmt: skip


def resample(m1: dict[str, np.ndarray], minutes: int) -> dict[str, np.ndarray]:
    if minutes == 1:
        return m1
    keys = m1["t"] - m1["t"] % minutes
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    ends = np.r_[starts[1:], len(keys)]
    return {
        "t": keys[starts], "o": m1["o"][starts],
        "h": np.maximum.reduceat(m1["h"], starts), "l": np.minimum.reduceat(m1["l"], starts),
        "c": m1["c"][ends - 1], "v": np.add.reduceat(m1["v"], starts), "s": m1["s"][starts],
    }  # fmt: skip


def cut(
    bars: dict[str, np.ndarray], start: int | None = None, end: int | None = None
) -> dict[str, np.ndarray]:
    keep = np.ones(len(bars["t"]), dtype=bool)
    if start is not None:
        keep &= bars["t"] >= start
    if end is not None:
        keep &= bars["t"] < end
    return {k: v[keep] for k, v in bars.items()}


def read_base(root: Path, symbol: str, tf: Timeframe, offset: int) -> dict[str, np.ndarray] | None:
    path = hs.bars_path(root, symbol, tf)
    if not path.is_file():
        return None
    cols = hs.read_columns(root, symbol, tf)
    t = np.array([int(x.timestamp()) // 60 + offset for x in cols["time"]], dtype=np.int64)
    return {
        "t": t, "o": np.asarray(cols["open"]), "h": np.asarray(cols["high"]), "l": np.asarray(cols["low"]),
        "c": np.asarray(cols["close"]), "v": np.asarray(cols["tick_volume"], dtype=np.int64),
        "s": np.asarray(cols["spread"], dtype=np.int32),
    }  # fmt: skip


def write(root: Path, symbol: str, tf: Timeframe, bars: dict[str, np.ndarray], offset: int) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    times = [epoch + timedelta(minutes=int(m) - offset) for m in bars["t"]]
    table = pa.table(
        {
            "time": times,
            "open": bars["o"],
            "high": bars["h"],
            "low": bars["l"],
            "close": bars["c"],
            "tick_volume": bars["v"],
            "spread": bars["s"],
        },
        schema=hs.SCHEMA.with_metadata(
            {"symbol": symbol, "timeframe": tf.value, "format_version": str(hs.FORMAT_VERSION)}
        ),
    )
    path = hs.bars_path(root, symbol, tf)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return len(times)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workshop", required=True, help="the workshop folder (holds <name>_data/)")
    p.add_argument("--base", help="an export_history.py root to extend (optional)")
    p.add_argument("--out", default=str(PROJECT_ROOT / "data" / "research" / "history"))
    p.add_argument(
        "--offset", type=int, default=180, help="server offset (minutes) the times are stored with"
    )
    p.add_argument("--symbols", default=",".join(WORKSHOP))
    args = p.parse_args(argv)
    workshop, out = Path(args.workshop), Path(args.out)
    base = Path(args.base) if args.base else None
    base_specs = hs.read_specs(base) if base else {}
    if base and hs.read_manifest(base).server_offset_minutes != args.offset:
        print("--offset must equal the base export's server offset", file=sys.stderr)
        return 2
    if out.exists():
        shutil.rmtree(out)
    specs: dict[str, SymbolSpec] = {}
    rows: dict[str, dict[str, int]] = {}
    first, last = None, None
    for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        folder, stem = WORKSHOP[symbol]
        spec = base_specs.get(symbol) or EXTRA_SPECS[symbol]
        m1 = load_workshop(workshop / folder / f"{stem}_M1.parquet", float(spec.point))
        day = -(-int(m1["t"][0]) // DAY) * DAY  # first FULL server day of the workshop data
        m1 = cut(m1, start=day)
        if len(m1["t"]) == 0:
            print(f"{symbol}: no full server day in the workshop data, skipped", file=sys.stderr)
            continue
        specs[symbol] = spec
        rows[symbol] = {}
        for tf in TFS:
            fresh = resample(m1, tf.minutes)
            older = read_base(base, symbol, tf, args.offset) if base and symbol in base_specs else None
            bars = fresh
            if older is not None:
                older = cut(older, end=day)
                bars = {k: np.concatenate([older[k], fresh[k]]) for k in fresh}
            rows[symbol][tf.value] = write(out, symbol, tf, bars, args.offset)
            start = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(minutes=int(bars["t"][0]) - args.offset)
            end = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(minutes=int(bars["t"][-1]) - args.offset + 1)
            first = start if first is None else min(first, start)
            last = end if last is None else max(last, end)
            print(f"{symbol} {tf.value}: {rows[symbol][tf.value]} bars from {start:%Y-%m-%d %H:%M} UTC")
    if first is None or last is None:
        print("nothing imported", file=sys.stderr)
        return 2
    hs.write_specs(out, specs)
    manifest = hs.Manifest(
        exported_at=datetime.now(UTC),
        server="VantageMarkets-Demo (workshop ticks + export)",
        account_trade_mode="DEMO",
        server_offset_minutes=args.offset,
        start=first,
        end=last,
        rows=rows,
    )
    if base:
        manifest = replace(manifest, max_bars=hs.read_manifest(base).max_bars)
    hs.write_manifest(out, manifest)
    print(f"history root: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
