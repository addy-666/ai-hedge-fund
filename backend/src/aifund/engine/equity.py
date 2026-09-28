"""Equity references for the loss limits, and the equity snapshotter (docs/01 §6, docs/03 §11; roadmap 3.5).

``EquityTracker`` keeps what the daily / weekly loss limits and the drawdown limit are measured against:
the equity at the start of the trading day and week, and the peak. The trading day rolls at a LOCAL time
(17:00 New York: the broker's server midnight), so it follows DST. On a roll the new reference is the higher
of the last equity seen and the current one: a loss made while the engine was down, or in the minute across
the boundary, is never forgotten (conservative). The peak never resets.

``EquitySnapshotter`` runs every 60 s: it writes an ``equity_snapshots`` row (balance, equity, margin, open
risk and notional, day P&L, drawdown), persists the references in ``engine_state`` so a restart keeps them,
and on a loss-limit breach halts the engine (``HALTED``), records an event and alerts, once per limit and
trading day. The pipeline shares the same tracker, so every decision sees the same references.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.orm import Session, sessionmaker

from aifund.config.trading_config import EngineConfig, LimitsConfig
from aifund.domain.enums import EngineState, EventType, Mode
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.intents import IntentRepository
from aifund.persistence.repositories.system import EngineStateRepository, EventRepository
from aifund.ports.broker import BrokerError, BrokerPort
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.risk.limits import (
    EquityState,
    LimitBreach,
    check_loss_limits,
    drawdown_pct,
    trading_day_start,
    trading_week_start,
)

T = TypeVar("T")
CENT = Decimal("0.01")


@dataclass
class EquityRefs:
    day_start_at: datetime | None = None
    day_start_equity: Decimal = Decimal(0)
    week_start_at: datetime | None = None
    week_start_equity: Decimal = Decimal(0)
    peak_equity: Decimal = Decimal(0)


class EquityTracker:
    def __init__(
        self,
        boundary: str = "17:00",
        tz: str = "America/New_York",
        *,
        refs: EquityRefs | None = None,
        last_equity: Decimal | None = None,
    ) -> None:
        self._boundary = boundary
        self._tz = tz
        self.refs = refs or EquityRefs()
        self._last = last_equity  # the last equity seen (restored from the latest snapshot on a restart)

    @classmethod
    def for_engine(cls, cfg: EngineConfig) -> EquityTracker:
        return cls(cfg.trading_day_boundary, cfg.trading_day_timezone)

    def restore(self, refs: EquityRefs, last_equity: Decimal | None) -> None:
        self.refs, self._last = refs, last_equity

    def update(self, equity: Decimal, now: datetime) -> EquityState:
        day = trading_day_start(now, self._boundary, self._tz)
        week = trading_week_start(now, self._boundary, self._tz)
        reference = equity if self._last is None else max(equity, self._last)
        if self.refs.day_start_at != day:
            self.refs.day_start_at, self.refs.day_start_equity = day, reference
        if self.refs.week_start_at != week:
            self.refs.week_start_at, self.refs.week_start_equity = week, reference
        self.refs.peak_equity = max(self.refs.peak_equity, equity)
        self._last = equity
        r = self.refs
        return EquityState(equity, r.day_start_equity, r.week_start_equity, r.peak_equity)

    @property
    def day_start(self) -> datetime | None:
        return self.refs.day_start_at


# ---------------------------------------------------------------------------------------------- snapshotter


@dataclass
class SnapshotReport:
    state: EquityState | None = None
    breach: LimitBreach | None = None
    alerted: bool = False
    errors: list[str] = field(default_factory=list)


class EquitySnapshotter:
    def __init__(
        self,
        tracker: EquityTracker,
        limits: LimitsConfig,
        *,
        broker: BrokerPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
        account_id: str,
        magic: int,
        mode: Mode,
    ) -> None:
        self._tracker = tracker
        self._limits = limits
        self._broker = broker
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._account = account_id
        self._magic = magic
        self._mode = mode
        self._alerted: set[tuple[str, datetime | None]] = set()

    def _run_tx(self, fn: Callable[[Session], T]) -> T:
        with unit_of_work(self._factory) as session:
            return fn(session)

    async def _tx(self, fn: Callable[[Session], T]) -> T:
        return await asyncio.to_thread(self._run_tx, fn)

    async def start(self) -> None:
        """On startup: restore the references and the last equity seen from the database."""

        def load(s: Session) -> tuple[EquityRefs, Decimal | None]:
            row = EngineStateRepository(s, self._clock).get_or_create(self._account, self._mode)
            last = EquitySnapshotRepository(s).latest(self._account)
            refs = EquityRefs(
                day_start_at=row.day_start_at,
                day_start_equity=row.day_start_equity or Decimal(0),
                week_start_at=row.week_start_at,
                week_start_equity=row.week_start_equity or Decimal(0),
                peak_equity=row.peak_equity or Decimal(0),
            )
            return refs, (last.equity if last is not None else None)

        refs, last = await self._tx(load)
        self._tracker.restore(refs, last)

    async def run_once(self) -> SnapshotReport:
        report = SnapshotReport()
        now = self._clock.now()
        try:
            account = await self._broker.account_info()
            positions = [p for p in await self._broker.positions() if p.magic == self._magic]
            notional = Decimal(0)
            for p in positions:  # notional = broker margin at the account's leverage (as the exposure checks)
                margin = await self._broker.calc_margin(p.side, p.symbol, p.volume, p.price_open)
                notional += margin * account.leverage
        except BrokerError as exc:
            report.errors.append(str(exc))
            return report
        state = self._tracker.update(account.equity, now)
        report.state = state
        breach = check_loss_limits(state, self._limits)
        report.breach = breach
        refs = self._tracker.refs
        key = (breach.kind.value, refs.day_start_at) if breach else None
        alert = breach is not None and key not in self._alerted

        def write(s: Session) -> None:
            opens = IntentRepository(s, self._clock).filled_opens([p.position_id for p in positions])
            open_risk = sum((r.risk_money for r in opens.values()), Decimal(0))  # orphans: unknown, 0
            EquitySnapshotRepository(s).add(
                account_id=self._account,
                ts=now,
                balance=account.balance,
                equity=account.equity,
                margin=account.margin,
                free_margin=account.free_margin,
                open_risk_money=open_risk,
                open_notional=notional.quantize(CENT),
                open_positions=len(positions),
                day_pnl=account.equity - refs.day_start_equity,
                drawdown_pct=drawdown_pct(state).quantize(CENT),
            )
            engine = EngineStateRepository(s, self._clock)
            row = engine.get_or_create(self._account, self._mode)
            engine.save_equity_refs(
                self._account,
                day_start_at=refs.day_start_at,
                day_start_equity=refs.day_start_equity,
                week_start_at=refs.week_start_at,
                week_start_equity=refs.week_start_equity,
                peak_equity=refs.peak_equity,
            )
            if alert and breach is not None:
                if row.state is not EngineState.HALTED:
                    engine.set_state(self._account, EngineState.HALTED, halt_reason=breach.describe())
                EventRepository(s, self._clock).append(
                    EventType.RISK_LIMIT_BREACH,
                    Severity.CRITICAL,
                    {
                        "limit": breach.kind.value,
                        "loss_pct": str(breach.loss_pct),
                        "limit_pct": str(breach.limit_pct),
                    },
                )

        await self._tx(write)
        if alert and breach is not None and key is not None:
            self._alerted.add(key)
            report.alerted = True
            await self._notifier.notify(
                Severity.CRITICAL,
                f"Loss limit breached: {breach.kind.value}",
                f"{breach.describe()}; engine HALTED, no new exposure (equity {account.equity})",
            )
        return report
