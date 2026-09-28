"""Replay harness: drive the bar clock and the decision pipeline over simulated time on the SimBroker.

It proves the PLUMBING (no duplicates, no orphans, one decision per bar, fills and SL/TP exits recorded,
and — with a reconciler — a trade ledger that matches the broker's deals to the cent) and reports what the
strategy did. It does not prove an edge: see docs/00 §3.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.sim_broker import SimBroker
from aifund.domain.enums import DealEntry, IntentKind, IntentStatus, TradeStatus
from aifund.domain.market import Deal
from aifund.engine.pipeline import DecisionPipeline
from aifund.engine.position_loop import PositionLoop
from aifund.market.bar_clock import BarClock
from aifund.persistence.tables import DecisionRow, OrderIntentRow, TradeRow
from aifund.reconcile.reconciler import Reconciler, ReconcileReport


@dataclass
class ReplayReport:
    start: datetime
    end: datetime
    bar_events: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    fills: int = 0
    closed_trades: int = 0
    wins: int = 0
    net_pnl: Decimal = Decimal(0)
    exit_reasons: Counter[str] = field(default_factory=Counter)
    max_positions_per_symbol: int = 0
    position_actions: Counter[str] = field(default_factory=Counter)
    ledger_closed: int | None = None  # trades the reconciler closed (None: no reconciler in this run)
    close_reasons: Counter[str] = field(default_factory=Counter)
    violations: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"replay {self.start:%Y-%m-%d %H:%M} -> {self.end:%Y-%m-%d %H:%M} UTC, "
            f"{self.bar_events} closed-bar events",
            "decisions: " + ", ".join(f"{k}={v}" for k, v in self.outcomes.most_common()),
            "reasons:   " + ", ".join(f"{k}={v}" for k, v in self.reasons.most_common(8)),
            f"fills: {self.fills}  closed trades: {self.closed_trades}  wins: {self.wins}  "
            f"net P&L: {self.net_pnl}",
            "exits:     " + ", ".join(f"{k}={v}" for k, v in self.exit_reasons.most_common()),
            "position mgmt: "
            + (", ".join(f"{k}={v}" for k, v in self.position_actions.most_common()) or "none"),
            f"max engine positions on one symbol at any time: {self.max_positions_per_symbol}",
            "ledger: "
            + (
                "no reconciler"
                if self.ledger_closed is None
                else f"{self.ledger_closed} closed trades; "
                + (", ".join(f"{k}={v}" for k, v in self.close_reasons.most_common()) or "none")
            ),
            f"invariant violations: {len(self.violations)}",
        ]
        lines += [f"  ! {v}" for v in self.violations[:20]]
        return "\n".join(lines)


async def run_replay(
    *,
    pipeline: DecisionPipeline,
    bar_clock: BarClock,
    broker: SimBroker,
    clock: FakeClock,
    factory: sessionmaker[Session],
    magic: int,
    end: datetime,
    step: timedelta = timedelta(seconds=60),
    max_positions_per_symbol: int = 1,
    position_loop: PositionLoop | None = None,
    reconciler: Reconciler | None = None,
    progress: Callable[[str], None] = lambda _msg: None,
) -> ReplayReport:
    report = ReplayReport(start=clock.now(), end=end)
    last_day = None
    while clock.now() < end:
        await clock.sleep(step.total_seconds())
        for event in (await bar_clock.poll()).events:
            report.bar_events += 1
            record = await pipeline.run(event)
            report.outcomes[record.outcome.value] += 1
            if record.reason is not None:
                report.reasons[record.reason.value] += 1
        if position_loop is not None:
            for kind, _result in (await position_loop.run_once()).actions:
                report.position_actions[kind.value] += 1
        if reconciler is not None:
            _record_reconcile(report, await reconciler.run_once())
        await _check_step(report, broker, factory, magic, max_positions_per_symbol)
        if clock.now().date() != last_day:
            last_day = clock.now().date()
            progress(f"{last_day} events={report.bar_events} fills={report.fills}")

    deals = await broker.deals_between(report.start, clock.now())
    mine = [d for d in deals if d.magic == magic]
    report.fills = len([d for d in mine if d.entry is DealEntry.IN])
    by_position: dict[int, Decimal] = {}
    for d in mine:
        by_position[d.position_id] = by_position.get(d.position_id, Decimal(0)) + d.net
        if d.entry is not DealEntry.IN:
            report.exit_reasons[d.reason.value] += 1
    open_ids = {p.position_id for p in await broker.positions() if p.magic == magic}
    closed = {pid: pnl for pid, pnl in by_position.items() if pid not in open_ids}
    report.closed_trades = len(closed)
    report.wins = len([p for p in closed.values() if p > 0])
    report.net_pnl = sum(closed.values(), Decimal(0))
    _check_final(report, factory)
    if reconciler is not None:
        _check_ledger(report, factory, mine, closed, open_ids)
    return report


def _record_reconcile(report: ReplayReport, result: ReconcileReport) -> None:
    for position_id in result.orphans:
        report.violations.append(f"reconciler found orphan position {position_id}")
    for message in result.errors:
        report.violations.append(f"reconciler error: {message}")
    for _position_id, reason in result.closed:
        report.close_reasons[reason.value] += 1


def _check_ledger(
    report: ReplayReport,
    factory: sessionmaker[Session],
    deals: list[Deal],
    closed: dict[int, Decimal],
    open_ids: set[int],
) -> None:
    """The trade ledger must agree with the broker: one trade per position, net P&L to the cent."""
    with factory() as s:
        trades = {t.position_id: t for t in s.scalars(select(TradeRow)).all()}
    report.ledger_closed = len([t for t in trades.values() if t.status is TradeStatus.CLOSED])
    for position_id, net in closed.items():
        t = trades.get(position_id)
        if t is None or t.status is not TradeStatus.CLOSED:
            report.violations.append(f"position {position_id} closed at the broker but not in the ledger")
        elif t.net_pnl != net:
            report.violations.append(f"position {position_id}: ledger net {t.net_pnl} != deals {net}")
    for position_id in open_ids:
        t = trades.get(position_id)
        if t is None or t.status is not TradeStatus.OPEN:
            report.violations.append(f"open position {position_id} has no OPEN trade")
    known = {d.position_id for d in deals}
    for position_id in trades.keys() - known:
        report.violations.append(f"trade for position {position_id} has no broker deals")


async def _check_step(
    report: ReplayReport, broker: SimBroker, factory: sessionmaker[Session], magic: int, max_per_symbol: int
) -> None:
    positions = [p for p in await broker.positions() if p.magic == magic]
    per_symbol = Counter(p.symbol for p in positions)
    report.max_positions_per_symbol = max([report.max_positions_per_symbol, *per_symbol.values()])
    for symbol, count in per_symbol.items():
        if count > max_per_symbol:
            report.violations.append(f"{symbol}: {count} engine positions open at once")
    if positions:
        with factory() as s:
            linked = set(
                s.scalars(
                    select(OrderIntentRow.position_id).where(
                        OrderIntentRow.kind == IntentKind.OPEN, OrderIntentRow.status == IntentStatus.FILLED
                    )
                ).all()
            )
        for p in positions:
            if p.position_id not in linked:
                report.violations.append(f"orphan position {p.position_id} ({p.symbol}) has no FILLED intent")


def _check_final(report: ReplayReport, factory: sessionmaker[Session]) -> None:
    with factory() as s:
        dupes = s.execute(
            select(DecisionRow.symbol, DecisionRow.bar_time, func.count())
            .group_by(DecisionRow.symbol, DecisionRow.trigger_tf, DecisionRow.bar_time)
            .having(func.count() > 1)
        ).all()
        dangling = s.scalar(
            select(func.count())
            .select_from(OrderIntentRow)
            .where(
                OrderIntentRow.status.in_([IntentStatus.PENDING, IntentStatus.SENT, IntentStatus.RETRYING])
            )
        )
        decisions = s.scalar(select(func.count()).select_from(DecisionRow))
    for symbol, bar_time, n in dupes:
        report.violations.append(f"{symbol} {bar_time}: {n} decision rows for one bar")
    if dangling:
        report.violations.append(f"{dangling} intents left in a non-final sending state")
    if decisions != report.bar_events:
        report.violations.append(f"{decisions} decision rows for {report.bar_events} bar events")
