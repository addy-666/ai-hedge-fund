"""Data-quality report for the exported history (roadmap R.8). Reads the Parquet export; changes nothing.

    cd backend
    uv run python scripts/data_quality.py                  # every symbol and timeframe in data/history
    uv run python scripts/data_quality.py --history /path/to/history

Prints a markdown table (paste it into docs/PROGRESS.md) and saves it next to the research ledger. A
timeframe marked CAPPED hit the terminal's "Max bars in chart" during export: raise that setting to
Unlimited, restart MT5 and export again before researching on it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.enums import Timeframe
from aifund.market.sessions import calendars
from aifund.research.quality import HEADER, assess, likely_capped


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--history", default=str(PROJECT_ROOT / "data" / "history"))
    p.add_argument("--out", default=str(PROJECT_ROOT / "data" / "research"))
    p.add_argument(
        "--details", type=int, default=0, help="also list the N largest unexpected gaps per series"
    )
    args = p.parse_args(argv)

    settings = Settings()
    config_path = (
        settings.CONFIG_PATH
        if settings.CONFIG_PATH.is_file()
        else PROJECT_ROOT / "config" / "trading.example.yaml"
    )
    cfg = load_trading_config(config_path).config
    root = Path(args.history)
    manifest = hs.read_manifest(root)
    specs = hs.read_specs(root)
    cals = calendars(cfg.sessions)
    by_broker = {s.broker: s for s in cfg.symbols}

    lines = [
        f"History {root} — server {manifest.server}, {manifest.start:%Y-%m-%d} → {manifest.end:%Y-%m-%d}, "
        f"exported {manifest.exported_at:%Y-%m-%d %H:%M}Z, terminal max bars "
        f"{manifest.max_bars if manifest.max_bars is not None else 'unknown'}",
        "",
        HEADER,
    ]
    details: list[str] = []
    for symbol, spec in sorted(specs.items()):
        sym_cfg = by_broker.get(symbol)
        calendar = cals.get(sym_cfg.session) if sym_cfg is not None and sym_cfg.session else None
        for tf in Timeframe:
            if not hs.bars_path(root, symbol, tf).is_file():
                continue
            bars = hs.read_bars(root, symbol, tf, spec.digits)
            q = assess(
                symbol, tf, bars, calendar=calendar,
                trade_weekends=sym_cfg.trade_weekends if sym_cfg is not None else False,
                max_spread_points=sym_cfg.max_spread_points if sym_cfg is not None else None,
                capped=f"{symbol} {tf.value}" in manifest.capped
                or (manifest.max_bars is None and likely_capped(len(bars), bars[0].time if bars else None,
                                                                 manifest.start)),
            )  # fmt: skip
            lines.append(q.row())
            worst = sorted(q.unexpected_gaps, key=lambda g: g.missing, reverse=True)[: args.details]
            details += [
                f"- {symbol} {tf.value}: after {g.after:%Y-%m-%d %H:%M}Z, {g.missing} missing" for g in worst
            ]
    if details:
        lines += ["", f"Largest unexpected gaps (up to {args.details} per series):", *details]
    report = "\n".join(lines)
    print(report)
    out = Path(args.out) / f"quality_{datetime.now(UTC):%Y%m%d_%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n")
    print(f"\nsaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
