"""Trade lifecycle (docs/02 §2.2): one trade per broker position, opened and closed by the reconciler."""

from __future__ import annotations

from aifund.domain.enums import TradeStatus

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
