"""Rollout gates end to end (roadmap 9.6-9.7): evidence gathered from the tables, the API, the sign-off."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.api.app import create_app
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.domain.decision import FeatureSnapshot
from aifund.domain.enums import (
    CommandStatus,
    CommandType,
    DecisionOutcome,
    IntentKind,
    IntentStatus,
    Side,
    Timeframe,
    TradeStatus,
)
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.market import FeatureSnapshotRepository
from aifund.persistence.repositories.rollout import RolloutRepository
from aifund.persistence.repositories.system import CommandRepository, EventRepository, HeartbeatRepository
from aifund.persistence.tables import AuditLogRow, CommandRow, OrderIntentRow, TradeRow
from aifund.ports.system import Severity

from .conftest import T0

PASSWORD = "correct horse battery"
EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"


def demo_config(tmp_path: Path) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    for old, new in (("account_label: vantage-demo-1", "account_label: acc"), ("mode: SIM ", "mode: DEMO "),
                     ("analyst_enabled: true ", "analyst_enabled: false "),
                     ("baseline_enabled: false ", "baseline_enabled: true ")):  # fmt: skip
        assert old in text, old
        text = text.replace(old, new, 1)
    path = tmp_path / "demo.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def demo(db_url: str, factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path) -> TestClient:
    settings = Settings(
        DATABASE_URL=db_url,
        CONFIG_PATH=demo_config(tmp_path),
        ADMIN_PASSWORD_HASH=PasswordHasher().hash(PASSWORD),
        API_COOKIE_SECURE=False,
    )
    return TestClient(create_app(settings, factory=factory, clock=clock, dist=tmp_path / "none",
                                 log_file=tmp_path / "log", exports=tmp_path / "exports"))  # fmt: skip


def seed_demo_period(factory: sessionmaker[Session], clock: FakeClock, n: int = 100) -> None:
    """A 30-day DEMO period meeting every L2 gate: n closed trades with decision + snapshot, the kill switch
    tested, a restart while a trade was open, and a clean nightly ledger check."""
    snap = FeatureSnapshot(symbol="XAUUSD", trigger_tf=Timeframe.M15, bar_time=T0, feature_set_version=2,
                           features={}, bars_ref={Timeframe.M15: T0})  # fmt: skip
    with unit_of_work(factory) as s:
        snapshot_id = FeatureSnapshotRepository(s, clock).get_or_add(snap).id
        decision = DecisionRepository(s, clock).add(
            account_id="acc", symbol="XAUUSD", trigger_tf="M15", bar_time=T0, stage_reached="EXECUTION",
            outcome=DecisionOutcome.ORDERED, snapshot_id=snapshot_id,
        )  # fmt: skip
        for i in range(n):
            opened = T0 + timedelta(hours=7 * i)
            s.add(TradeRow(
                id=f"T{i}", account_id="acc", position_id=i + 1, symbol="XAUUSD", side=Side.BUY,
                status=TradeStatus.CLOSED, open_time=opened, close_time=opened + timedelta(hours=2),
                open_price=D("4150"), volume_opened=D("0.10"), volume_open_now=D(0), net_pnl=D("5"),
                r_multiple=D("0.3"), commission=D("-0.70"), decision_id=decision.id, snapshot_id=snapshot_id,
                created_at=opened, updated_at=opened,
            ))  # fmt: skip
        clock.set(T0 + timedelta(hours=1))  # trade T0 is open: a restart now counts
        EventRepository(s, clock).append("engine.state", Severity.INFO, {"trigger": "BOOT", "to": "STARTING"})
        cmd = CommandRepository(s, clock).enqueue(CommandType.FLATTEN_ALL, None, requested_by="operator")
        s.get(CommandRow, cmd.id).status = CommandStatus.DONE  # type: ignore[union-attr]
        clock.set(T0 + timedelta(days=30))
        HeartbeatRepository(s, clock).beat("job.verify_ledger", "ok", {"positions": n, "differences": []})


def login(tc: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": tc.post("/api/auth/login", json={"password": PASSWORD}).json()["csrf_token"]}


def test_the_evidence_is_read_from_the_tables(factory: sessionmaker[Session], clock: FakeClock) -> None:
    seed_demo_period(factory, clock, n=3)
    with unit_of_work(factory) as s:  # a duplicate: two filled opens for one decision and one position
        decision_id = s.scalars(select(TradeRow.decision_id)).first()
        for i in range(2):
            s.add(OrderIntentRow(
                id=f"I{i}", idempotency_key=f"k{i}", decision_id=decision_id, kind=IntentKind.OPEN,
                symbol="XAUUSD", side=Side.BUY, volume=D("0.1"), price_ref=D("4150"), risk_money=D(10),
                risk_pct=D("0.5"), magic=1,
                comment="c", status=IntentStatus.FILLED, position_id=1, created_at=T0, account_id="acc",
            ))  # fmt: skip
        EventRepository(s, clock).append("position.sl_missing", Severity.WARN, {"position_id": 1})
        s.get(TradeRow, "T2").snapshot_id = None  # type: ignore[union-attr]
    with factory() as s:
        repo = RolloutRepository(s)
        assert repo.first_trade("acc") == T0
        facts = repo.facts("acc", T0, baseline=(T0, T0 + timedelta(hours=8)))
        later = repo.facts("acc", T0 + timedelta(days=40))
    assert (len(facts.trades), len(facts.baseline_trades)) == (3, 2)
    assert (facts.duplicate_opens, facts.sl_missing, facts.without_context) == (2, 1, 1)
    assert (facts.restarts_with_open, facts.flatten_tested, facts.ledger_status) == (1, 1, "ok")
    assert (facts.trades[0].costs, facts.trades[0].volume) == (D("-0.70"), D("0.10"))
    assert (len(later.trades), later.restarts_with_open, later.flatten_tested, later.sl_missing) == (
        0,
        0,
        0,
        0,
    )


def test_a_ready_demo_period_is_signed_off_with_a_fresh_password(
    demo: TestClient, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    headers = login(demo)
    not_ready = demo.get("/api/rollout").json()
    assert (not_ready["level"], not_ready["level_name"], not_ready["ready"]) == ("L2", "DEMO", False)
    assert demo.post("/api/rollout/signoff", json={"note": "too early"}, headers=headers).status_code == 409
    seed_demo_period(factory, clock)
    headers = login(demo)  # the clock moved 30 days: a new session
    out = demo.get("/api/rollout").json()
    assert (out["ready"], out["next_level"], out["period_start"]) == (True, "L3", "2026-09-28T09:00:00Z")
    assert {g["name"]: g["status"] for g in out["gates"]}["closed_trades"] == "PASS"
    clock.advance(minutes=6)
    stale = demo.post("/api/rollout/signoff", json={"note": "L2 soak done"}, headers=headers)
    assert stale.status_code == 403
    assert demo.post("/api/auth/reauth", json={"password": PASSWORD}, headers=headers).status_code == 200
    signed = demo.post("/api/rollout/signoff", json={"note": "L2 soak done"}, headers=headers).json()
    ((row,),) = [signed["signoffs"]]
    assert (row["level_from"], row["level_to"], row["note"]) == ("L2", "L3", "L2 soak done")
    with factory() as s:
        (audit,) = s.scalars(select(AuditLogRow).where(AuditLogRow.action == "ROLLOUT_SIGNOFF")).all()
    assert audit.after["gates"][0]["status"] == "PASS"
    assert (
        demo.post("/api/rollout/signoff", json={"note": "x"}, headers=headers).status_code == 422
    )  # too short
