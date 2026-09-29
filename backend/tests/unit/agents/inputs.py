"""A fixed analyst input (XAUUSD, M15/H1/H4, one LONG mtf_trend_pullback candidate) for prompt and agent tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from aifund.agents.analyst import AnalystInput
from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction, Timeframe
from aifund.domain.market import Bar, Tick
from aifund.strategies.base import TfRoles

PLAYBOOKS = Path(__file__).resolve().parents[4] / "config" / "playbooks"
BAR = datetime(2026, 9, 28, 9, 45, tzinfo=UTC)  # the M15 bar 09:45-10:00
ROLES = TfRoles(Timeframe.M15, Timeframe.H1, (Timeframe.H4,))

SNAPSHOT = FeatureSnapshot(
    symbol="XAUUSD",
    trigger_tf=Timeframe.M15,
    bar_time=BAR,
    feature_set_version=2,
    features={
        "m15.atr14": 9.67, "m15.rsi14": 44.1, "m15.ema50": 4148.52, "m15.close": 4150.0,
        "h1.ema50": 4141.3, "h1.adx14": 27.4, "h4.ema_stack": "BULL",
        "ctx.regime": "TREND", "ctx.spread_to_atr": 0.029, "ctx.session": "LONDON", "ctx.drawdown_pct": None,
    },
    bars_ref={Timeframe.M15: BAR, Timeframe.H1: datetime(2026, 9, 28, 8, 0, tzinfo=UTC)},
)  # fmt: skip

CANDIDATE = SetupCandidate(
    setup_tag="mtf_trend_pullback",
    playbook_id="mtf_trend_pullback",
    direction_hint=Direction.LONG,
    key_levels={"invalidation": D("4138.20"), "target": D("4182.50")},
    strength=0.62,
    notes="stochastic hook from 22",
)


def bars(count: int = 20) -> list[Bar]:
    out = []
    for i in range(count):
        close = D("4140") + D(i) / 2
        out.append(
            Bar(
                symbol="XAUUSD", timeframe=Timeframe.M15, time=BAR - timedelta(minutes=15 * (count - 1 - i)),
                open=close - D("1"), high=close + D("2"), low=close - D("3"), close=close, tick_volume=100,
                spread_points=28,
            )
        )  # fmt: skip
    return out


def analyst_input(candidates: list[SetupCandidate] | None = None) -> AnalystInput:
    return AnalystInput(
        decision_id="01JAXDECISION000000000000A",
        symbol="XAUUSD",
        snapshot=SNAPSHOT,
        roles=ROLES,
        candidates=[CANDIDATE] if candidates is None else candidates,
        trigger_bars=bars(),
        trigger_atr=D("9.67"),
        tick=Tick(
            symbol="XAUUSD", time=BAR + timedelta(minutes=15, seconds=3), bid=D("4150.00"), ask=D("4150.28")
        ),
        spread_points=28,
        position=None,
        portfolio="0 open positions, equity 10000.00, drawdown 0.00%, today +0.00",
    )
