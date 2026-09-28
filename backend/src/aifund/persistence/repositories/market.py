"""Feature snapshot persistence (docs/02 `feature_snapshots`). Snapshots are immutable once stored."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aifund.domain.decision import FeatureSnapshot
from aifund.domain.enums import Timeframe
from aifund.domain.ids import new_id
from aifund.persistence.tables import FeatureSnapshotRow
from aifund.ports.system import ClockPort


class FeatureSnapshotRepository:
    def __init__(self, session: Session, clock: ClockPort) -> None:
        self._s = session
        self._clock = clock

    def get_or_add(self, snapshot: FeatureSnapshot) -> FeatureSnapshotRow:
        """Store a snapshot once per (symbol, trigger tf, bar, feature-set version); repeats return the
        stored row unchanged (the first snapshot of a bar is the one decisions refer to)."""
        existing = self._s.scalars(
            select(FeatureSnapshotRow).where(
                FeatureSnapshotRow.symbol == snapshot.symbol,
                FeatureSnapshotRow.trigger_tf == snapshot.trigger_tf.value,
                FeatureSnapshotRow.bar_time == snapshot.bar_time,
                FeatureSnapshotRow.feature_set_version == snapshot.feature_set_version,
            )
        ).first()
        if existing is not None:
            return existing
        row = FeatureSnapshotRow(
            id=new_id(),
            symbol=snapshot.symbol,
            trigger_tf=snapshot.trigger_tf.value,
            bar_time=snapshot.bar_time,
            feature_set_version=snapshot.feature_set_version,
            features=dict(snapshot.features),
            bars_ref={tf.value: t.isoformat() for tf, t in snapshot.bars_ref.items()},
            created_at=self._clock.now(),
        )
        self._s.add(row)
        self._s.flush()
        return row

    def get(self, snapshot_id: str) -> FeatureSnapshot | None:
        row = self._s.get(FeatureSnapshotRow, snapshot_id)
        if row is None:
            return None
        return FeatureSnapshot(
            symbol=row.symbol,
            trigger_tf=Timeframe(row.trigger_tf),
            bar_time=row.bar_time,
            feature_set_version=row.feature_set_version,
            features=row.features,
            bars_ref={Timeframe(k): datetime.fromisoformat(v) for k, v in row.bars_ref.items()},
        )
