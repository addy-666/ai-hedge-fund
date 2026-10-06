"""Guards: one test per duplicate layer and reversal rule, the precedence between them, and flip-flops."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.config.trading_config import GuardsConfig
from aifund.domain.enums import Direction, ReasonCode, ReversalMode, Side, Timeframe
from aifund.domain.market import Position
from aifund.risk.guards import GuardAction, GuardContext, evaluate_guards, is_flip_flop

NOW = datetime(2026, 9, 28, 12, 0, 5, tzinfo=UTC)
MAGIC = 26092801
L, S = Direction.LONG, Direction.SHORT


def position(side: Side, *, magic: int = MAGIC, ticket: int = 501, age_bars: int = 10) -> Position:
    return Position(
        ticket=ticket,
        position_id=ticket,
        symbol="XAUUSD",
        side=side,
        volume=D("0.04"),
        price_open=D("4150"),
        magic=magic,
        time=NOW - timedelta(minutes=15 * age_bars),
    )


BASE = GuardContext(
    symbol="XAUUSD",
    direction=L,
    final_confidence=75,
    confidence_threshold=65,
    magic=MAGIC,
    now=NOW,
    trigger_tf=Timeframe.M15,
    broker_positions=[],
)
CFG = GuardsConfig()  # close_only, +15 conf, hold 3 bars, 1 reversal/day, flip-flop window 3, cooldown 2/4


def run(ctx: GuardContext = BASE, cfg: GuardsConfig = CFG):  # type: ignore[no-untyped-def]
    return evaluate_guards(ctx, cfg, max_positions_per_symbol=1, max_trades_per_symbol_per_day=4)


def reason(ctx: GuardContext, cfg: GuardsConfig = CFG) -> ReasonCode | None:
    verdict = run(ctx, cfg)
    return verdict.rejection.reason if verdict.rejection else None


def test_clean_symbol_opens() -> None:
    assert run().action is GuardAction.OPEN


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"idempotency_key_used": True}, ReasonCode.DUPLICATE_IDEMPOTENCY),
        ({"in_flight_intents": 1}, ReasonCode.INTENT_IN_FLIGHT),
        ({"broker_positions": [position(Side.SELL, magic=0)]}, ReasonCode.FOREIGN_POSITION),  # manual trade
        ({"broker_positions": [position(Side.BUY, magic=202609)]}, ReasonCode.FOREIGN_POSITION),  # other EA
        ({"flip_flop_locked": True}, ReasonCode.FLIP_FLOP),
        ({"broker_positions": [position(Side.BUY)]}, ReasonCode.DUPLICATE_SAME_DIRECTION),
        ({"bars_since_last_close": 1}, ReasonCode.COOLDOWN),
        ({"bars_since_last_close": 3, "last_close_was_loss": True}, ReasonCode.COOLDOWN),
        ({"trades_today": 4}, ReasonCode.DAILY_SYMBOL_CAP),
    ],
)
def test_each_duplicate_layer_rejects(changes: dict[str, object], expected: ReasonCode) -> None:
    assert reason(replace(BASE, **changes)) is expected  # type: ignore[arg-type]


def test_cooldown_expires() -> None:
    assert run(replace(BASE, bars_since_last_close=2)).action is GuardAction.OPEN
    assert run(replace(BASE, bars_since_last_close=4, last_close_was_loss=True)).action is GuardAction.OPEN


def test_foreign_positions_can_be_tolerated_but_are_never_targeted() -> None:
    cfg = CFG.model_copy(update={"block_if_foreign_position_on_symbol": False})
    ctx = replace(BASE, direction=L, final_confidence=90, broker_positions=[position(Side.SELL, magic=0)])
    verdict = run(ctx, cfg)
    assert verdict.action is GuardAction.OPEN  # the manual SELL is not ours: no reversal, no close
    assert verdict.target_position is None


def test_other_symbols_do_not_count() -> None:
    other = position(Side.BUY).model_copy(update={"symbol": "BTCUSD"})
    assert run(replace(BASE, broker_positions=[other])).action is GuardAction.OPEN


# ---------------------------------------------------------------- reversals

LONG_OPEN = [position(Side.BUY)]


def test_reversal_close_only_by_default() -> None:
    verdict = run(replace(BASE, direction=S, final_confidence=80, broker_positions=LONG_OPEN,
                          recent_directions=[L]))  # fmt: skip
    assert verdict.action is GuardAction.CLOSE_ONLY
    assert verdict.target_position == LONG_OPEN[0]


def test_close_and_reverse_mode() -> None:
    cfg = CFG.model_copy(update={"reversal_mode": ReversalMode.CLOSE_AND_REVERSE})
    verdict = run(replace(BASE, direction=S, final_confidence=80, broker_positions=LONG_OPEN), cfg)
    assert verdict.action is GuardAction.CLOSE_AND_REVERSE


@pytest.mark.parametrize(
    ("changes", "cfg_changes", "expected"),
    [
        ({"final_confidence": 79}, {}, ReasonCode.REVERSAL_WEAK),  # needs 65 + 15 = 80
        ({"final_confidence": 90, "broker_positions": [position(Side.BUY, age_bars=2)]}, {},
         ReasonCode.REVERSAL_TOO_EARLY),
        ({"final_confidence": 90, "reversals_today": 1}, {}, ReasonCode.REVERSAL_CAP),
        ({"final_confidence": 90}, {"reversal_mode": ReversalMode.IGNORE}, ReasonCode.POSITION_EXISTS),
    ],
)  # fmt: skip
def test_reversal_rules(
    changes: dict[str, object], cfg_changes: dict[str, object], expected: ReasonCode
) -> None:
    ctx = replace(BASE, direction=S, broker_positions=LONG_OPEN)
    assert reason(replace(ctx, **changes), CFG.model_copy(update=cfg_changes)) is expected  # type: ignore[arg-type]


# ---------------------------------------------------------------- flip-flops


def test_flip_flop_detection() -> None:
    assert is_flip_flop([L, S], L, 3)
    assert not is_flip_flop([L, L], S, 3)
    assert not is_flip_flop([S], L, 3)  # not enough history
    assert is_flip_flop([L, L, S, L, S], L, 3)  # only the last window counts
    assert not is_flip_flop([L, Direction.NONE, L], L, 3)  # HOLDs are ignored


def test_flip_flop_rejects_and_starts_a_lock() -> None:
    verdict = run(replace(BASE, direction=L, recent_directions=[L, S]))
    assert verdict.rejection is not None
    assert verdict.rejection.reason is ReasonCode.FLIP_FLOP
    assert verdict.start_flip_flop_lock


def test_precedence_idempotency_first_foreign_before_reversal() -> None:
    everything = replace(
        BASE,
        direction=S,
        final_confidence=90,
        idempotency_key_used=True,
        in_flight_intents=1,
        broker_positions=[*LONG_OPEN, position(Side.SELL, magic=0, ticket=777)],
    )
    assert reason(everything) is ReasonCode.DUPLICATE_IDEMPOTENCY
    assert reason(replace(everything, idempotency_key_used=False)) is ReasonCode.INTENT_IN_FLIGHT
    assert (
        reason(replace(everything, idempotency_key_used=False, in_flight_intents=0))
        is ReasonCode.FOREIGN_POSITION
    )


def test_none_direction_is_rejected() -> None:
    assert reason(replace(BASE, direction=Direction.NONE)) is ReasonCode.INTERNAL_ERROR


# ---------------------------------------------------------------- family slots (roadmap 10.8-10.9)

SLOTS = GuardsConfig(family_slots=True)
HEDGE = GuardsConfig(family_slots=True, hedge_across_families=True)


def fam(
    *positions: tuple[Side, str | None, int], direction: Direction = L, **changes: object
) -> GuardContext:
    """A candidate of the breakout family with engine positions (side, family, ticket) on the symbol."""
    return replace(
        BASE, direction=direction, family="breakout",
        broker_positions=[position(side, ticket=t) for side, _, t in positions],
        position_families={t: f for _, f, t in positions if f is not None}, **changes,
    )  # type: ignore[arg-type]  # fmt: skip


def run_slots(ctx: GuardContext, cfg: GuardsConfig = SLOTS, *, per_symbol: int = 3, per_day: int = 12):  # type: ignore[no-untyped-def]
    return evaluate_guards(
        ctx, cfg, max_positions_per_symbol=per_symbol, max_trades_per_symbol_per_day=per_day
    )


def test_another_family_may_stack_the_same_direction() -> None:
    assert run_slots(fam((Side.BUY, "trend", 501))).action is GuardAction.OPEN
    # without family slots the same facts are a duplicate, as before
    legacy = run_slots(fam((Side.BUY, "trend", 501)), CFG)
    assert legacy.rejection is not None
    assert legacy.rejection.reason is ReasonCode.DUPLICATE_SAME_DIRECTION


def test_within_a_family_the_old_rules_hold() -> None:
    same = run_slots(fam((Side.BUY, "breakout", 501)))
    assert same.rejection is not None
    assert same.rejection.reason is ReasonCode.DUPLICATE_SAME_DIRECTION
    opposite = run_slots(fam((Side.SELL, "breakout", 501)))  # a reversal of its own position: 75 < 65 + 15
    assert opposite.rejection is not None
    assert opposite.rejection.reason is ReasonCode.REVERSAL_WEAK
    strong = run_slots(fam((Side.SELL, "breakout", 501), final_confidence=85))
    assert (strong.action, strong.target_position.ticket) == (GuardAction.CLOSE_ONLY, 501)  # type: ignore[union-attr]


def test_a_position_of_unknown_family_conflicts_with_every_family() -> None:
    """Opened before 10.7 (no setup tag): treated as this family's, the conservative reading."""
    dup = run_slots(fam((Side.BUY, None, 501)))
    assert dup.rejection is not None
    assert dup.rejection.reason is ReasonCode.DUPLICATE_SAME_DIRECTION
    rev = run_slots(fam((Side.SELL, None, 501)))
    assert rev.rejection is not None
    assert rev.rejection.reason is ReasonCode.REVERSAL_WEAK


