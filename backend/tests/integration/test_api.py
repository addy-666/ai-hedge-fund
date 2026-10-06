"""API (roadmap 6.1–6.4): auth with CSRF and re-auth gates, read contracts on a seeded database, commands and
config, and the event stream's resume."""

from __future__ import annotations

import importlib.util
import json
import sys
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
    CalibrationStatus,
    CommandType,
    EngineState,
    Mode,
)
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.calibration import CalibrationRepository
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.persistence.repositories.system import EngineStateRepository, EventRepository, HeartbeatRepository
from aifund.persistence.tables import AuditLogRow, CommandRow
from aifund.ports.system import Severity

from .conftest import T0

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_script", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None  # noqa: PT018
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed = load("demo_api").seed  # the dashboard's demo data is also this file's fixture data

PASSWORD = "correct horse battery"
EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "trading.yaml"
    path.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("account_label: ", "account_label: acc #", 1),
        encoding="utf-8",
    )
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
        create_app(
            settings,
            factory=factory,
            clock=clock,
            dist=tmp_path / "nodist",
            log_file=log,
            exports=tmp_path / "exports",
        )
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
    assert system["versions"]["prompts"] == [
        "analyst_v1.j2",
        "analyst_v2.j2",
        "auditor_v1.j2",
        "critic_v1.j2",
        "researcher_v1.j2",
        "researcher_v2.j2",
        "reviewer_v1.j2",
        "specialist_v1.j2",
    ]

    acct = client.get("/api/account").json()
    assert (acct["equity"], acct["heat_pct"], acct["open_positions"]) == ("9950", "0.50", 1)
    assert {x["name"]: x["used_pct"] for x in acct["limits"]}["daily_loss"] == "0.50"

    curve = client.get("/api/equity", params={"from": T0.isoformat(), "granularity": 5}).json()
    assert [p["equity"] for p in curve] == ["10010", "9950"]  # 0 and 1 minute share a bucket: the last wins
    curve = client.get("/api/equity", params={"from": T0.isoformat(), "granularity": 10}).json()
    assert [p["equity"] for p in curve] == ["10010", "9950"]  # buckets are fixed on the clock: [:00, :10)
    curve = client.get("/api/equity", params={"from": T0.isoformat(), "granularity": 60}).json()
    assert [p["equity"] for p in curve] == ["9950"]

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


def test_read_filters_llm_usage_and_the_log_tail(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path, tmp_path: Path
) -> None:
    ids = seed(factory, clock, account(config_path))
    with unit_of_work(factory) as s:
        for latency, valid, cost in ((100, True, "0.01"), (300, False, None), (200, True, "0.02")):
            LLMCallRepository(s, clock).add(
                agent="analyst",
                decision_id=ids["decision"],
                model="deepseek-chat",
                prompt_template="analyst",
                prompt_version="v1",
                prompt_sha256="0" * 64,
                valid=valid,
                prompt_tokens=1000,
                completion_tokens=100,
                cached_tokens=500,
                cost_usd=None if cost is None else D(cost),
                latency_ms=latency,
            )
    login(client)
    day = {"from": T0.isoformat(), "to": (T0 + timedelta(days=1)).isoformat()}

    def trade_ids(**params: str) -> list[str]:
        return [t["id"] for t in client.get("/api/trades", params=params).json()["items"]]

    assert trade_ids(symbol="XAUUSD", setup="mtf_trend_pullback", **day) == ["T-CLOSED"]
    assert trade_ids(symbol="NAS100.r") == [] and trade_ids(outcome="LOSS") == []  # noqa: PT018

    def decision_symbols(**params: str) -> list[str]:
        return [d["symbol"] for d in client.get("/api/decisions", params=params).json()["items"]]

    assert decision_symbols(symbol="NAS100.r", **day) == ["NAS100.r"]
    assert decision_symbols(reason="SPREAD_TOO_WIDE") == []
    assert decision_symbols(**{"to": T0.isoformat()}) == []

    def virtual(**params: str) -> list[str]:
        return [v["id"] for v in client.get("/api/virtual-trades", params=params).json()["items"]]

    assert virtual(status="PENDING", symbol="XAUUSD") == ["V1"] and virtual(symbol="BTCUSD") == []  # noqa: PT018
    clock.advance(hours=1)
    assert len(client.get("/api/bars", params={"symbol": "XAUUSD"}).json()) == 4  # the last two days

    (usage,) = client.get("/api/llm/usage").json()
    assert (usage["calls"], usage["errors"], usage["cost_usd"], usage["cache_hit_rate"]) == (
        3,
        1,
        "0.03",
        0.5,
    )
    assert (usage["latency_p50_ms"], usage["latency_p95_ms"]) == (200, 300)

    lines = client.get("/api/logs", params={"component": "engine", "since": "2026-09-28T09:00:00Z"}).json()
    assert [line["event"] for line in lines] == ["failed"]
    big = tmp_path / "engine.jsonl"  # past the 512 kB tail window: the cut first line is dropped
    filler = json.dumps({"timestamp": "2026-09-28T08:00:00Z", "level": "debug", "event": "x" * 1000})
    big.write_text("\n".join([filler] * 600) + "\n" + json.dumps({"event": "last"}) + "\n")
    assert client.get("/api/logs", params={"limit": 1}).json()[0]["event"] == "last"
    kept = client.get("/api/logs", params={"level": "debug", "limit": 500}).json()
    assert 400 < len(kept) < 500 and all(line["event"] == "x" * 1000 for line in kept)  # noqa: PT018
    big.unlink()
    assert client.get("/api/logs").json() == []


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


