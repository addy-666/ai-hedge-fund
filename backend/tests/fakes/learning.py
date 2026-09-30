"""Seed the learning loop's inputs: closed trades and finished virtual trades, each with a decision and an
entry snapshot (tests only)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from aifund.domain.enums import (
    CloseReason,
    DecisionOutcome,
    Side,
    TradeOutcome,
    TradeStatus,
    VirtualArm,
    VirtualStatus,
)
from aifund.domain.ids import new_id
from aifund.persistence.tables import DecisionRow, FeatureSnapshotRow, TradeRow, VirtualTradeRow

_positions = itertools.count(1)


def seed_outcome(
    s: Session,
    *,
    when: datetime,
    r: float | Decimal,
    features: dict[str, Any],
    side: Side = Side.BUY,
    symbol: str = "XAUUSD",
    setup_tag: str = "mtf_trend_pullback",
    virtual: bool = False,
    arm: VirtualArm = VirtualArm.BLOCKED,
    account: str = "acc",
    tf: str = "M15",
    confidence: int = 70,
    enriched: bool = True,
) -> tuple[str, str]:
    """One finished outcome entered at ``when``; returns (decision id, trade or virtual id)."""
    snap = FeatureSnapshotRow(
        id=new_id(),
        symbol=symbol,
        trigger_tf=tf,
        bar_time=when - timedelta(minutes=15),
        feature_set_version=2,
        features=features,
        bars_ref={},
        created_at=when,
    )
    s.add(snap)
    s.flush()
    decision = DecisionRow(
        id=new_id(),
        account_id=account,
        symbol=symbol,
        trigger_tf=tf,
        bar_time=snap.bar_time,
        snapshot_id=snap.id,
        stage_reached="EXECUTION",
        outcome=DecisionOutcome.ORDERED if not virtual else DecisionOutcome.RULE_BLOCKED,
        proposal={
            "direction": "LONG" if side is Side.BUY else "SHORT",
            "setup_tag": setup_tag,
            "thesis": "seeded",
            "key_risks": ["none"],
        },
        llm_confidence=confidence,
        created_at=when,
    )
    s.add(decision)
    s.flush()
    rr = Decimal(str(r))
    if virtual:
        row: Any = VirtualTradeRow(
            id=new_id(),
            account_id=account,
            decision_id=decision.id,
            arm=arm,
            snapshot_id=snap.id,
            symbol=symbol,
            side=side,
            setup_tag=setup_tag,
            entry_time=when,
            entry_price=Decimal("100"),
            sl_distance=Decimal("1"),
            tp_distance=Decimal("2"),
            expires_at=when + timedelta(hours=3),
            expire_reason=CloseReason.TIME_STOP,
            status=VirtualStatus.CLOSED,
            exit_time=when + timedelta(hours=1),
            r_multiple=rr,
            created_at=when,
        )
    else:
        row = TradeRow(
            id=new_id(),
            account_id=account,
            position_id=next(_positions),
            decision_id=decision.id,
            snapshot_id=snap.id,
            symbol=symbol,
            side=side,
            setup_tag=setup_tag,
            trigger_tf=tf,
            status=TradeStatus.CLOSED,
            open_time=when,
            open_price=Decimal("100"),
            volume_opened=Decimal("0.1"),
            volume_open_now=Decimal(0),
            initial_sl=Decimal("99") if side is Side.BUY else Decimal("101"),
            close_time=when + timedelta(hours=1),
            close_reason=CloseReason.SL if rr < 0 else CloseReason.TP,
            r_multiple=rr,
            net_pnl=rr * 10,
            outcome=TradeOutcome.WIN if rr > 0 else TradeOutcome.LOSS,
            mae_r=min(rr, Decimal(0)),
            mfe_r=max(rr, Decimal(0)),
            bars_held=4,
            holding_minutes=60,
            enriched_at=when + timedelta(hours=1) if enriched else None,
            created_at=when,
            updated_at=when,
        )
    s.add(row)
    s.flush()
    return decision.id, row.id
