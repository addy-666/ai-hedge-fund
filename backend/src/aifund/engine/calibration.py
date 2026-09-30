"""Confidence calibration in the engine (roadmap 8.5, docs/04 §9).

``CalibrationCache``: the ACTIVE map per source (analyst, committee), reloaded when the set of ACTIVE versions
changes; the pipeline refreshes it before each decision (through ``asyncio.to_thread``) and the portfolio
managers call ``calibrator(source)``. Identity while a source has no ACTIVE model.

``Calibration``: the weekly fit (``learning.calibration``: weekday + UTC time, or the FIT_CALIBRATION command)
and the operator's APPROVE_CALIBRATION / REJECT_CALIBRATION {version}. Per source:

- fewer than ``min_samples`` samples → nothing is stored (identity stays; the reliability chart shows them);
- the fit does not cut the held-out Brier score by ``min_brier_improvement`` → stored REJECTED (history);
- it does → a CANDIDATE (an older CANDIDATE of the source is superseded); with ``activation: auto`` it is
  activated at once, otherwise it waits for the operator. Activation retires the source's previous model.

Every stored model, activation and rejection appends a ``calibration.changed`` event and notifies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import CalibrationStatus, CommandType
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.calibration import SOURCES, CalibrationRepository
from aifund.persistence.repositories.system import EventRepository
from aifund.persistence.tables import CalibrationModelRow
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.rules.calibration import ISOTONIC, FitResult, IsotonicMap, Sample, fit, reliability

log = structlog.get_logger(__name__)
CALIBRATION_COMMANDS = {
    CommandType.FIT_CALIBRATION,
    CommandType.APPROVE_CALIBRATION,
    CommandType.REJECT_CALIBRATION,
}


class CalibrationError(Exception):
    """A calibration command that cannot be carried out (its message becomes the command's result)."""


def samples(s: Session, clock: ClockPort, account_id: str, source: str) -> list[Sample]:
    return [
        Sample(confidence=c, win=r > 0, time=t)
        for c, r, t in CalibrationRepository(s, clock).outcomes(account_id, source)
    ]


class CalibrationCache:
    def __init__(self, factory: sessionmaker[Session], clock: ClockPort) -> None:
        self._factory = factory
        self._clock = clock
        self._versions: tuple[int, ...] | None = None
        self._maps: dict[str, IsotonicMap] = {}

    def refresh(self) -> None:
        """Synchronous (call it through ``asyncio.to_thread``): one cheap query unless the models changed."""
        with unit_of_work(self._factory) as s:
            repo = CalibrationRepository(s, self._clock)
            versions = repo.active_versions()
            if versions == self._versions:
                return
            maps = {}
            for source in SOURCES:
                row = repo.active(source)
                if row is not None and row.method == ISOTONIC and row.params:
                    maps[source] = IsotonicMap.from_params(row.params)
            self._maps, self._versions = maps, versions
            log.info("calibration.loaded", active=list(versions), sources=sorted(maps))

    def calibrator(self, source: str) -> Callable[[int], int]:
        def calibrate(p_raw: int) -> int:
            model = self._maps.get(source)
            return model(p_raw) if model is not None else p_raw

        return calibrate


class Calibration:
    def __init__(
        self,
        cfg: TradingConfig,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
    ) -> None:
        self._cfg = cfg.learning.calibration
        self._account = cfg.engine.account_label
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._last_run: datetime | None = None

    # ------------------------------------------------------------------ the schedule

    def scheduled(self, now: datetime) -> datetime:
        """This week's fit time (it may lie ahead of ``now``)."""
        hh, mm = (int(x) for x in self._cfg.fit_utc.split(":"))
        day = now.date() - timedelta(days=(now.weekday() - self._cfg.fit_weekday) % 7)
        return datetime.combine(day, time(hh, mm), tzinfo=now.tzinfo)

    def due(self) -> bool:
        now = self._clock.now()
        at = self.scheduled(now)
        if now < at or (self._last_run is not None and self._last_run >= at):
            return False
        with unit_of_work(self._factory) as s:
            latest = CalibrationRepository(s, self._clock).history(limit=1)
        return not latest or latest[0].created_at < at

    # ------------------------------------------------------------------ fitting

    async def fit_all(self, trigger: str) -> dict[str, Any]:
        self._last_run = self._clock.now()
        notices: list[tuple[Severity, str, str]] = []
        out = await asyncio.to_thread(self._fit_all, trigger, notices)
        for severity, title, body in notices:
            await self._notifier.notify(severity, title, body)
        return out

    def _fit_all(self, trigger: str, notices: list[tuple[Severity, str, str]]) -> dict[str, Any]:
        out: dict[str, Any] = {"trigger": trigger}
        cfg = self._cfg
        for source in SOURCES:
            with unit_of_work(self._factory) as s:
                data = samples(s, self._clock, self._account, source)
                result = fit(
                    data, min_samples=cfg.min_samples, holdout=float(cfg.holdout_fraction),
                    min_improvement=float(cfg.min_brier_improvement),
                )  # fmt: skip
                summary: dict[str, Any] = {"result": result.result.value, "n": result.n}
                if result.result is FitResult.TOO_FEW or result.model is None:
                    out[source] = summary
                    continue
                repo = CalibrationRepository(s, self._clock)
                status = (
                    CalibrationStatus.CANDIDATE
                    if result.result is FitResult.CANDIDATE
                    else CalibrationStatus.REJECTED
                )
                if status is CalibrationStatus.CANDIDATE:
                    for old in repo.with_status(source, CalibrationStatus.CANDIDATE):
                        repo.decide(old.version, CalibrationStatus.REJECTED, "superseded")
                row = repo.add(
                    source=source, method=ISOTONIC, status=status, params=result.model.params(),
                    n_samples=result.n, brier_before=result.brier_before, brier_after=result.brier_after,
                    details={
                        "trigger": trigger, "n_train": result.n_train, "n_test": result.n_test,
                        "improvement": result.improvement,
                        "reliability": [vars(b) for b in reliability(data)],
                    },
                    decided_by="fit" if status is CalibrationStatus.REJECTED else None,
                    decided_at=self._clock.now() if status is CalibrationStatus.REJECTED else None,
                )  # fmt: skip
                summary.update(version=row.version, status=row.status.value, improvement=result.improvement)
                self._event(s, row, "fitted")
                if status is CalibrationStatus.CANDIDATE:
                    if cfg.activation == "auto":
                        self._activate(s, row, "auto", notices)
                    else:
                        notices.append((
                            Severity.WARN,
                            f"Calibration v{row.version} ({source}) awaits approval",
                            _describe(row) + " — approve it on the Analytics page (APPROVE_CALIBRATION).",
                        ))  # fmt: skip
                summary["status"] = row.status.value
                out[source] = summary
        log.info("calibration.fit", **{k: v for k, v in out.items() if k != "trigger"}, trigger=trigger)
        return out

    def _activate(
        self, s: Session, row: CalibrationModelRow, by: str, notices: list[tuple[Severity, str, str]]
    ) -> None:
        repo = CalibrationRepository(s, self._clock)
        for old in repo.with_status(row.source, CalibrationStatus.ACTIVE):
            repo.decide(old.version, CalibrationStatus.RETIRED, by)
            self._event(s, old, "retired")
        repo.decide(row.version, CalibrationStatus.ACTIVE, by)
        self._event(s, row, "activated")
        notices.append((
            Severity.WARN, f"Calibration v{row.version} ({row.source}) is ACTIVE ({by})", _describe(row),
        ))  # fmt: skip

    def _event(self, s: Session, row: CalibrationModelRow, what: str) -> None:
        EventRepository(s, self._clock).append(
            "calibration.changed",
            Severity.INFO,
            {"version": row.version, "source": row.source, "status": row.status.value, "what": what},
        )

    # ------------------------------------------------------------------ operator commands

    async def handle(self, command: CommandType, payload: dict[str, Any]) -> dict[str, Any]:
        if command is CommandType.FIT_CALIBRATION:
            return await self.fit_all("manual")
        version = payload.get("version")
        if not isinstance(version, int):
            raise CalibrationError(f"{command.value} needs a version")
        notices: list[tuple[Severity, str, str]] = []

        def run() -> dict[str, Any]:
            with unit_of_work(self._factory) as s:
                row = CalibrationRepository(s, self._clock).get(version)
                if row is None:
                    raise CalibrationError(f"no calibration model {version}")
                if row.status is not CalibrationStatus.CANDIDATE:
                    raise CalibrationError(f"calibration v{version} is {row.status.value}, not a CANDIDATE")
                if command is CommandType.APPROVE_CALIBRATION:
                    self._activate(s, row, "operator", notices)
                else:
                    CalibrationRepository(s, self._clock).decide(
                        version, CalibrationStatus.REJECTED, "operator"
                    )
                    self._event(s, row, "rejected")
                return {"version": version, "status": row.status.value}

        out = await asyncio.to_thread(run)
        for severity, title, body in notices:
            await self._notifier.notify(severity, title, body)
        return out


def _describe(row: CalibrationModelRow) -> str:
    return (
        f"{row.n_samples} samples, held-out Brier {row.brier_before:.4f} -> {row.brier_after:.4f}"
        if row.brier_before is not None and row.brier_after is not None
        else f"{row.n_samples} samples"
    )
