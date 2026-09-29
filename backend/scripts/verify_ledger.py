"""Verify the trade ledger against MT5's deal history (roadmap 5.7c; the 3.6 shakedown and 9.3 nightly). READ-ONLY.

    cd backend
    uv run python scripts/verify_ledger.py                      # Windows + MT5: the last 30 days
    uv run python scripts/verify_ledger.py --days 7
    uv run python scripts/verify_ledger.py --fixture tests/fixtures/mt5_deals/<file>.json   # offline, any OS

Compares every engine-magic position in the broker's deal history with the ``trades`` table and prints each
difference: UNRECORDED (e.g. opened and closed while the engine was down), NOT_AT_BROKER, STATUS, VOLUME, NET.
Exit code 0 = no difference, 1 = differences, 2 = could not run. Never sends or checks an order.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from aifund.adapters.clock import SystemClock
from aifund.adapters.mt5.deal_history import DealFixture
from aifund.adapters.mt5.mapping import deal_from_mt5
from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.market import Deal
from aifund.persistence.db import make_engine, make_session_factory
from aifund.persistence.tables import TradeRow
from aifund.reconcile.ledger_check import TradeFacts, diff_ledger


async def broker_deals(settings: Settings, since: datetime, symbols: list[str]) -> list[Deal]:
    from aifund.adapters.mt5.gateway import MT5Credentials, MT5Gateway  # Windows only
    from aifund.adapters.mt5.server_time import OffsetUnavailable

    if not (settings.MT5_LOGIN and settings.MT5_PASSWORD and settings.MT5_SERVER):
        raise RuntimeError("set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in the repo-root .env")
    gw = MT5Gateway(
        MT5Credentials(login=settings.MT5_LOGIN, password=settings.MT5_PASSWORD.get_secret_value(),
                       server=settings.MT5_SERVER, path=settings.MT5_PATH, portable=settings.MT5_PORTABLE),
        clock=SystemClock(),
    )  # fmt: skip
    try:
        await gw.connect()
        try:
            offset, _ = await gw.refresh_server_offset(symbols)
        except OffsetUnavailable:
            offset = timedelta(0)  # markets closed: deal times stay server time (a few hours at the edges)
            print("warning: server UTC offset unknown (markets closed); the window edges may be off by hours")
        start = int((since + offset - timedelta(days=1)).timestamp())
        end = int((datetime.now(UTC) + offset + timedelta(days=1)).timestamp())
        raw = await gw.raw_deals_between(start, end)
    finally:
        await gw.close()
    return [d for r in raw if (d := deal_from_mt5(SimpleNamespace(**r), offset)) is not None]


def fixture_deals(path: Path) -> list[Deal]:
    fixture = DealFixture.load(path)
    return [
        d for r in fixture.deals if (d := deal_from_mt5(SimpleNamespace(**r), fixture.offset)) is not None
    ]


def database_trades(url: str, account: str) -> list[TradeFacts]:
    factory = make_session_factory(make_engine(url))
    with factory() as s:
        rows = s.scalars(select(TradeRow).where(TradeRow.account_id == account)).all()
        return [
            TradeFacts(r.position_id, r.symbol, r.status, r.open_time, r.volume_opened, r.net_pnl)
            for r in rows
        ]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--fixture", help="a deal-history fixture instead of the live terminal")
    p.add_argument("--account", default="", help="account id in the database (default: engine.account_label)")
    args = p.parse_args(argv)

    settings = Settings()
    example = PROJECT_ROOT / "config" / "trading.example.yaml"
    try:
        cfg = load_trading_config(settings.CONFIG_PATH if settings.CONFIG_PATH.is_file() else example).config
    except ConfigError as exc:
        print(f"config problem: {exc}", file=sys.stderr)
        return 2
    since = (
        datetime.now(UTC) - timedelta(days=args.days)
        if not args.fixture
        else datetime(2000, 1, 1, tzinfo=UTC)
    )
    try:
        deals = (
            fixture_deals(Path(args.fixture))
            if args.fixture
            else asyncio.run(broker_deals(settings, since, [s.broker for s in cfg.symbols]))
        )
    except Exception as exc:
        print(f"could not read the deal history: {exc}", file=sys.stderr)
        return 2
    trades = database_trades(settings.DATABASE_URL, args.account or cfg.engine.account_label)
    diffs = diff_ledger(trades, deals, magic=cfg.engine.magic, since=since)
    positions = len({d.position_id for d in deals if d.magic == cfg.engine.magic})
    print(
        f"{positions} engine positions at the broker since {since:%Y-%m-%d}, {len(trades)} trades in the database"
    )
    for d in diffs:
        print(f"  {d.render()}")
    print("ledger OK: no difference" if not diffs else f"{len(diffs)} difference(s)")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
