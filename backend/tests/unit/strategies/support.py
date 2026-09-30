"""Shared helpers for detector tests: snapshots from feature dicts and the reflected market.

``reflect`` maps a snapshot's features onto the market reflected through price ``k`` (price -> k - price,
highs <-> lows), using the registry's mirror table; prices (``*.close``) become ``k - close``. A symmetric
detector must find the mirrored SHORT on the reflected market wherever it finds a LONG, with every level at
``k - level``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aifund.domain.decision import FeatureSnapshot
from aifund.domain.enums import Timeframe
from aifund.market.feature_registry import CATEGORY_MIRROR, MirrorKind, mirror_of
from aifund.strategies.base import TfRoles

ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4, Timeframe.D1))
T = datetime(2026, 9, 28, 9, 45, tzinfo=UTC)


def snap(base: dict[str, Any], **overrides: Any) -> FeatureSnapshot:
    features = {**base, **{k.replace("__", "."): v for k, v in overrides.items()}}
    return FeatureSnapshot(
        symbol="XAUUSD",
        trigger_tf=Timeframe.M15,
        bar_time=T,
        feature_set_version=2,
        features=features,
        bars_ref={Timeframe.M15: T},
    )


def _map(kind: MirrorKind, value: Any) -> Any:
    if value is None or kind is MirrorKind.SAME:
        return value
    if kind is MirrorKind.NEGATE:
        return -value
    if kind is MirrorKind.COMPLEMENT:
        return 100.0 - value
    if kind is MirrorKind.NOT:
        return not value
    return CATEGORY_MIRROR.get(value, value)


def reflect(features: dict[str, Any], k: float) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in features.items():
        if name.endswith(".close"):
            out[name] = k - value
            continue
        mirror = mirror_of(name)
        assert mirror is not None, f"{name} has no mirror"
        partner, kind = mirror
        out[partner] = _map(kind, value)
    return out


def mirrored(levels: dict[str, Decimal], k: float) -> dict[str, Decimal]:
    return {name: Decimal(str(k)) - value for name, value in levels.items()}
