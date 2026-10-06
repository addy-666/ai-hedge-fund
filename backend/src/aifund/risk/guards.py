"""Duplicate and reversal guards (docs/03 §9). Pure: the Risk Manager gathers the facts, this decides.

Duplicate protection is layered; any layer rejects. Here: idempotency (a key already used — the DB's
UNIQUE constraint remains the final word at insert time), in-flight intents on the symbol, foreign
positions (other magics / manual trades are never touched and, by default, block the symbol), a
same-direction engine position, the per-symbol position cap, the post-close cooldown (longer after a
loss) and the per-symbol daily trade cap. (The per-symbol asyncio lock is the pipeline's job.)

An opposite-direction engine position triggers the reversal rules: ``reversal_mode`` (ignore /
close_only / close_and_reverse), extra confidence, a minimum holding time, a daily reversal cap and the
flip-flop lock. The guard never sends anything: it returns what may happen, and the executor closes the
old position (and verifies the close) BEFORE any new one is opened.

Family slots (roadmap 10.8, ``guards.family_slots``): the duplicate and reversal rules look only at the
positions of the candidate's strategy family (a position whose family is unknown counts as the candidate's:
the conservative reading). A position of another family does not block; an opposite one is hedged only with
``hedge_across_families``. ``max_positions_per_symbol`` stays the hard count, the per-symbol daily cap the
hard ceiling, and the strategy's own daily cap (10.9) follows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise

from aifund.config.trading_config import GuardsConfig
from aifund.domain.enums import Direction, ReasonCode, ReversalMode, Timeframe
from aifund.domain.intent import Rejection
from aifund.domain.market import Position


class GuardAction(StrEnum):
    OPEN = "OPEN"  # no engine position on the symbol: open the new one
    CLOSE_ONLY = "CLOSE_ONLY"  # close the opposite position, open nothing
    CLOSE_AND_REVERSE = "CLOSE_AND_REVERSE"  # close the opposite position, then re-check and open
    REJECT = "REJECT"


@dataclass(frozen=True)
class GuardContext:
    symbol: str
    direction: Direction  # LONG or SHORT
    final_confidence: int
    confidence_threshold: int
    magic: int
    now: datetime
    trigger_tf: Timeframe
    broker_positions: list[Position]  # ALL positions on the symbol, any magic (live broker state)
    in_flight_intents: int = 0
    idempotency_key_used: bool = False
    bars_since_last_close: int | None = None
    last_close_was_loss: bool = False
    trades_today: int = 0
    reversals_today: int = 0
    recent_directions: list[Direction] = field(default_factory=list)  # directional decisions, oldest first
    flip_flop_locked: bool = False
    # roadmap 10.8-10.9 (the pipeline judges cooldown and flip-flops within the family when slots are on)
    family: str | None = None  # the candidate's strategy family; None: the family slots do not apply
    position_families: Mapping[int, str] = field(default_factory=dict)  # engine ticket -> family (known)
    trades_today_strategy: int = 0  # this strategy's (setup tag) opens on this symbol today
    strategy_cap: int | None = None  # its cap for today's regime; None: no strategy cap


@dataclass(frozen=True)
class GuardVerdict:
    action: GuardAction
    rejection: Rejection | None = None
    target_position: Position | None = None  # the position to close for CLOSE_* actions
    start_flip_flop_lock: bool = False


def _reject(reason: ReasonCode, detail: str, *, lock: bool = False) -> GuardVerdict:
    return GuardVerdict(
        GuardAction.REJECT, Rejection(reason=reason, detail=detail), start_flip_flop_lock=lock
    )


def is_flip_flop(recent: list[Direction], new: Direction, window: int) -> bool:
    """True if the last ``window`` directional decisions, including ``new``, strictly alternate."""
    seq = [*[d for d in recent if d is not Direction.NONE][-(window - 1) :], new]
    return len(seq) >= window and all(a is not b for a, b in pairwise(seq))


def evaluate_guards(
    ctx: GuardContext, cfg: GuardsConfig, *, max_positions_per_symbol: int, max_trades_per_symbol_per_day: int
) -> GuardVerdict:
    if ctx.direction is Direction.NONE:
        return _reject(ReasonCode.INTERNAL_ERROR, "guards called without a direction")
    side = ctx.direction.to_side()
    every = [p for p in ctx.broker_positions if p.symbol == ctx.symbol and p.magic == ctx.magic]
    foreign = [p for p in ctx.broker_positions if p.symbol == ctx.symbol and p.magic != ctx.magic]
    if cfg.family_slots and ctx.family is not None:
        own = [p for p in every if ctx.position_families.get(p.ticket, ctx.family) == ctx.family]
        others = [p for p in every if p not in own]
    else:
        own, others = every, []

    if ctx.idempotency_key_used:
        return _reject(ReasonCode.DUPLICATE_IDEMPOTENCY, "this bar and direction were already acted on")
    if ctx.in_flight_intents:
        return _reject(ReasonCode.INTENT_IN_FLIGHT, f"{ctx.in_flight_intents} intent(s) unresolved")
    if foreign and cfg.block_if_foreign_position_on_symbol:
        tickets = ", ".join(str(p.ticket) for p in foreign)
        return _reject(ReasonCode.FOREIGN_POSITION, f"positions from other EAs/manual trading: {tickets}")
    if ctx.flip_flop_locked:
        return _reject(ReasonCode.FLIP_FLOP, "symbol locked after flip-flopping")
    if any(p.side is side for p in own):
        return _reject(ReasonCode.DUPLICATE_SAME_DIRECTION, f"already {ctx.direction} on {ctx.symbol}")
    if is_flip_flop(ctx.recent_directions, ctx.direction, cfg.flip_flop_window):
        return _reject(ReasonCode.FLIP_FLOP, f"last {cfg.flip_flop_window} decisions alternate", lock=True)

    opposite = [p for p in own if p.side is side.opposite]
    if opposite:
        return _reversal(ctx, cfg, opposite[0])
    hedged = [p for p in others if p.side is side.opposite]
    if hedged and not cfg.hedge_across_families:
        p = hedged[0]
        family = ctx.position_families.get(p.ticket)
        return _reject(ReasonCode.HEDGE_OFF, f"opposite {family} position {p.ticket} open; hedging is off")

    if len(every) >= max_positions_per_symbol:
        return _reject(ReasonCode.MAX_PER_SYMBOL, f"{len(every)} engine position(s) on {ctx.symbol}")
    if ctx.bars_since_last_close is not None:
        needed = cfg.cooldown_bars_after_loss if ctx.last_close_was_loss else cfg.cooldown_bars_after_close
        if ctx.bars_since_last_close < needed:
            kind = "loss" if ctx.last_close_was_loss else "close"
            return _reject(
                ReasonCode.COOLDOWN, f"{ctx.bars_since_last_close}/{needed} bars since last {kind}"
            )
    if ctx.trades_today >= max_trades_per_symbol_per_day:
        return _reject(ReasonCode.DAILY_SYMBOL_CAP, f"{ctx.trades_today} trade(s) on {ctx.symbol} today")
    if ctx.strategy_cap is not None and ctx.trades_today_strategy >= ctx.strategy_cap:
        done, cap = ctx.trades_today_strategy, ctx.strategy_cap
        return _reject(ReasonCode.DAILY_STRATEGY_CAP, f"{done} trade(s) of this strategy today (cap {cap})")
    return GuardVerdict(GuardAction.OPEN)


def _reversal(ctx: GuardContext, cfg: GuardsConfig, pos: Position) -> GuardVerdict:
    if cfg.reversal_mode is ReversalMode.IGNORE:
        return _reject(ReasonCode.POSITION_EXISTS, f"opposite position {pos.ticket} open; reversals disabled")
    needed = ctx.confidence_threshold + cfg.reversal_extra_confidence
    if ctx.final_confidence < needed:
        return _reject(
            ReasonCode.REVERSAL_WEAK, f"confidence {ctx.final_confidence} < {needed} for a reversal"
        )
    held = int((ctx.now - pos.time) / timedelta(minutes=ctx.trigger_tf.minutes))
    if held < cfg.reversal_min_hold_bars:
        return _reject(
            ReasonCode.REVERSAL_TOO_EARLY, f"position held {held}/{cfg.reversal_min_hold_bars} bars"
        )
    if ctx.reversals_today >= cfg.max_reversals_per_symbol_per_day:
        return _reject(ReasonCode.REVERSAL_CAP, f"{ctx.reversals_today} reversal(s) today")
    action = (
        GuardAction.CLOSE_AND_REVERSE
        if cfg.reversal_mode is ReversalMode.CLOSE_AND_REVERSE
        else GuardAction.CLOSE_ONLY
    )
    return GuardVerdict(action, target_position=pos)
