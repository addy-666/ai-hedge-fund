"""Review queue (roadmap 7.3, docs/04 §2): every closed, enriched trade gets one reviewer call, oldest first.

A few trades per pass keep the LLM spend smooth; the daily LLM budget still applies (the adapter refuses
calls past it and the trade stays PENDING for the next day). A review that fails twice on the schema, or a
trade that cannot be described (no decision and no snapshot is fine; no trigger timeframe is not), is marked
FAILED and left for the operator — never reviewed by guesswork.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy.orm import Session, sessionmaker

from aifund.agents.reviewer import CANDLES_BEFORE, Reviewer, ReviewInput
from aifund.domain.enums import Direction, Side, Timeframe
from aifund.domain.values import to_decimal
from aifund.market.feature_registry import tf_prefix
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.learning import TradeReviewRepository
from aifund.persistence.repositories.market import FeatureSnapshotRepository
from aifund.persistence.tables import DecisionRow, TradeRow
from aifund.ports.broker import MarketDataPort
from aifund.ports.system import ClockPort

log = structlog.get_logger(__name__)
PER_PASS = 3


@dataclass(frozen=True)
class _Pending:
    trade: TradeRow
    decision: DecisionRow | None
    snapshot: Any


class ReviewQueue:
    def __init__(
        self,
        reviewer: Reviewer,
        market: MarketDataPort,
        factory: sessionmaker[Session],
        clock: ClockPort,
        *,
        per_pass: int = PER_PASS,
    ) -> None:
        self._reviewer = reviewer
        self._market = market
        self._factory = factory
        self._clock = clock
        self._per_pass = per_pass

    def _load(self) -> list[_Pending]:
        with unit_of_work(self._factory) as s:
            out = []
            for t in TradeReviewRepository(s, self._clock).pending(self._per_pass):
                decision = s.get(DecisionRow, t.decision_id) if t.decision_id else None
                snapshot = (
                    FeatureSnapshotRepository(s, self._clock).get(t.snapshot_id) if t.snapshot_id else None
                )
                s.expunge(t)
                if decision is not None:
                    s.expunge(decision)
                out.append(_Pending(t, decision, snapshot))
            return out

    def _mark(self, trade_id: str, status: str) -> None:
        with unit_of_work(self._factory) as s:
            TradeReviewRepository(s, self._clock).mark(trade_id, status)

    def _save(self, trade_id: str, fields: dict[str, Any]) -> None:
        with unit_of_work(self._factory) as s:
            TradeReviewRepository(s, self._clock).add(trade_id, **fields)

    async def run_once(self) -> int:
        """Review up to ``per_pass`` trades; returns how many got a review."""
        done = 0
        for p in await asyncio.to_thread(self._load):
            inp = await self._input(p)
            if inp is None:
                log.warning("review.undescribable", trade_id=p.trade.id)
                await asyncio.to_thread(self._mark, p.trade.id, "FAILED")
                continue
            result = await self._reviewer.review(inp)
            review = result.review
            if review is None:
                if (result.error or "").startswith("invalid output"):
                    log.warning("review.invalid", trade_id=p.trade.id, error=result.error)
                    await asyncio.to_thread(self._mark, p.trade.id, "FAILED")
                    continue
                log.warning("review.deferred", trade_id=p.trade.id, error=result.error)
                break  # provider trouble or the budget: the trade stays PENDING for a later pass
            fields = {
                "tags": [tag.value for tag in review.tags],
                "thesis_verdict": review.thesis_verdict.value,
                "execution_quality": review.execution_quality,
                "lesson": review.lesson,
                "llm_call_id": result.call_ids[-1] if result.call_ids else None,
            }
            await asyncio.to_thread(self._save, p.trade.id, fields)
            done += 1
        return done

    async def _input(self, p: _Pending) -> ReviewInput | None:
        t, d = p.trade, p.decision
        tf_name = t.trigger_tf or (d.trigger_tf if d is not None else None)
        if tf_name is None or t.close_time is None or t.r_multiple is None:
            return None
        tf = Timeframe(tf_name)
        start = t.open_time - timedelta(minutes=tf.minutes * CANDLES_BEFORE)
        candles = await self._market.bars_range(t.symbol, tf, start, t.close_time)
        proposal: dict[str, Any] = (d.proposal or {}) if d is not None else {}
        atr = p.snapshot.features.get(f"{tf_prefix(tf)}.atr14") if p.snapshot is not None else None
        return ReviewInput(
            trade_id=t.id,
            symbol=t.symbol,
            direction=Direction.LONG if t.side is Side.BUY else Direction.SHORT,
            setup_tag=t.setup_tag,
            trigger_tf=tf.value,
            opened=t.open_time,
            closed=t.close_time,
            entry=t.open_price,
            stop=t.initial_sl,
            target=t.initial_tp,
            atr=to_decimal(atr) if isinstance(atr, float) else None,
            r=t.r_multiple,
            close_reason=t.close_reason.value if t.close_reason is not None else "unknown",
            mae_r=t.mae_r,
            mfe_r=t.mfe_r,
            bars_held=t.bars_held,
            minutes=t.holding_minutes,
            snapshot=p.snapshot,
            candles=candles,
            thesis=proposal.get("thesis") if isinstance(proposal.get("thesis"), str) else None,
            key_risks=[str(k) for k in proposal.get("key_risks", []) if isinstance(k, str)],
            lessons=[str(x) for x in (d.lessons_shown or [])] if d is not None else [],
            rules=[str(x) for x in (d.rules_matched or [])] if d is not None else [],
        )
