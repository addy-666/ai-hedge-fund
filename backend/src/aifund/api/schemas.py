"""API response models (docs/05 §1): every response is one of these, so the OpenAPI schema — and the
frontend's generated types — describe exactly what the dashboard receives. Money and prices are decimal
strings in JSON."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Row(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):  # noqa: UP046 - pydantic generic model
    items: list[T]
    next_cursor: str | None = None


# ---------------------------------------------------------------- system & account


class HeartbeatOut(Row):
    component: str
    status: str
    last_beat_at: datetime
    age_s: float = 0.0
    detail: dict[str, Any] | None = None


class EngineStateOut(Row):
    account_id: str
    state: str
    mode: str
    halt_reason: str | None
    rulebook_version: int
    updated_at: datetime


class Versions(BaseModel):
    config_version: str | None
    config_sha256: str | None
    feature_set: int
    prompts: list[str]
    rulebook: int


class SystemOut(BaseModel):
    now: datetime
    engine: EngineStateOut | None
    engine_stale: bool  # no engine heartbeat for 30 s
    heartbeats: list[HeartbeatOut]
    llm_spent_today_usd: Decimal
    llm_budget_usd: Decimal
    versions: Versions


class LimitHeadroom(BaseModel):
    name: str
    used_pct: Decimal
    limit_pct: Decimal


class AccountOut(BaseModel):
    as_of: datetime | None
    balance: Decimal | None
    equity: Decimal | None
    margin: Decimal | None
    free_margin: Decimal | None
    day_pnl: Decimal | None
    drawdown_pct: Decimal | None
    open_risk_money: Decimal | None
    open_positions: int
    heat_pct: Decimal | None
    limits: list[LimitHeadroom]


class EquityPoint(Row):
    ts: datetime
    equity: Decimal
    balance: Decimal
    drawdown_pct: Decimal
    day_pnl: Decimal


# ---------------------------------------------------------------- positions, trades, decisions


class PositionOut(BaseModel):
    trade_id: str
    position_id: int
    symbol: str
    side: str
    status: str
    volume: Decimal
    open_price: Decimal
    open_time: datetime
    sl: Decimal | None
    tp: Decimal | None
    setup_tag: str | None
    last_price: Decimal | None  # last cached close (the API never asks MT5)
    r_now: Decimal | None
    age_minutes: int
    decision_id: str | None
    thesis: str | None


class TradeOut(Row):
    id: str
    position_id: int
    symbol: str
    side: str
    setup_tag: str | None
    status: str
    open_time: datetime
    open_price: Decimal
    volume_opened: Decimal
    close_time: datetime | None
    close_price_vwap: Decimal | None
    close_reason: str | None
    net_pnl: Decimal | None
    r_multiple: Decimal | None
    mae_r: Decimal | None
    mfe_r: Decimal | None
    holding_minutes: int | None
    outcome: str | None
    decision_id: str | None


class DealOut(Row):
    ticket: int
    time_utc: datetime
    side: str
    entry: str
    reason: str
    volume: Decimal
    price: Decimal
    profit: Decimal
    commission: Decimal
    swap: Decimal
    fee: Decimal


class LLMCallOut(Row):
    id: str
    agent: str
    model: str
    prompt_version: str
    messages: list[Any] | None
    response_text: str | None
    parsed: dict[str, Any] | None
    valid: bool
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: Decimal | None
    latency_ms: int | None
    created_at: datetime


class DecisionOut(Row):
    id: str
    symbol: str
    trigger_tf: str
    bar_time: datetime
    stage_reached: str
    outcome: str
    reason_code: str | None
    reason_detail: str | None
    llm_confidence: int | None
    final_confidence: int | None
    model: str | None
    cost_usd: Decimal | None
    created_at: datetime


class IntentOut(Row):
    id: str
    kind: str
    status: str
    side: str
    volume: Decimal
    price_ref: Decimal
    sl: Decimal | None
    tp: Decimal | None
    fill_price: Decimal | None
    retcode: int | None
    created_at: datetime


class DecisionDossier(DecisionOut):
    setups: list[Any] | None
    proposal: dict[str, Any] | None
    calibrated_confidence: int | None
    penalty_points: int | None
    risk_factor: Decimal | None
    rules_matched: list[Any] | None
    risk_calc: dict[str, Any] | None
    prompt_version: str | None
    latency_ms: int | None
    features: dict[str, Any] | None = None
    llm_calls: list[LLMCallOut] = Field(default_factory=list)
    intents: list[IntentOut] = Field(default_factory=list)


class Marker(BaseModel):
    time: datetime
    kind: str  # entry | exit
    price: Decimal
    label: str


class BarOut(Row):
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int


class TradeReviewOut(Row):
    """The trade reviewer's verdict (docs/04 §2): explanations, never rule conditions."""

    tags: list[str]
    thesis_verdict: str
    execution_quality: int
    lesson: str
    created_at: datetime


class TradeDossier(TradeOut):
    initial_sl: Decimal | None
    initial_tp: Decimal | None
    initial_risk_money: Decimal | None
    gross_profit: Decimal | None
    commission: Decimal | None
    swap: Decimal | None
    fee: Decimal | None
    entry_slippage_points: int | None
    exit_slippage_points: int | None
    bars_held: int | None
    review_status: str
    review: TradeReviewOut | None = None
    decision: DecisionDossier | None = None
    deals: list[DealOut] = Field(default_factory=list)
    bars: list[BarOut] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)


