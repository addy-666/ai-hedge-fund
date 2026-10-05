"""Analytics endpoints (roadmap 6.9, docs/05 §3): summary, breakdowns and costs over closed trades."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from aifund.api import context as ctx
from aifund.api.auth import Authenticated
from aifund.api.schemas import (
    ArmStat,
    BreakdownRow,
    CalibrationBin,
    CalibrationModelOut,
    CalibrationOut,
    CalibrationSourceOut,
    ChallengerComparison,
    CommitteeComparison,
    Costs,
    Summary,
    UpliftStat,
)
from aifund.domain.enums import CalibrationStatus
from aifund.persistence.repositories.calibration import SOURCES, CalibrationRepository
from aifund.persistence.repositories.dashboard import DashboardQueries
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.virtual import VirtualTradeRepository
from aifund.persistence.tables import CalibrationModelRow, DecisionRow, FeatureSnapshotRow, TradeRow
from aifund.research.committee import compare
from aifund.rules.calibration import IsotonicMap, Sample, brier, reliability
from aifund.stats import Summary as Stats

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
From = Annotated[datetime | None, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]


class Dimension(StrEnum):
    symbol = "symbol"
    setup_tag = "setup_tag"
    session = "session"
    regime = "regime"
    direction = "direction"
    confidence_bucket = "confidence_bucket"
    rulebook_version = "rulebook_version"


def _window(request: Request, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or ctx.clock(request).now() + timedelta(minutes=1)
    return start or end - timedelta(days=90), end


def _load(
    request: Request, start: datetime, end: datetime
) -> tuple[list[TradeRow], dict[str, DecisionRow], dict[str, FeatureSnapshotRow]]:
    with ctx.read(request) as s:
        q = DashboardQueries(s)
        trades = list(q.closed_trades(ctx.account(request), start, end))
        decisions = q.decisions_by_id([t.decision_id for t in trades if t.decision_id])
        snaps = q.snapshots_by_id([d.snapshot_id for d in decisions.values() if d.snapshot_id])
        for row in (*trades, *decisions.values(), *snaps.values()):
            s.expunge(row)
    return trades, decisions, snaps


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return (sum(values, Decimal(0)) / len(values)).quantize(Decimal("0.0001")) if values else None


def summarize(trades: Sequence[TradeRow], days: float) -> Summary:
    nets = [t.net_pnl or Decimal(0) for t in trades]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    wins = sum(n for n in nets if n > 0)
    losses = -sum(n for n in nets if n < 0)
    equity, peak, max_dd = Decimal(0), Decimal(0), Decimal(0)
    daily: defaultdict[str, Decimal] = defaultdict(Decimal)
    for t, n in zip(trades, nets, strict=True):
        equity += n
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if t.close_time is not None:
            daily[f"{t.close_time:%Y-%m-%d}"] += n
    sharpe = None
    if len(daily) >= 2:
        values = [float(v) for v in daily.values()]
        mu = sum(values) / len(values)
        sd = math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1))
        sharpe = round(mu / sd * math.sqrt(252), 3) if sd > 0 else None
    costs = [(t.commission or 0) + (t.swap or 0) + (t.fee or 0) for t in trades]
    return Summary(
        trades=len(trades),
        net_pnl=sum(nets, Decimal(0)),
        r_total=sum(rs, Decimal(0)),
        expectancy_r=_mean(rs),
        win_rate=round(sum(1 for n in nets if n > 0) / len(nets), 4) if nets else None,
        profit_factor=round(float(wins / losses), 3) if losses > 0 else None,
        max_drawdown=max_dd,
        sharpe_daily=sharpe,
        trades_per_day=round(len(trades) / max(days, 1.0), 3),
        avg_cost=_mean([Decimal(c) for c in costs]),
    )


@router.get("/summary")
def summary(request: Request, _s: Authenticated, start: From = None, end: To = None) -> Summary:
    start, end = _window(request, start, end)
    trades, _, _ = _load(request, start, end)
    return summarize(trades, (end - start).total_seconds() / 86400)


def _key(
    dim: Dimension, decisions: dict[str, DecisionRow], snaps: dict[str, FeatureSnapshotRow]
) -> Callable[[TradeRow], str]:
    def feature(t: TradeRow, name: str) -> str:
        d = decisions.get(t.decision_id or "")
        snap = snaps.get(d.snapshot_id or "") if d else None
        value: Any = snap.features.get(name) if snap is not None else None
        return "unknown" if value is None else str(value)

    def confidence(t: TradeRow) -> str:
        d = decisions.get(t.decision_id or "")
        if d is None or d.final_confidence is None:
            return "unknown"
        lo = d.final_confidence // 10 * 10
        return f"{lo}-{lo + 9}"

    keys: dict[Dimension, Callable[[TradeRow], str]] = {
        Dimension.symbol: lambda t: t.symbol,
        Dimension.setup_tag: lambda t: t.setup_tag or "unknown",
        Dimension.session: lambda t: feature(t, "ctx.session"),
        Dimension.regime: lambda t: feature(t, "ctx.regime"),
        Dimension.direction: lambda t: "LONG" if t.side.value == "BUY" else "SHORT",
        Dimension.confidence_bucket: confidence,
        Dimension.rulebook_version: lambda t: str(
            getattr(decisions.get(t.decision_id or ""), "rulebook_version", None) or 0
        ),
    }
    return keys[dim]


@router.get("/breakdown")
def breakdown(
    request: Request, _s: Authenticated, dim: Dimension, start: From = None, end: To = None
) -> list[BreakdownRow]:
    start, end = _window(request, start, end)
    trades, decisions, snaps = _load(request, start, end)
    key = _key(dim, decisions, snaps)
    groups: defaultdict[str, list[TradeRow]] = defaultdict(list)
    for t in trades:
        groups[key(t)].append(t)
    out = []
    for name, ts in sorted(groups.items()):
        nets = [t.net_pnl or Decimal(0) for t in ts]
        out.append(
            BreakdownRow(
                key=name,
                trades=len(ts),
                net_pnl=sum(nets, Decimal(0)),
                expectancy_r=_mean([t.r_multiple for t in ts if t.r_multiple is not None]),
                win_rate=round(sum(1 for n in nets if n > 0) / len(nets), 4),
            )
        )
    return out


@router.get("/costs")
def costs(request: Request, _s: Authenticated, start: From = None, end: To = None) -> Costs:
    start, end = _window(request, start, end)
    trades, _, _ = _load(request, start, end)
    commission = sum((t.commission or Decimal(0) for t in trades), Decimal(0))
    swap = sum((t.swap or Decimal(0) for t in trades), Decimal(0))
    fee = sum((t.fee or Decimal(0) for t in trades), Decimal(0))
    entry = [t.entry_slippage_points for t in trades if t.entry_slippage_points is not None]
    exit_ = [t.exit_slippage_points for t in trades if t.exit_slippage_points is not None]
    return Costs(
        trades=len(trades),
        commission=commission,
        swap=swap,
        fee=fee,
        per_trade=((commission + swap + fee) / len(trades)).quantize(Decimal("0.01")) if trades else None,
        avg_entry_slippage_points=round(sum(entry) / len(entry), 2) if entry else None,
        avg_exit_slippage_points=round(sum(exit_) / len(exit_), 2) if exit_ else None,
    )


def _uplift(stats: Stats | None) -> UpliftStat | None:
    if stats is None:
        return None
    return UpliftStat(n=stats.n, mean_r=round(stats.mean, 4), ci_low=stats.ci_low, ci_high=stats.ci_high)


def _comparison(
    request: Request, start: datetime | None, end: datetime | None, contender: str
) -> dict[str, Any]:
    """The fields of a contender's comparison (committee or challenger) over the window."""
    start, end = _window(request, start, end)
    cfg = ctx.state(request).config.current().config
    account = ctx.account(request)
    with ctx.read(request) as s:
        bars = VirtualTradeRepository(s, ctx.clock(request)).contender_bars(account, start, end, contender)
        latest = EquitySnapshotRepository(s).latest(account)
    risk_usd = latest.equity * cfg.risk.risk_per_trade_pct / 100 if latest is not None else None
    report = compare(bars, risk_usd=risk_usd if risk_usd and risk_usd > 0 else None, contender=contender)
    days = (report.last - report.first).total_seconds() / 86400 if report.first and report.last else 0.0
    return dict(
        bars=report.bars,
        first=report.first,
        last=report.last,
        days=round(days, 2),
        arms=[
            ArmStat(
                arm=a.name,
                trades=a.trades,
                total_r=a.total_r,
                mean_r_per_trade=a.mean_r_per_trade,
                win_rate=a.win_rate,
                cost_usd=a.cost_usd,
            )
            for a in report.arms
        ],
        agreement=report.agreement,
        risk_usd=report.risk_usd.quantize(Decimal("0.01")) if report.risk_usd is not None else None,
        vs_analyst=_uplift(report.vs_analyst),
        vs_baseline=_uplift(report.vs_baseline),
    )


