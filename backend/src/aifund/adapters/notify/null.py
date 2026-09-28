"""Notifier that records messages instead of sending them (tests, SIM mode)."""

from __future__ import annotations

from dataclasses import dataclass, field

from aifund.ports.system import Severity


@dataclass
class NullNotifier:
    sent: list[tuple[Severity, str, str]] = field(default_factory=list)

    async def notify(self, severity: Severity, title: str, body: str = "") -> None:
        self.sent.append((severity, title, body))
