"""Security review of the API (roadmap 9.4, docs/06 §6): a pen test of the auth flows over EVERY route.

- every route but health, login and the SPA needs a session (401 without one);
- every mutation needs the CSRF header (403 without it, 403 with a wrong one);
- the gated actions need a fresh password (re-auth), everything else does not;
- the session cookie is HttpOnly, SameSite=Strict and Secure; logout and expiry kill the session server-side;
- logins are rate limited per socket address, and a spoofed X-Forwarded-For does not reset the limit;
- the WebSocket needs the session and refuses a foreign Origin;
- the SPA fallback never serves a file outside the built dashboard;
- no route can enqueue anything but the operator commands (no order of any kind).
"""

from __future__ import annotations

import ast
import re
from datetime import timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from starlette.websockets import WebSocketDisconnect

from aifund.adapters.clock import FakeClock
from aifund.api.app import create_app
from aifund.config.settings import Settings
from aifund.domain.enums import CommandType
from tests.integration.test_api import PASSWORD, client, config_path, login  # noqa: F401

API = Path(__file__).resolve().parents[2] / "src" / "aifund" / "api"
PUBLIC = {("GET", "/api/health"), ("POST", "/api/auth/login")}
MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
# operator commands only: engine state, the kill switch, closing a position, config, learning and calibration
ALLOWED_COMMANDS = {
    CommandType.START, CommandType.PAUSE, CommandType.RESUME, CommandType.STOP, CommandType.REARM,
    CommandType.FLATTEN_ALL, CommandType.CLOSE_POSITION, CommandType.RELOAD_CONFIG, CommandType.SET_MODE,
    CommandType.RUN_AUDIT, CommandType.APPROVE_RULE, CommandType.REJECT_RULE, CommandType.RETIRE_RULE,
    CommandType.FIT_CALIBRATION, CommandType.APPROVE_CALIBRATION, CommandType.REJECT_CALIBRATION,
}  # fmt: skip


def routes(tc: TestClient) -> list[tuple[str, str]]:
    """Every HTTP route of the API, from its schema (path parameters filled with 1)."""
    schema = tc.app.openapi()  # type: ignore[attr-defined]
    return [
        (method.upper(), re.sub(r"\{[^}]+\}", "1", path))
        for path, operations in schema["paths"].items()
        for method in operations
    ]


def call(tc: TestClient, method: str, path: str, headers: dict[str, str] | None = None) -> int:
    body = {} if method in MUTATIONS else None
    return tc.request(method, path, json=body, headers=headers or {}).status_code


def test_every_route_needs_a_session(client: TestClient) -> None:  # noqa: F811
    checked = [(m, p) for m, p in routes(client) if (m, p) not in PUBLIC]
    assert len(checked) > 40  # the whole API, not a sample
    wrong = {(m, p): code for m, p in checked if (code := call(client, m, p)) != 401}
    assert wrong == {}
    client.cookies.set("aifund_session", "forged")
    assert call(client, "GET", "/api/system") == 401


def test_every_mutation_needs_the_csrf_token(client: TestClient) -> None:  # noqa: F811
    headers = login(client)
    mutations = [(m, p) for m, p in routes(client) if m in MUTATIONS and (m, p) not in PUBLIC]
    assert len(mutations) >= 12
    no_token = {(m, p): code for m, p in mutations if (code := call(client, m, p)) != 403}
    assert no_token == {}
    bad = {"X-CSRF-Token": "x" * len(headers["X-CSRF-Token"])}
    wrong = {(m, p): code for m, p in mutations if (code := call(client, m, p, bad)) != 403}
    assert wrong == {}


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/engine/commands", {"type": "SET_MODE", "payload": {"mode": "LIVE", "confirm": True}}),
        ("/api/calibration/1/approve", None),
    ],
)
def test_gated_actions_need_a_fresh_password(
    client: TestClient,  # noqa: F811
    clock: FakeClock,
    factory: sessionmaker[Session],
    path: str,
    body: dict[str, object] | None,
) -> None:
    from aifund.domain.enums import CalibrationStatus
    from aifund.persistence.db import unit_of_work
    from aifund.persistence.repositories.calibration import CalibrationRepository

    with unit_of_work(factory) as s:
        CalibrationRepository(s, clock).add(
            source="analyst", method="ISOTONIC", status=CalibrationStatus.CANDIDATE, n_samples=1
        )
    headers = login(client)
    clock.advance(minutes=6)
    stale = client.post(path, json=body, headers=headers)
    assert (stale.status_code, stale.json()["detail"].split(":")[0]) == (403, "reauth_required")
    assert client.post("/api/auth/reauth", json={"password": "wrong"}, headers=headers).status_code == 401
    assert client.post("/api/auth/reauth", json={"password": PASSWORD}, headers=headers).status_code == 200
    assert client.post(path, json=body, headers=headers).status_code in (200, 202)


