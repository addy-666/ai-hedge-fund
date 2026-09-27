"""ORM tables — one per entity in docs/02 §1. Schema changes go through Alembic only.

Conventions: ULID string ids; money/prices as ``DecimalText``; timestamps as ``UtcDateTime``; small stable
enums stored as strings with a CHECK constraint; evolving vocabularies (reason codes) as plain strings.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aifund.domain.enums import (
    CommandStatus,
    DealEntry,
    DealReason,
    DecisionOutcome,
    EngineState,
    IntentKind,
    IntentStatus,
    Mode,
    RuleStatus,
    Side,
    TradeOutcome,
    TradeStatus,
)
from aifund.persistence.types import DecimalText, UtcDateTime

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

ULID = String(26)
ACCOUNT = String(64)


def _enum(enum_cls: type[StrEnum]) -> Enum:
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=24,
        name=enum_cls.__name__.lower(),
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        Decimal: DecimalText(),
        datetime: UtcDateTime(),
        dict[str, Any]: JSON(),
        list[Any]: JSON(),
    }


# ============================================================ system & control


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    yaml_text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime]


class EngineStateRow(Base):
    __tablename__ = "engine_state"

    account_id: Mapped[str] = mapped_column(ACCOUNT, primary_key=True)
    state: Mapped[EngineState] = mapped_column(_enum(EngineState))
    mode: Mapped[Mode] = mapped_column(_enum(Mode))
    halt_reason: Mapped[str | None] = mapped_column(String(500))
    day_start_equity: Mapped[Decimal | None]
    peak_equity: Mapped[Decimal | None]
    config_version_id: Mapped[str | None] = mapped_column(ForeignKey("config_versions.id"))
    rulebook_version: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime]


class HeartbeatRow(Base):
    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_beat_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any] | None]


class CommandRow(Base):
    __tablename__ = "commands"
    __table_args__ = (Index("ix_commands_status_created", "status", "created_at"),)

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any] | None]
    status: Mapped[CommandStatus] = mapped_column(_enum(CommandStatus))
    result: Mapped[dict[str, Any] | None]
    requested_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class EventRow(Base):
    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any] | None]


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any] | None]
    ip: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(index=True)


# ============================================================ market & decisions


class SymbolRow(Base):
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    canonical: Mapped[str] = mapped_column(String(16))
    asset_class: Mapped[str] = mapped_column(String(16))
    digits: Mapped[int]
    point: Mapped[Decimal]
    tick_size: Mapped[Decimal]
    tick_value: Mapped[Decimal]
    contract_size: Mapped[Decimal]
    volume_min: Mapped[Decimal]
    volume_max: Mapped[Decimal]
    volume_step: Mapped[Decimal]
    stops_level_points: Mapped[int]
    freeze_level_points: Mapped[int]
    filling_mode_flags: Mapped[int]
    currency_profit: Mapped[str] = mapped_column(String(8))
    currency_margin: Mapped[str] = mapped_column(String(8))
    raw: Mapped[dict[str, Any] | None]
    refreshed_at: Mapped[datetime]


class FeatureSnapshotRow(Base):
    __tablename__ = "feature_snapshots"
    __table_args__ = (UniqueConstraint("symbol", "trigger_tf", "bar_time", "feature_set_version"),)

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    trigger_tf: Mapped[str] = mapped_column(String(4))
    bar_time: Mapped[datetime]
    feature_set_version: Mapped[int]
    features: Mapped[dict[str, Any]]
    bars_ref: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]


class DecisionRow(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_symbol_bar", "symbol", "bar_time"),
        Index("ix_decisions_outcome_created", "outcome", "created_at"),
    )

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    account_id: Mapped[str] = mapped_column(ACCOUNT)
    symbol: Mapped[str] = mapped_column(String(32))
    trigger_tf: Mapped[str] = mapped_column(String(4))
    bar_time: Mapped[datetime]
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("feature_snapshots.id"))
    stage_reached: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[DecisionOutcome] = mapped_column(_enum(DecisionOutcome))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    reason_detail: Mapped[str | None] = mapped_column(Text)
    setups: Mapped[list[Any] | None]
    proposal: Mapped[dict[str, Any] | None]
    llm_confidence: Mapped[int | None]
    calibrated_confidence: Mapped[int | None]
    penalty_points: Mapped[int | None]
    final_confidence: Mapped[int | None]
    risk_factor: Mapped[Decimal | None]
    rules_matched: Mapped[list[Any] | None]
    lessons_shown: Mapped[list[Any] | None]
    rulebook_version: Mapped[int | None]
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    config_version_id: Mapped[str | None] = mapped_column(ForeignKey("config_versions.id"))
    risk_calc: Mapped[dict[str, Any] | None]
    latency_ms: Mapped[int | None]
    cost_usd: Mapped[Decimal | None]
    created_at: Mapped[datetime]


# ============================================================ orders, trades, deals


class OrderIntentRow(Base):
    __tablename__ = "order_intents"
    __table_args__ = (Index("ix_order_intents_symbol_status", "symbol", "status"),)

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(24), unique=True)
    account_id: Mapped[str] = mapped_column(ACCOUNT)
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))
    kind: Mapped[IntentKind] = mapped_column(_enum(IntentKind))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(_enum(Side))
    volume: Mapped[Decimal]
    price_ref: Mapped[Decimal]
    sl: Mapped[Decimal | None]
    tp: Mapped[Decimal | None]
    sl_distance: Mapped[Decimal | None]
    tp_distance: Mapped[Decimal | None]
    risk_money: Mapped[Decimal]
    risk_pct: Mapped[Decimal]
    comment: Mapped[str] = mapped_column(String(31))
    magic: Mapped[int] = mapped_column(BigInteger)
    position_ticket: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[IntentStatus] = mapped_column(_enum(IntentStatus))
    retcode: Mapped[int | None]
    retcode_name: Mapped[str | None] = mapped_column(String(64))
    broker_comment: Mapped[str | None] = mapped_column(String(255))
    order_ticket: Mapped[int | None] = mapped_column(BigInteger)
    deal_ticket: Mapped[int | None] = mapped_column(BigInteger)
    position_id: Mapped[int | None] = mapped_column(BigInteger)
    fill_price: Mapped[Decimal | None]
    fill_volume: Mapped[Decimal | None]
    slippage_points: Mapped[int | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]


class TradeRow(Base):
    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_status_symbol", "status", "symbol"),)

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    account_id: Mapped[str] = mapped_column(ACCOUNT)
    position_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    intent_id: Mapped[str | None] = mapped_column(ForeignKey("order_intents.id"))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("feature_snapshots.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(_enum(Side))
    setup_tag: Mapped[str | None] = mapped_column(String(40))
    trigger_tf: Mapped[str | None] = mapped_column(String(4))
    status: Mapped[TradeStatus] = mapped_column(_enum(TradeStatus))
    open_time: Mapped[datetime]
    open_price: Mapped[Decimal]
    volume_opened: Mapped[Decimal]
    volume_open_now: Mapped[Decimal]
    initial_sl: Mapped[Decimal | None]
    initial_tp: Mapped[Decimal | None]
    current_sl: Mapped[Decimal | None]
    current_tp: Mapped[Decimal | None]
    initial_risk_money: Mapped[Decimal | None]
    close_time: Mapped[datetime | None]
    close_price_vwap: Mapped[Decimal | None]
    close_reason: Mapped[str | None] = mapped_column(String(24))
    gross_profit: Mapped[Decimal | None]
    commission: Mapped[Decimal | None]
    swap: Mapped[Decimal | None]
    fee: Mapped[Decimal | None]
    net_pnl: Mapped[Decimal | None]
    r_multiple: Mapped[Decimal | None]
    mae_r: Mapped[Decimal | None]
    mfe_r: Mapped[Decimal | None]
    bars_held: Mapped[int | None]
    holding_minutes: Mapped[int | None]
    outcome: Mapped[TradeOutcome | None] = mapped_column(_enum(TradeOutcome))
    review_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class DealRow(Base):
    __tablename__ = "deals"

    ticket: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    account_id: Mapped[str] = mapped_column(ACCOUNT)
    order: Mapped[int] = mapped_column(BigInteger)
    position_id: Mapped[int] = mapped_column(BigInteger, index=True)
    time_utc: Mapped[datetime]
    time_server: Mapped[int] = mapped_column(BigInteger)
    side: Mapped[Side] = mapped_column(_enum(Side))
    entry: Mapped[DealEntry] = mapped_column(_enum(DealEntry))
    reason: Mapped[DealReason] = mapped_column(_enum(DealReason))
    magic: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(32))
    volume: Mapped[Decimal]
    price: Mapped[Decimal]
    profit: Mapped[Decimal]
    commission: Mapped[Decimal]
    swap: Mapped[Decimal]
    fee: Mapped[Decimal]
    comment: Mapped[str | None] = mapped_column(String(64))
    raw: Mapped[dict[str, Any] | None]


class VirtualTradeRow(Base):
    __tablename__ = "virtual_trades"
    __table_args__ = (Index("ix_virtual_trades_status", "status"),)

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    account_id: Mapped[str] = mapped_column(ACCOUNT)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"))
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("feature_snapshots.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(_enum(Side))
    setup_tag: Mapped[str | None] = mapped_column(String(40))
    entry_time: Mapped[datetime]
    entry_price: Mapped[Decimal]
    sl: Mapped[Decimal]
    tp: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(16))
    exit_time: Mapped[datetime | None]
    exit_price: Mapped[Decimal | None]
    exit_reason: Mapped[str | None] = mapped_column(String(24))
    r_multiple: Mapped[Decimal | None]
    mae_r: Mapped[Decimal | None]
    mfe_r: Mapped[Decimal | None]
    blocked_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ACCOUNT)
    ts: Mapped[datetime] = mapped_column(index=True)
    balance: Mapped[Decimal]
    equity: Mapped[Decimal]
    margin: Mapped[Decimal]
    free_margin: Mapped[Decimal]
    open_risk_money: Mapped[Decimal]
    open_notional: Mapped[Decimal]
    open_positions: Mapped[int]
    day_pnl: Mapped[Decimal]
    drawdown_pct: Mapped[Decimal]


# ============================================================ learning


class AuditRunRow(Base):
    __tablename__ = "audit_runs"

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))
    window_from: Mapped[datetime]
    window_to: Mapped[datetime]
    n_trades: Mapped[int]
    n_virtual: Mapped[int]
    miner_output: Mapped[dict[str, Any] | None]
    candidates: Mapped[list[Any] | None]
    validation: Mapped[dict[str, Any] | None]
    lessons_md: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class LLMCallRow(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_agent_created", "agent", "created_at"),)

    id: Mapped[str] = mapped_column(ULID, primary_key=True)
    agent: Mapped[str] = mapped_column(String(32))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"), index=True)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("trades.id"))
    audit_run_id: Mapped[str | None] = mapped_column(ForeignKey("audit_runs.id"))
    model: Mapped[str] = mapped_column(String(128))
    model_reported: Mapped[str | None] = mapped_column(String(128))
    prompt_template: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    messages: Mapped[list[Any] | None]
    response_text: Mapped[str | None] = mapped_column(Text)
    parsed: Mapped[dict[str, Any] | None]
    valid: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal | None]
    latency_ms: Mapped[int | None]
    created_at: Mapped[datetime]


class TradeReviewRow(Base):
    __tablename__ = "trade_reviews"

    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"), primary_key=True)
    tags: Mapped[list[Any]]
    thesis_verdict: Mapped[str] = mapped_column(String(16))
    execution_quality: Mapped[int]
    lesson: Mapped[str] = mapped_column(Text)
    llm_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.id"))
    created_at: Mapped[datetime]


class RuleRow(Base):
    __tablename__ = "rules"
    __table_args__ = (Index("ix_rules_status", "status"),)

    rule_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[RuleStatus] = mapped_column(_enum(RuleStatus))
    dsl: Mapped[dict[str, Any]]
    dsl_sha256: Mapped[str] = mapped_column(String(64), index=True)
    hypothesis: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None]
    origin: Mapped[str] = mapped_column(String(16))
    audit_run_id: Mapped[str | None] = mapped_column(ForeignKey("audit_runs.id"))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None]
    shadow_started_at: Mapped[datetime | None]
    activated_at: Mapped[datetime | None]
    review_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    retired_at: Mapped[datetime | None]
    retire_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class RulebookVersionRow(Base):
    __tablename__ = "rulebook_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    active_rules: Mapped[list[Any]]
    shadow_rules: Mapped[list[Any]]
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime]


class RuleEvaluationRow(Base):
    __tablename__ = "rule_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(16), index=True)
    rule_version: Mapped[int]
    mode: Mapped[str] = mapped_column(String(8))
    matched: Mapped[bool] = mapped_column(Boolean)
    action_applied: Mapped[dict[str, Any] | None]


class CalibrationModelRow(Base):
    __tablename__ = "calibration_models"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    method: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict[str, Any] | None]
    n_samples: Mapped[int]
    brier_before: Mapped[float | None] = mapped_column(Float)
    brier_after: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]
