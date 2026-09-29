"""The API on a throw-away seeded database, for the dashboard's browser smoke test (roadmap 6.9) and for a
look at the dashboard without an engine.

    cd frontend && npm run build
    cd backend && uv run python scripts/demo_api.py --port 8765     # password: $DEMO_PASSWORD or "demo password"

Nothing here touches data/aifund.db or config/trading.yaml; the database and config live in a temp directory.
``seed`` is also the fixture data of tests/integration/test_api.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from alembic import command
from argon2 import PasswordHasher
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.notify.null import NullNotifier
from aifund.api.app import create_app
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.enums import (
    CloseReason,
    CommandType,
    DealEntry,
    DealReason,
    DecisionOutcome,
    EngineState,
    Mode,
    RuleStatus,
    Side,
    Timeframe,
    TradeStatus,
    VirtualArm,
    VirtualStatus,
)
from aifund.domain.market import Bar
from aifund.engine.learning import Learning
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.migrations import alembic_config
from aifund.persistence.repositories.api import BarCacheRepository
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.learning import (
    AuditRunRepository,
    RulebookRepository,
    RuleEvaluationRepository,
    RuleRepository,
)
from aifund.persistence.repositories.system import (
    CommandRepository,
    EngineStateRepository,
    EventRepository,
    HeartbeatRepository,
)
from aifund.persistence.tables import DealRow, TradeRow, VirtualTradeRow
from aifund.ports.system import ClockPort, Severity
from aifund.rules import dsl

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


def seed_learning(factory: sessionmaker[Session], clock: ClockPort, decision_id: str) -> None:
    """A rule in every lifecycle column, two rulebook versions, one audit run and one rule match."""
    t0 = clock.now()

    def rule(rid: str, feature: str, op: str, value: Any, action: dict[str, Any], **scope: Any) -> dsl.Rule:
        return dsl.parse(
            {"rule_id": rid, "scope": scope, "conditions": {"all": [{"feature": feature, "op": op, "value": value}]},
             "action": action, "hypothesis": "demo"}
        )  # fmt: skip

    evidence = {
        "n_matched": 34, "n_holdout": 11, "win_matched": 0.24, "win_unmatched": 0.47, "mean_matched": -0.52,
        "mean_unmatched": 0.14, "effect": -0.66, "effect_discovery": -0.71, "effect_holdout": -0.48,
        "ci_mean": [-0.81, -0.2], "coverage": 0.12, "failures": [],
    }  # fmt: skip
    rules = [
        (rule("R-0001", "h1.rsi14", ">", 70, {"type": "penalty", "points": 15}, directions=["LONG"]),
         RuleStatus.ACTIVE, evidence),
        (rule("R-0002", "ctx.session", "==", "NY", {"type": "block"}, symbols=["NAS100"]),
         RuleStatus.SHADOW, {**evidence, "shadow": {"n": 12, "mean_r": -0.6, "days": 9}, "awaiting_approval": True}),
        (rule("R-0003", "m15.nr7", "==", True, {"type": "penalty", "points": 10}), RuleStatus.CANDIDATE, None),
        (rule("R-0004", "m15.rsi14", "<", 30, {"type": "penalty", "points": 5}), RuleStatus.REJECTED,
         {**evidence, "failures": ["holdout effect +0.050 > -0.15"]}),
        (rule("R-0005", "ctx.regime", "==", "RANGE", {"type": "risk_scale", "factor": "0.5"}), RuleStatus.RETIRED,
         {**evidence, "review_failures": 2}),
    ]  # fmt: skip
    with unit_of_work(factory) as s:
        run = AuditRunRepository(s, clock).start(
            trigger="nightly", window_from=t0 - timedelta(days=120), window_to=t0, n_trades=180, n_virtual=64
        )
        AuditRunRepository(s, clock).finish(
            run.id, "DONE", candidates=[], validation={"results": [], "findings": ["setup X loses in ASIA"]},
            miner_output={"n_samples": 244, "n_discovery": 171, "tested": 812, "clusters": [], "weak": []},
            lessons_md="# Audit (nightly)\n\nLong entries above H1 RSI 70 lose.\n",
        )  # fmt: skip
        repo = RuleRepository(s, clock)
        for r, status, ev in rules:
            repo.add(
                rule_id=r.rule_id, version=1, status=RuleStatus.SHADOW if status is RuleStatus.ACTIVE else status,
                dsl=dsl.dump(r), dsl_sha256=dsl.dsl_sha256(r), hypothesis="Late-trend longs get stopped out.",
                evidence=ev, origin="AUDITOR", audit_run_id=run.id, shadow_started_at=t0 - timedelta(days=20),
            )  # fmt: skip
        RulebookRepository(s, clock).record("R-0001v1 CANDIDATE->SHADOW")
        repo.update("R-0001", 1, status=RuleStatus.ACTIVE, activated_at=t0, review_at=t0 + timedelta(days=30),
                    expires_at=t0 + timedelta(days=90))  # fmt: skip
        RulebookRepository(s, clock).record("R-0001v1 SHADOW->ACTIVE")
        for i in range(12):  # R-0002's live shadow evidence: 12 NY decisions whose virtual trades lost
            when = t0 - timedelta(days=10) + timedelta(hours=i)
            d = DecisionRepository(s, clock).add(
                account_id="demo", symbol="NAS100.r", trigger_tf="M15", bar_time=when, stage_reached="DECISION",
                outcome=DecisionOutcome.BELOW_THRESHOLD, proposal={"direction": "LONG", "setup_tag": "mtf_trend_pullback"},
            )  # fmt: skip
            s.add(
                VirtualTradeRow(
                    id=f"VD{i:02d}", account_id="demo", decision_id=d.id, arm=VirtualArm.BLOCKED, symbol="NAS100.r",
                    side=Side.BUY, setup_tag="mtf_trend_pullback", entry_time=when, entry_price=D("20000"),
                    sl_distance=D("40"), tp_distance=D("80"), expires_at=when + timedelta(hours=3),
                    expire_reason=CloseReason.TIME_STOP, status=VirtualStatus.CLOSED, r_multiple=D("-1"), created_at=when,
                )
            )  # fmt: skip
            s.flush()
            RuleEvaluationRepository(s).add_many(
                d.id,
                [
                    {
                        "rule_id": "R-0002",
                        "rule_version": 1,
                        "mode": "SHADOW",
                        "matched": True,
                        "action_applied": None,
                    }
                ],
            )
        RuleEvaluationRepository(s).add_many(
            decision_id,
            [
                {
                    "rule_id": "R-0001",
                    "rule_version": 1,
                    "mode": "ACTIVE",
                    "matched": True,
                    "action_applied": {"type": "penalty", "points": 15},
                }
            ],
        )


def serve_commands(factory: sessionmaker[Session], clock: ClockPort, cfg: Any, stop: threading.Event) -> None:
    """The demo has no engine: rule commands run through the real learning loop, others are acknowledged."""
    learning = Learning(cfg, factory, clock, NullNotifier())

    async def once() -> None:
        with unit_of_work(factory) as s:
            claimed = CommandRepository(s, clock).claim_next()
            job = (claimed.id, claimed.type, dict(claimed.payload or {})) if claimed else None
        if job is None:
            return
        cid, type_, payload = job
        try:
            command = CommandType(type_)
            if command in (CommandType.RUN_AUDIT, CommandType.APPROVE_RULE, CommandType.REJECT_RULE,
                           CommandType.RETIRE_RULE):  # fmt: skip
                ok, result = True, await learning.handle(command, payload)
            else:
                ok, result = True, {"note": "demo server: no engine to run this"}
        except Exception as exc:
            ok, result = False, {"error": str(exc)}
        with unit_of_work(factory) as s:
            CommandRepository(s, clock).finish(cid, ok=ok, result=result)

    async def loop() -> None:
        while not stop.is_set():
            await once()
            await asyncio.sleep(0.3)

    asyncio.run(loop())


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
    ids = seed(factory, clock, account, t0=clock.now() - timedelta(hours=2))
    seed_learning(factory, clock, ids["decision"])
    stop = threading.Event()
    worker = threading.Thread(
        target=serve_commands, args=(factory, clock, app.state.config.current().config, stop)
    )
    worker.start()
    print(f"demo database in {work}", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        stop.set()
        worker.join()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
