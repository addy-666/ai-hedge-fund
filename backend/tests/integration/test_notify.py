"""Telegram alerts, the healthchecks.io ping and the daily summary (roadmap 5.5): mocked HTTP, fixed text."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
import respx
from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.notify.null import NullNotifier
from aifund.adapters.notify.telegram import (
    FanOut,
    HealthchecksPinger,
    LogNotifier,
    TelegramNotifier,
    format_alert,
)
from aifund.domain.enums import DecisionOutcome, Side, TradeStatus
from aifund.engine.summary import DailySummary
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.decisions import DecisionRepository
from aifund.persistence.tables import TradeRow
from aifund.ports.system import Severity

from .conftest import T0

TOKEN = "123456:SECRET-token"
SEND = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def test_alert_format_is_fixed() -> None:
    assert format_alert(Severity.CRITICAL, "Engine HALTED", "LIMIT_BREACH: DAILY_LOSS: 3.10% >= limit 3.0%",
                        account="vantage-demo") == (
        "🔴 CRITICAL · Engine HALTED [vantage-demo]\nLIMIT_BREACH: DAILY_LOSS: 3.10% >= limit 3.0%"
    )  # fmt: skip
    assert format_alert(Severity.INFO, "Trade closed") == "🔵 INFO · Trade closed"
    long = format_alert(Severity.WARN, "x", "y" * 5000)
    assert len(long) == 4000 and long.endswith("…")  # noqa: PT018


@respx.mock
async def test_telegram_sends_filters_and_dedupes(clock: FakeClock) -> None:
    route = respx.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as client:
        tg = TelegramNotifier(TOKEN, "42", client, clock, account="demo", min_severity=Severity.WARN)
        await tg.notify(Severity.INFO, "Trade opened")  # below the threshold
        await tg.notify(Severity.CRITICAL, "Engine HALTED", "reason")
        await tg.notify(Severity.CRITICAL, "Engine HALTED", "reason")  # duplicate within a minute
        clock.advance(61)
        await tg.notify(Severity.CRITICAL, "Engine HALTED", "reason")
    assert route.call_count == 2
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "chat_id": "42",
        "text": "🔴 CRITICAL · Engine HALTED [demo]\nreason",
        "disable_web_page_preview": True,
    }


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(429), httpx.ConnectError("down")])
async def test_telegram_failures_never_raise(
    clock: FakeClock, response: object, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.post(SEND)
    route.mock(side_effect=response) if isinstance(response, Exception) else route.mock(return_value=response)
    async with httpx.AsyncClient() as client:
        await TelegramNotifier(TOKEN, "42", client, clock).notify(Severity.WARN, "x")
    assert route.called


@respx.mock
async def test_healthchecks_ping_and_fail() -> None:
    ok = respx.get("https://hc-ping.com/uuid").mock(return_value=httpx.Response(200))
    fail = respx.get("https://hc-ping.com/uuid/fail").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as client:
        pinger = HealthchecksPinger("https://hc-ping.com/uuid/", client)
        assert await pinger.ping()
        assert await pinger.ping(failing=True)
    assert (ok.call_count, fail.call_count) == (1, 1)
    respx.get("https://hc-ping.com/down").mock(side_effect=httpx.ConnectTimeout("t"))
    async with httpx.AsyncClient() as client:
        assert not await HealthchecksPinger("https://hc-ping.com/down", client).ping()


async def test_fan_out_survives_a_broken_notifier() -> None:
    class Broken:
        async def notify(self, severity: Severity, title: str, body: str = "") -> None:
            raise RuntimeError("nope")

    sink = NullNotifier()
    await FanOut(Broken(), LogNotifier(), sink).notify(Severity.WARN, "t", "b")
    assert sink.sent == [(Severity.WARN, "t", "b")]


async def test_daily_summary_once_a_day_after_its_time(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    clock.set(T0.replace(hour=20, minute=0))
    with unit_of_work(factory) as s:
        for i, (net, r) in enumerate(
            ((D("30.10"), D("1.2")), (D("-25.00"), D("-1")), (D("12.00"), D("0.5")))
        ):
            s.add(TradeRow(
                id=f"T{i}", account_id="acc", position_id=100 + i, symbol="XAUUSD", side=Side.BUY,
                status=TradeStatus.CLOSED, open_time=T0, open_price=D("4150"), volume_opened=D("0.1"),
                volume_open_now=D(0), close_time=T0 + timedelta(hours=i + 1), net_pnl=net, r_multiple=r,
                created_at=T0, updated_at=T0,
            ))  # fmt: skip
        repo = DecisionRepository(s, clock)
        for outcome in (DecisionOutcome.NO_SETUP, DecisionOutcome.NO_SETUP, DecisionOutcome.ORDERED):
            repo.add(account_id="acc", symbol="XAUUSD", trigger_tf="M15", bar_time=T0, stage_reached="SETUP",
                     outcome=outcome)  # fmt: skip
    sink = NullNotifier()
    daily = DailySummary(
        "21:05", account="acc", state=lambda: "RUNNING", factory=factory, clock=clock, notifier=sink
    )
    assert await daily.run_once() is None  # before 21:05
    clock.set(T0.replace(hour=21, minute=6))
    summary = await daily.run_once()
    assert summary is not None
    assert await daily.run_once() is None  # once a day
    ((severity, title, text),) = sink.sent
    assert (severity, title) == (Severity.INFO, "Daily summary")
    assert text == (
        "2026-09-27 21:05 → 2026-09-28 21:05 UTC\n"
        "Trades closed: 3 (win 67%), net +17.10, +0.70R\n"
        "Decisions: NO_SETUP 2, ORDERED 1\n"
        "LLM: 0 calls, $0.0000\n"
        "Engine: RUNNING"
    )
    assert (
        await DailySummary(
            None, account="acc", state=lambda: "x", factory=factory, clock=clock, notifier=sink
        ).run_once()
        is None
    )
