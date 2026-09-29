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
    end: datetime | None = None,
    merge: bool = False,
) -> ExportResult:
    """Fetch [start, end or now) in 30-day chunks per symbol/timeframe and write CLOSED bars only.

    A bar is closed when ``open_time + timeframe <= now``; the forming bar is always dropped. Chunks can
    overlap at their edges; duplicates are removed by open time. With ``merge`` the bars are added to what the
    store already holds (a date-range export extends an existing export; the manifest spans both).

    The terminal returns at most "Max bars in chart" bars per symbol and timeframe
    (``terminal_info().maxbars``, whatever range is asked for): a timeframe that reaches it is CAPPED —
    recorded in the manifest and warned about, because its oldest part is missing. Set Tools > Options >
    Charts > Max bars in chart to Unlimited, restart the terminal, and export again.
    """
    until = min(end, now) if end is not None else now
    account = await gateway.account_info()
    max_bars = await gateway.max_bars()
    previous = hs.read_manifest(root) if merge and (root / "manifest.json").is_file() else None
    result = ExportResult(root=root)
    capped: set[str] = set(previous.capped) if previous is not None else set()
    specs = {}
    for symbol in symbols:
        specs[symbol] = await gateway.refresh_symbol_spec(symbol)
        result.rows[symbol] = {}
        for tf in timeframes:
            bars: list[Bar] = []
            cursor = start
            while cursor < until:
                chunk_end = min(cursor + CHUNK, until)
                bars.extend(await gateway.bars_range(symbol, tf, cursor, chunk_end))
                cursor = chunk_end
            span = timedelta(minutes=tf.minutes)
            closed = [b for b in bars if b.time + span <= now]
            fetched = len({b.time for b in closed})
            if merge and hs.bars_path(root, symbol, tf).is_file():
                closed = hs.read_bars(root, symbol, tf, specs[symbol].digits) + closed
            unique = {b.time: b for b in closed}
            if not unique:
                result.warnings.append(f"{symbol} {tf}: no bars returned")
                continue
            key = f"{symbol} {tf.value}"
            if max_bars is not None and fetched >= max_bars - 1:
                capped.add(key)
                result.warnings.append(
                    f"{key}: CAPPED at the terminal's Max bars in chart ({max_bars}): older bars are "
                    "missing. Set Tools > Options > Charts > Max bars in chart = Unlimited, restart MT5, "
                    "export again"
                )
            first = min(unique)
            if first - start > timedelta(days=7):
                result.warnings.append(
                    f"{symbol} {tf}: history starts {first:%Y-%m-%d}, later than requested "
                    f"{start:%Y-%m-%d} (raise 'Max bars in chart' in the terminal to get more)"
                )
            last_close = max(unique) + span
            if until - last_close > max(timedelta(days=4), 3 * span):
                result.warnings.append(
                    f"{symbol} {tf}: history ends {last_close:%Y-%m-%d %H:%M}Z — the terminal's local "
                    f"history looks unsynchronised; open a {symbol} {tf.value} chart in MT5 and re-export"
                )
            hs.write_bars(root, symbol, tf, list(unique.values()))
            result.rows[symbol][tf.value] = len(unique)
            progress(f"{symbol} {tf.value}: {len(unique)} bars from {first:%Y-%m-%d}")
    if previous is not None:
        if previous.server != account.server:
            raise ValueError(f"cannot merge exports from {previous.server!r} and {account.server!r}")
        specs = {**hs.read_specs(root), **specs}
        rows = {sym: {**tfs} for sym, tfs in previous.rows.items()}
        for sym, tfs in result.rows.items():
            rows.setdefault(sym, {}).update(tfs)
        result.rows = rows
    hs.write_specs(root, specs)
    hs.write_manifest(
        root,
        hs.Manifest(
            exported_at=now,
            server=account.server,
            account_trade_mode=account.trade_mode.value,
            server_offset_minutes=int(gateway.server_offset / timedelta(minutes=1)),
            start=min(start, previous.start) if previous is not None else start,
            end=max(until, previous.end) if previous is not None else until,
            rows=result.rows,
            max_bars=max_bars,
            capped=sorted(capped),
        ),
    )
    return result
