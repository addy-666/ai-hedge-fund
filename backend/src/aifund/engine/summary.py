"""Daily summary (roadmap 5.5, docs/06 §7): one message a day with what the engine did.

Sent once per UTC day at ``alerts.daily_summary_utc`` (or on the first check after it, e.g. after a restart),
covering the 24 hours before: closed trades and their net P&L and R, win rate, decisions by outcome, LLM
calls and cost, the engine state and equity.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aifund.domain.enums import TradeStatus
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.equity import EquitySnapshotRepository
from aifund.persistence.repositories.llm import LLMCallRepository
from aifund.persistence.tables import DecisionRow, TradeRow
from aifund.ports.system import ClockPort, NotifierPort, Severity


@dataclass(frozen=True)
class DaySummary:
    start: datetime
    end: datetime
    trades: int
    wins: int
    net_pnl: Decimal
    r_total: Decimal
    decisions: dict[str, int]
    llm_calls: int
    llm_cost: Decimal
    state: str
    equity: Decimal | None

    def render(self) -> str:
        win = f"{self.wins / self.trades:.0%}" if self.trades else "-"
        funnel = ", ".join(
            f"{k} {v}" for k, v in sorted(self.decisions.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        return "\n".join([
            f"{self.start:%Y-%m-%d %H:%M} → {self.end:%Y-%m-%d %H:%M} UTC",
            f"Trades closed: {self.trades} (win {win}), net {self.net_pnl:+.2f}, {self.r_total:+.2f}R",
            f"Decisions: {funnel or 'none'}",
            f"LLM: {self.llm_calls} calls, ${self.llm_cost:.4f}",
            f"Engine: {self.state}" + (f", equity {self.equity:.2f}" if self.equity is not None else ""),
        ])  # fmt: skip


def summarize_day(s: Session, clock: ClockPort, account: str, state: str, end: datetime) -> DaySummary:
    start = end - timedelta(days=1)
    closed = s.scalars(
        select(TradeRow).where(
            TradeRow.account_id == account,
            TradeRow.status == TradeStatus.CLOSED,
            TradeRow.close_time >= start,
            TradeRow.close_time < end,
        )
    ).all()
    outcomes = s.scalars(
        select(DecisionRow.outcome).where(
            DecisionRow.account_id == account, DecisionRow.created_at >= start, DecisionRow.created_at < end
        )
    ).all()
    llm = LLMCallRepository(s, clock)
    latest = EquitySnapshotRepository(s).latest(account)
    return DaySummary(
        start=start,
        end=end,
        trades=len(closed),
        wins=sum(1 for t in closed if (t.net_pnl or 0) > 0),
        net_pnl=sum((t.net_pnl or Decimal(0) for t in closed), Decimal(0)),
        r_total=sum((t.r_multiple or Decimal(0) for t in closed), Decimal(0)),
        decisions=dict(Counter(o.value for o in outcomes)),
        llm_calls=llm.count_since(start),
        llm_cost=llm.spent_since(start),
        state=state,
        equity=latest.equity if latest is not None else None,
    )


class DailySummary:
    def __init__(
        self,
        at_utc: str | None,
        *,
        account: str,
        state: Callable[[], str],  # the current engine state
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
    ) -> None:
        self._at = time.fromisoformat(at_utc) if at_utc else None
        self._account = account
        self._state = state
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._sent: date | None = None

    async def run_once(self) -> DaySummary | None:
        now = self._clock.now()
        if self._at is None or self._sent == now.date() or now.time() < self._at:
            return None
        end = now.replace(hour=self._at.hour, minute=self._at.minute, second=0, microsecond=0)

        def build() -> DaySummary:
            with unit_of_work(self._factory) as s:
                return summarize_day(s, self._clock, self._account, self._state(), end)

        summary = await asyncio.to_thread(build)
        self._sent = now.date()
        await self._notifier.notify(Severity.INFO, "Daily summary", summary.render())
        return summary
