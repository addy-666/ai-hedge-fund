"""Cross-asset view (roadmap 10.6): the instruments every decision sees, and what each traded symbol's
newest decision saw of them (``xa.*`` features from its snapshot). Read-only."""

from __future__ import annotations

from fastapi import APIRouter, Request

from aifund.api import context as ctx
from aifund.api.auth import Authenticated
from aifund.api.schemas import CrossAssetOut, IntermarketCell, IntermarketRow
from aifund.market.feature_registry import BASES as XA_BASES
from aifund.market.feature_registry import xa_name
from aifund.persistence.repositories.dashboard import DashboardQueries

router = APIRouter(prefix="/api", tags=["cross-asset"])


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@router.get("/cross-asset")
def cross_asset(request: Request, _s: Authenticated) -> CrossAssetOut:
    cfg = ctx.state(request).config.current().config
    instruments = cfg.instruments()
    rows = []
    with ctx.read(request) as s:
        q = DashboardQueries(s)
        for sym in cfg.symbols:
            snap = q.latest_snapshot(ctx.account(request), sym.broker)
            if snap is None or not instruments:
                continue
            features = snap.features or {}
            cells = []
            for inst in instruments:
                if inst.broker == sym.broker:
                    continue
                values = {base: features.get(xa_name(inst.slug, base)) for base in XA_BASES}
                stack = values["ema_stack"]
                cells.append(
                    IntermarketCell(
                        instrument=inst.canonical,
                        open=any(v is not None for v in values.values()),
                        corr100=_float(values["corr100"]),
                        ret24_z=_float(values["ret24_z"]),
                        ema_stack=stack if isinstance(stack, str) else None,
                    )
                )
            rows.append(IntermarketRow(symbol=sym.canonical, bar_time=snap.bar_time, cells=cells))
    ca = cfg.cross_asset
    return CrossAssetOut(
        enabled=ca.enabled,
        timeframe=ca.timeframe.value,
        max_age_minutes=ca.max_age_minutes,
        instruments=[i.canonical for i in instruments],
        references=[i.canonical for i in instruments if not i.traded],
        rows=rows,
    )