def test_the_committee_comparison(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    login(client)
    empty = client.get("/api/analytics/committee").json()
    assert (empty["bars"], empty["mode"], empty["risk_usd"], empty["vs_analyst"]) == (0, "off", None, None)
    acc = account(config_path)
    seed(factory, clock, acc)  # equity 9950 -> one trade risks 0.5% = 49.75
    load("demo_api").seed_committee(factory, clock, acc, clock.now())
    out = client.get("/api/analytics/committee").json()
    assert (out["bars"], out["days"], out["risk_usd"]) == (6, 0.21, "49.75")
    arms = {a["arm"]: a for a in out["arms"]}
    assert (arms["baseline"]["trades"], arms["baseline"]["total_r"]) == (6, "3.0")
    assert (arms["analyst"]["trades"], arms["analyst"]["total_r"], arms["analyst"]["cost_usd"]) == (
        5,
        "0.5",
        "0.0060",
    )
    assert (arms["committee"]["trades"], arms["committee"]["total_r"], arms["committee"]["win_rate"]) == (
        4,
        "3.5",
        0.75,
    )
    assert out["agreement"] == 0.8  # 4 of the 5 bars where either traded (the same side here)
    assert out["vs_analyst"]["n"] == 6
    assert out["vs_analyst"]["mean_r"] == round((3.5 - 0.5) / 6 - (0.003 - 0.001) / 49.75, 4)


def test_the_challenger_comparison(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    """Roadmap 10.4: the challenger prompt's shadow record, like the committee's (the example runs v2)."""
    login(client)
    empty = client.get("/api/analytics/challenger").json()
    assert (empty["bars"], empty["mode"], empty["analyst_prompt"], empty["challenger_prompt"]) == (
        0, "shadow", "analyst_v1", "analyst_v2",
    )  # fmt: skip
    acc = account(config_path)
    seed(factory, clock, acc)  # equity 9950 -> one trade risks 49.75
    demo = load("demo_api")
    demo.seed_committee(factory, clock, acc, clock.now())
    demo.seed_challenger(factory, clock, acc, clock.now())
    out = client.get("/api/analytics/challenger").json()
    assert out["bars"] == 5  # the committee's bars are not the challenger's
    arms = {a["arm"]: a for a in out["arms"]}
    assert (arms["challenger"]["trades"], arms["challenger"]["total_r"], arms["challenger"]["cost_usd"]) == (
        4, "3", "0.0060",
    )  # fmt: skip
    assert (arms["analyst"]["trades"], arms["analyst"]["total_r"]) == (4, "-0.5")
    assert out["agreement"] == 0.6  # 3 of the 5 bars where either traded
    assert out["vs_analyst"]["mean_r"] == round((3 - (-0.5)) / 5 - (0.0012 - 0.001) / 49.75, 4)
    assert client.get("/api/analytics/committee").json()["bars"] == 6


def test_the_cross_asset_view(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    """Roadmap 10.6: the universe from the config, and what each symbol's newest decision saw of it."""
    from aifund.domain.decision import FeatureSnapshot
    from aifund.domain.enums import DecisionOutcome, Timeframe
    from aifund.persistence.repositories.decisions import DecisionRepository
    from aifund.persistence.repositories.market import FeatureSnapshotRepository

    login(client)
    empty = client.get("/api/cross-asset").json()
    assert (empty["enabled"], empty["timeframe"], empty["references"], empty["rows"]) == (
        True, "H1", ["EURUSD"], [],
    )  # fmt: skip
    assert empty["instruments"] == ["EURUSD", "XAUUSD", "BTCUSD", "NAS100"]
    acc = account(config_path)
    for i, corr in enumerate((0.1, 0.42)):  # the newer decision wins
        features = {
            "xa.eurusd.corr100": corr, "xa.eurusd.ret24_z": 1.25, "xa.eurusd.ema_stack": "BULL",
            "xa.btcusd.corr100": None, "xa.btcusd.ret24_z": None, "xa.btcusd.ema_stack": None,
        }  # fmt: skip
        bar = T0 + timedelta(minutes=15 * i)
        snap = FeatureSnapshot(symbol="XAUUSD", trigger_tf=Timeframe.M15, bar_time=bar, feature_set_version=3,
                               features=features, bars_ref={Timeframe.M15: bar})  # fmt: skip
        with unit_of_work(factory) as s:
            sid = FeatureSnapshotRepository(s, clock).get_or_add(snap).id
            DecisionRepository(s, clock).add(account_id=acc, symbol="XAUUSD", trigger_tf="M15", bar_time=bar,
                                             stage_reached="SETUP", outcome=DecisionOutcome.NO_SETUP,
                                             snapshot_id=sid)  # fmt: skip
    (row,) = client.get("/api/cross-asset").json()["rows"]
    assert (row["symbol"], row["bar_time"]) == ("XAUUSD", "2026-09-28T09:15:00Z")
    cells = {c["instrument"]: c for c in row["cells"]}
    assert set(cells) == {"EURUSD", "BTCUSD", "NAS100"}  # never itself
    assert cells["EURUSD"] == {"instrument": "EURUSD", "open": True, "corr100": 0.42, "ret24_z": 1.25,
                               "ema_stack": "BULL"}  # fmt: skip
    assert cells["BTCUSD"]["open"] is False  # all null: shut or missing
    assert cells["NAS100"]["open"] is False  # not in the snapshot at all


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
    assert current["yaml"] == config_path.read_text(encoding="utf-8")
    assert "properties" in current["json_schema"]

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
    assert config_path.read_text(encoding="utf-8") == safer
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


def test_a_stream_from_now_skips_history(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock
) -> None:
    login(client)
    with unit_of_work(factory) as s:
        old = EventRepository(s, clock).append("alert", Severity.INFO, {"i": "old"})
    with client.websocket_connect("/api/ws?since=-1") as ws:
        start = ws.receive_json()
        with unit_of_work(factory) as s:
            EventRepository(s, clock).append("alert", Severity.INFO, {"i": "new"})
        new = ws.receive_json()
    assert (start["type"], start["seq"], new["payload"]) == ("stream.start", old, {"i": "new"})


def test_the_event_stream_needs_a_session(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws") as ws:
        ws.receive_json()


def test_commandtype_values_stay_in_sync() -> None:
    assert {t.value for t in CommandType} >= {"PAUSE", "FLATTEN_ALL", "RELOAD_CONFIG"}


def test_the_committed_openapi_schema_is_current() -> None:
    committed = PROJECT_ROOT / "frontend" / "src" / "api" / "openapi.json"
    assert committed.read_text(encoding="utf-8") == load("export_openapi").render(), (
        "the API changed: run `npm run gen:api` in frontend/ and commit openapi.json and openapi.d.ts"
    )


def test_the_demo_server_seeds_a_throwaway_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    demo = load("demo_api")
    served: dict[str, Any] = {}
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(tmp_path))
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(app=app, **kw))
    monkeypatch.setenv("DEMO_PASSWORD", PASSWORD)
    assert demo.main(["--port", "9999"]) == 0
    assert served["port"] == 9999
    assert (tmp_path / "demo.db").is_file()
    client = TestClient(served["app"])
    headers = login(client)
    assert headers["X-CSRF-Token"]
    assert len(client.get("/api/trades").json()["items"]) == 1


def test_riskier_names_every_change_that_takes_more_risk() -> None:
    from aifund.api.routes.commands import riskier
    from aifund.config.loader import load_trading_config

    base = load_trading_config(EXAMPLE).config
    safe = base.model_copy(
        update={
            "strategy": base.strategy.model_copy(update={"dry_run": True}),
            "risk": base.risk.model_copy(
                update={"news": base.risk.news.model_copy(update={"enabled": True})}
            ),
        }
    )
    bold = base.model_copy(
        update={
            "engine": base.engine.model_copy(update={"allow_live": True, "mode": Mode.DEMO}),
            "risk": base.risk.model_copy(update={"confidence_threshold": 50}),
        }
    )
    assert riskier(safe, safe) == []
    assert riskier(safe, bold) == [
        "risk.confidence_threshold lowered",
        "engine.allow_live turned on",
        "strategy.dry_run turned off",
        "risk.news.enabled turned off",
        "engine.mode -> DEMO",
    ]
    assert riskier(bold, safe) == []


def test_loosening_the_part_b_controls_needs_a_fresh_password() -> None:
    """Roadmap 10.8-10.11: more positions per symbol, hedges, a higher or removed symbol heat or strategy
    cap and the DEMO evidence override all take more risk; tightening them does not ask."""
    from aifund.api.routes.commands import riskier
    from aifund.config.loader import load_trading_config
    from aifund.config.trading_config import StrategyDailyCap

    base = load_trading_config(EXAMPLE).config
    limits, guards = base.risk.limits, base.risk.guards
    tight = base.model_copy(update={"risk": base.risk.model_copy(update={
        "guards": guards.model_copy(update={"family_slots": False, "hedge_across_families": False}),
        "limits": limits.model_copy(update={"max_symbol_heat_pct": D("0.5"),
                                            "strategy_daily_cap": StrategyDailyCap(base=2, max=3)}),
    })})  # fmt: skip
    assert riskier(base, tight) == []
    assert riskier(tight, base) == [
        "risk.guards.family_slots turned on",
        "risk.guards.hedge_across_families turned on",
        "risk.limits.max_symbol_heat_pct raised",
        "risk.limits.strategy_daily_cap raised",
    ]
    capped = limits.strategy_daily_cap
    assert capped is not None
    one_regime = capped.model_copy(
        update={"by_family_regime": {**capped.by_family_regime, "reversal": {"RANGE": 4}}}
    )
    off = base.model_copy(update={"risk": base.risk.model_copy(update={"limits": limits.model_copy(update={
        "max_symbol_heat_pct": None, "strategy_daily_cap": None})})})  # fmt: skip
    regime = base.model_copy(update={"risk": base.risk.model_copy(update={"limits": limits.model_copy(update={
        "strategy_daily_cap": one_regime})})})  # fmt: skip
    assert riskier(base, off) == [
        "risk.limits.max_symbol_heat_pct raised",
        "risk.limits.strategy_daily_cap raised",
    ]
    assert riskier(base, regime) == []  # RANGE for reversal went 5 -> 4: tighter
    assert riskier(regime, base) == ["risk.limits.strategy_daily_cap raised"]
    demo = base.engine.model_copy(update={"mode": Mode.DEMO})
    on = base.model_copy(update={"engine": demo.model_copy(update={"demo_orders_without_evidence": True})})
    assert riskier(base.model_copy(update={"engine": demo}), on) == [
        "engine.demo_orders_without_evidence turned on"
    ]


def test_the_built_dashboard_is_served_with_a_spa_fallback(db_url: str, engine: Any, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "app.js").write_text("js")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("no")
    client = TestClient(create_app(Settings(DATABASE_URL=db_url), dist=dist))
    assert client.get("/assets/app.js").text == "js"
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/journal").text == "<html>app</html>"  # client-side routes
    assert client.get("/..%2Fsecret.txt").text == "<html>app</html>"  # nothing outside dist
    assert client.get("/api/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- 7.9 learning lab


def test_learning_lab_reads_rules_rulebook_audits_and_features(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    ids = seed(factory, clock, account(config_path))
    load("demo_api").seed_learning(factory, clock, ids["decision"])
    login(client)
    board = client.get("/api/rules").json()
    assert [(r["rule_id"], r["status"]) for r in board] == [
        ("R-0005", "RETIRED"), ("R-0004", "REJECTED"), ("R-0003", "CANDIDATE"), ("R-0002", "SHADOW"),
        ("R-0001", "ACTIVE"),
    ]  # fmt: skip
    shadow = next(r for r in board if r["rule_id"] == "R-0002")
    assert shadow["awaiting_approval"] and shadow["action"] == {"type": "block"}  # noqa: PT018
    assert shadow["text"] == "LONG/SHORT any setup on NAS100 when ctx.session == NY"
    assert [r["rule_id"] for r in client.get("/api/rules", params={"status": "ACTIVE"}).json()] == ["R-0001"]
    detail = client.get("/api/rules/R-0001").json()
    assert [v["version"] for v in detail["versions"]] == [1]
    (match,) = detail["matches"]
    assert (match["decision_id"], match["mode"], match["r"]) == (ids["decision"], "ACTIVE", "2.0")
    assert client.get("/api/rules/R-9999").status_code == 404

    versions = client.get("/api/rulebook/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    diff = client.get("/api/rulebook/versions/2/diff").json()
    assert (diff["activated"], diff["unshadowed"], diff["previous"]) == (["R-0001v1"], ["R-0001v1"], 1)
    assert client.get("/api/rulebook/versions/1/diff").json()["shadowed"] == ["R-0001v1", "R-0002v1"]
    assert client.get("/api/rulebook/versions/9/diff").status_code == 404
    assert client.get("/api/system").json()["versions"]["rulebook"] == 2

    (run,) = client.get("/api/audits").json()
    audit = client.get(f"/api/audits/{run['id']}").json()
    assert (audit["status"], audit["validation"]["findings"]) == ("DONE", ["setup X loses in ASIA"])
    assert client.get("/api/audits/nope").status_code == 404

    names = {f["name"]: f for f in client.get("/api/features").json()}
    assert names["m15.rsi14"]["bounds"] == [0.0, 100.0] and "ctx.session" in names  # noqa: PT018
    assert "prop.rr_target" not in names and "prop.direction" not in names  # noqa: PT018
    assert names["xa.eurusd.corr100"]["unit"] == "corr"  # the configured instruments (roadmap 10.5)
    assert "xa.xagusd.corr100" not in names


def test_rule_actions_are_engine_commands_with_reauth_for_blocks_and_force(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    ids = seed(factory, clock, account(config_path))
    load("demo_api").seed_learning(factory, clock, ids["decision"])
    headers = login(client)
    clock.advance(minutes=6)  # the login no longer counts as a fresh password
    assert client.post("/api/rules/R-0002/approve", headers=headers).status_code == 403  # a block
    assert client.post("/api/rules/R-0003/approve?force=true", headers=headers).status_code == 403
    assert client.post("/api/rules/R-0001/retire", headers=headers).status_code == 202
    assert client.post("/api/rules/R-0003/reject", headers=headers).status_code == 202
    assert client.post("/api/rules/R-9999/retire", headers=headers).status_code == 404
    assert client.post("/api/auth/reauth", json={"password": PASSWORD}, headers=headers).status_code == 200
    assert client.post("/api/rules/R-0002/approve", headers=headers).status_code == 202
    assert client.post("/api/audits/run", headers=headers).status_code == 202
    with factory() as s:
        queued = [
            (c.type, c.payload) for c in s.scalars(select(CommandRow).order_by(CommandRow.created_at)).all()
        ]
    assert queued == [
        ("RETIRE_RULE", {"rule_id": "R-0001"}), ("REJECT_RULE", {"rule_id": "R-0003"}),
        ("APPROVE_RULE", {"rule_id": "R-0002"}), ("RUN_AUDIT", None),
    ]  # fmt: skip


def test_calibration_view_and_actions(
    client: TestClient, factory: sessionmaker[Session], clock: FakeClock, config_path: Path
) -> None:
    headers = login(client)
    empty = client.get("/api/analytics/calibration").json()
    assert (empty["activation"], empty["min_samples"], empty["models"]) == ("approve", 150, [])
    assert [(src["source"], src["n"], src["brier_raw"]) for src in empty["sources"]] == [
        ("analyst", 0, None), ("committee", 0, None),
    ]  # fmt: skip
    load("demo_api").seed_calibration(factory, clock, account(config_path), clock.now())
    out = client.get("/api/analytics/calibration").json()
    analyst = out["sources"][0]
    assert analyst["n"] == 200
    assert analyst["active"] is None
    assert (analyst["candidate"]["version"], analyst["candidate"]["improvement"]) == (2, 0.1447)
    assert sum(b["n"] for b in analyst["reliability"]) == 200
    assert all(b["calibrated"] is None for b in analyst["reliability"])  # nothing active yet
    assert [(m["version"], m["status"]) for m in out["models"]] == [(2, "CANDIDATE"), (1, "REJECTED")]
    assert out["models"][1]["points"] == [[45.0, 0.25], [95.0, 0.7]]

    clock.advance(minutes=6)  # the login no longer counts as a fresh password
    assert client.post("/api/calibration/2/approve", headers=headers).status_code == 403
    assert client.post("/api/calibration/1/approve", headers=headers).status_code == 409  # not a candidate
    assert client.post("/api/calibration/9/reject", headers=headers).status_code == 404
    assert client.post("/api/calibration/2/reject", headers=headers).status_code == 202
    assert client.post("/api/calibration/fit", headers=headers).status_code == 202
    assert client.post("/api/auth/reauth", json={"password": PASSWORD}, headers=headers).status_code == 200
    assert client.post("/api/calibration/2/approve", headers=headers).status_code == 202
    with factory() as s:
        queued = [(c.type, c.payload) for c in s.scalars(select(CommandRow).order_by(CommandRow.created_at))]
    assert queued == [
        ("REJECT_CALIBRATION", {"version": 2}), ("FIT_CALIBRATION", None),
        ("APPROVE_CALIBRATION", {"version": 2}),
    ]  # fmt: skip
    with unit_of_work(factory) as s:  # what the engine does with the approval
        CalibrationRepository(s, clock).decide(2, CalibrationStatus.ACTIVE, "operator")
    active = client.get("/api/analytics/calibration").json()["sources"][0]
    assert active["active"]["version"] == 2
    top = next(b for b in active["reliability"] if b["lo"] == 80)
    assert top["calibrated"] is not None
    assert top["calibrated"] < top["mean_confidence"]


def test_an_operator_rule_is_validated_as_dsl_and_stored_as_a_candidate(client: TestClient) -> None:
    headers = login(client)
    body = {
        "scope": {"symbols": ["XAUUSD"], "directions": ["LONG"]},
        "conditions": {"all": [{"feature": "h1.rsi14", "op": ">", "value": 75}]},
        "action": {"type": "penalty", "points": 10},
        "hypothesis": "Exhausted longs revert.",
    }
    created = client.post("/api/rules", json=body, headers=headers)
    assert created.status_code == 201, created.text
    assert created.json() == {"rule_id": "R-0001", "version": 1, "status": "CANDIDATE"}
    bad = client.post("/api/rules", json={**body, "scope": {"symbols": ["EURUSD"]}}, headers=headers)
    assert bad.status_code == 422 and "EURUSD" in bad.json()["detail"]  # noqa: PT018
    unknown = {**body, "conditions": {"all": [{"feature": "m15.nope", "op": ">", "value": 1}]}}
    assert client.post("/api/rules", json=unknown, headers=headers).status_code == 422
    assert client.get("/api/rules/R-0001").json()["rule"]["origin"] == "OPERATOR"


def test_vault_exports_are_listed_and_served(client: TestClient, tmp_path: Path) -> None:
    login(client)
    assert client.get("/api/exports/vault").json() == []
    (tmp_path / "exports").mkdir()
    for name in ("ai-fund-review-2026-W38.md", "ai-fund-review-2026-W39.md", "notes.md"):
        (tmp_path / "exports" / name).write_text(f"# {name}\n", encoding="utf-8")
    assert client.get("/api/exports/vault").json() == [
        "ai-fund-review-2026-W39.md",
        "ai-fund-review-2026-W38.md",
    ]
    note = client.get("/api/exports/vault/ai-fund-review-2026-W39.md")
    assert (note.status_code, note.text) == (200, "# ai-fund-review-2026-W39.md\n")
    assert client.get("/api/exports/vault/notes.md").status_code == 404
    assert client.get("/api/exports/vault/ai-fund-review-2026-W40.md").status_code == 404
