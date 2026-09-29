"""API (roadmap 6.1–6.4): auth with CSRF and re-auth gates, read contracts on a seeded database, commands and
config, and the event stream's resume."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
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
    Side,
    Timeframe,
    TradeStatus,
    VirtualArm,
    VirtualStatus,
)
from aifund.domain.market import Bar
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.api import BarCacheRepository
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.system import EngineStateRepository, EventRepository, HeartbeatRepository
from aifund.persistence.tables import AuditLogRow, CommandRow, DealRow, TradeRow, VirtualTradeRow
from aifund.ports.system import Severity

from .conftest import T0

PASSWORD = "correct horse battery"
EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "trading.yaml"
    path.write_text(EXAMPLE.read_text().replace("account_label: ", "account_label: acc #", 1))
    return path


@pytest.fixture
def client(
    db_url: str, factory: sessionmaker[Session], clock: FakeClock, config_path: Path, tmp_path: Path
) -> TestClient:
    settings = Settings(
        DATABASE_URL=db_url,
        CONFIG_PATH=config_path,
        ADMIN_PASSWORD_HASH=PasswordHasher().hash(PASSWORD),
        API_COOKIE_SECURE=False,
    )
    log = tmp_path / "engine.jsonl"
    lines = [
        {"timestamp": "2026-09-28T09:00:00Z", "level": "info", "component": "engine", "event": "booted"},
        "not json",
        {"timestamp": "2026-09-28T09:00:01Z", "level": "error", "component": "engine", "event": "failed"},
    ]
    log.write_text("".join((json.dumps(x) if isinstance(x, dict) else x) + "\n" for x in lines))
    return TestClient(
        create_app(settings, factory=factory, clock=clock, dist=tmp_path / "nodist", log_file=log)
    )


def account(config_path: Path) -> str:
    from aifund.config.loader import load_trading_config

    return load_trading_config(config_path).config.engine.account_label


def login(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf_token"]}


# ---------------------------------------------------------------- 6.1 auth


def test_health_is_open_and_everything_else_needs_a_session(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    for path in ("/api/system", "/api/me", "/api/trades", "/api/config"):
        assert client.get(path).status_code == 401, path


def test_login_sets_a_strict_http_only_cookie_and_logout_ends_it(
    client: TestClient, factory: sessionmaker[Session]
) -> None:
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie  # noqa: PT018
    headers = {"X-CSRF-Token": r.json()["csrf_token"]}
    assert client.get("/api/me").json()["reauth_fresh"] is True
    assert client.post("/api/auth/logout").status_code == 403  # no CSRF token
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/me").status_code == 401
    with factory() as s:
        actions = [a.action for a in s.scalars(select(AuditLogRow)).all()]
    assert actions[:2] == ["LOGIN_FAILED", "LOGIN"]


def test_logins_are_rate_limited(client: TestClient) -> None:
    codes = [client.post("/api/auth/login", json={"password": "nope"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    assert (
        client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429
    )  # even the right one


def test_no_password_hash_means_no_login(
    db_url: str, factory: sessionmaker[Session], clock: FakeClock, tmp_path: Path
) -> None:
    app = create_app(
        Settings(DATABASE_URL=db_url, API_COOKIE_SECURE=False), factory=factory, clock=clock, dist=tmp_path
    )
    assert TestClient(app).post("/api/auth/login", json={"password": PASSWORD}).status_code == 401


def test_mutations_need_csrf_and_gated_ones_a_fresh_password(client: TestClient, clock: FakeClock) -> None:
    headers = login(client)
    assert client.post("/api/engine/commands", json={"type": "PAUSE"}).status_code == 403  # CSRF
    assert (
        client.post("/api/engine/commands", json={"type": "PAUSE"}, headers={"X-CSRF-Token": "x"}).status_code
        == 403
    )
    clock.advance(minutes=6)  # the password was entered 6 minutes ago
    assert (
        client.post("/api/engine/commands", json={"type": "PAUSE"}, headers=headers).status_code == 202
    )  # not gated
    assert (
        client.post("/api/engine/commands", json={"type": "FLATTEN_ALL"}, headers=headers).status_code == 202
    )
    gated = client.post(
        "/api/engine/commands",
        json={"type": "SET_MODE", "payload": {"mode": "LIVE", "confirm": True}},
        headers=headers,
    )
    assert (gated.status_code, gated.json()["detail"].startswith("reauth_required")) == (403, True)
    assert client.get("/api/me").json()["reauth_fresh"] is False
    assert client.post("/api/auth/reauth", json={"password": PASSWORD}, headers=headers).status_code == 200
    assert (
        client.post(
            "/api/engine/commands",
            json={"type": "SET_MODE", "payload": {"mode": "LIVE", "confirm": True}},
            headers=headers,
        ).status_code
        == 202
    )


def test_sessions_expire(client: TestClient, clock: FakeClock) -> None:
    login(client)
    clock.advance(hours=12, seconds=1)
    assert client.get("/api/me").status_code == 401


# ---------------------------------------------------------------- 6.2 read contracts


def seed(factory: sessionmaker[Session], clock: FakeClock, acc: str) -> dict[str, Any]:
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


def test_read_contracts_on_a_seeded_database(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    acc = account(config_path)
    ids = seed(factory, clock, acc)
    clock.advance(minutes=20)
    login(client)
    assert client.get("/api/system").json()["engine_stale"] is True  # its last heartbeat is 20 minutes old
    with unit_of_work(factory) as s:
        HeartbeatRepository(s, clock).beat("engine", "RUNNING")
    system = client.get("/api/system").json()
    assert (system["engine"]["state"], system["engine"]["mode"], system["engine_stale"]) == (
        "RUNNING",
        "DEMO",
        False,
    )
    assert {h["component"] for h in system["heartbeats"]} == {"engine", "loop.decisions"}
    assert system["versions"]["prompts"] == ["analyst_v1.j2", "researcher_v1.j2"]

    acct = client.get("/api/account").json()
    assert (acct["equity"], acct["heat_pct"], acct["open_positions"]) == ("9950", "0.50", 1)
    assert {x["name"]: x["used_pct"] for x in acct["limits"]}["daily_loss"] == "0.50"

    curve = client.get("/api/equity", params={"from": T0.isoformat(), "granularity": 5}).json()
    assert [p["equity"] for p in curve] == ["10010", "9950"]  # 0 and 1 minute share a bucket: the last wins

    (pos,) = client.get("/api/positions").json()
    assert (pos["position_id"], pos["last_price"], pos["r_now"], pos["thesis"]) == (
        12,
        "4153",
        "0.30",
        "trend pullback",
    )

    page = client.get("/api/trades").json()
    assert [t["id"] for t in page["items"]] == ["T-CLOSED"] and page["next_cursor"] is None  # noqa: PT018
    dossier = client.get("/api/trades/T-CLOSED").json()
    assert [d["ticket"] for d in dossier["deals"]] == [1, 2]
    assert [m["kind"] for m in dossier["markers"]] == ["entry", "exit"]
    assert len(dossier["bars"]) == 4 and dossier["decision"]["id"] == ids["decision"]  # noqa: PT018
    assert client.get("/api/trades/nope").status_code == 404

    feed = client.get("/api/decisions", params={"limit": 1}).json()
    assert len(feed["items"]) == 1 and feed["next_cursor"]  # noqa: PT018
    rest = client.get("/api/decisions", params={"cursor": feed["next_cursor"]}).json()
    assert len(rest["items"]) == 1 and rest["items"][0]["id"] != feed["items"][0]["id"]  # noqa: PT018
    assert (
        client.get("/api/decisions", params={"outcome": "NO_SETUP"}).json()["items"][0]["symbol"]
        == "NAS100.r"
    )
    assert client.get(f"/api/decisions/{ids['decision']}").json()["proposal"] == {"thesis": "trend pullback"}
    assert client.get("/api/decisions/nope").status_code == 404

    assert (
        client.get("/api/virtual-trades", params={"arm": "SHADOW_BASELINE"}).json()["items"][0]["id"] == "V1"
    )
    bars = client.get(
        "/api/bars",
        params={"symbol": "XAUUSD", "from": T0.isoformat(), "to": (T0 + timedelta(hours=1)).isoformat()},
    ).json()
    assert [b["close"] for b in bars] == ["4150", "4151", "4152", "4153"]
    assert client.get("/api/llm/usage").json() == []
    logs = client.get("/api/logs", params={"level": "error"}).json()
    assert [line["event"] for line in logs] == ["failed"]
    assert [line["event"] for line in client.get("/api/logs").json()] == ["booted", "failed"]


def test_analytics_on_the_seeded_trades(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    seed(factory, clock, account(config_path))
    clock.advance(hours=2)
    login(client)
    summary = client.get("/api/analytics/summary").json()
    assert (summary["trades"], summary["net_pnl"], summary["expectancy_r"], summary["win_rate"]) == (
        1,
        "199.30",
        "2.0000",
        1.0,
    )
    rows = client.get("/api/analytics/breakdown", params={"dim": "confidence_bucket"}).json()
    assert rows == [
        {"key": "70-79", "trades": 1, "net_pnl": "199.30", "expectancy_r": "2.0000", "win_rate": 1.0}
    ]
    assert client.get("/api/analytics/breakdown", params={"dim": "session"}).json()[0]["key"] == "unknown"
    assert client.get("/api/analytics/breakdown", params={"dim": "nope"}).status_code == 422
    assert client.get("/api/analytics/costs").json()["commission"] == "-0.70"


# ---------------------------------------------------------------- 6.3 commands and config


def test_commands_are_queued_for_the_engine(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    seed(factory, clock, account(config_path))
    headers = login(client)
    r = client.post("/api/engine/commands", json={"type": "PAUSE"}, headers=headers)
    command_id = r.json()["command_id"]
    got = client.get(f"/api/commands/{command_id}").json()
    assert (got["type"], got["status"], got["requested_by"]) == ("PAUSE", "PENDING", "operator")
    assert client.post("/api/engine/commands", json={"type": "BOGUS"}, headers=headers).status_code == 422
    assert (
        client.post("/api/engine/commands", json={"type": "CLOSE_POSITION"}, headers=headers).status_code
        == 422
    )
    assert client.post("/api/positions/12/close", headers=headers).status_code == 202
    assert client.post("/api/positions/99/close", headers=headers).status_code == 404
    assert client.get("/api/commands/nope").status_code == 404
    with factory() as s:
        queued = [
            (c.type, c.payload) for c in s.scalars(select(CommandRow).order_by(CommandRow.created_at)).all()
        ]
        audited = [a.action for a in s.scalars(select(AuditLogRow)).all()]
    assert queued == [("PAUSE", None), ("CLOSE_POSITION", {"position_id": 12})]
    assert "POST /api/engine/commands" in audited


def test_rearm_after_a_drawdown_halt_needs_a_fresh_password(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    acc = account(config_path)
    seed(factory, clock, acc)
    headers = login(client)
    clock.advance(minutes=6)
    with unit_of_work(factory) as s:
        EngineStateRepository(s, clock).set_state(acc, EngineState.HALTED, halt_reason="DAILY_LOSS: 3.1%")
    assert client.post("/api/engine/commands", json={"type": "REARM"}, headers=headers).status_code == 202
    with unit_of_work(factory) as s:
        EngineStateRepository(s, clock).set_state(acc, EngineState.HALTED, halt_reason="MAX_DRAWDOWN: 10.4%")
    assert client.post("/api/engine/commands", json={"type": "REARM"}, headers=headers).status_code == 403


def test_config_get_and_put(
    client: TestClient, clock: FakeClock, config_path: Path, factory: sessionmaker[Session]
) -> None:
    headers = login(client)
    current = client.get("/api/config").json()
    assert current["yaml"] == config_path.read_text() and "properties" in current["json_schema"]  # noqa: PT018

    bad = client.put(
        "/api/config",
        json={"yaml": current["yaml"].replace("risk_per_trade_pct: 0.5", "risk_per_trade_pct: 50")},
        headers=headers,
    )
    assert bad.status_code == 422
    assert bad.json() == [
        {"field": "risk", "message": "Value error, risk.risk_per_trade_pct must be <= max_risk_per_trade_pct"}
    ]
    assert (
        client.put("/api/config", json={"yaml": "engine: ["}, headers=headers).json()[0]["field"] == "(file)"
    )

    same = client.put("/api/config", json={"yaml": current["yaml"]}, headers=headers).json()
    assert (same["changed"], same["command_id"]) == (False, None)

    safer = current["yaml"].replace("risk_per_trade_pct: 0.5", "risk_per_trade_pct: 0.4")
    saved = client.put("/api/config", json={"yaml": safer, "comment": "less risk"}, headers=headers).json()
    assert saved["changed"] is True and saved["command_id"]  # noqa: PT018
    assert config_path.read_text() == safer
    assert client.get(f"/api/commands/{saved['command_id']}").json()["type"] == "RELOAD_CONFIG"

    clock.advance(minutes=6)
    riskier = safer.replace("risk_per_trade_pct: 0.4", "risk_per_trade_pct: 0.6")
    gated = client.put("/api/config", json={"yaml": riskier}, headers=headers)
    assert gated.status_code == 403  # raising risk needs the password again
    client.post("/api/auth/reauth", json={"password": PASSWORD}, headers=headers)
    assert client.put("/api/config", json={"yaml": riskier}, headers=headers).json()["changed"] is True
    versions = client.get("/api/config/versions").json()
    assert [v["comment"] for v in versions][:2] == [None, "less risk"]


# ---------------------------------------------------------------- 6.4 events


def test_the_event_stream_resumes_without_gaps_or_repeats(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    login(client)
    with unit_of_work(factory) as s:
        for i in range(3):
            EventRepository(s, clock).append("command.updated", Severity.INFO, {"i": i})
    with client.websocket_connect("/api/ws?since=0") as ws:
        first = [ws.receive_json() for _ in range(3)]
    assert [e["payload"]["i"] for e in first] == [0, 1, 2]
    with unit_of_work(factory) as s:  # while disconnected
        for i in range(3, 5):
            EventRepository(s, clock).append("alert", Severity.WARN, {"i": i})
    with client.websocket_connect(f"/api/ws?since={first[-1]['seq']}") as ws:
        missed = [ws.receive_json() for _ in range(2)]
        with unit_of_work(factory) as s:
            EventRepository(s, clock).append("engine.state", Severity.CRITICAL, {"i": 5})
        live = ws.receive_json()
    assert [e["payload"]["i"] for e in missed] == [3, 4]
    assert (live["payload"]["i"], live["type"], live["severity"]) == (5, "engine.state", "critical")


def test_the_event_stream_needs_a_session(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws") as ws:
        ws.receive_json()


def test_commandtype_values_stay_in_sync() -> None:
    assert {t.value for t in CommandType} >= {"PAUSE", "FLATTEN_ALL", "RELOAD_CONFIG"}
