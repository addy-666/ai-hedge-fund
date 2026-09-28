"""Record the demo account's MT5 deal history as a test fixture — READ-ONLY (roadmap task 3.2).

    cd backend
    uv run python scripts/capture_deals.py                       # -> tests/fixtures/mt5_deals/<server>_<date>.json
    uv run python scripts/capture_deals.py --out some/file.json

Run it on Windows with the terminal logged in to the DEMO account (repo-root .env as for mt5_smoke.py). It
reads the account's WHOLE deal history plus its balance, rebuilds the balance from the deals exactly as the
reconciler would (production mapping + P&L aggregation), prints the per-position P&L and writes the fixture.
Exit code 0 = balance matches to the cent, 1 = mismatch (the fixture is still written, for diagnosis),
2 = could not connect / not a demo account.

For a useful fixture, first place a few small trades by hand in the terminal (see docs/PROGRESS.md, task
3.2): a quick manual close, a partial close, one stopped out, one at its take-profit, and one held past the
daily rollover so it pays swap.

It never sends or checks an order, and the fixture contains no account login or owner name (the repo is
public); deal comments are kept as the broker wrote them.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aifund.adapters.clock import SystemClock
from aifund.adapters.mt5.deal_history import DealFixture, check_ledger, keep_fields
from aifund.adapters.mt5.gateway import GatewayError, MT5Credentials, MT5Gateway
from aifund.adapters.mt5.server_time import OffsetUnavailable
from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.settings import Settings
from aifund.domain.enums import AccountTradeMode
from aifund.reconcile import pnl

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "mt5_deals"
HISTORY_START = 946_684_800  # 2000-01-01: before any account on this server existed


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", help="fixture path (default: tests/fixtures/mt5_deals/<server>_<date>.json)")
    p.add_argument("--note", default="", help="free text stored in the fixture (what the trades exercise)")
    args = p.parse_args(argv or [])

    settings = Settings()
    if not (settings.MT5_LOGIN and settings.MT5_PASSWORD and settings.MT5_SERVER):
        print("Set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in the repo-root .env first.")
        return 2
    try:
        symbols = [s.broker for s in load_trading_config(settings.CONFIG_PATH).config.symbols]
    except ConfigError as exc:
        print(f"Trading config problem:\n{exc}")
        return 2
    creds = MT5Credentials(
        login=settings.MT5_LOGIN,
        password=settings.MT5_PASSWORD.get_secret_value(),
        server=settings.MT5_SERVER,
        path=settings.MT5_PATH,
        portable=settings.MT5_PORTABLE,
    )
    gw = MT5Gateway(creds, clock=SystemClock())
    try:
        try:
            account = await gw.connect()
        except GatewayError as exc:
            print(f"FAILED to connect: {exc}")
            return 2
        if account.trade_mode is not AccountTradeMode.DEMO:
            print(f"Refusing: this is a {account.trade_mode} account. Fixtures are recorded from DEMO only.")
            return 2
        try:
            offset, _ = await gw.refresh_server_offset(symbols)
            offset_minutes: int | None = int(offset / timedelta(minutes=1))
        except OffsetUnavailable:
            offset_minutes = None  # market closed: deal times stay in server time (P&L does not depend on it)
        # server time runs ahead of UTC by at most 14 h: a day of margin reaches every deal booked so far
        end = int((datetime.now(UTC) + timedelta(days=1)).timestamp())
        raw = await gw.raw_deals_between(HISTORY_START, end)
    finally:
        await gw.close()

    fixture = DealFixture(
        server=account.server,
        currency=account.currency,
        trade_mode=account.trade_mode.value,
        server_offset_minutes=offset_minutes,
        balance=account.balance,
        deals=[keep_fields(d) for d in raw],
        note=args.note,
    )
    check = check_ledger(fixture)
    print(f"{len(raw)} deals: {sum(len(d) for d in check.trade_deals.values())} trade deals in "
          f"{len(check.positions)} positions, {check.non_trade_count} balance/credit/fee deals")  # fmt: skip
    print(f"{'position':>12} {'symbol':<10} {'in':>6} {'out':>6} {'gross':>10} {'comm':>8} {'swap':>8} "
          f"{'fee':>7} {'net':>10}  close")  # fmt: skip
    for pid, s in sorted(check.positions.items()):
        symbol = check.trade_deals[pid][0].symbol
        reason = (
            pnl.close_reason(s.last_exit, None).value
            if s.fully_closed(s.volume_in) and s.last_exit
            else "open"
        )
        print(f"{pid:>12} {symbol:<10} {s.volume_in:>6} {s.volume_out:>6} {s.gross:>10} {s.commission:>8} "
              f"{s.swap:>8} {s.fee:>7} {s.net:>10}  {reason}")  # fmt: skip
    print(f"trades net {check.trades_net} + balance/credit/fee deals {check.non_trade_total} = {check.total}")
    print(f"MT5 balance {check.balance}  ->  mismatch {check.mismatch}")

    out = (
        Path(args.out)
        if args.out
        else FIXTURES / (f"{account.server.lower().replace(' ', '_')}_{datetime.now(UTC):%Y%m%d_%H%M}.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(fixture.to_json() + "\n")
    print(f"fixture written: {out}")
    if check.mismatch != 0:
        print("MISMATCH: the deals do not rebuild the balance. Keep the fixture and report it.")
        return 1
    print("OK: the deals rebuild the MT5 balance exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
