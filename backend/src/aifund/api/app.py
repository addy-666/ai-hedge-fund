"""The API process (roadmap 6.1, docs/05): reads the database, writes only commands, config versions, the
audit log and sessions. It never talks to MT5 or DeepSeek.

cd backend && uv run uvicorn aifund.api.app:app --host <tailscale-ip> --port 8000

``create_app`` builds it for a database and clock (tests pass their own); ``app`` is the production instance.
Every mutation (POST/PUT/DELETE) is written to ``audit_log`` with the client address and the response status.
The built dashboard (``frontend/dist``), when present, is served from ``/``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import SystemClock
from aifund.api import auth
from aifund.api.context import ConfigSource
from aifund.api.routes import analytics, commands, learning, read, ws
from aifund.config.settings import PROJECT_ROOT, Settings
from aifund.observability import register_settings_secrets
from aifund.persistence.db import make_engine, make_session_factory, unit_of_work
from aifund.persistence.repositories.system import AuditLogRepository
from aifund.ports.system import ClockPort

MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
DIST = PROJECT_ROOT / "frontend" / "dist"


def create_app(
    settings: Settings | None = None,
    *,
    factory: sessionmaker[Session] | None = None,
    clock: ClockPort | None = None,
    dist: Path = DIST,
    log_file: Path = PROJECT_ROOT / "logs" / "engine.jsonl",
) -> FastAPI:
    settings = settings or Settings()
    register_settings_secrets(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title="aifund", version="1", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.state.settings = settings
    app.state.factory = factory or make_session_factory(make_engine(settings.DATABASE_URL))
    app.state.clock = clock or SystemClock()
    app.state.limiter = auth.LoginLimiter()
    app.state.config = ConfigSource(settings.CONFIG_PATH)
    app.state.log_file = log_file

    origins = [o.strip() for o in settings.API_ALLOWED_ORIGINS.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def audit_mutations(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if (
            request.method in MUTATIONS
            and request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/auth/")
        ):
            with unit_of_work(app.state.factory) as s:
                AuditLogRepository(s, app.state.clock).record(
                    actor="operator",
                    action=f"{request.method} {request.url.path}",
                    after={"status": response.status_code},
                    ip=auth.client_ip(request),
                )
        return response

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    for router in (auth.router, read.router, analytics.router, commands.router, learning.router, ws.router):
        app.include_router(router)

    if (dist / "index.html").is_file():  # the built dashboard; any unknown path falls back to the SPA
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Any:
            target = (dist / path).resolve()
            if path and target.is_file() and dist.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(dist / "index.html")

    return app


def __getattr__(name: str) -> Any:  # ``aifund.api.app:app`` builds the production app on first use
    if name == "app":
        globals()["app"] = created = create_app()
        return created
    raise AttributeError(name)
