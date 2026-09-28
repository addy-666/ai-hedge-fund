"""Position manager: each rule, their priority, and the safety rules (never loosen, freeze, breached)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.config.trading_config import PositionManagementConfig, StopsConfig, TrailingConfig
from aifund.domain.enums import CloseReason, IntentKind, Side, Timeframe
from aifund.domain.market import Position, Tick
from aifund.risk.position_manager import ActionKind, PositionFacts, PositionManager
from tests.unit.risk.test_stops import XAU

MON = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)  # Monday
FRI_LATE = datetime(2026, 10, 2, 20, 45, tzinfo=UTC)  # Friday after 20:30
MAGIC = 26092801


def pos(
    side: Side = Side.BUY,
    *,
    sl: str | None = "4138.35",
    tp: str | None = "4179.60",
    age_bars: int = 4,
    now: datetime = MON,
    price: str = "4150.28",
) -> Position:
    return Position(
        ticket=77,
        position_id=77,
        symbol="XAUUSD",
        side=side,
        volume=D("0.04"),
        price_open=D(price),
        sl=D(sl) if sl else None,
        tp=D(tp) if tp else None,
        magic=MAGIC,
        time=now - timedelta(minutes=15 * age_bars),
    )


def tick(bid: str = "4152.00", ask: str = "4152.28", now: datetime = MON) -> Tick:
    return Tick(symbol="XAUUSD", time=now, bid=D(bid), ask=D(ask))


def facts(p: Position | None = None, t: Tick | None = None, **over: object) -> PositionFacts:
    base: dict[str, object] = dict(
        position=p or pos(),
        spec=XAU,
        tick=t or tick(),
        trigger_tf=Timeframe.M15,
        trade_weekends=False,
        planned_sl_distance=D("11.93"),
        atr=D("9.67"),
    )
    base.update(over)
    return PositionFacts(**base)  # type: ignore[arg-type]


def manager(**cfg: object) -> PositionManager:
    return PositionManager(PositionManagementConfig(**cfg), StopsConfig(), magic=MAGIC, account=90000001)  # type: ignore[arg-type]


def test_healthy_position_needs_nothing() -> None:
    assert manager().plan(facts(), MON) is None


def test_friday_flatten_only_for_non_weekend_symbols() -> None:
    p, t = pos(now=FRI_LATE), tick(now=FRI_LATE)
    action = manager().plan(facts(p, t), FRI_LATE)
    assert action is not None
    assert (action.kind, action.intent.kind, action.intent.side) == (
        ActionKind.FRIDAY_FLATTEN,
        IntentKind.FLATTEN,
        Side.SELL,
    )
    assert action.intent.position_ticket == 77
    assert action.intent.close_reason is CloseReason.FLATTEN
    assert manager().plan(facts(p, t, trade_weekends=True), FRI_LATE) is None  # BTC keeps its position
    assert manager().plan(facts(p, t), FRI_LATE.replace(hour=20, minute=29)) is None


def test_time_stop() -> None:
    action = manager().plan(facts(pos(age_bars=48)), MON)
    assert action is not None
    assert (action.kind, action.intent.kind) == (ActionKind.TIME_STOP, IntentKind.CLOSE)
    assert action.intent.close_reason is CloseReason.TIME_STOP
    assert manager(time_stop_bars=None).plan(facts(pos(age_bars=48)), MON) is None


def test_missing_sl_is_repaired_at_the_planned_distance() -> None:
    action = manager().plan(facts(pos(sl=None)), MON)
    assert action is not None
    assert action.kind is ActionKind.REPAIR_SL
    assert action.intent.kind is IntentKind.MODIFY_SLTP
    assert (action.intent.sl, action.intent.tp) == (D("4138.35"), D("4179.60"))  # 4150.28 - 11.93


def test_missing_sl_for_an_orphan_uses_atr() -> None:
    action = manager().plan(facts(pos(sl=None), planned_sl_distance=None), MON)
    assert action is not None
    assert action.intent.sl == D("4135.77")  # 4150.28 - 1.5 x 9.67 = 4135.775 -> rounded away (down)


def test_missing_sl_already_crossed_closes_instead() -> None:
    action = manager().plan(facts(pos(sl=None), tick(bid="4130.00", ask="4130.28")), MON)
    assert action is not None
    assert (action.kind, action.intent.kind) == (ActionKind.STOP_BREACHED, IntentKind.CLOSE)
    assert action.intent.close_reason is CloseReason.ENGINE


def test_slippage_realignment_only_tightens() -> None:
    # filled with the SL 16 away instead of 11.93 (> 10% off): pull it back to the planned distance
    action = manager().plan(facts(pos(sl="4134.28")), MON)
    assert action is not None
    assert (action.kind, action.intent.sl) == (ActionKind.REALIGN_SL, D("4138.35"))
    # an SL closer than planned is never loosened
    assert manager().plan(facts(pos(sl="4145.00")), MON) is None


def test_break_even() -> None:
    m = manager(break_even_at_r=D("1.0"))
    assert m.plan(facts(t=tick(bid="4160.00", ask="4160.28")), MON) is None  # +9.72 < 1R (11.93)
    action = m.plan(facts(t=tick(bid="4162.30", ask="4162.58")), MON)
    assert action is not None
    assert (action.kind, action.intent.sl) == (ActionKind.BREAK_EVEN, D("4150.28"))


def test_atr_trailing_uses_closed_bars_and_never_loosens() -> None:
    m = manager(trailing=TrailingConfig(mode="atr", k=D("2")))
    action = m.plan(facts(t=tick(bid="4170.00", ask="4170.28"), last_closed_close=D("4169.00")), MON)
    assert action is not None
    assert (action.kind, action.intent.sl) == (ActionKind.TRAIL, D("4149.66"))  # 4169 - 2 x 9.67
    assert m.plan(facts(last_closed_close=D("4155.00")), MON) is None  # 4135.66 would loosen


def test_short_position_repair_mirrors() -> None:
    p = pos(Side.SELL, sl=None, tp="4121.00", price="4150.00")
    action = manager().plan(facts(p, tick(bid="4148.00", ask="4148.28")), MON)
    assert action is not None
    assert action.intent.sl == D("4161.93")


def test_freeze_level_blocks_modifications_but_not_closes() -> None:
    spec = XAU.model_copy(update={"freeze_level_points": 500})  # 5.00
    near_tp = tick(bid="4176.00", ask="4176.28")  # 3.60 from the TP
    assert manager(break_even_at_r=D("1.0")).plan(facts(t=near_tp, spec=spec), MON) is None
    action = manager().plan(facts(pos(age_bars=48), near_tp, spec=spec), MON)
    assert action is not None
    assert action.kind is ActionKind.TIME_STOP


def test_idempotency_is_per_action_per_minute() -> None:
    m = manager()
    a1 = m.plan(facts(pos(age_bars=48)), MON)
    a2 = m.plan(facts(pos(age_bars=48)), MON + timedelta(seconds=30))
    a3 = m.plan(facts(pos(age_bars=48)), MON + timedelta(minutes=1))
    assert a1 is not None and a2 is not None and a3 is not None  # noqa: PT018
    assert a1.intent.idempotency_key == a2.intent.idempotency_key  # repeat within the minute: duplicate
    assert a1.intent.idempotency_key != a3.intent.idempotency_key  # a failed attempt can be retried
    assert a1.intent.is_issued()


@pytest.mark.parametrize("kind", [ActionKind.REPAIR_SL, ActionKind.TIME_STOP])
def test_actions_are_issued_intents(kind: ActionKind) -> None:
    f = facts(pos(sl=None)) if kind is ActionKind.REPAIR_SL else facts(pos(age_bars=60))
    action = manager().plan(f, MON)
    assert action is not None
    assert action.kind is kind
    assert action.intent.is_issued()
    assert action.intent.magic == MAGIC
