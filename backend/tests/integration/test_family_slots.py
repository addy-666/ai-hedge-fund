"""Several positions per symbol (roadmap 10.8-10.10): two strategies of different families trade the same
symbol, hedges included, through the whole stack (pipeline, guards, executor, SimBroker, reconciler), and
every invariant holds: at most one position per (symbol, family), the count cap, no orphans, a ledger that
matches the broker's deals. The per-strategy daily cap and a hedge-off configuration are enforced."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from decimal import Decimal as D
from itertools import combinations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.config.loader import load_trading_config
from aifund.config.trading_config import RiskConfig, StrategyConfig, StrategyDailyCap, SymbolConfig
from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction, IntentKind, IntentStatus, ReasonCode
from aifund.persistence.tables import OrderIntentRow, TradeRow
from aifund.strategies.base import TfRoles
from tests.integration.test_baseline_replay import CONFIG, market, replay  # noqa: F401

pytestmark = pytest.mark.scenario
BASELINE = StrategyConfig(analyst_enabled=False, baseline_enabled=True)
FAMILIES = {"trend": ["stub_trend"], "reversal": ["stub_fade"]}
FILLED_OPEN = (OrderIntentRow.kind == IntentKind.OPEN) & (OrderIntentRow.status == IntentStatus.FILLED)


class Stub:
    """Fires every hour at ``minute``: always ``direction`` (one family's strategy)."""

    def __init__(self, tag: str, minute: int, direction: Direction) -> None:
        self.setup_tag = self.playbook_id = tag
        self.version = "test"
        self.minute, self.direction = minute, direction

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        closed_at = snapshot.bar_time + timedelta(minutes=15)
        if closed_at.minute != self.minute:
            return []
        return [SetupCandidate(setup_tag=self.setup_tag, playbook_id=self.playbook_id,
                               direction_hint=self.direction, strength=0.5)]  # fmt: skip


def two_families(_s: SymbolConfig, _r: TfRoles) -> list[Stub]:
    return [Stub("stub_trend", 0, Direction.LONG), Stub("stub_fade", 30, Direction.SHORT)]


def risk(**guards: object) -> RiskConfig:
    base = load_trading_config(CONFIG).config.risk
    loose = base.guards.model_copy(update={"cooldown_bars_after_close": 0, "cooldown_bars_after_loss": 0,
                                           **guards})  # fmt: skip
    limits = base.limits.model_copy(update={"max_symbol_heat_pct": D("1.5")})
    return base.model_copy(update={"guards": loose, "limits": limits,
                                   "news": base.news.model_copy(update={"enabled": False})})  # fmt: skip


def overlapping_hedges(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        trades = s.scalars(select(TradeRow)).all()
    far = max(t.open_time for t in trades) + timedelta(days=1)
    return sum(
        1
        for a, b in combinations(trades, 2)
        if a.side != b.side and a.open_time < (b.close_time or far) and b.open_time < (a.close_time or far)
    )


async def test_two_families_hold_positions_side_by_side_and_hedge(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
) -> None:
    report = await replay(market, factory, strategy=BASELINE, detectors=two_families, families=FAMILIES,
                          risk_config=risk())  # fmt: skip
    print(report.render())
    assert report.violations == []  # incl. never two positions of one family on the symbol
    assert report.max_positions_per_symbol == 2
    assert overlapping_hedges(factory) >= 1  # a long trend and a short fade open at the same time
    with factory() as s:
        tags = Counter(s.scalars(select(OrderIntentRow.setup_tag).where(FILLED_OPEN)).all())
    assert set(tags) == {"stub_trend", "stub_fade"}
    assert ReasonCode.HEDGE_OFF.value not in report.reasons


async def test_with_hedging_off_an_opposite_family_waits(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
) -> None:
    report = await replay(market, factory, strategy=BASELINE, detectors=two_families, families=FAMILIES,
                          risk_config=risk(hedge_across_families=False))  # fmt: skip
    assert report.violations == []
    assert report.reasons[ReasonCode.HEDGE_OFF.value] >= 1
    assert overlapping_hedges(factory) == 0


async def test_the_strategy_cap_holds_each_strategy_to_its_daily_count(
    market: dict,  # type: ignore[type-arg]  # noqa: F811
    factory: sessionmaker[Session],
) -> None:
    capped = risk()
    capped = capped.model_copy(update={"limits": capped.limits.model_copy(
        update={"strategy_daily_cap": StrategyDailyCap(base=1, max=1)})})  # fmt: skip
    report = await replay(market, factory, strategy=BASELINE, detectors=two_families, families=FAMILIES,
                          risk_config=capped)  # fmt: skip
    assert report.violations == []
    assert report.reasons[ReasonCode.DAILY_STRATEGY_CAP.value] >= 1
    with factory() as s:
        query = select(OrderIntentRow.setup_tag, OrderIntentRow.created_at).where(FILLED_OPEN)
        opens = s.execute(query).all()
    per_day = Counter((tag, (at - timedelta(hours=21)).date()) for tag, at in opens)  # NY 17:00 = 21:00 UTC
    assert per_day
    assert max(per_day.values()) == 1