@router.get("/committee")
def committee(request: Request, _s: Authenticated, start: From = None, end: To = None) -> CommitteeComparison:
    """The committee's shadow record beside the analyst and the baseline (roadmap 8.4)."""
    mode = ctx.state(request).config.current().config.committee.mode
    return CommitteeComparison(mode=mode, **_comparison(request, start, end, "committee"))


@router.get("/challenger")
def challenger(
    request: Request, _s: Authenticated, start: From = None, end: To = None
) -> ChallengerComparison:
    """The challenger prompt's shadow record beside the analyst and the baseline: the prompt A/B (10.4)."""
    strategy = ctx.state(request).config.current().config.strategy
    version = strategy.challenger_prompt_version
    return ChallengerComparison(
        mode="shadow" if version is not None else "off",
        analyst_prompt=f"analyst_v{strategy.analyst_prompt_version}",
        challenger_prompt=f"analyst_v{version}" if version is not None else None,
        **_comparison(request, start, end, "challenger"),
    )


def _model(row: CalibrationModelRow) -> CalibrationModelOut:
    improvement = (row.details or {}).get("improvement")
    return CalibrationModelOut(
        version=row.version, source=row.source, method=row.method, status=row.status.value,
        n_samples=row.n_samples, brier_before=row.brier_before, brier_after=row.brier_after,
        improvement=round(improvement, 4) if isinstance(improvement, float) else None,
        points=[[float(x), float(y)] for x, y in (row.params or {}).get("points", [])],
        created_at=row.created_at, decided_by=row.decided_by, decided_at=row.decided_at,
    )  # fmt: skip


