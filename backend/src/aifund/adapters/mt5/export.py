"""Export closed-bar history from the MT5 terminal to the Parquet history store (roadmap task 1.2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.adapters.mt5.gateway import MT5Gateway
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar

CHUNK = timedelta(days=30)


@dataclass
class ExportResult:
    root: Path
    rows: dict[str, dict[str, int]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


async def export_history(
    gateway: MT5Gateway,
    *,
    symbols: list[str],
    timeframes: list[Timeframe],
    start: datetime,
    now: datetime,
    root: Path,
    progress: Callable[[str], None] = lambda _msg: None,
) -> ExportResult:
    """Fetch [start, now) in 30-day chunks per symbol/timeframe and write CLOSED bars only.

    A bar is closed when ``open_time + timeframe <= now``; the forming bar is always dropped. Chunks can
    overlap at their edges; duplicates are removed by open time. If the terminal returns less history
    than requested (its "Max bars in chart" setting limits it), a warning says so.
    """
    account = await gateway.account_info()
    result = ExportResult(root=root)
    specs = {}
    for symbol in symbols:
        specs[symbol] = await gateway.refresh_symbol_spec(symbol)
        result.rows[symbol] = {}
        for tf in timeframes:
            bars: list[Bar] = []
            cursor = start
            while cursor < now:
                chunk_end = min(cursor + CHUNK, now)
                bars.extend(await gateway.bars_range(symbol, tf, cursor, chunk_end))
                cursor = chunk_end
            span = timedelta(minutes=tf.minutes)
            closed = [b for b in bars if b.time + span <= now]
            unique = {b.time: b for b in closed}
            if not unique:
                result.warnings.append(f"{symbol} {tf}: no bars returned")
                continue
            first = min(unique)
            if first - start > timedelta(days=7):
                result.warnings.append(
                    f"{symbol} {tf}: history starts {first:%Y-%m-%d}, later than requested "
                    f"{start:%Y-%m-%d} (raise 'Max bars in chart' in the terminal to get more)"
                )
            hs.write_bars(root, symbol, tf, list(unique.values()))
            result.rows[symbol][tf.value] = len(unique)
            progress(f"{symbol} {tf.value}: {len(unique)} bars from {first:%Y-%m-%d}")
    hs.write_specs(root, specs)
    hs.write_manifest(
        root,
        hs.Manifest(
            exported_at=now,
            server=account.server,
            account_trade_mode=account.trade_mode.value,
            server_offset_minutes=int(gateway.server_offset / timedelta(minutes=1)),
            start=start,
            end=now,
            rows=result.rows,
        ),
    )
    return result
