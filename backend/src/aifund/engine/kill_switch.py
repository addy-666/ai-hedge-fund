"""Kill switch (roadmap 5.4, docs/01 §9): FLATTEN_ALL closes every engine position, verifies, then HALTS.

While the engine is FLATTENING, every cycle closes each remaining engine position (magic match) at the market
through the normal path — a close intent issued by the risk layer, persisted and sent by the executor — then
re-reads the broker's positions. Only when none is left does the engine move to HALTED. A close that fails
is retried on the next cycle with a new attempt (and a new idempotency key); after ``alert_after`` failed
attempts on one position a critical alert names it (once), and the switch keeps trying. A symbol with an
intent still in flight is left for the executor's UNKNOWN resolution first. Other magics are never touched.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from aifund.config.trading_config import TradingConfig
from aifund.domain.enums import CloseReason, EngineState, IntentStatus, Timeframe
from aifund.engine.state import StateMachine, Trigger
from aifund.execution.executor import Executor
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.intents import IntentRepository
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort, NotifierPort, Severity
from aifund.risk.position_manager import PositionManager


@dataclass
class FlattenReport:
    closed: list[int] = field(default_factory=list)  # tickets closed this cycle
    remaining: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    done: bool = False


def trigger_tf(cfg: TradingConfig, symbol: str) -> Timeframe:
    """The symbol's trigger timeframe (it only shapes the close's idempotency key); the first profile's for
    a symbol the config no longer lists."""
    sym = next((s for s in cfg.symbols if s.broker == symbol), None)
    profile = cfg.profiles[sym.profile] if sym is not None else next(iter(cfg.profiles.values()))
    return profile.trigger_tf


class Flattener:
    def __init__(
        self,
        cfg: TradingConfig,
        manager: PositionManager,
        state: StateMachine,
        *,
        broker: BrokerPort,
        market: MarketDataPort,
        executor: Executor,
        factory: sessionmaker[Session],
        clock: ClockPort,
        notifier: NotifierPort,
        alert_after: int = 3,
    ) -> None:
        self._cfg = cfg
        self._manager = manager
        self._state = state
        self._broker = broker
        self._market = market
        self._executor = executor
        self._factory = factory
        self._clock = clock
        self._notifier = notifier
        self._alert_after = alert_after
        self.attempts: Counter[int] = Counter()
        self._alerted: set[int] = set()

    def _busy(self, symbols: set[str]) -> set[str]:
        with unit_of_work(self._factory) as s:
            repo = IntentRepository(s, self._clock)
            return {sym for sym in symbols if repo.non_terminal(sym)}

    async def run_once(self) -> FlattenReport:
        report = FlattenReport()
        if self._state.state is not EngineState.FLATTENING:
            return report
        magic = self._cfg.engine.magic
        positions = [p for p in await self._broker.positions() if p.magic == magic]
        busy = await asyncio.to_thread(self._busy, {p.symbol for p in positions}) if positions else set()
        for pos in positions:
            if pos.symbol in busy:
                report.errors.append(f"{pos.symbol} {pos.ticket}: an intent is still in flight")
                continue
            self.attempts[pos.ticket] += 1
            attempt = self.attempts[pos.ticket]
            try:
                tick = await self._market.tick(pos.symbol)
                if tick is None:
                    raise BrokerError(f"no quote for {pos.symbol}")
                spec = await self._broker.symbol_spec(pos.symbol)
                trigger = trigger_tf(self._cfg, pos.symbol)
                intent = self._manager.operator_close(
                    pos, tick, trigger, self._clock.now(), reason=CloseReason.FLATTEN, label="flatten_all",
                    attempt=attempt,
                )  # fmt: skip
                result = await self._executor.execute(intent, spec)
                if result.status is not IntentStatus.FILLED:
                    raise BrokerError(f"close {result.status}: {result.detail}")
                report.closed.append(pos.ticket)
            except BrokerError as exc:
                report.errors.append(f"{pos.symbol} {pos.ticket} attempt {attempt}: {exc}")
                if attempt >= self._alert_after and pos.ticket not in self._alerted:
                    self._alerted.add(pos.ticket)
                    await self._notifier.notify(
                        Severity.CRITICAL,
                        f"Kill switch cannot close {pos.symbol} #{pos.ticket}",
                        f"{attempt} attempts failed; last: {exc}. Still retrying — check the terminal.",
                    )
        left = [p.ticket for p in await self._broker.positions() if p.magic == magic]
        report.remaining = left
        if not left:
            report.done = True
            await self._state.fire(Trigger.FLATTENED, "all engine positions closed")
        return report
