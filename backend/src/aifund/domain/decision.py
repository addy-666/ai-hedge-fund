"""Decision-pipeline models: feature snapshots, setup candidates, the analyst's proposal, final decisions.

``TradeProposal`` is the contract with the LLM (docs/03 §7.2). It is deliberately strict: numbers must be
JSON numbers (no "85" strings, no "15%"), enums must match exactly, unknown fields are rejected. A
proposal that does not validate yields an INVALID decision — it is never repaired into a trade.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from aifund.domain.enums import Direction, MistakeTag, ObjectionSeverity, ThesisVerdict, Timeframe
from aifund.domain.values import UtcDatetime

FeatureValue = StrictInt | StrictFloat | StrictBool | StrictStr | None


class FeatureSnapshot(BaseModel):
    """Immutable, persisted features for one closed trigger bar (docs/02 §3).

    Computed from closed bars only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    trigger_tf: Timeframe
    bar_time: UtcDatetime
    feature_set_version: int = Field(ge=1)
    features: dict[str, FeatureValue]
    bars_ref: dict[Timeframe, UtcDatetime]
    """Open time of the last closed bar used per timeframe — evidence that no forming bar was used."""


class SetupCandidate(BaseModel):
    """Output of a deterministic setup detector (docs/03 §6)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    setup_tag: str = Field(pattern=r"^[a-z0-9_]{3,40}$")
    playbook_id: str
    direction_hint: Direction
    key_levels: dict[str, Decimal] = Field(default_factory=dict)
    strength: float = Field(ge=0, le=1)
    notes: str = Field(default="", max_length=300)


LLMPrice = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class TradeProposal(BaseModel):
    """The analyst LLM's structured output. The LLM never supplies volume (docs/03 §7)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    direction: Direction
    confidence: StrictInt = Field(ge=0, le=100)
    setup_tag: StrictStr = Field(pattern=r"^[a-z0-9_]{3,40}$")
    """One of the detected setup candidates' tags, or "none"."""
    invalidation_price: LLMPrice | None = None
    target_price: LLMPrice | None = None
    thesis: StrictStr = Field(min_length=1, max_length=600)
    key_risks: list[StrictStr] = Field(default_factory=list, max_length=5)
    lessons_considered: list[StrictStr] = Field(default_factory=list, max_length=20)
    time_horizon_bars: StrictInt | None = Field(default=None, ge=1, le=200)

    @property
    def is_trade(self) -> bool:
        return self.direction is not Direction.NONE and self.setup_tag != "none"


class Objection(BaseModel):
    """One reason the risk critic thinks the proposal is wrong, with how serious it is."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    severity: ObjectionSeverity
    point: StrictStr = Field(min_length=1, max_length=300)


class Critique(BaseModel):
    """The risk critic's structured output (roadmap 8.3): the devil's advocate against the best proposal.
    It never changes a direction or a level; its objections only cost the committee confidence (03 §8)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    objections: list[Objection] = Field(max_length=5)
    summary: StrictStr = Field(min_length=1, max_length=400)


class TradeReview(BaseModel):
    """The trade reviewer's structured output (docs/04 §2). Tags explain an outcome; they are never rule
    conditions (outcome information is not available at entry)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tags: list[MistakeTag] = Field(max_length=4)
    thesis_verdict: ThesisVerdict
    execution_quality: StrictInt = Field(ge=1, le=5)
    lesson: StrictStr = Field(min_length=1, max_length=400)


class FinalDecision(BaseModel):
    """Portfolio-manager output handed to the Risk Manager (docs/03 §8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    symbol: str
    direction: Direction
    setup_tag: str
    llm_confidence: int = Field(ge=0, le=100)
    calibrated_confidence: int = Field(ge=0, le=100)
    penalty_points: int = Field(ge=0, le=100)
    final_confidence: int = Field(ge=-100, le=100)
    risk_factor: Decimal = Field(gt=0, le=1)
    invalidation_price: Decimal | None = None
    target_price: Decimal | None = None
