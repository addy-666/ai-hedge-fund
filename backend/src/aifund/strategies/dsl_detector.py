"""Entry-rule DSL detector (docs/09 §5, roadmap R.5): a hypothesis written as data becomes a detector.

    {"id": "H-0007", "version": 1, "setup_tag": "h1_trend_m15_rsi_reclaim",
     "long":  {"all": [{"feature": "h4.ema50_above_ema200", "op": "==", "value": true},
                       {"feature": "m15.rsi14", "op": "between", "value": [35, 50]}]},
     "short": "mirror",
     "invalidation": {"type": "atr", "tf": "trigger", "k": 1.5},
     "target": {"type": "rr", "rr": 2.0},
     "mechanism": "Pullbacks in an H4 uptrend that reclaim RSI mid-range resume the trend."}

Conditions use the shared grammar (``market/conditions.py``) restricted to features the snapshot builder
fills (bar and context features; not portfolio or proposal fields) that are scale-free (no raw price
levels such as ``close`` or ``atr14``, which do not transfer between symbols or years). ``"mirror"`` builds
the SHORT side from the registry's declared mirrors. Levels are ATR-based from the trigger close:
invalidation = close ∓ k·ATR(tf); target = close ± rr·k·ATR. They are PRICES, like any detector's levels:
the live stop planner (``risk/stops.plan_stops``) measures them from the actual entry (the ask for a BUY),
adds its invalidation buffer and the spread, and clamps to the ATR and RR bands — so the realised stop is a
little wider than k·ATR and the realised RR a little below ``rr``, exactly as a live trade would be.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction, Timeframe
from aifund.domain.values import to_decimal
from aifund.market import conditions as cnd
from aifund.market.conditions import Condition
from aifund.market.feature_registry import FeatureSource, FeatureSpec, tf_prefix
from aifund.strategies.base import TfRoles, num

MAX_PREDICATES = 6  # per side: a hypothesis with more conditions is a curve fit, not a mechanism
SNAPSHOT_SOURCES = (FeatureSource.BARS, FeatureSource.CONTEXT)


def entry_feature(spec: FeatureSpec) -> str | None:
    if spec.source not in SNAPSHOT_SOURCES:
        return f"{spec.name} is a {spec.source.value} feature, not in the entry snapshot"
    if spec.unit == "price":
        return f"{spec.name} is a raw price level (not scale-free); use an ATR-normalised feature"
    return None


class AtrStop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["atr"]
    tf: Literal["trigger", "setup"] = "trigger"
    k: float = Field(gt=0, le=10)


class RrTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["rr"]
    rr: float = Field(gt=0, le=10)


class EntryHypothesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
    version: int = Field(ge=1)
    setup_tag: str = Field(pattern=r"^[a-z0-9_]{3,40}$")  # SetupCandidate's pattern
    long: Condition | None = None
    short: Condition | Literal["mirror"] | None = None
    invalidation: AtrStop
    target: RrTarget | None = None
    mechanism: str = Field(min_length=10, max_length=600)

    @model_validator(mode="after")
    def _valid(self) -> EntryHypothesis:
        if self.long is None and self.short is None:
            raise ValueError("a hypothesis needs a long side, a short side, or both")
        if self.short == "mirror" and self.long is None:
            raise ValueError('short "mirror" needs a long side to mirror')
        for side in ("long", "short"):  # long first: a mirror is only resolved once its source is valid
            try:
                cond = self.long if side == "long" else self.short_condition
                if cond is not None:
                    cnd.check(cond, max_predicates=MAX_PREDICATES, allow=entry_feature)
            except cnd.ConditionError as exc:
                raise ValueError(f"{side}: {exc}") from None
        return self

    @property
    def short_condition(self) -> Condition | None:
        """The SHORT condition, with ``"mirror"`` resolved (ConditionError if a feature has no mirror)."""
        if self.short == "mirror":
            assert self.long is not None  # checked by the validator
            return cnd.mirror(self.long)
        return self.short

    def behaviour(self) -> dict[str, object]:
        """Everything that changes which signals fire and where the levels sit (not the prose)."""
        short = self.short_condition
        return {
            "setup_tag": self.setup_tag,
            "long": self.long.model_dump(mode="json", by_alias=True) if self.long is not None else None,
            "short": short.model_dump(mode="json", by_alias=True) if short is not None else None,
            "invalidation": self.invalidation.model_dump(mode="json"),
            "target": self.target.model_dump(mode="json") if self.target is not None else None,
        }

    def params_sha256(self) -> str:
        return cnd.sha256(self.behaviour())

    def timeframes(self) -> set[Timeframe]:
        names = {n for c in (self.long, self.short_condition) if c is not None for n in cnd.features_of(c)}
        prefixes = {n.split(".", 1)[0] for n in names}
        return {tf for tf in Timeframe if tf_prefix(tf) in prefixes}


class DslDetector:
    def __init__(self, hypothesis: EntryHypothesis, roles: TfRoles) -> None:
        profile = {roles.trigger, roles.setup, *roles.context}
        outside = hypothesis.timeframes() - profile
        if outside:
            names = ", ".join(sorted(tf.value for tf in outside))
            raise ValueError(f"{hypothesis.id} uses {names}, not in the profile's timeframes")
        self.hypothesis = hypothesis
        self.roles = roles
        self.setup_tag = hypothesis.setup_tag
        self.playbook_id = hypothesis.id
        self.version = str(hypothesis.version)
        self._sides = [
            (d, c)
            for d, c in ((Direction.LONG, hypothesis.long), (Direction.SHORT, hypothesis.short_condition))
            if c is not None
        ]

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        out = []
        for direction, cond in self._sides:
            if cnd.evaluate(cond, snapshot.features):
                candidate = self._candidate(snapshot, direction)
                if candidate is not None:
                    out.append(candidate)
        return out

    def _candidate(self, s: FeatureSnapshot, d: Direction) -> SetupCandidate | None:
        h = self.hypothesis
        stop_tf = self.roles.trigger if h.invalidation.tf == "trigger" else self.roles.setup
        close, atr = num(s, self.roles.trigger, "close"), num(s, stop_tf, "atr14")
        if close is None or atr is None or atr <= 0:
            return None
        sign = 1 if d is Direction.LONG else -1
        risk = h.invalidation.k * atr
        levels = {
            "entry_ref": to_decimal(close),
            "invalidation": to_decimal(round(close - sign * risk, 10)),
        }
        if h.target is not None:
            levels["target"] = to_decimal(round(close + sign * h.target.rr * risk, 10))
        return SetupCandidate(
            setup_tag=h.setup_tag,
            playbook_id=h.id,
            direction_hint=d,
            key_levels=levels,
            strength=0.5,
            notes=f"{h.id} v{h.version}",
        )