@router.get("/calibration")
def calibration(request: Request, _s: Authenticated) -> CalibrationOut:
    """Reliability per source over every finished sample, the active and waiting models, the Brier history."""
    cfg = ctx.state(request).config.current().config
    account = ctx.account(request)
    sources = []
    with ctx.read(request) as s:
        repo = CalibrationRepository(s, ctx.clock(request))
        for source in SOURCES:
            data = [Sample(c, r > 0, t) for c, r, t in repo.outcomes(account, source)]
            active = repo.active(source)
            waiting = repo.with_status(source, CalibrationStatus.CANDIDATE)
            model = IsotonicMap.from_params(active.params) if active is not None and active.params else None
            bins = [
                CalibrationBin(
                    **vars(b),
                    calibrated=model(round(b.mean_confidence))
                    if model and b.mean_confidence is not None
                    else None,
                )
                for b in reliability(data)
            ]
            sources.append(
                CalibrationSourceOut(
                    source=source, n=len(data), reliability=bins,
                    brier_raw=round(brier([x.confidence / 100 for x in data], [x.win for x in data]), 6)
                    if data else None,
                    active=_model(active) if active is not None else None,
                    candidate=_model(waiting[-1]) if waiting else None,
                )
            )  # fmt: skip
        models = [_model(r) for r in repo.history()]
    cal = cfg.learning.calibration
    return CalibrationOut(
        activation=cal.activation, min_samples=cal.min_samples, sources=sources, models=models
    )
