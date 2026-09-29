"""Read endpoints (roadmap 6.2, docs/05 §3): system, account, equity, positions, trades, decisions, virtual
trades, bars, LLM usage and logs. Everything comes from the database; nothing here changes trading state."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from aifund.agents.prompting import released_templates
from aifund.api import context as ctx
from aifund.api.auth import Authenticated
from aifund.api.schemas import (
    AccountOut,
    BarOut,
    DealOut,
    DecisionDossier,
    DecisionOut,
    EngineStateOut,
    EquityPoint,
    HeartbeatOut,
    IntentOut,
    LimitHeadroom,
    LLMCallOut,
    LLMUsageRow,
    LogLine,
    Marker,
    Page,
    PositionOut,
    SystemOut,
    TradeDossier,
    TradeOut,
    Versions,
    VirtualTradeOut,
)
from aifund.domain.enums import Side
from aifund.market.feature_registry import FEATURE_SET_VERSION
from aifund.persistence.repositories.api import BarCacheRepository
from aifund.persistence.repositories.dashboard import DashboardQueries
from aifund.persistence.tables import DecisionRow, TradeRow
from aifund.risk.limits import trading_day_start

router = APIRouter(prefix="/api", tags=["read"])
Limit = Annotated[int, Query(ge=1, le=500)]
ENGINE_STALE = timedelta(seconds=30)


def _age(now: datetime, then: datetime) -> float:
    return round((now - then).total_seconds(), 1)


@router.get("/system")
def system(request: Request, _s: Authenticated) -> SystemOut:
    cfg = ctx.state(request).config.current().config
    now = ctx.clock(request).now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with ctx.read(request) as s:
        q = DashboardQueries(s)
        engine = next((e for e in q.engine_states() if e.account_id == cfg.engine.account_label), None)
        beats = [
            HeartbeatOut.model_validate(h).model_copy(update={"age_s": _age(now, h.last_beat_at)})
            for h in q.heartbeats()
        ]
        latest = q.latest_config()
        spent = q.llm_spend(day)
    engine_beat = next((b for b in beats if b.component == "engine"), None)
    return SystemOut(
        now=now,
        engine=EngineStateOut.model_validate(engine) if engine else None,
        engine_stale=engine_beat is None or now - engine_beat.last_beat_at > ENGINE_STALE,
        heartbeats=beats,
        llm_spent_today_usd=spent,
        llm_budget_usd=cfg.llm.daily_budget_usd,
        versions=Versions(
            config_version=latest.id if latest else None,
            config_sha256=latest.sha256 if latest else None,
            feature_set=FEATURE_SET_VERSION,
            prompts=sorted(released_templates()),
            rulebook=engine.rulebook_version if engine else 0,
        ),
    )


@router.get("/account")
def account(request: Request, _s: Authenticated) -> AccountOut:
    cfg = ctx.state(request).config.current().config
    limits = cfg.risk.limits
    with ctx.read(request) as s:
        q = DashboardQueries(s)
        snap = q.latest_equity(cfg.engine.account_label)
        engine = next((e for e in q.engine_states() if e.account_id == cfg.engine.account_label), None)
    if snap is None:
        return AccountOut(
            as_of=None,
            balance=None,
            equity=None,
            margin=None,
            free_margin=None,
            day_pnl=None,
            drawdown_pct=None,
            open_risk_money=None,
            open_positions=0,
            heat_pct=None,
            limits=[],
        )
    heat = (snap.open_risk_money / snap.equity * 100).quantize(Decimal("0.01")) if snap.equity > 0 else None

    def loss(ref: Decimal | None) -> Decimal:
        return (
            max(Decimal(0), (ref - snap.equity) / ref * 100).quantize(Decimal("0.01")) if ref else Decimal(0)
        )

    headroom = [
        LimitHeadroom(
            name="daily_loss",
            used_pct=loss(engine.day_start_equity if engine else None),
            limit_pct=limits.daily_loss_limit_pct,
        ),
        LimitHeadroom(
            name="weekly_loss",
            used_pct=loss(engine.week_start_equity if engine else None),
            limit_pct=limits.weekly_loss_limit_pct,
        ),
        LimitHeadroom(name="drawdown", used_pct=snap.drawdown_pct, limit_pct=limits.max_drawdown_pct),
        LimitHeadroom(
            name="portfolio_heat", used_pct=heat or Decimal(0), limit_pct=limits.max_portfolio_heat_pct
        ),
    ]
    return AccountOut(
        as_of=snap.ts,
        balance=snap.balance,
        equity=snap.equity,
        margin=snap.margin,
        free_margin=snap.free_margin,
        day_pnl=snap.day_pnl,
        drawdown_pct=snap.drawdown_pct,
        open_risk_money=snap.open_risk_money,
        open_positions=snap.open_positions,
        heat_pct=heat,
        limits=headroom,
    )


@router.get("/equity")
def equity(
    request: Request,
    _s: Authenticated,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    granularity: Annotated[int, Query(ge=1, le=1440)] = 5,
) -> list[EquityPoint]:
    now = ctx.clock(request).now()
    end = end or now + timedelta(minutes=1)
    start = start or end - timedelta(days=30)
    with ctx.read(request) as s:
        rows = DashboardQueries(s).equity(ctx.account(request), start, end)
    last: dict[int, EquityPoint] = {}  # one point per ``granularity``-minute bucket: the last in it
    for r in rows:
        last[int(r.ts.timestamp()) // (granularity * 60)] = EquityPoint.model_validate(r)
    return list(last.values())


@router.get("/positions")
def positions(request: Request, _s: Authenticated) -> list[PositionOut]:
    now = ctx.clock(request).now()
    with ctx.read(request) as s:
        q, cache = DashboardQueries(s), BarCacheRepository(s)
        trades = q.open_trades(ctx.account(request))
        decisions = q.decisions_by_id([t.decision_id for t in trades if t.decision_id])
        out = []
        for t in trades:
            last = cache.latest(t.symbol)
            price = last.close if last is not None else None
            r_now = None
            risk = abs(t.open_price - t.initial_sl) if t.initial_sl is not None else None
            if price is not None and risk:
                sign = 1 if t.side is Side.BUY else -1
                r_now = (sign * (price - t.open_price) / risk).quantize(Decimal("0.01"))
            decision = decisions.get(t.decision_id or "")
            thesis = (decision.proposal or {}).get("thesis") if decision else None
            out.append(
                PositionOut(
                    trade_id=t.id,
                    position_id=t.position_id,
                    symbol=t.symbol,
                    side=t.side.value,
                    status=t.status.value,
                    volume=t.volume_open_now,
                    open_price=t.open_price,
                    open_time=t.open_time,
                    sl=t.current_sl,
                    tp=t.current_tp,
                    setup_tag=t.setup_tag,
                    last_price=price,
                    r_now=r_now,
                    age_minutes=int((now - t.open_time).total_seconds() // 60),
                    decision_id=t.decision_id,
                    thesis=thesis if isinstance(thesis, str) else None,
                )
            )
    return out


def _cursor(items: list[Any], limit: int) -> str | None:
    return items[-1].id if len(items) == limit else None


@router.get("/trades")
def trades(
    request: Request,
    _s: Authenticated,
    symbol: str | None = None,
    setup: str | None = None,
    outcome: str | None = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: str | None = None,
    limit: Limit = 50,
) -> Page[TradeOut]:
    with ctx.read(request) as s:
        rows = DashboardQueries(s).trades(
            ctx.account(request),
            symbol=symbol,
            setup=setup,
            outcome=outcome,
            start=start,
            end=end,
            cursor=cursor,
            limit=limit,
        )
        return Page(items=[TradeOut.model_validate(r) for r in rows], next_cursor=_cursor(rows, limit))


def _dossier(q: DashboardQueries, d: DecisionRow) -> DecisionDossier:
    snap = q.snapshot(d.snapshot_id)
    return DecisionDossier.model_validate(d).model_copy(
        update={
            "features": snap.features if snap is not None else None,
            "llm_calls": [LLMCallOut.model_validate(c) for c in q.llm_calls_for(d.id)],
            "intents": [IntentOut.model_validate(i) for i in q.intents_for(d.id)],
        }
    )


@router.get("/trades/{trade_id}")
def trade(request: Request, trade_id: str, _s: Authenticated) -> TradeDossier:
    with ctx.read(request) as s:
        q = DashboardQueries(s)
        t = q.trade(trade_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such trade")
        decision = q.decision(t.decision_id) if t.decision_id else None
        deals = [DealOut.model_validate(d) for d in q.deals(t.position_id)]
        bars = _chart_bars(s, t)
        dossier = TradeDossier.model_validate(t).model_copy(
            update={"decision": _dossier(q, decision) if decision else None, "deals": deals, "bars": bars}
        )
    markers = [
        Marker(
            time=d.time_utc,
            kind="entry" if d.entry == "IN" else "exit",
            price=d.price,
            label=f"{d.entry} {d.volume} @ {d.price} ({d.reason})",
        )
        for d in deals
    ]
    return dossier.model_copy(update={"markers": markers})


def _chart_bars(s: Any, t: TradeRow) -> list[BarOut]:
    tf = t.trigger_tf or "M15"
    minutes = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}.get(tf, 15)
    start = t.open_time - timedelta(minutes=minutes * 60)
    end = (t.close_time or t.open_time) + timedelta(minutes=minutes * 20)
    return [BarOut.model_validate(b) for b in BarCacheRepository(s).between(t.symbol, tf, start, end)]


@router.get("/decisions")
def decisions(
    request: Request,
    _s: Authenticated,
    symbol: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    cursor: str | None = None,
    limit: Limit = 50,
) -> Page[DecisionOut]:
    with ctx.read(request) as s:
        rows = DashboardQueries(s).decisions(
            ctx.account(request),
            symbol=symbol,
            outcome=outcome,
            reason=reason,
            start=start,
            end=end,
            cursor=cursor,
            limit=limit,
        )
        return Page(items=[DecisionOut.model_validate(r) for r in rows], next_cursor=_cursor(rows, limit))


@router.get("/decisions/{decision_id}")
def decision(request: Request, decision_id: str, _s: Authenticated) -> DecisionDossier:
    with ctx.read(request) as s:
        q = DashboardQueries(s)
        d = q.decision(decision_id)
        if d is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such decision")
        return _dossier(q, d)


@router.get("/virtual-trades")
def virtual_trades(
    request: Request,
    _s: Authenticated,
    arm: str | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    symbol: str | None = None,
    cursor: str | None = None,
    limit: Limit = 50,
) -> Page[VirtualTradeOut]:
    with ctx.read(request) as s:
        rows = DashboardQueries(s).virtual_trades(
            ctx.account(request), arm=arm, status=status_, symbol=symbol, cursor=cursor, limit=limit
        )
        return Page(items=[VirtualTradeOut.model_validate(r) for r in rows], next_cursor=_cursor(rows, limit))


@router.get("/bars")
def bars(
    request: Request,
    _s: Authenticated,
    symbol: str,
    tf: str = "M15",
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
) -> list[BarOut]:
    end = end or ctx.clock(request).now()
    start = start or end - timedelta(days=2)
    with ctx.read(request) as s:
        return [BarOut.model_validate(b) for b in BarCacheRepository(s).between(symbol, tf, start, end)]


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


@router.get("/llm/usage")
def llm_usage(
    request: Request,
    _s: Authenticated,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
) -> list[LLMUsageRow]:
    end = end or ctx.clock(request).now() + timedelta(minutes=1)
    start = start or end - timedelta(days=1)
    with ctx.read(request) as s:
        calls = DashboardQueries(s).llm_calls(start, end)
    groups: defaultdict[tuple[str, str], list[Any]] = defaultdict(list)
    for c in calls:
        groups[(c.agent, c.model)].append(c)
    out = []
    for (agent, model), cs in sorted(groups.items()):
        prompt = sum(c.prompt_tokens for c in cs)
        latencies = [c.latency_ms for c in cs if c.latency_ms is not None]
        out.append(
            LLMUsageRow(
                agent=agent,
                model=model,
                calls=len(cs),
                errors=sum(1 for c in cs if not c.valid),
                prompt_tokens=prompt,
                completion_tokens=sum(c.completion_tokens for c in cs),
                cache_hit_rate=round(sum(c.cached_tokens for c in cs) / prompt, 4) if prompt else 0.0,
                cost_usd=sum((c.cost_usd or Decimal(0) for c in cs), Decimal(0)),
                latency_p50_ms=_percentile(latencies, 0.5),
                latency_p95_ms=_percentile(latencies, 0.95),
            )
        )
    return out


@router.get("/logs")
def logs(
    request: Request,
    _s: Authenticated,
    level: str | None = None,
    component: str | None = None,
    since: str | None = None,
    limit: Limit = 200,
) -> list[LogLine]:
    """The tail of the engine's JSON log (read-only), newest last."""
    path = ctx.state(request).log_file
    if not path.is_file():
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        start = max(0, fh.tell() - 512_000)
        fh.seek(start)
        tail = fh.read().decode("utf-8", errors="replace").splitlines()
    if start > 0:
        tail = tail[1:]  # the first line was cut by the seek
    out: list[LogLine] = []
    for line in tail:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if level and data.get("level") != level:
            continue
        if component and data.get("component") != component:
            continue
        if since and str(data.get("timestamp", "")) <= since:
            continue
        out.append(
            LogLine(
                ts=data.get("timestamp"),
                level=data.get("level"),
                component=data.get("component"),
                event=data.get("event"),
                data=data,
            )
        )
    return out[-limit:]


def trading_day(request: Request) -> datetime:
    engine = ctx.state(request).config.current().config.engine
    return trading_day_start(
        ctx.clock(request).now(), engine.trading_day_boundary, engine.trading_day_timezone
    )
