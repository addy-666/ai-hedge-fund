"""Repositories for engine control tables: engine state, config versions, commands, events,
heartbeats and the operator audit log."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.config.loader import LoadedConfig
from aifund.domain.enums import CommandStatus, CommandType, EngineState, Mode
from aifund.domain.errors import InvariantViolation
from aifund.domain.ids import new_id
from aifund.persistence.tables import (
    AuditLogRow,
    CommandRow,
    ConfigVersionRow,
    EngineStateRow,
    EventRow,
    HeartbeatRow,
)
from aifund.ports.system import ClockPort, Severity


class _Repo:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock


class EngineStateRepository(_Repo):
    def get(self, account_id: str) -> EngineStateRow | None:
        return self._s.get(EngineStateRow, account_id)

    def get_or_create(self, account_id: str, mode: Mode) -> EngineStateRow:
        row = self.get(account_id)
        if row is None:
            row = EngineStateRow(
                account_id=account_id,
                state=EngineState.STOPPED,
                mode=mode,
                rulebook_version=0,
                updated_at=self._clock.now(),
            )
            self._s.add(row)
            self._s.flush()
        return row

    def set_state(
        self, account_id: str, state: EngineState, *, halt_reason: str | None = None
    ) -> EngineStateRow:
        row = self.get(account_id)
        if row is None:
            raise InvariantViolation(f"no engine_state row for {account_id}")
        row.state = state
        row.halt_reason = halt_reason
        row.updated_at = self._clock.now()
        self._s.flush()
        return row


class ConfigVersionRepository(_Repo):
    def record(
        self, loaded: LoadedConfig, *, created_by: str, comment: str | None = None
    ) -> ConfigVersionRow:
        """Store a config version; loading an unchanged file again returns the existing latest row."""
        latest = self.latest()
        if latest is not None and latest.sha256 == loaded.sha256:
            return latest
        row = ConfigVersionRow(
            id=new_id(),
            yaml_text=loaded.yaml_text,
            sha256=loaded.sha256,
            created_by=created_by,
            comment=comment,
            created_at=self._clock.now(),
        )
        self._s.add(row)
        self._s.flush()
        return row

    def latest(self) -> ConfigVersionRow | None:
        stmt = select(ConfigVersionRow).order_by(ConfigVersionRow.id.desc()).limit(1)
        return self._s.scalars(stmt).first()


class CommandRepository(_Repo):
    def enqueue(self, type_: CommandType, payload: dict[str, Any] | None, *, requested_by: str) -> CommandRow:
        row = CommandRow(
            id=new_id(),
            type=type_.value,
            payload=payload,
            status=CommandStatus.PENDING,
            requested_by=requested_by,
            created_at=self._clock.now(),
        )
        self._s.add(row)
        self._s.flush()
        return row

    def claim_next(self) -> CommandRow | None:
        """Oldest PENDING command → RUNNING. The engine is the only consumer."""
        stmt = (
            select(CommandRow)
            .where(CommandRow.status == CommandStatus.PENDING)
            .order_by(CommandRow.created_at, CommandRow.id)
            .limit(1)
        )
        row = self._s.scalars(stmt).first()
        if row is not None:
            row.status = CommandStatus.RUNNING
            row.started_at = self._clock.now()
            self._s.flush()
        return row

    def finish(self, command_id: str, *, ok: bool, result: dict[str, Any] | None = None) -> CommandRow:
        row = self._s.get(CommandRow, command_id)
        if row is None or row.status is not CommandStatus.RUNNING:
            raise InvariantViolation(f"command {command_id} is not RUNNING")
        row.status = CommandStatus.DONE if ok else CommandStatus.FAILED
        row.result = result
        row.finished_at = self._clock.now()
        self._s.flush()
        return row


class EventRepository(_Repo):
    def append(self, type_: str, severity: Severity, payload: dict[str, Any] | None = None) -> int:
        row = EventRow(ts=self._clock.now(), type=type_, severity=severity.value, payload=payload)
        self._s.add(row)
        self._s.flush()
        return row.seq

    def since(self, seq: int, limit: int = 500) -> Sequence[EventRow]:
        stmt = select(EventRow).where(EventRow.seq > seq).order_by(EventRow.seq).limit(limit)
        return self._s.scalars(stmt).all()


class HeartbeatRepository(_Repo):
    def beat(self, component: str, status: str = "ok", detail: dict[str, Any] | None = None) -> None:
        row = self._s.get(HeartbeatRow, component)
        now = self._clock.now()
        if row is None:
            self._s.add(HeartbeatRow(component=component, last_beat_at=now, status=status, detail=detail))
        else:
            row.last_beat_at, row.status, row.detail = now, status, detail
        self._s.flush()

    def all(self) -> Sequence[HeartbeatRow]:
        return self._s.scalars(select(HeartbeatRow).order_by(HeartbeatRow.component)).all()


class AuditLogRepository(_Repo):
    def record(
        self,
        *,
        actor: str,
        action: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> AuditLogRow:
        row = AuditLogRow(
            id=new_id(), actor=actor, action=action, before=before, after=after, ip=ip, ts=self._clock.now()
        )
        self._s.add(row)
        self._s.flush()
        return row
