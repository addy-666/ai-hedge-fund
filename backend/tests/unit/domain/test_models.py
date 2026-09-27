from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from aifund.domain._issuance import issue_order_intent
from aifund.domain.decision import TradeProposal
from aifund.domain.enums import (
    DealEntry,
    Direction,
    IntentKind,
    IntentStatus,
    Side,
    Timeframe,
)
from aifund.domain.ids import new_id
from aifund.domain.intent import OrderIntent, intent_comment, make_idempotency_key
from aifund.domain.market import Bar, Tick

T0 = datetime(2026, 9, 28, 10, 15, tzinfo=UTC)


# ---------------------------------------------------------------- enums


def test_direction_to_side() -> None:
    assert Direction.LONG.to_side() is Side.BUY
    assert Direction.SHORT.to_side() is Side.SELL
    with pytest.raises(ValueError, match=r"Direction\.NONE"):
        Direction.NONE.to_side()
    assert Side.BUY.opposite is Side.SELL


def test_timeframe_minutes() -> None:
    assert [tf.minutes for tf in Timeframe] == [1, 5, 15, 30, 60, 240, 1440]


def test_non_terminal_intents_lock_the_symbol() -> None:
    locking = {s for s in IntentStatus if s.locks_symbol}
    assert locking == {IntentStatus.PENDING, IntentStatus.SENT, IntentStatus.RETRYING, IntentStatus.UNKNOWN}


def test_deal_entries_that_reduce_positions() -> None:
    # The prototype only looked at OUT and missed OUT_BY / INOUT.
    assert {e for e in DealEntry if e.reduces_position} == {DealEntry.OUT, DealEntry.OUT_BY, DealEntry.INOUT}


# ---------------------------------------------------------------- market


def test_bar_rejects_inconsistent_ohlc() -> None:
    kw: dict[str, Any] = dict(symbol="X", timeframe=Timeframe.M15, time=T0, tick_volume=10)
    Bar(open=Decimal(10), high=Decimal(11), low=Decimal(9), close=Decimal(10), **kw)
    with pytest.raises(ValidationError, match="inconsistent OHLC"):
        Bar(open=Decimal(10), high=Decimal("9.5"), low=Decimal(9), close=Decimal(10), **kw)


def test_tick_rejects_crossed_quotes_and_prices_entries() -> None:
    t = Tick(symbol="X", time=T0, bid=Decimal("1999.80"), ask=Decimal("2000.00"))
    assert t.spread == Decimal("0.20")
    assert t.entry_price(Side.BUY) == Decimal("2000.00")
    assert t.entry_price(Side.SELL) == Decimal("1999.80")  # the prototype used the ask for sells too
    with pytest.raises(ValidationError, match="crossed quote"):
        Tick(symbol="X", time=T0, bid=Decimal(2), ask=Decimal(1))


# ---------------------------------------------------------------- TradeProposal (LLM contract)

VALID = {
    "direction": "LONG",
    "confidence": 78,
    "setup_tag": "mtf_trend_pullback",
    "invalidation_price": 2345.5,
    "target_price": 2372.0,
    "thesis": "H1 uptrend, M15 pullback to EMA20 with RSI reset.",
    "key_risks": ["US CPI in 2h"],
}


def _proposal(**overrides: Any) -> TradeProposal:
    return TradeProposal.model_validate_json(json.dumps({**VALID, **overrides}))


def test_valid_proposal_parses_from_json() -> None:
    p = _proposal()
    assert p.direction is Direction.LONG
    assert p.invalidation_price == Decimal("2345.5")
    assert p.is_trade


