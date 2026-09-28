"""MT5 smoke test — READ-ONLY. Run on Windows against a DEMO account (roadmap task 1.1).

    cd backend
    uv run python scripts/mt5_smoke.py

Needs a repo-root .env with MT5_LOGIN / MT5_PASSWORD / MT5_SERVER (and MT5_PATH if the terminal is not the
default install) and config/trading.yaml (copy config/trading.example.yaml). It connects, verifies the
account identity, runs the engine's startup checks and prints account, symbols, server-time offset, the
last closed bars and the broker's own profit/margin calculators for a hypothetical 0.01 lot.

It never sends or checks an order: order_check / order_send are not called anywhere in this script.
Exit code 0 = all startup checks passed, 1 = problems found, 2 = could not connect.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from decimal import Decimal

from aifund.adapters.clock import SystemClock
from aifund.adapters.mt5.gateway import GatewayError, MT5Credentials, MT5Gateway
from aifund.adapters.mt5.server_time import OffsetUnavailable
from aifund.config.loader import ConfigError, load_trading_config
from aifund.config.settings import Settings
from aifund.domain.enums import AccountTradeMode, Mode, ReversalMode, Side, Timeframe


def line(title: str = "") -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


async def main() -> int:
    settings = Settings()
    if not (settings.MT5_LOGIN and settings.MT5_PASSWORD and settings.MT5_SERVER):
        print("Set MT5_LOGIN, MT5_PASSWORD and MT5_SERVER in the repo-root .env first.")
        return 2
    try:
        cfg = load_trading_config(settings.CONFIG_PATH).config
    except ConfigError as exc:
        print(f"Trading config problem:\n{exc}")
        return 2
    symbols = [s.broker for s in cfg.symbols]
    creds = MT5Credentials(
        login=settings.MT5_LOGIN,
        password=settings.MT5_PASSWORD.get_secret_value(),
        server=settings.MT5_SERVER,
        path=settings.MT5_PATH,
        portable=settings.MT5_PORTABLE,
    )
    gw = MT5Gateway(creds, clock=SystemClock())
    try:
        line("connect")
        try:
            account = await gw.connect()
        except GatewayError as exc:
            print(f"FAILED: {exc}")
            return 2
        print(f"account   {account.login} @ {account.server}  ({account.trade_mode}, {account.margin_mode})")
        print(f"currency  {account.currency}   leverage 1:{account.leverage}")
        print(f"balance   {account.balance}   equity {account.equity}   free margin {account.free_margin}")
        if account.trade_mode is AccountTradeMode.REAL:
            print(
                "WARNING: this is a REAL account. This script is read-only, but use a DEMO account for testing."
            )

        line("startup checks (as the engine will run them in DEMO mode)")
        report = await gw.startup_checks(
            symbols,
            mode=Mode.DEMO,
            require_hedging=cfg.risk.guards.reversal_mode is ReversalMode.CLOSE_AND_REVERSE,
        )
        for p in report.problems:
            print(f"PROBLEM  {p}")
        for w in report.warnings:
            print(f"warning  {w}")
        print("RESULT   " + ("all checks passed" if report.ok else f"{len(report.problems)} problem(s)"))

        line("server time")
        if report.server_offset is None:
            print(
                "offset unknown (no symbol ticked during sampling — market closed?). Re-run while a market is open."
            )
        else:
            hours = report.server_offset / timedelta(hours=1)
            print(f"broker server time = UTC{hours:+.2f}h")

        line("symbols")
        for sym in symbols:
            spec = report.specs.get(sym)
            if spec is None:
                print(f"{sym:<10} NOT AVAILABLE")
                continue
            print(
                f"{sym:<10} digits={spec.digits} tick={spec.tick_size} tick_value={spec.tick_value} "
                f"contract={spec.contract_size} vol=[{spec.volume_min}..{spec.volume_max} step {spec.volume_step}] "
                f"stops_level={spec.stops_level_points} freeze={spec.freeze_level_points} "
                f"filling_flags={spec.filling_mode_flags} open_for_entries={spec.trade_allowed}"
            )

        if report.server_offset is not None:
            line("market data (last 3 CLOSED M15 bars, UTC)")
            for sym in report.specs:
                try:
                    tick = await gw.tick(sym)
                    bars = await gw.closed_bars(sym, Timeframe.M15, 3)
                except (GatewayError, OffsetUnavailable) as exc:
                    print(f"{sym}: {exc}")
                    continue
                if tick is not None:
                    print(
                        f"{sym}  tick {tick.time:%Y-%m-%d %H:%M:%S}Z  bid {tick.bid}  ask {tick.ask}  spread {tick.spread}"
                    )
                for b in bars:
                    print(
                        f"   {b.time:%Y-%m-%d %H:%M}Z  O {b.open}  H {b.high}  L {b.low}  C {b.close}  vol {b.tick_volume}"
                    )

            line("broker calculators (hypothetical 0.01 lot, 100 points move — no order is sent)")
            for sym, spec in report.specs.items():
                tick = await gw.tick(sym)
                if tick is None:
                    continue
                vol = max(spec.volume_min, Decimal("0.01"))
                move = spec.point * 100
                try:
                    loss = await gw.calc_profit(Side.BUY, sym, vol, tick.ask, tick.ask - move)
                    margin = await gw.calc_margin(Side.BUY, sym, vol, tick.ask)
                except GatewayError as exc:
                    print(f"{sym}: {exc}")
                    continue
                print(f"{sym:<10} {vol} lot, -{move} price move = {loss} {account.currency}; margin {margin}")

        line("open positions (all magics, count only)")
        print(len(await gw.positions()))
        return 0 if report.ok else 1
    finally:
        await gw.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
