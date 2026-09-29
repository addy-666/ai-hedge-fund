"""Shared API helpers: the trading config (re-read when the file changes) and request-scoped access."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from aifund.config.loader import ConfigError, LoadedConfig, load_trading_config
from aifund.config.settings import PROJECT_ROOT
from aifund.persistence.db import unit_of_work
from aifund.ports.system import ClockPort

EXAMPLE = PROJECT_ROOT / "config" / "trading.example.yaml"


class ConfigSource:
    """The engine's config file (the example config when there is none, e.g. on a development machine)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._loaded: LoadedConfig | None = None

    @property
    def file(self) -> Path:
        return self.path if self.path.is_file() else EXAMPLE

    def current(self) -> LoadedConfig:
        path = self.file
        mtime = path.stat().st_mtime
        if self._loaded is None or mtime != self._mtime:
            try:
                self._loaded = load_trading_config(path)
            except ConfigError as exc:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR, f"config on disk is invalid: {exc}"
                ) from exc
            self._mtime = mtime
        return self._loaded


def state(request: Request) -> Any:
    return request.app.state


def clock(request: Request) -> ClockPort:
    c: ClockPort = request.app.state.clock
    return c


def read(request: Request) -> Session:
    """A read-only session for one request (closed by the caller's ``with``)."""
    session: Session = request.app.state.factory()
    return session


def account(request: Request) -> str:
    label: str = state(request).config.current().config.engine.account_label
    return label


def tx(request: Request) -> Any:
    return unit_of_work(request.app.state.factory)