class VirtualTradeOut(Row):
    id: str
    decision_id: str
    arm: str
    symbol: str
    side: str
    setup_tag: str | None
    status: str
    entry_time: datetime
    entry_price: Decimal | None
    exit_time: datetime | None
    exit_reason: str | None
    r_multiple: Decimal | None
    blocked_by: str | None


# ---------------------------------------------------------------- LLM, logs, analytics


class LLMUsageRow(BaseModel):
    agent: str
    model: str
    calls: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_rate: float
    cost_usd: Decimal
    latency_p50_ms: int | None
    latency_p95_ms: int | None


class LogLine(BaseModel):
    ts: str | None
    level: str | None
    component: str | None
    event: str | None
    data: dict[str, Any]


class Summary(BaseModel):
    trades: int
    net_pnl: Decimal
    r_total: Decimal
    expectancy_r: Decimal | None
    win_rate: float | None
    profit_factor: float | None
    max_drawdown: Decimal
    sharpe_daily: float | None
    trades_per_day: float
    avg_cost: Decimal | None


class BreakdownRow(BaseModel):
    key: str
    trades: int
    net_pnl: Decimal
    expectancy_r: Decimal | None
    win_rate: float | None


class UpliftStat(BaseModel):
    n: int
    mean_r: float
    ci_low: float | None
    ci_high: float | None


class ArmStat(BaseModel):
    arm: str  # baseline / analyst / committee
    trades: int
    total_r: Decimal
    mean_r_per_trade: float | None
    win_rate: float | None
    cost_usd: Decimal


class CommitteeComparison(BaseModel):
    """Committee vs analyst vs baseline in shadow (roadmap 8.4): paired bars, net of LLM cost in R."""

    mode: str  # committee.mode now
    bars: int
    first: datetime | None
    last: datetime | None
    days: float
    arms: list[ArmStat]
    agreement: float | None
    risk_usd: Decimal | None
    vs_analyst: UpliftStat | None
    vs_baseline: UpliftStat | None


class Costs(BaseModel):
    trades: int
    commission: Decimal
    swap: Decimal
    fee: Decimal
    per_trade: Decimal | None
    avg_entry_slippage_points: float | None
    avg_exit_slippage_points: float | None


# ---------------------------------------------------------------- commands & config


class CommandIn(BaseModel):
    type: str
    payload: dict[str, Any] | None = None


class CommandAccepted(BaseModel):
    command_id: str


class CommandOut(Row):
    id: str
    type: str
    status: str
    payload: dict[str, Any] | None
    result: dict[str, Any] | None
    requested_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ConfigOut(BaseModel):
    yaml: str
    sha256: str
    version_id: str | None
    json_schema: dict[str, Any]


class ConfigIn(BaseModel):
    yaml: str = Field(min_length=1, max_length=200_000)
    comment: str | None = Field(default=None, max_length=500)


class FieldError(BaseModel):
    field: str
    message: str


class ConfigSaved(BaseModel):
    version_id: str
    changed: bool
    command_id: str | None


class ConfigVersionOut(Row):
    id: str
    sha256: str
    created_by: str
    comment: str | None
    created_at: datetime


# ---------------------------------------------------------------- learning lab (Phase 7)


class RuleOut(Row):
    rule_id: str
    version: int
    status: str
    origin: str
    text: str = ""  # the rule in one line
    action: dict[str, Any] = Field(default_factory=dict)
    dsl: dict[str, Any]
    hypothesis: str | None
    evidence: dict[str, Any] | None
    audit_run_id: str | None
    approved_by: str | None
    created_at: datetime
    shadow_started_at: datetime | None
    activated_at: datetime | None
    review_at: datetime | None
    expires_at: datetime | None
    retired_at: datetime | None
    retire_reason: str | None
    awaiting_approval: bool = False


class RuleMatch(BaseModel):
    decision_id: str
    symbol: str
    bar_time: datetime
    outcome: str
    mode: str
    matched: bool
    r: Decimal | None  # its trade's R, else its virtual trade's


class RuleDetail(BaseModel):
    rule: RuleOut
    versions: list[RuleOut]
    matches: list[RuleMatch]


class RuleIn(BaseModel):
    """An operator-authored rule: it becomes a CANDIDATE and goes through the same validator."""

    scope: dict[str, Any] = Field(default_factory=dict)
    conditions: dict[str, Any]
    action: dict[str, Any] = Field(default_factory=lambda: {"type": "penalty", "points": 10})
    hypothesis: str = Field(min_length=10, max_length=1000)


class RuleCreated(BaseModel):
    rule_id: str
    version: int
    status: str


class RulebookVersionOut(Row):
    version: int
    active_rules: list[str]
    shadow_rules: list[str]
    reason: str
    created_at: datetime


class RulebookDiff(BaseModel):
    version: int
    previous: int | None
    activated: list[str]
    deactivated: list[str]
    shadowed: list[str]
    unshadowed: list[str]
    reason: str


class AuditRunOut(Row):
    id: str
    trigger: str
    status: str
    window_from: datetime
    window_to: datetime
    n_trades: int
    n_virtual: int
    created_at: datetime
    finished_at: datetime | None


class AuditRunDetail(AuditRunOut):
    miner_output: dict[str, Any] | None
    candidates: list[Any] | None
    validation: dict[str, Any] | None
    lessons_md: str | None


class FeatureOut(BaseModel):
    name: str
    dtype: str
    unit: str
    description: str
    categories: list[str] | None
    bounds: list[float] | None