def test_an_opposite_signal_from_another_family_hedges_only_when_allowed() -> None:
    ctx = fam((Side.SELL, "reversal", 501))
    assert run_slots(ctx, HEDGE).action is GuardAction.OPEN  # operator decision 2026-10-06
    refused = run_slots(ctx, SLOTS)
    assert refused.rejection is not None
    assert refused.rejection.reason is ReasonCode.HEDGE_OFF
    assert "reversal position 501" in refused.rejection.detail
    # the family's own opposite position still goes through the reversal rules first
    both = fam((Side.SELL, "reversal", 501), (Side.SELL, "breakout", 502), final_confidence=85)
    assert run_slots(both, HEDGE).target_position.ticket == 502  # type: ignore[union-attr]


def test_the_count_cap_stays_the_hard_limit() -> None:
    ctx = fam((Side.BUY, "trend", 501), (Side.SELL, "reversal", 502))
    assert run_slots(ctx, HEDGE, per_symbol=3).action is GuardAction.OPEN
    full = run_slots(ctx, HEDGE, per_symbol=2)
    assert full.rejection is not None
    assert full.rejection.reason is ReasonCode.MAX_PER_SYMBOL


@pytest.mark.parametrize(
    ("today_symbol", "today_strategy", "cap", "expected"),
    [
        (2, 2, 3, None),  # under both
        (2, 3, 3, ReasonCode.DAILY_STRATEGY_CAP),  # this strategy is done for the day on this symbol
        (2, 4, 5, None),  # a trending day raised its cap to 5
        (2, 9, None, None),  # no strategy cap configured
        (12, 0, 3, ReasonCode.DAILY_SYMBOL_CAP),  # the hard per-symbol ceiling comes first
    ],
)
def test_the_daily_cap_per_strategy(
    today_symbol: int, today_strategy: int, cap: int | None, expected: ReasonCode | None
) -> None:
    ctx = replace(BASE, trades_today=today_symbol, trades_today_strategy=today_strategy, strategy_cap=cap)
    verdict = run_slots(ctx)
    assert (verdict.rejection.reason if verdict.rejection else None) is expected


def test_without_a_family_the_slots_do_not_apply() -> None:
    ctx = replace(fam((Side.BUY, "trend", 501)), family=None)
    verdict = run_slots(ctx, HEDGE)
    assert verdict.rejection is not None
    assert verdict.rejection.reason is ReasonCode.DUPLICATE_SAME_DIRECTION
