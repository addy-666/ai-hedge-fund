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

    def params_sha256(self) -> str:
        """Fingerprint of the parameters: evidence records match it (gate E1, docs/09 §7)."""
        ...

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]: ...


def feature(snapshot: FeatureSnapshot, tf: Timeframe, name: str) -> FeatureValue:
    return snapshot.features.get(f"{tf_prefix(tf)}.{name}")


def num(snapshot: FeatureSnapshot, tf: Timeframe, name: str) -> float | None:
    value = feature(snapshot, tf, name)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class Candle:
    """A closed bar rebuilt from its snapshot features (close, ATR, range and wick ratios, direction).

    The snapshot carries no raw OHLC, only ratios; they pin the bar down exactly when its direction is known.
    A DOJI's body sign is unknown (its body is under 10% of the range), so ``candle`` returns None for it and
    ``envelope`` returns the widest high/low it can have.
    """

    open: float
    high: float
    low: float
    close: float

    @property
    def range(self) -> float:
        return self.high - self.low


def _geometry(s: FeatureSnapshot, tf: Timeframe) -> tuple[float, float, float, float, str] | None:
    close, atr, rng = num(s, tf, "close"), num(s, tf, "atr14"), num(s, tf, "range_to_atr")
    body, upper = num(s, tf, "body_to_range"), num(s, tf, "upper_wick_to_range")
    lower, direction = num(s, tf, "lower_wick_to_range"), feature(s, tf, "candle_dir")
    if None in (close, atr, rng, body, upper, lower) or not isinstance(direction, str):
        return None
    assert close is not None and atr is not None and rng is not None  # noqa: PT018 - checked above
    assert body is not None and upper is not None and lower is not None  # noqa: PT018 - checked above
    size = rng * atr
    return close, body * size, upper * size, lower * size, direction


def candle(s: FeatureSnapshot, tf: Timeframe) -> Candle | None:
    g = _geometry(s, tf)
    if g is None or g[4] not in ("BULL", "BEAR"):
        return None
    close, body, upper, lower, direction = g
    if direction == "BULL":
        return Candle(open=close - body, high=close + upper, low=close - body - lower, close=close)
    return Candle(open=close + body, high=close + body + upper, low=close - lower, close=close)


def envelope(s: FeatureSnapshot, tf: Timeframe) -> tuple[float, float] | None:
    """(high, low) of the last closed bar; for a DOJI the widest pair its unknown body sign allows."""
    g = _geometry(s, tf)
    if g is None:
        return None
    close, body, upper, lower, direction = g
    high = close + upper + (0.0 if direction == "BULL" else body)
    low = close - lower - (0.0 if direction == "BEAR" else body)
    return high, low


def trend_aligned(s: FeatureSnapshot, tf: Timeframe, long: bool) -> bool:
    """The playbooks' higher-timeframe trend filter: EMA50 beyond EMA200 and the close beyond EMA50."""
    stacked = feature(s, tf, "ema50_above_ema200")
    dist = num(s, tf, "dist_ema50_atr")
    return stacked is long and dist is not None and (dist > 0 if long else dist < 0)
