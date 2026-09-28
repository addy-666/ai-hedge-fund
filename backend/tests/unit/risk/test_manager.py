"""Risk Manager: the whole chain on the real Vantage XAUUSD figures, every rejection path, fail-closed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.adapters.clock import FakeClock
from aifund.config.trading_config import RiskConfig
from aifund.domain.decision import FinalDecision
from aifund.domain.enums import (
    AccountTradeMode,
    CloseReason,
    Direction,
    IntentKind,
    MarginMode,
    ReasonCode,
    Side,
    Timeframe,
)
from aifund.domain.intent import make_idempotency_key
from aifund.domain.market import AccountInfo, Position, Tick
from aifund.domain.values import is_multiple_of
from aifund.ports.broker import BrokerError
from aifund.risk.guards import GuardAction, GuardContext
from aifund.risk.limits import EquityState, Exposure
from aifund.risk.manager import RiskManager, RiskRequest
from tests.unit.risk.test_stops import XAU

NOW = datetime(2026, 9, 28, 14, 0, 5, tzinfo=UTC)
BAR = datetime(2026, 9, 28, 13, 45, tzinfo=UTC)
MAGIC = 26092801


class CalcBroker:
    """calc_profit / calc_margin as the Vantage demo reported them.

    Gold: $1 per 0.01 move per lot of 100 oz; margin at 1:500.
    """

    fail = False

    async def calc_profit(self, side: Side, symbol: str, volume: D, p_open: D, p_close: D) -> D:
        if self.fail:
            raise BrokerError("terminal disconnected")
        return (p_close - p_open) * (1 if side is Side.BUY else -1) * 100 * volume

    async def calc_margin(self, side: Side, symbol: str, volume: D, price: D) -> D:
        return volume * 100 * price / 500


ACCOUNT = AccountInfo(
    login=90000001,
    server="VantageMarkets-Demo",
    trade_mode=AccountTradeMode.DEMO,
    margin_mode=MarginMode.HEDGING,
    currency="USD",
    leverage=500,
    balance=D("10000"),
    equity=D("10000"),
    margin=D(0),
    free_margin=D("10000"),
    trade_allowed=True,
    trade_expert=True,
)
DECISION = FinalDecision(
    decision_id="01JDECISION00000000000000A",
    symbol="XAUUSD",
    direction=Direction.LONG,
    setup_tag="mtf_trend_pullback",
    llm_confidence=90,
    calibrated_confidence=90,
    penalty_points=0,
    final_confidence=90,
    risk_factor=D(1),
    invalidation_price=D("4140.00"),
    target_price=D("4180.00"),
)
TICK = Tick(symbol="XAUUSD", time=NOW, bid=D("4150.40"), ask=D("4150.68"))


def request(**over: object) -> RiskRequest:
    base = dict(
        decision=DECISION, trigger_tf=Timeframe.M15, bar_time=BAR, strategy_version="baseline_v1", spec=XAU,
        tick=TICK, reference_price=D("4150.20"), atr=D("9.67"), atr_pct_rank=0.5, account=ACCOUNT,
        equity_state=EquityState(D("10000"), D("10000"), D("10000"), D("10000")),
        guards=GuardContext(symbol="XAUUSD", direction=Direction.LONG, final_confidence=90,
                            confidence_threshold=65, magic=MAGIC, now=NOW, trigger_tf=Timeframe.M15,
                            broker_positions=[]),
        open_exposure=[], bucket="USD_INVERSE",
    )  # fmt: skip
    base.update(over)
    return RiskRequest(**base)  # type: ignore[arg-type]


def manager(broker: CalcBroker | None = None, cfg: RiskConfig | None = None) -> RiskManager:
    return RiskManager(cfg or RiskConfig(), magic=MAGIC, broker=broker or CalcBroker(), clock=FakeClock(NOW))  # type: ignore[arg-type]


async def test_open_intent_end_to_end_by_hand() -> None:
    out = await manager().evaluate(request())
    assert out.rejection is None
    assert out.action is GuardAction.OPEN
    intent = out.intent
    assert intent is not None
    assert intent.is_issued()
    # stops (see test_stops): SL 4138.75, TP 4180.00 ; loss per lot = 11.93 x 100 = $1,193
    # sizing: $50 budget / 1193 = 0.0419 -> 0.04 lot ; risk 0.04 x 1193 = $47.72
    assert (intent.kind, intent.side, intent.volume) == (IntentKind.OPEN, Side.BUY, D("0.04"))
    assert (intent.price_ref, intent.sl, intent.tp) == (D("4150.68"), D("4138.75"), D("4180.00"))
    assert (intent.risk_money, intent.risk_pct) == (D("47.72"), D("0.4772"))
    assert intent.magic == MAGIC
    assert intent.comment.startswith("AF:")
    assert intent.idempotency_key == make_idempotency_key(
        account=90000001,
        symbol="XAUUSD",
        trigger_tf=Timeframe.M15,
        bar_time=BAR,
        direction=Direction.LONG,
        strategy_version="baseline_v1",
    )
    assert is_multiple_of(intent.volume, XAU.volume_step)
    assert out.worksheet["sizing.lots"] == "0.04"
    assert out.worksheet["sl"] == "4138.75"


async def test_short_open() -> None:
    d = DECISION.model_copy(
        update={
            "direction": Direction.SHORT,
            "invalidation_price": D("4160.00"),
            "target_price": D("4120.00"),
        }
    )
    guards = replace(request().guards, direction=Direction.SHORT)
    out = await manager().evaluate(request(decision=d, guards=guards))
    assert out.intent is not None
    assert out.intent.side is Side.SELL
    assert out.intent.sl > out.intent.price_ref > out.intent.tp


async def test_reversal_issues_only_a_verified_close_intent_with_its_own_key() -> None:
    long_pos = Position(
        ticket=501,
        position_id=501,
        symbol="XAUUSD",
        side=Side.BUY,
        volume=D("0.04"),
        price_open=D("4140"),
        magic=MAGIC,
        time=NOW - timedelta(hours=3),
    )
    d = DECISION.model_copy(update={"direction": Direction.SHORT, "final_confidence": 85})
    guards = replace(
        request().guards, direction=Direction.SHORT, final_confidence=85, broker_positions=[long_pos]
    )
    out = await manager().evaluate(request(decision=d, guards=guards))
    assert out.action is GuardAction.CLOSE_ONLY
    intent = out.intent
    assert intent is not None
    assert (intent.kind, intent.side, intent.volume, intent.position_ticket) == (
        IntentKind.REVERSE_CLOSE,
        Side.SELL,
        D("0.04"),
        501,
    )
    assert intent.price_ref == TICK.bid
    assert intent.close_reason is CloseReason.REVERSAL
    open_key = make_idempotency_key(
        account=90000001,
        symbol="XAUUSD",
        trigger_tf=Timeframe.M15,
        bar_time=BAR,
        direction=Direction.SHORT,
        strategy_version="baseline_v1",
    )
    assert (
        intent.idempotency_key != open_key
    )  # the re-open of a close_and_reverse can still get its own intent


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"equity_state": EquityState(D("9650"), D("10000"), D("10000"), D("10000"))}, ReasonCode.LOSS_LIMIT),
        ({"decision": DECISION.model_copy(update={"final_confidence": 60})}, ReasonCode.BELOW_THRESHOLD),
        ({"decision": DECISION.model_copy(update={"direction": Direction.NONE})}, ReasonCode.INTERNAL_ERROR),
        ({"guards": GuardContext(symbol="XAUUSD", direction=Direction.LONG, final_confidence=90,
                                 confidence_threshold=65, magic=MAGIC, now=NOW, trigger_tf=Timeframe.M15,
                                 broker_positions=[], in_flight_intents=1)}, ReasonCode.INTENT_IN_FLIGHT),
        ({"reference_price": D("4140.00")}, ReasonCode.PRICE_MOVED),  # 10.68 > 0.5 x 9.67
        ({"account": ACCOUNT.model_copy(update={"equity": D("500"), "free_margin": D("500")})},
         ReasonCode.RISK_BELOW_MIN_LOT),
        ({"open_exposure": [Exposure("BTCUSD", "CRYPTO", D("280"), D("1000"))]}, ReasonCode.PORTFOLIO_HEAT),
        ({"atr": D("0")}, ReasonCode.INTERNAL_ERROR),
    ],
)  # fmt: skip
async def test_every_rejection_path(over: dict[str, object], expected: ReasonCode) -> None:
    out = await manager().evaluate(request(**over))
    assert out.intent is None
    assert out.rejection is not None
    assert out.rejection.reason is expected


async def test_broker_failure_fails_closed() -> None:
    broker = CalcBroker()
    broker.fail = True
    out = await manager(broker).evaluate(request())
    assert out.intent is None
    assert out.rejection is not None
    assert out.rejection.reason is ReasonCode.INTERNAL_ERROR
    assert "broker calculator failed" in out.rejection.detail


async def test_flip_flop_lock_request_is_passed_through() -> None:
    guards = replace(request().guards, recent_directions=[Direction.LONG, Direction.SHORT])
    out = await manager().evaluate(request(guards=guards))
    assert out.rejection is not None
    assert out.rejection.reason is ReasonCode.FLIP_FLOP
    assert out.start_flip_flop_lock


async def test_rule_risk_factor_shrinks_the_position() -> None:
    out = await manager().evaluate(request(decision=DECISION.model_copy(update={"risk_factor": D("0.5")})))
    assert out.intent is not None
    assert out.intent.volume == D("0.02")  # $25 / 1193 = 0.021 -> 0.02
