"""Export bar history from MT5 to Parquet for replay on any machine (roadmap task 1.2). READ-ONLY.

    cd backend
    uv run python scripts/export_history.py                       # 24 months, M1..D1, all configured symbols
                                                                  #   and cross_asset.references
    uv run python scripts/export_history.py --months 12 --timeframes M15,H1,H4,D1
    uv run python scripts/export_history.py --from 2024-01-01 --to 2024-07-01 --timeframes M1,M5 --merge
                                                                  # a date range added to the existing export
    uv run python scripts/export_history.py --update              # the bars since the last export, merged in
                                                                  #   (the weekly research task, roadmap 8.7)
    uv run python scripts/export_history.py --server-offset-hours 3   # weekends: no live tick to detect it

BEFORE a long export set MT5 Tools > Options > Charts > "Max bars in chart" to Unlimited and restart the
terminal: every copy_rates_* call is capped at that number per symbol and timeframe (the default 100,000 is
~70 days of M1). A capped timeframe is warned about and listed under "capped" in manifest.json.
Then check the export: uv run python scripts/data_quality.py

Writes <repo>/data/history/ (git-ignored): <SYMBOL>/<TF>.parquet, specs.json, manifest.json.
Copy that folder to the Mac (same path under the repo) to replay real data there.
Only closed bars are exported; no order is ever checked or sent.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aifund.adapters import history_store as hs
from aifund.adapters.clock import SystemClock
from aifund.adapters.mt5.export import export_history
from aifund.adapters.mt5.gateway import GatewayError, MT5Credentials, MT5Gateway
from aifund.adapters.mt5.server_time import OffsetUnavailable
from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.enums import Timeframe

DEFAULT_TFS = "M1,M5,M15,H1,H4,D1"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--months", type=int, default=24, help="how far back to export (default 24)")
    p.add_argument(
        "--from", dest="date_from", default=None, help="start date YYYY-MM-DD (UTC), instead of --months"
    )
    p.add_argument(
        "--to", dest="date_to", default=None, help="end date YYYY-MM-DD (UTC, exclusive; default now)"
    )
    p.add_argument("--merge", action="store_true", help="add to the existing export instead of replacing it")
    p.add_argument(
        "--update",
        action="store_true",
        help="merge the bars since the existing export's end (2 days overlap)",
    )
    p.add_argument("--timeframes", default=DEFAULT_TFS, help=f"comma-separated (default {DEFAULT_TFS})")
    p.add_argument(
        "--symbols", default="", help="comma-separated broker symbols (default: from trading.yaml)"
    )
    p.add_argument("--out", default=str(PROJECT_ROOT / "data" / "history"), help="output folder")
    p.add_argument("--server-offset-hours", type=float, default=None, help="use when markets are closed")
    return p.parse_args(argv)


def _date(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)


def update_start(root: Path) -> datetime:
    """Where an ``--update`` starts: two days before the existing export ends (duplicates are dropped)."""
    if not (root / "manifest.json").is_file():
        raise GatewayError(f"--update needs an existing export in {root}: run a full export first")
    return hs.read_manifest(root).end - timedelta(days=2)


async def main(argv: list[str]) -> int:
    args = parse_args(argv)
    settings = Settings()
    if not (settings.MT5_LOGIN and settings.MT5_PASSWORD and settings.MT5_SERVER):
        print("Set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in the repo-root .env first.")
        return 2
    try:
        timeframes = [Timeframe(tf.strip().upper()) for tf in args.timeframes.split(",") if tf.strip()]
    except ValueError as exc:
        print(f"bad --timeframes: {exc}")
        return 2
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        try:
            cfg = load_trading_config(settings.CONFIG_PATH).config
        except ConfigError as exc:
            print(f"Trading config problem (or pass --symbols):\n{exc}")
            return 2
        # traded symbols, then the cross-asset references (data only, roadmap 10.1)
        symbols = [s.broker for s in cfg.symbols] + [r.broker for r in cfg.cross_asset.references]

    gw = MT5Gateway(
        MT5Credentials(
            login=settings.MT5_LOGIN,
            password=settings.MT5_PASSWORD.get_secret_value(),
            server=settings.MT5_SERVER,
            path=settings.MT5_PATH,
            portable=settings.MT5_PORTABLE,
        ),
        clock=SystemClock(),
    )
    try:
        account = await gw.connect()
        print(f"connected: {account.login} @ {account.server} ({account.trade_mode})")
        if args.server_offset_hours is not None:
            gw.set_server_offset(timedelta(hours=args.server_offset_hours))
        else:
            try:
                offset, _ = await gw.refresh_server_offset(symbols)
            except OffsetUnavailable as exc:
                print(f"Cannot detect the server UTC offset ({exc}). Re-run with --server-offset-hours N.")
                return 2
            print(f"server time = UTC{offset / timedelta(hours=1):+.2f}h")
        now = datetime.now(UTC)
        start = _date(args.date_from) if args.date_from else now - timedelta(days=30 * args.months)
        if args.update:
            start, args.merge = update_start(Path(args.out)), True
        end = _date(args.date_to) if args.date_to else None
        result = await export_history(
            gw,
            symbols=symbols,
            timeframes=timeframes,
            start=start,
            now=now,
            end=end,
            merge=args.merge,
            root=Path(args.out),
            progress=print,
        )
        for w in result.warnings:
            print(f"warning: {w}")
        print(f"done -> {result.root}")
        return 0
    except GatewayError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        await gw.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