def test_the_session_cookie_and_its_lifetime(
    db_url: str,
    factory: sessionmaker[Session],
    clock: FakeClock,
    config_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    settings = Settings(
        DATABASE_URL=db_url, CONFIG_PATH=config_path, ADMIN_PASSWORD_HASH=PasswordHasher().hash(PASSWORD)
    )  # API_COOKIE_SECURE defaults to True
    tc = TestClient(create_app(settings, factory=factory, clock=clock, dist=tmp_path, log_file=tmp_path / "x",
                               exports=tmp_path / "e"), base_url="https://testserver")  # fmt: skip
    r = tc.post("/api/auth/login", json={"password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" in cookie
    headers = {"X-CSRF-Token": r.json()["csrf_token"]}
    token = tc.cookies.get("aifund_session")
    assert tc.get("/api/me").status_code == 200
    clock.advance(hours=12, seconds=1)
    assert tc.get("/api/me").status_code == 401  # expired server-side, whatever the browser keeps
    tc.post("/api/auth/login", json={"password": PASSWORD})
    headers = {"X-CSRF-Token": tc.get("/api/me").json()["csrf_token"]}
    live = tc.cookies.get("aifund_session")
    assert tc.post("/api/auth/logout", headers=headers).status_code == 204
    tc.cookies.set("aifund_session", live or "")  # a copied cookie after logout
    assert tc.get("/api/me").status_code == 401
    tc.cookies.set("aifund_session", token or "")  # and the expired one
    assert tc.get("/api/me").status_code == 401


def test_logins_are_rate_limited_per_socket_not_per_header(client: TestClient) -> None:  # noqa: F811
    for i in range(5):
        spoof = {"X-Forwarded-For": f"10.0.0.{i}"}
        assert client.post("/api/auth/login", json={"password": "guess"}, headers=spoof).status_code == 401
    blocked = client.post(
        "/api/auth/login", json={"password": PASSWORD}, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    assert blocked.status_code == 429  # even the right password waits


def test_the_websocket_needs_the_session_and_its_own_origin(client: TestClient) -> None:  # noqa: F811
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws") as ws:
        ws.receive_json()
    login(client)
    evil = {"origin": "https://evil.example"}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws?since=-1", headers=evil) as ws:
        ws.receive_json()
    with client.websocket_connect("/api/ws?since=-1", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "stream.start"
    tailnet = {"origin": "https://engine.tailnet.ts.net", "x-forwarded-host": "engine.tailnet.ts.net"}
    with client.websocket_connect("/api/ws?since=-1", headers=tailnet) as ws:
        assert ws.receive_json()["type"] == "stream.start"


def test_the_spa_never_serves_outside_the_dashboard(
    db_url: str,
    factory: sessionmaker[Session],
    clock: FakeClock,
    config_path: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>dashboard</html>")
    (tmp_path / "secret.txt").write_text("not for you")
    settings = Settings(DATABASE_URL=db_url, CONFIG_PATH=config_path, API_COOKIE_SECURE=False)
    tc = TestClient(create_app(settings, factory=factory, clock=clock, dist=dist, log_file=tmp_path / "x",
                               exports=tmp_path / "e"))  # fmt: skip
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt", "/..%2fsecret.txt"):
        assert "not for you" not in tc.get(path).text, path


def test_no_route_can_enqueue_anything_but_operator_commands() -> None:
    """The API's only trading mutations are close / pause / flatten commands: it never builds an order."""
    used: set[str] = set()
    for path in API.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used |= {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "CommandType"
        }  # fmt: skip
    assert {CommandType(u) for u in used} <= ALLOWED_COMMANDS
    source = "\n".join(p.read_text(encoding="utf-8") for p in API.rglob("*.py"))
    for forbidden in ("order_send", "OrderIntent", "Executor", "issue_order_intent", "RiskManager"):
        assert forbidden not in source, forbidden
    assert timedelta(minutes=5)  # (keeps the import used when the list above changes)
