"""Trade lifecycle (docs/02 §2.2): one trade per broker position, opened and closed by the reconciler."""

from __future__ import annotations

from aifund.domain.enums import DecisionOutcome, ReasonCode, TradeStatus

# A trade only moves from open to closed; orphans stay orphans (no entry snapshot, excluded from learning).
TRADE_TRANSITIONS: dict[TradeStatus, frozenset[TradeStatus]] = {
    TradeStatus.OPEN: frozenset({TradeStatus.CLOSED}),
    TradeStatus.ORPHAN_OPEN: frozenset({TradeStatus.ORPHAN_CLOSED}),
    TradeStatus.CLOSED: frozenset(),
    TradeStatus.ORPHAN_CLOSED: frozenset(),
}
OPEN_STATUSES = frozenset({TradeStatus.OPEN, TradeStatus.ORPHAN_OPEN})


def closed_status(status: TradeStatus) -> TradeStatus:
    """The status an open trade moves to when its position is gone."""
    return TradeStatus.ORPHAN_CLOSED if status is TradeStatus.ORPHAN_OPEN else TradeStatus.CLOSED


def can_transition(current: TradeStatus, new: TradeStatus) -> bool:
    return new in TRADE_TRANSITIONS[current]


# Signals that get a virtual (counterfactual) trade (docs/03 §14.4): a directional decision stopped by a rule,
# the confidence threshold, a guard or a limit. Not when the engine already holds that position (duplicates),
# nor for sizing, broker or internal failures: those say nothing about whether the signal was any good.
VIRTUAL_OUTCOMES = frozenset(
    {DecisionOutcome.RULE_BLOCKED, DecisionOutcome.BELOW_THRESHOLD, DecisionOutcome.RISK_REJECTED}
)
VIRTUAL_REASONS = frozenset(
    {
        ReasonCode.RULE_BLOCK,
        ReasonCode.BELOW_THRESHOLD,
        ReasonCode.LOSS_LIMIT,
        ReasonCode.COOLDOWN,
        ReasonCode.FLIP_FLOP,
        ReasonCode.DAILY_SYMBOL_CAP,
        ReasonCode.FOREIGN_POSITION,
        ReasonCode.REVERSAL_WEAK,
        ReasonCode.REVERSAL_TOO_EARLY,
        ReasonCode.REVERSAL_CAP,
        ReasonCode.MAX_POSITIONS,
        ReasonCode.PORTFOLIO_HEAT,
        ReasonCode.BUCKET_HEAT,
        ReasonCode.LEVERAGE_CAP,
    }
)


def gets_virtual_trade(outcome: DecisionOutcome, reason: ReasonCode | None) -> bool:
    return outcome in VIRTUAL_OUTCOMES and reason in VIRTUAL_REASONS
