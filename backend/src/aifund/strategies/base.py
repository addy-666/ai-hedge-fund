"""Setup detector interface (docs/03 §6). Detectors are deterministic and read ONLY the feature snapshot,
so every setup they report can be re-derived later from stored data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aifund.domain.decision import FeatureSnapshot, FeatureValue, SetupCandidate
from aifund.domain.enums import Timeframe
from aifund.market.feature_registry import tf_prefix


@dataclass(frozen=True)
class TfRoles:
    trigger: Timeframe
    setup: Timeframe
    context: tuple[Timeframe, ...]

    @property
    def trend_filter(self) -> Timeframe:
        """The highest context timeframe (e.g. D1) — the playbooks' 'higher timeframe trend'."""
        return max(self.context, key=lambda tf: tf.minutes) if self.context else self.setup


class SetupDetector(Protocol):
    setup_tag: str
    playbook_id: str
    version: str

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]: ...


def feature(snapshot: FeatureSnapshot, tf: Timeframe, name: str) -> FeatureValue:
    return snapshot.features.get(f"{tf_prefix(tf)}.{name}")


def num(snapshot: FeatureSnapshot, tf: Timeframe, name: str) -> float | None:
    value = feature(snapshot, tf, name)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
