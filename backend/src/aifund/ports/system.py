"""Clock and notifier ports."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@runtime_checkable
class NotifierPort(Protocol):
    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        """Best effort: must never raise into trading code."""
        ...
