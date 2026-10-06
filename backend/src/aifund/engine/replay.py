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
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.sim_broker import SimBroker
from aifund.domain.enums import (
    CloseReason,
    DealEntry,
    IntentKind,
    IntentStatus,
    ReasonCode,
    Side,
    TradeStatus,
    VirtualArm,
)
from aifund.domain.market import Deal
from aifund.domain.trade import gets_virtual_trade
from aifund.engine.equity import EquitySnapshotter
from aifund.engine.pipeline import DecisionPipeline
from aifund.engine.position_loop import PositionLoop
from aifund.market.bar_clock import BarClock
from aifund.persistence.tables import (
    DecisionRow,
    EquitySnapshotRow,
    OrderIntentRow,
    TradeRow,
    VirtualTradeRow,
)
from aifund.reconcile.enrichment import Enricher
from aifund.reconcile.reconciler import Reconciler, ReconcileReport
from aifund.reconcile.virtual import VirtualTracker


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
    enriched: int | None = None  # closed trades with MAE/MFE etc. (None: no enricher in this run)
    mean_mae_r: Decimal | None = None
    mean_mfe_r: Decimal | None = None
    virtual: Counter[str] | None = None  # virtual trades by status (None: no tracker in this run)
    virtual_arms: Counter[str] | None = None  # by arm: BLOCKED, SHADOW_BASELINE, SHADOW_ANALYST
    virtual_mean_r: Decimal | None = None
    snapshots: int = 0
    final_equity: Decimal | None = None
    max_drawdown_pct: Decimal = Decimal(0)
    worst_day_pnl: Decimal = Decimal(0)
    breaches: Counter[str] = field(default_factory=Counter)
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
            "enrichment: "
            + (
                "no enricher"
                if self.enriched is None
                else f"{self.enriched} trades; mean MAE {self.mean_mae_r}R, mean MFE {self.mean_mfe_r}R"
            ),
            "virtual: "
            + (
                "no tracker"
                if self.virtual is None
                else (", ".join(f"{k}={v}" for k, v in self.virtual.most_common()) or "none")
                + (f"; mean R of finished {self.virtual_mean_r}" if self.virtual_mean_r is not None else "")
            ),
            "equity: "
            + (
                "no snapshotter"
                if self.final_equity is None
                else f"{self.snapshots} snapshots; final {self.final_equity}, max drawdown "
                f"{self.max_drawdown_pct}%, worst day {self.worst_day_pnl}; limit breaches "
                + (", ".join(f"{k}={v}" for k, v in self.breaches.most_common()) or "none")
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
    family_of: Callable[[str], str] | None = None,  # family slots (10.8): at most one per (symbol, family)
    position_loop: PositionLoop | None = None,
    reconciler: Reconciler | None = None,
    enricher: Enricher | None = None,
    virtual_tracker: VirtualTracker | None = None,
    equity_snapshotter: EquitySnapshotter | None = None,
    snapshot_every: timedelta = timedelta(seconds=60),
    progress: Callable[[str], None] = lambda _msg: None,
) -> ReplayReport:
    report = ReplayReport(start=clock.now(), end=end)
    last_day = None
    next_snapshot = clock.now()
    if equity_snapshotter is not None:
        await equity_snapshotter.start()
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
        if enricher is not None:
            for message in (await enricher.run_once()).errors:
                report.violations.append(f"enricher error: {message}")
        if virtual_tracker is not None:
            for message in (await virtual_tracker.run_once()).errors:
                report.violations.append(f"virtual tracker error: {message}")
        if equity_snapshotter is not None and clock.now() >= next_snapshot:
            next_snapshot = clock.now() + snapshot_every
            snap = await equity_snapshotter.run_once()
            report.violations += [f"snapshotter error: {m}" for m in snap.errors]
            if snap.alerted and snap.breach is not None:
                report.breaches[snap.breach.kind.value] += 1
        await _check_step(report, broker, factory, magic, max_positions_per_symbol, family_of)
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
    if enricher is not None:
        _check_enrichment(report, factory, clock.now() - timedelta(minutes=1))
    if virtual_tracker is not None:
        _check_virtual(report, factory)
    if equity_snapshotter is not None:
        await _check_equity(report, factory, broker)
    return report


async def _check_equity(report: ReplayReport, factory: sessionmaker[Session], broker: SimBroker) -> None:
    """The equity curve: one snapshot per step, the last one matching the broker, drawdown and day P&L."""
    with factory() as s:
        rows = s.scalars(select(EquitySnapshotRow).order_by(EquitySnapshotRow.id)).all()
    report.snapshots = len(rows)
    if not rows:
        report.violations.append("no equity snapshots")
        return
    report.final_equity = rows[-1].equity
    report.max_drawdown_pct = max(r.drawdown_pct for r in rows)
    report.worst_day_pnl = min(r.day_pnl for r in rows)
    account = await broker.account_info()
    if rows[-1].balance != account.balance:
        report.violations.append(f"last snapshot balance {rows[-1].balance} != broker {account.balance}")


def _analyst_verdict(proposal: dict[str, Any] | None) -> str | None:
    if not proposal:
        return None
    analyst = proposal.get("shadow_analyst", proposal)
    return analyst.get("verdict") if isinstance(analyst, dict) else None


