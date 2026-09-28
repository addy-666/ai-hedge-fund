from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aifund.adapters.clock import FakeClock, SystemClock
from aifund.adapters.notify.null import NullNotifier
from aifund.domain.enums import Side
from aifund.domain.market import OrderRequest, TradeAction
from aifund.ports.llm import LLMMessage, LLMRequest
from aifund.ports.system import ClockPort, NotifierPort, Severity

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


def test_adapters_satisfy_their_ports() -> None:
    clocks: list[ClockPort] = [SystemClock(), FakeClock(T0)]
    notifier: NotifierPort = NullNotifier()
    assert all(isinstance(c, ClockPort) for c in clocks)
    assert isinstance(notifier, NotifierPort)


def test_system_clock_is_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)


async def test_fake_clock_only_moves_forward_and_sleep_advances() -> None:
    clock = FakeClock(T0)
    assert clock.now() == T0
    await clock.sleep(90)
    assert clock.now() == T0 + timedelta(seconds=90)
    clock.advance(minutes=15)
    assert clock.now() == T0 + timedelta(seconds=90, minutes=15)
    with pytest.raises(ValueError, match="backwards"):
        clock.set(T0)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)
    with pytest.raises(ValueError, match="UTC"):
        FakeClock(datetime(2026, 1, 1))


async def test_null_notifier_records() -> None:
    n = NullNotifier()
    await n.notify(Severity.CRITICAL, "HALTED", "daily loss limit")
    assert n.sent == [(Severity.CRITICAL, "HALTED", "daily loss limit")]


def test_order_request_requires_fields_per_action() -> None:
    OrderRequest(
        action=TradeAction.DEAL, symbol="X", side=Side.BUY, volume=Decimal("0.1"), price=Decimal(1), magic=1
    )
    with pytest.raises(ValidationError, match="DEAL requests require"):
        OrderRequest(action=TradeAction.DEAL, symbol="X", magic=1)
    with pytest.raises(ValidationError, match="SLTP requests require"):
        OrderRequest(action=TradeAction.SLTP, symbol="X", magic=1, sl=Decimal(1))


def test_llm_request_validation() -> None:
    with pytest.raises(ValidationError):
        LLMRequest(agent="analyst", model="m", messages=[], temperature=Decimal("0.1"), timeout_s=30)
    req = LLMRequest(
        agent="analyst",
        model="m",
        messages=[LLMMessage(role="user", content="hi")],
        temperature=Decimal("0.1"),
        timeout_s=30,
    )
    assert req.json_output
