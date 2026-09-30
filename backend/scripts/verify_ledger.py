"""Verify the trade ledger against MT5's deal history (roadmap 5.7c; the 3.6 shakedown and 9.3 nightly). READ-ONLY.

    cd backend
    uv run python scripts/verify_ledger.py                      # Windows + MT5: the last 30 days
    uv run python scripts/verify_ledger.py --days 7
    uv run python scripts/verify_ledger.py --fixture tests/fixtures/mt5_deals/<file>.json   # offline, any OS
    uv run python scripts/verify_ledger.py --days 3 --alert     # the nightly job (roadmap 9.3, task aifund-ledger)

Compares every engine-magic position in the broker's deal history with the ``trades`` table and prints each
difference: UNRECORDED (e.g. opened and closed while the engine was down), NOT_AT_BROKER, STATUS, VOLUME, NET.
Exit code 0 = no difference, 1 = differences, 2 = could not run. Never sends or checks an order.

With ``--alert`` (the nightly job) the result is also recorded as the ``job.verify_ledger`` heartbeat (the
System page and the L2 gate read it: status ``ok`` / ``diff`` / ``failed``, with the differences) and a
difference or a failure is sent as a CRITICAL alert (Telegram when ``alerts.telegram`` and the bot are set).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from sqlalchemy import select

from aifund.adapters.clock import SystemClock
from aifund.adapters.mt5.deal_history import DealFixture
from aifund.adapters.mt5.mapping import deal_from_mt5
from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.market import Deal
from aifund.engine.__main__ import notifier_for
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.repositories.system import EventRepository, HeartbeatRepository
from aifund.persistence.tables import TradeRow
from aifund.ports.system import Severity
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
    p.add_argument("--alert", action="store_true", help="record the result and alert on any difference")
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
        if args.alert:
            report(settings, cfg, "failed", [f"could not read the deal history: {exc}"])
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
    if args.alert:
        report(settings, cfg, "diff" if diffs else "ok", [d.render() for d in diffs], positions=positions)
    return 1 if diffs else 0


def report(settings: Settings, cfg: Any, status: str, lines: list[str], positions: int | None = None) -> None:
    """The nightly job's result: a heartbeat row (dashboard, L2 gate) and, unless OK, a CRITICAL alert."""
    clock = SystemClock()
    factory = make_session_factory(make_engine(settings.DATABASE_URL))
    with unit_of_work(factory) as s:
        HeartbeatRepository(s, clock).beat(
            "job.verify_ledger", status, {"positions": positions, "differences": lines[:50]}
        )
        if status != "ok":
            EventRepository(s, clock).append("ledger.mismatch", Severity.CRITICAL, {"lines": lines[:50]})
    if status == "ok":
        return

    async def send() -> None:
        async with httpx.AsyncClient() as client:
            notifier = notifier_for(settings, cfg, client, clock)
            body = "\n".join(lines[:20]) + (f"\n… {len(lines) - 20} more" if len(lines) > 20 else "")
            title = (
                "Ledger check FAILED"
                if status == "failed"
                else f"Ledger mismatch: {len(lines)} difference(s)"
            )
            await notifier.notify(Severity.CRITICAL, title, body)

    asyncio.run(send())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
