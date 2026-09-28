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
