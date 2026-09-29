"""Command and config endpoints (roadmap 6.3, docs/05 §2–§3). The API asks; the engine decides and executes.

Commands go into the ``commands`` table (202 + ``command_id``); the engine picks them up within a second and
the result arrives as a ``command.updated`` event or through ``GET /api/commands/{id}``. Re-auth (a password
within 5 minutes) is required for SET_MODE, for REARM after a drawdown halt, and for any config change that
raises risk; PAUSE and FLATTEN_ALL are never gated — safety actions must be fast.

``PUT /api/config`` validates the YAML against the full schema (field errors on failure), writes the file,
records a config version and asks the engine to RELOAD_CONFIG (it applies at the next restart)."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from aifund.api import context as ctx
from aifund.api.auth import Authenticated, Mutating, client_ip, require_reauth
from aifund.api.schemas import (
    CommandAccepted,
    CommandIn,
    CommandOut,
    ConfigIn,
    ConfigOut,
    ConfigSaved,
    ConfigVersionOut,
    FieldError,
)
from aifund.config.loader import ConfigError, parse_trading_config
from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import CommandType, Mode
from aifund.persistence.repositories.dashboard import DashboardQueries
from aifund.persistence.repositories.system import (
    AuditLogRepository,
    CommandRepository,
    ConfigVersionRepository,
)

router = APIRouter(prefix="/api", tags=["commands"])
ENGINE_COMMANDS = {
    CommandType.START,
    CommandType.PAUSE,
    CommandType.RESUME,
    CommandType.STOP,
    CommandType.REARM,
    CommandType.FLATTEN_ALL,
    CommandType.SET_MODE,
}
DRAWDOWN_HALTS = ("MAX_DRAWDOWN", "Guardian EA: HARD_MAX_DRAWDOWN")

# (path, riskier when) — a change in this direction needs re-auth
RISKIER_WHEN_HIGHER = (
    "risk.risk_per_trade_pct",
    "risk.max_risk_per_trade_pct",
    "risk.max_lots_per_symbol",
    "risk.max_margin_utilisation",
    "risk.limits.max_open_positions",
    "risk.limits.max_positions_per_symbol",
    "risk.limits.max_portfolio_heat_pct",
    "risk.limits.max_bucket_heat_pct",
    "risk.limits.max_trades_per_symbol_per_day",
    "risk.limits.daily_loss_limit_pct",
    "risk.limits.weekly_loss_limit_pct",
    "risk.limits.max_drawdown_pct",
    "risk.limits.max_notional_leverage",
)
RISKIER_WHEN_LOWER = ("risk.confidence_threshold",)
RISKIER_WHEN_TRUE = ("engine.allow_live", "strategy.analyst_orders")
RISKIER_WHEN_FALSE = ("strategy.dry_run", "risk.news.enabled")


def enqueue(request: Request, type_: CommandType, payload: dict[str, Any] | None) -> str:
    with ctx.tx(request) as s:
        command_id: str = (
            CommandRepository(s, ctx.clock(request)).enqueue(type_, payload, requested_by="operator").id
        )
    return command_id


@router.post("/engine/commands", status_code=status.HTTP_202_ACCEPTED)
def engine_command(body: CommandIn, request: Request, session: Mutating) -> CommandAccepted:
    try:
        type_ = CommandType(body.type)
    except ValueError:
        raise HTTPException(422, f"unknown command {body.type!r}") from None
    if type_ not in ENGINE_COMMANDS:
        raise HTTPException(422, f"{type_.value} has its own endpoint")
    if type_ is CommandType.SET_MODE:
        require_reauth(request, session)
    if type_ is CommandType.REARM and _drawdown_halt(request):
        require_reauth(request, session)
    return CommandAccepted(command_id=enqueue(request, type_, body.payload))


def _drawdown_halt(request: Request) -> bool:
    account = ctx.account(request)
    with ctx.read(request) as s:
        engine = next((e for e in DashboardQueries(s).engine_states() if e.account_id == account), None)
    return engine is not None and (engine.halt_reason or "").startswith(DRAWDOWN_HALTS)


@router.get("/commands/{command_id}")
def command(request: Request, command_id: str, _s: Authenticated) -> CommandOut:
    with ctx.read(request) as s:
        row = DashboardQueries(s).command(command_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
        return CommandOut.model_validate(row)


@router.post("/positions/{position_id}/close", status_code=status.HTTP_202_ACCEPTED)
def close_position(request: Request, position_id: int, _s: Mutating) -> CommandAccepted:
    with ctx.read(request) as s:
        if DashboardQueries(s).trade_by_position(position_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no engine trade for that position")
    return CommandAccepted(
        command_id=enqueue(request, CommandType.CLOSE_POSITION, {"position_id": position_id})
    )


# ---------------------------------------------------------------- config


@router.get("/config")
def get_config(request: Request, _s: Authenticated) -> ConfigOut:
    loaded = ctx.state(request).config.current()
    with ctx.read(request) as s:
        latest = DashboardQueries(s).latest_config()
    return ConfigOut(
        yaml=loaded.yaml_text,
        sha256=loaded.sha256,
        version_id=latest.id if latest else None,
        json_schema=TradingConfig.model_json_schema(),
    )


@router.get("/config/versions")
def config_versions(request: Request, _s: Authenticated) -> list[ConfigVersionOut]:
    with ctx.read(request) as s:
        return [ConfigVersionOut.model_validate(v) for v in DashboardQueries(s).config_versions()]


def field_errors(message: str) -> list[FieldError]:
    """``ConfigError`` text → field errors (``  - risk.limits: message`` lines; else one for the file)."""
    found = [
        FieldError(field=m.group(1), message=m.group(2))
        for m in re.finditer(r"^\s*- ([^:]+): (.+)$", message, re.M)
    ]
    return found or [FieldError(field="(file)", message=message.split(": ", 1)[-1])]


def _get(cfg: TradingConfig, path: str) -> Any:
    value: Any = cfg
    for part in path.split("."):
        value = getattr(value, part)
    return value


def riskier(old: TradingConfig, new: TradingConfig) -> list[str]:
    """What in ``new`` takes more risk than ``old`` (any item → re-auth)."""
    out = []
    for path in RISKIER_WHEN_HIGHER:
        if Decimal(str(_get(new, path))) > Decimal(str(_get(old, path))):
            out.append(f"{path} raised")
    for path in RISKIER_WHEN_LOWER:
        if _get(new, path) < _get(old, path):
            out.append(f"{path} lowered")
    for path in RISKIER_WHEN_TRUE:
        if _get(new, path) and not _get(old, path):
            out.append(f"{path} turned on")
    for path in RISKIER_WHEN_FALSE:
        if not _get(new, path) and _get(old, path):
            out.append(f"{path} turned off")
    if new.engine.mode is not old.engine.mode and new.engine.mode in (Mode.DEMO, Mode.LIVE):
        out.append(f"engine.mode -> {new.engine.mode.value}")
    return out


@router.put("/config", response_model=ConfigSaved, responses={422: {"model": list[FieldError]}})
def put_config(body: ConfigIn, request: Request, session: Mutating) -> ConfigSaved | JSONResponse:
    source = ctx.state(request).config
    try:
        new = parse_trading_config(body.yaml, source="config")
    except ConfigError as exc:
        errors = [e.model_dump() for e in field_errors(str(exc))]
        return JSONResponse(status_code=422, content=errors)
    old = source.current()
    if riskier(old.config, new.config):
        require_reauth(request, session)
    changed = new.sha256 != old.sha256
    if changed:
        target = source.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(body.yaml, encoding="utf-8")
        os.replace(tmp, target)
    with ctx.tx(request) as s:
        version = ConfigVersionRepository(s, ctx.clock(request)).record(
            new, created_by="api", comment=body.comment
        )
        AuditLogRepository(s, ctx.clock(request)).record(
            actor="operator",
            action="CONFIG_PUT",
            before={"sha256": old.sha256},
            after={"sha256": new.sha256},
            ip=client_ip(request),
        )
        version_id = version.id
    command_id = enqueue(request, CommandType.RELOAD_CONFIG, None) if changed else None
    return ConfigSaved(version_id=version_id, changed=changed, command_id=command_id)