@pytest.mark.parametrize(
    "overrides",
    [
        {"confidence": "85"},  # string number: prototype turned this into HOLD by accident
        {"confidence": 150},  # prototype traded this
        {"confidence": 77.5},
        {"direction": "BUY"},
        {"direction": "STRONG_BUY"},
        {"invalidation_price": "15%"},
        {"invalidation_price": -1},
        {"invalidation_price": 0},
        {"volume": 50},  # the LLM never supplies volume
        {"risk_percent": 25},
        {"setup_tag": "Trend Pullback!"},
        {"thesis": ""},
        {"thesis": "x" * 601},
        {"key_risks": ["a", "b", "c", "d", "e", "f"]},
        {"time_horizon_bars": 0},
    ],
)
def test_invalid_llm_output_is_rejected_not_repaired(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _proposal(**overrides)


def test_missing_required_fields_are_rejected() -> None:
    body = {k: v for k, v in VALID.items() if k != "confidence"}
    with pytest.raises(ValidationError):
        TradeProposal.model_validate_json(json.dumps(body))


def test_non_finite_prices_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeProposal.model_validate_json(json.dumps(VALID).replace("2345.5", "NaN"))


def test_none_direction_is_not_a_trade() -> None:
    assert not _proposal(direction="NONE").is_trade
    assert not _proposal(setup_tag="none").is_trade


# ---------------------------------------------------------------- idempotency key


def _key(**overrides: Any) -> str:
    base: dict[str, Any] = dict(
        account=12345678,
        symbol="XAUUSDm",
        trigger_tf=Timeframe.M15,
        bar_time=T0,
        direction=Direction.LONG,
        strategy_version="analyst_v1",
    )
    return make_idempotency_key(**{**base, **overrides})


def test_idempotency_key_is_deterministic_and_input_sensitive() -> None:
    assert _key() == _key()
    assert len(_key()) == 24
    variants = [
        _key(account=1),
        _key(symbol="EURUSDm"),
        _key(trigger_tf=Timeframe.H1),
        _key(bar_time=T0 + timedelta(minutes=15)),
        _key(direction=Direction.SHORT),
        _key(strategy_version="analyst_v2"),
    ]
    assert len({_key(), *variants}) == 7


def test_idempotency_key_is_timezone_independent_for_the_same_instant() -> None:
    same_instant = T0.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert _key(bar_time=same_instant.astimezone(UTC)) == _key()
    with pytest.raises(ValueError, match="timezone-aware"):
        _key(bar_time=T0.replace(tzinfo=None))


# ---------------------------------------------------------------- OrderIntent


def _intent_fields(**overrides: Any) -> dict[str, Any]:
    intent_id = new_id()
    fields: dict[str, Any] = dict(
        id=intent_id,
        idempotency_key=_key(),
        decision_id=new_id(),
        kind=IntentKind.OPEN,
        symbol="XAUUSDm",
        side=Side.BUY,
        volume=Decimal("0.10"),
        price_ref=Decimal("2350.00"),
        sl=Decimal("2335.00"),
        tp=Decimal("2380.00"),
        sl_distance=Decimal("15.00"),
        tp_distance=Decimal("30.00"),
        risk_money=Decimal("150"),
        risk_pct=Decimal("0.5"),
        magic=26092801,
        comment=intent_comment(intent_id),
        created_at=T0,
    )
    fields.update(overrides)
    return fields


def test_only_issued_intents_are_executable() -> None:
    assert not OrderIntent(**_intent_fields()).is_issued()
    assert issue_order_intent(**_intent_fields()).is_issued()


def test_copies_and_reconstructions_are_never_executable() -> None:
    issued = issue_order_intent(**_intent_fields())
    tampered = issued.model_copy(update={"volume": Decimal("100")})
    assert tampered.volume == Decimal("100")
    assert not tampered.is_issued()
    assert not issued.model_copy().is_issued()
    assert not copy.copy(issued).is_issued()
    assert not copy.deepcopy(issued).is_issued()
    assert not OrderIntent.model_validate(issued.model_dump()).is_issued()
    assert issued.is_issued()


@pytest.mark.parametrize(
    "overrides",
    [
        {"sl": Decimal("2355")},  # BUY with SL above entry
        {"tp": Decimal("2340")},  # BUY with TP below entry
        {"side": Side.SELL},  # SELL with BUY geometry
        {"sl": None},
        {"volume": Decimal(0)},
        {"risk_money": Decimal(0)},
        {"comment": "AI Brain Entry"},
        {"comment": "AF:" + "x" * 40},
    ],
)
def test_intent_geometry_and_fields_are_validated(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OrderIntent(**_intent_fields(**overrides))


def test_sell_intent_geometry() -> None:
    issue_order_intent(**_intent_fields(side=Side.SELL, sl=Decimal("2365"), tp=Decimal("2320")))


def test_close_intents_require_a_target_position() -> None:
    fields = _intent_fields(
        kind=IntentKind.CLOSE, sl=None, tp=None, sl_distance=None, tp_distance=None, risk_money=Decimal(0)
    )
    with pytest.raises(ValidationError, match="position_ticket"):
        OrderIntent(**fields)
    assert OrderIntent(**{**fields, "position_ticket": 555}).position_ticket == 555


def test_intent_is_immutable() -> None:
    intent = issue_order_intent(**_intent_fields())
    with pytest.raises(ValidationError):
        intent.volume = Decimal("100")  # type: ignore[misc]
