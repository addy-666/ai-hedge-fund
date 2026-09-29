"""The API on a throw-away seeded database, for the dashboard's browser smoke test (roadmap 6.9) and for a
look at the dashboard without an engine.

    cd frontend && npm run build
    cd backend && uv run python scripts/demo_api.py --port 8765     # password: $DEMO_PASSWORD or "demo password"

Nothing here touches data/aifund.db or config/trading.yaml; the database and config live in a temp directory.
``seed`` is also the fixture data of tests/integration/test_api.py.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from alembic import command
from argon2 import PasswordHasher
from sqlalchemy.orm import Session, sessionmaker

from aifund.api.app import create_app
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.enums import (
    CloseReason,
    DealEntry,
    DealReason,
    DecisionOutcome,
    EngineState,
    Mode,
    Side,
    Timeframe,
    TradeStatus,
    VirtualArm,
    VirtualStatus,
)
from aifund.domain.market import Bar
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.migrations import alembic_config
from aifund.persistence.repositories.api import BarCacheRepository
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.system import EngineStateRepository, EventRepository, HeartbeatRepository
from aifund.persistence.tables import DealRow, TradeRow, VirtualTradeRow
from aifund.ports.system import ClockPort, Severity

EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"


def seed(
    factory: sessionmaker[Session], clock: ClockPort, acc: str, t0: datetime | None = None
) -> dict[str, Any]:
    """A running DEMO engine with equity, two decisions, a closed and an open trade, deals, a shadow trade,
    cached bars and one event, from ``t0`` (default ``clock.now()``). Returns the ordered decision's id."""
    T0 = clock.now()
    with unit_of_work(factory) as s:
        engine = EngineStateRepository(s, clock)
        engine.get_or_create(acc, Mode.DEMO)
        engine.set_state(acc, EngineState.RUNNING)
        engine.save_equity_refs(
            acc, day_start_equity=D("10000"), week_start_equity=D("10100"), peak_equity=D("10200")
        )
        HeartbeatRepository(s, clock).beat("engine", "RUNNING")
        HeartbeatRepository(s, clock).beat("loop.decisions", "ok")
        for minutes, equity in ((0, "10000"), (1, "10010"), (10, "9950")):
            EquitySnapshotRepository(s).add(
                account_id=acc,
                ts=T0 + timedelta(minutes=minutes),
                balance=D("10000"),
                equity=D(equity),
                margin=D("100"),
                free_margin=D("9900"),
                open_risk_money=D("50"),
                open_notional=D("1000"),
                open_positions=1,
                day_pnl=D(equity) - D("10000"),
                drawdown_pct=D("2.45"),
            )
        decision = DecisionRepository(s, clock).add(
            account_id=acc,
            symbol="XAUUSD",
            trigger_tf="M15",
            bar_time=T0,
            stage_reached="EXECUTION",
            outcome=DecisionOutcome.ORDERED,
            proposal={"thesis": "trend pullback"},
            final_confidence=72,
        )
        DecisionRepository(s, clock).add(
            account_id=acc,
            symbol="NAS100.r",
            trigger_tf="M15",
            bar_time=T0,
            stage_reached="SETUP",
            outcome=DecisionOutcome.NO_SETUP,
        )
        common = dict(
            account_id=acc,
            symbol="XAUUSD",
            side=Side.BUY,
            setup_tag="mtf_trend_pullback",
            trigger_tf="M15",
            open_price=D("4150.00"),
            volume_opened=D("0.10"),
            initial_sl=D("4140.00"),
            created_at=T0,
            updated_at=T0,
        )
        s.add(
            TradeRow(
                id="T-CLOSED",
                position_id=11,
                status=TradeStatus.CLOSED,
                open_time=T0,
                volume_open_now=D(0),
                close_time=T0 + timedelta(hours=1),
                close_price_vwap=D("4170.00"),
                close_reason=CloseReason.TP,
                net_pnl=D("199.30"),
                r_multiple=D("2.0"),
                commission=D("-0.70"),
                decision_id=decision.id,
                **common,
            )
        )
        s.add(
            TradeRow(
                id="T-OPEN",
                position_id=12,
                status=TradeStatus.OPEN,
                open_time=T0,
                volume_open_now=D("0.10"),
                current_sl=D("4140.00"),
                current_tp=D("4180.00"),
                decision_id=decision.id,
                **common,
            )
        )
        for ticket, entry, price, profit in (
            (1, DealEntry.IN, "4150.00", "0"),
            (2, DealEntry.OUT, "4170.00", "200.00"),
        ):
            s.add(
                DealRow(
                    ticket=ticket,
                    account_id=acc,
                    order=ticket,
                    position_id=11,
                    time_utc=T0 + timedelta(minutes=ticket),
                    time_server=0,
                    side=Side.BUY,
                    entry=entry,
                    reason=DealReason.EXPERT,
                    magic=1,
                    symbol="XAUUSD",
                    volume=D("0.10"),
                    price=D(price),
                    profit=D(profit),
                    commission=D("-0.35"),
                    swap=D(0),
                    fee=D(0),
                )
            )
        s.add(
            VirtualTradeRow(
                id="V1",
                account_id=acc,
                decision_id=decision.id,
                arm=VirtualArm.SHADOW_BASELINE,
                symbol="XAUUSD",
                side=Side.BUY,
                entry_time=T0,
                sl_distance=D("10"),
                tp_distance=D("20"),
                expires_at=T0 + timedelta(hours=3),
                expire_reason=CloseReason.TIME_STOP,
                status=VirtualStatus.PENDING,
                created_at=T0,
            )
        )
        BarCacheRepository(s).upsert(
            [
                Bar(
                    symbol="XAUUSD",
                    timeframe=Timeframe.M15,
                    time=T0 + timedelta(minutes=15 * i),
                    open=D("4150"),
                    high=D("4156"),
                    low=D("4148"),
                    close=D(f"{4150 + i}"),
                    tick_volume=10,
                    spread_points=20,
                )
                for i in range(4)
            ]
        )
        EventRepository(s, clock).append("engine.state", Severity.INFO, {"to": "RUNNING"})
    return {"decision": decision.id}


def main(argv: list[str]) -> int:
    import uvicorn

    from aifund.adapters.clock import SystemClock

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="aifund-demo-"))
    url = f"sqlite:///{work / 'demo.db'}"
    command.upgrade(alembic_config(url), "head")
    config = work / "trading.yaml"
    config.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(
        DATABASE_URL=url,
        CONFIG_PATH=config,
        ADMIN_PASSWORD_HASH=PasswordHasher().hash(os.environ.get("DEMO_PASSWORD", "demo password")),
        API_COOKIE_SECURE=False,
    )
    factory = make_session_factory(make_engine(url))
    clock = SystemClock()
    app = create_app(settings, factory=factory, clock=clock, log_file=work / "engine.jsonl")
    account = app.state.config.current().config.engine.account_label
    seed(factory, clock, account, t0=clock.now() - timedelta(hours=2))
    print(f"demo database in {work}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
