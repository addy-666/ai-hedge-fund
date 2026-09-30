"""Review queue (roadmap 7.3): every closed, enriched trade gets one review; failures are handled honestly."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from aifund.adapters.clock import FakeClock
from aifund.adapters.llm.fake_llm import FakeLLM, Scripted
from aifund.agents.reviewer import Reviewer
from aifund.config.trading_config import LLMConfig
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from aifund.engine.reviews import ReviewQueue
from aifund.persistence.db import unit_of_work
from aifund.persistence.tables import LLMCallRow, TradeReviewRow, TradeRow
from tests.fakes.learning import seed_outcome
from tests.unit.agents.test_reviewer import CANDLES, OPEN, review

from .conftest import T0

CFG = LLMConfig(analyst_model="deepseek-chat", auditor_model="deepseek-reasoner")
FEATURES = {"m15.atr14": 9.67, "m15.rsi14": 44.1, "ctx.session": "LONDON"}


class Market:
    def __init__(self) -> None:
        self.asked: list[tuple[str, Timeframe, datetime, datetime]] = []

    async def bars_range(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        self.asked.append((symbol, timeframe, start, end))
        return CANDLES


def queue(factory: sessionmaker[Session], clock: FakeClock, *script: Scripted) -> tuple[ReviewQueue, FakeLLM]:
    llm = FakeLLM(list(script), factory=factory, clock=clock)
    return ReviewQueue(Reviewer(llm, CFG), Market(), factory, clock, per_pass=2), llm  # type: ignore[arg-type]


def trades(factory: sessionmaker[Session]) -> dict[str, Any]:
    with factory() as s:
        return {t.id: t.review_status for t in s.query(TradeRow).all()}


async def test_closed_enriched_trades_are_reviewed_oldest_first_two_per_pass(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    with unit_of_work(factory) as s:
        ids = [
            seed_outcome(s, when=OPEN.replace(hour=h), r=r, features=FEATURES)[1]
            for h, r in ((9, -1), (8, 2), (7, 1))
        ]
        seed_outcome(s, when=OPEN.replace(hour=6), r=-1, features=FEATURES, enriched=False)  # not yet
    q, llm = queue(factory, clock, review())
    assert await q.run_once() == 2
    status = trades(factory)
    assert [status[i] for i in ids] == ["PENDING", "DONE", "DONE"]
    assert await q.run_once() == 1
    assert await q.run_once() == 0
    with factory() as s:
        reviews = s.query(TradeReviewRow).all()
        calls = {c.id: c.trade_id for c in s.query(LLMCallRow).all()}
    assert len(reviews) == 3 == len(llm.requests)
    assert all(calls[r.llm_call_id] == r.trade_id for r in reviews)  # the call is linked to its trade
    assert reviews[0].tags == ["GOOD_TRADE_BAD_OUTCOME"]
    assert "THESIS AT ENTRY: seeded" in llm.requests[0].messages[1].content


async def test_invalid_output_fails_the_trade_and_a_provider_error_waits(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    from aifund.ports.llm import LLMError

    with unit_of_work(factory) as s:
        _, first = seed_outcome(s, when=T0, r=-1, features=FEATURES)
        _, second = seed_outcome(s, when=T0.replace(hour=10), r=1, features=FEATURES)
    q, _ = queue(factory, clock, review(tags=["NOPE"]), review(tags=["NOPE"]), LLMError("HTTP 503"))
    assert await q.run_once() == 0
    assert trades(factory) == {first: "FAILED", second: "PENDING"}  # the second waits for a later pass


async def test_a_trade_without_a_trigger_timeframe_is_failed_not_guessed(
    factory: sessionmaker[Session], clock: FakeClock
) -> None:
    with unit_of_work(factory) as s:
        _, trade_id = seed_outcome(s, when=T0, r=-1, features=FEATURES)
        t = s.get(TradeRow, trade_id)
        assert t is not None
        t.trigger_tf, t.decision_id, t.snapshot_id = None, None, None
    q, llm = queue(factory, clock, review())
    assert await q.run_once() == 0
    assert trades(factory) == {trade_id: "FAILED"}
    assert llm.requests == []
