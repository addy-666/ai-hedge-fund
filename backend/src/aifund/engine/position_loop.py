"""Runs the position manager over every open engine position (docs/03 §13; every 5 s in the engine)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
from sqlalchemy.orm import Session, sessionmaker

from aifund.config.trading_config import TradingConfig
from aifund.domain.values import to_decimal
from aifund.execution.executor import ExecutionResult, Executor
from aifund.market import indicators as ind
from aifund.persistence.db import unit_of_work
from aifund.persistence.repositories.intents import IntentRepository
from aifund.ports.broker import BrokerError, BrokerPort, MarketDataPort
from aifund.ports.system import ClockPort
from aifund.risk.position_manager import ActionKind, PositionFacts, PositionManager

ATR_BARS = 60  # enough closed bars for a settled ATR(14)


@dataclass
class LoopReport:
    actions: list[tuple[ActionKind, ExecutionResult]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class PositionLoop:
    def __init__(
        self,
        cfg: TradingConfig,
        manager: PositionManager,
        *,
        broker: BrokerPort,
        market: MarketDataPort,
        executor: Executor,
        factory: sessionmaker[Session],
        clock: ClockPort,
    ) -> None:
        self._cfg = cfg
        self._manager = manager
        self._broker = broker
        self._market = market
        self._executor = executor
        self._factory = factory
        self._clock = clock
        self._symbols = {s.broker: s for s in cfg.symbols}

    def _facts_sync(self, position_ids: list[int], symbols: set[str]) -> tuple[dict[int, Decimal], set[str]]:
        with unit_of_work(self._factory) as s:
            repo = IntentRepository(s, self._clock)
            planned = {
                pid: r.sl_distance for pid, r in repo.filled_opens(position_ids).items() if r.sl_distance
            }
            busy = {sym for sym in symbols if repo.non_terminal(sym)}
        return planned, busy

    async def run_once(self) -> LoopReport:
        report = LoopReport()
        now = self._clock.now()
        positions = [p for p in await self._broker.positions() if p.magic == self._cfg.engine.magic]
        if not positions:
            return report
        planned, busy = await asyncio.to_thread(
            self._facts_sync, [p.position_id for p in positions], {p.symbol for p in positions}
        )
        for pos in positions:
            sym_cfg = self._symbols.get(pos.symbol)
            if sym_cfg is None or pos.symbol in busy:
                continue  # unknown symbol (never touched) or an intent is still in flight for it
            try:
                tick = await self._market.tick(pos.symbol)
                if tick is None:
                    continue
                spec = await self._broker.symbol_spec(pos.symbol)
                trigger = self._cfg.profiles[sym_cfg.profile].trigger_tf
                bars = await self._market.closed_bars(pos.symbol, trigger, ATR_BARS)
                atr = last_close = None
                if len(bars) >= 15:
                    series = ind.atr(
                        np.array([float(b.high) for b in bars]),
                        np.array([float(b.low) for b in bars]),
                        np.array([float(b.close) for b in bars]),
                        14,
                    )
                    if not np.isnan(series[-1]):
                        atr = to_decimal(float(series[-1])).quantize(spec.tick_size)
                    last_close = bars[-1].close
                action = self._manager.plan(
                    PositionFacts(
                        position=pos,
                        spec=spec,
                        tick=tick,
                        trigger_tf=trigger,
                        trade_weekends=sym_cfg.trade_weekends,
                        planned_sl_distance=planned.get(pos.position_id),
                        atr=atr,
                        last_closed_close=last_close,
                    ),
                    now,
                )
                if action is not None:
                    report.actions.append((action.kind, await self._executor.execute(action.intent, spec)))
            except BrokerError as exc:
                report.errors.append(f"{pos.symbol} {pos.ticket}: {exc}")
        return report