def _check_virtual(report: ReplayReport, factory: sessionmaker[Session]) -> None:
    """BLOCKED virtual trades exist only for qualifying blocked signals; G-LLM shadows only where the analyst
    decided on a bar with a candidate (SHADOW_ANALYST only for an analyst PROPOSAL); every finished one is
    consistent."""
    with factory() as s:
        rows = s.scalars(select(VirtualTradeRow)).all()
        decisions = {d.id: d for d in s.scalars(select(DecisionRow)).all()}
    report.virtual = Counter(v.status.value for v in rows)
    report.virtual_arms = Counter(v.arm.value for v in rows)
    finished = []
    for v in rows:
        d = decisions[v.decision_id]
        reason = ReasonCode(d.reason_code) if d.reason_code else None
        if v.arm is VirtualArm.BLOCKED:
            if not gets_virtual_trade(d.outcome, reason) or v.blocked_by != d.reason_code:
                report.violations.append(f"virtual trade {v.id} for a {d.outcome} / {d.reason_code} decision")
        elif _analyst_verdict(d.proposal) is None or not d.setups:  # the analyst ran (even if it failed)
            report.violations.append(f"{v.arm} {v.id} on a decision without an analyst or a setup")
        elif v.arm is VirtualArm.SHADOW_ANALYST and _analyst_verdict(d.proposal) != "PROPOSAL":
            report.violations.append(f"{v.arm} {v.id} although the analyst did not propose a trade")
        elif v.arm is VirtualArm.SHADOW_COMMITTEE and not ((d.proposal or {}).get("committee") or {}).get(
            "tradable"
        ):
            report.violations.append(f"{v.arm} {v.id} although the committee did not decide a trade")
        if v.r_multiple is None:
            continue
        finished.append(v.r_multiple)
        rr = (v.tp_distance / v.sl_distance).quantize(Decimal("0.0001"))
        ok = {
            CloseReason.SL: v.r_multiple <= Decimal("-1"),
            CloseReason.TP: v.r_multiple == rr,
        }.get(v.exit_reason, Decimal("-1") <= v.r_multiple <= rr)  # type: ignore[arg-type]
        if not ok:
            report.violations.append(f"virtual {v.id}: {v.exit_reason} at {v.r_multiple}R (RR {rr})")
        if v.mae_r is None or v.mfe_r is None or not v.mae_r <= v.r_multiple <= v.mfe_r:
            report.violations.append(f"virtual {v.id}: R {v.r_multiple} outside [{v.mae_r}, {v.mfe_r}]")
    if finished:
        report.virtual_mean_r = (sum(finished, Decimal(0)) / len(finished)).quantize(Decimal("0.01"))


def _check_enrichment(report: ReplayReport, factory: sessionmaker[Session], settled_before: datetime) -> None:
    """Every settled closed trade is enriched, and its exit lies within its own excursions."""
    with factory() as s:
        closed = [t for t in s.scalars(select(TradeRow)).all() if t.close_time is not None]
    done = [t for t in closed if t.enriched_at is not None]
    report.enriched = len(done)
    for t in closed:
        if t.enriched_at is None and t.close_time is not None and t.close_time < settled_before:
            report.violations.append(f"trade {t.position_id} closed {t.close_time} but was never enriched")
    for t in done:
        if t.mae_price is None or t.mfe_price is None or t.close_price_vwap is None:
            report.violations.append(f"trade {t.position_id}: enriched without excursions")
            continue
        realised = (t.close_price_vwap - t.open_price) * (1 if t.side is Side.BUY else -1)
        if not -t.mae_price <= realised <= t.mfe_price:
            report.violations.append(
                f"trade {t.position_id}: exit move {realised} outside [-{t.mae_price}, {t.mfe_price}]"
            )
        if t.mae_r is not None and t.mfe_r is not None and not t.mae_r <= 0 <= t.mfe_r:
            report.violations.append(f"trade {t.position_id}: MAE {t.mae_r}R / MFE {t.mfe_r}R signs")
    maes = [t.mae_r for t in done if t.mae_r is not None]
    mfes = [t.mfe_r for t in done if t.mfe_r is not None]
    cent = Decimal("0.01")
    if maes:
        report.mean_mae_r = (sum(maes, Decimal(0)) / len(maes)).quantize(cent)
    if mfes:
        report.mean_mfe_r = (sum(mfes, Decimal(0)) / len(mfes)).quantize(cent)


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
    report: ReplayReport,
    broker: SimBroker,
    factory: sessionmaker[Session],
    magic: int,
    max_per_symbol: int,
    family_of: Callable[[str], str] | None = None,
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
        if family_of is not None:  # family slots (10.8): never two positions of one family on a symbol
            with factory() as s:
                rows = s.execute(
                    select(OrderIntentRow.position_id, OrderIntentRow.setup_tag).where(
                        OrderIntentRow.kind == IntentKind.OPEN,
                        OrderIntentRow.position_id.in_([p.position_id for p in positions]),
                    )
                ).all()
            tags: dict[int | None, str | None] = {pid: tag for pid, tag in rows}
            families = Counter((p.symbol, family_of(tags.get(p.position_id) or "?")) for p in positions)
            for (symbol, family), count in families.items():
                if count > 1:
                    report.violations.append(f"{symbol}: {count} positions of the {family} family at once")


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
