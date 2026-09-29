"""Schema of config/trading.yaml (docs/02 §4).

Every model forbids unknown keys: a misspelled option must fail at startup, not silently fall back to a
default in an unattended trading process. The engine refuses to start on any validation error.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aifund.domain.enums import AssetClass, Mode, ReversalMode, Timeframe

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Pct = Annotated[Decimal, Field(gt=0, le=100)]
Multiplier = Annotated[Decimal, Field(gt=0)]
Factor = Annotated[Decimal, Field(gt=0, le=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_hhmm(value: str | None) -> str | None:
    if value is not None and not _HHMM.match(value):
        raise ValueError(f"expected HH:MM (24h), got {value!r}")
    return value


# ---------------------------------------------------------------- engine / strategy


class EngineConfig(_Strict):
    account_label: str = Field(min_length=1)
    mode: Mode = Mode.SIM
    allow_live: bool = False
    magic: int = Field(gt=0, lt=2**63)
    # the trading day (daily/weekly loss limits, day P&L) rolls at this LOCAL time: 17:00 New York is the
    # FX/metals rollover and the broker's server midnight; it moves with DST (21:00 UTC summer, 22:00 winter)
    trading_day_boundary: str = "17:00"
    trading_day_timezone: str = "America/New_York"
    timezone_display: str = "UTC"
    auto_resume_after_crash: bool = False
    bar_close_grace_s: int = Field(default=3, ge=0, le=60)
    max_deviation_points: int = Field(default=20, ge=0, le=1000)

    _hhmm = field_validator("trading_day_boundary")(_check_hhmm)

    @field_validator("trading_day_timezone")
    @classmethod
    def _known_day_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _live_requires_allow_live(self) -> Self:
        if self.mode is Mode.LIVE and not self.allow_live:
            raise ValueError("mode LIVE requires engine.allow_live: true")
        return self


class StrategyConfig(_Strict):
    analyst_enabled: bool = True  # the LLM analyst decides (Phase 4); else the deterministic baseline
    baseline_enabled: bool = False
    dry_run: bool = False  # decide and risk-check everything, record what would be sent, send nothing
    analyst_prompt_version: int = Field(default=1, ge=1)
    # The analyst's decisions reach the Risk Manager only with this on (docs/09 §7 G-LLM). Off, the analyst
    # decides in SHADOW: both its decision and the baseline's become shadow virtual trades on every bar with a
    # candidate, and the baseline (if enabled) trades. Outside SIM, on also needs a G-LLM sign-off record.
    analyst_orders: bool = False

    @model_validator(mode="after")
    def _orders_need_the_analyst(self) -> StrategyConfig:
        if self.analyst_orders and not self.analyst_enabled:
            raise ValueError("strategy.analyst_orders needs strategy.analyst_enabled")
        return self


# ---------------------------------------------------------------- symbols / profiles


class SymbolConfig(_Strict):
    canonical: str = Field(pattern=r"^[A-Z0-9]{3,12}$")
    broker: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass
    profile: str
    correlation_bucket: str = Field(min_length=1)
    max_spread_points: int | None = Field(default=None, gt=0)
    max_spread_to_atr: Decimal = Field(gt=0, le=1)
    trade_weekends: bool = False
    news_currencies: list[Annotated[str, Field(pattern=r"^[A-Z]{3}$")]] = Field(
        default_factory=lambda: ["USD"]
    )  # the news gate blacks out HIGH-impact events of these currencies (docs/03 §4 gate 5)
    session: str | None = None
    """Name of a ``sessions`` calendar. Required unless the symbol trades weekends: the position manager
    flattens before every long closure (weekend, holiday early close) that calendar knows about."""


# ---------------------------------------------------------------- market sessions


class SessionConfig(_Strict):
    """A broker trading-session calendar, in the exchange's local time so DST is handled.

    The MT5 Python API cannot read a symbol's sessions, so the calendar is configuration: the weekly
    schedule plus holiday early closes and closed days copied from the broker's holiday notices. Trading day
    D runs from ``daily_open`` on the previous calendar day to ``daily_close`` on D (Monday's starts Sunday).
    An unlisted early close cannot be anticipated: when unsure, list it (flattening early is the safe side).
    """

    timezone: str = "America/New_York"
    daily_close: str = "17:00"
    daily_open: str = "18:00"
    early_closes: dict[date, str] = Field(default_factory=dict)
    closed_days: list[date] = Field(default_factory=list)

    _hhmm = field_validator("daily_close", "daily_open")(_check_hhmm)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("early_closes")
    @classmethod
    def _early_hhmm(cls, value: dict[date, str]) -> dict[date, str]:
        for day, hhmm in value.items():
            if not _HHMM.match(hhmm):
                raise ValueError(f"early close {day}: expected HH:MM (24h), got {hhmm!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        both = sorted(set(self.early_closes) & set(self.closed_days))
        if both:
            raise ValueError(f"days listed as both early close and closed: {both}")
        if self.daily_open <= self.daily_close:
            raise ValueError(
                "daily_open must be after daily_close (the next trading day starts that evening)"
            )
        return self


class ProfileConfig(_Strict):
    trigger_tf: Timeframe
    setup_tf: Timeframe
    context_tfs: list[Timeframe] = Field(min_length=1)
    bars_per_tf: int = Field(default=300, ge=300, le=5000)  # EMA200 warm-up + 100-bar percentiles
    require_setup: bool = True

    @model_validator(mode="after")
    def _timeframes_ascend(self) -> Self:
        if not self.trigger_tf.minutes < self.setup_tf.minutes:
            raise ValueError("setup_tf must be a higher timeframe than trigger_tf")
        for tf in self.context_tfs:
            if tf.minutes <= self.setup_tf.minutes:
                raise ValueError(f"context timeframe {tf} must be higher than setup_tf {self.setup_tf}")
        if len(set(self.context_tfs)) != len(self.context_tfs):
            raise ValueError("context_tfs contains duplicates")
        return self


# ---------------------------------------------------------------- risk


class ConfidenceScaling(_Strict):
    at_threshold: Factor = Decimal("0.5")
    at_90: Factor = Decimal("1.0")

    @model_validator(mode="after")
    def _monotonic(self) -> Self:
        if self.at_threshold > self.at_90:
            raise ValueError("confidence_risk_scaling.at_threshold must be <= at_90")
        return self


class VolRegimeScaling(_Strict):
    atr_pct_rank_above: Decimal = Field(default=Decimal("0.9"), gt=0, lt=1)
    factor: Factor = Decimal("0.5")


class DrawdownScaling(_Strict):
    dd_pct_above: Pct = Decimal("5.0")
    factor: Factor = Decimal("0.5")


class StopsConfig(_Strict):
    atr_tf: Literal["trigger", "setup"] = "trigger"
    k_sl_default: Multiplier = Decimal("1.5")
    k_sl_min: Multiplier = Decimal("1.0")
    k_sl_max: Multiplier = Decimal("3.0")
    invalidation_buffer_atr: Decimal = Field(default=Decimal("0.1"), ge=0, le=1)
    rr_default: Multiplier = Decimal("2.0")
    rr_min: Multiplier = Decimal("1.2")
    rr_max: Multiplier = Decimal("4.0")

    @model_validator(mode="after")
    def _bands_ordered(self) -> Self:
        if not self.k_sl_min <= self.k_sl_default <= self.k_sl_max:
            raise ValueError("stops require k_sl_min <= k_sl_default <= k_sl_max")
        if not self.rr_min <= self.rr_default <= self.rr_max:
            raise ValueError("stops require rr_min <= rr_default <= rr_max")
        if self.rr_min < 1:
            raise ValueError("stops.rr_min must be >= 1")
        return self


class LimitsConfig(_Strict):
    max_open_positions: int = Field(default=5, ge=1, le=50)
    max_positions_per_symbol: int = Field(default=1, ge=1, le=5)
    max_portfolio_heat_pct: Pct = Decimal("3.0")
    max_bucket_heat_pct: Pct = Decimal("1.5")
    max_trades_per_symbol_per_day: int = Field(default=4, ge=1, le=100)
    daily_loss_limit_pct: Pct = Decimal("3.0")
    weekly_loss_limit_pct: Pct = Decimal("6.0")
    max_drawdown_pct: Pct = Decimal("10.0")
    # Notional exposure cap independent of broker margin: high-leverage accounts make margin checks
    # meaningless (prototype audit Q100c). Σ open notional (account ccy) <= equity × this.
    max_notional_leverage: Decimal = Field(default=Decimal("10"), gt=0, le=500)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_bucket_heat_pct > self.max_portfolio_heat_pct:
            raise ValueError("limits.max_bucket_heat_pct must be <= max_portfolio_heat_pct")
        if not self.daily_loss_limit_pct <= self.weekly_loss_limit_pct <= self.max_drawdown_pct:
            raise ValueError(
                "limits require daily_loss_limit_pct <= weekly_loss_limit_pct <= max_drawdown_pct"
            )
        if self.max_positions_per_symbol > self.max_open_positions:
            raise ValueError("limits.max_positions_per_symbol must be <= max_open_positions")
        return self


class GuardsConfig(_Strict):
    cooldown_bars_after_close: int = Field(default=2, ge=0, le=500)
    cooldown_bars_after_loss: int = Field(default=4, ge=0, le=500)
    block_if_foreign_position_on_symbol: bool = True
    reversal_mode: ReversalMode = ReversalMode.CLOSE_ONLY
    reversal_extra_confidence: int = Field(default=15, ge=0, le=100)
    reversal_min_hold_bars: int = Field(default=3, ge=0, le=500)
    max_reversals_per_symbol_per_day: int = Field(default=1, ge=0, le=20)
    flip_flop_window: int = Field(default=3, ge=2, le=20)
    flip_flop_lock_bars: int = Field(default=8, ge=0, le=500)


ImpactLevel = Literal["HIGH", "MEDIUM", "LOW"]


def _default_impact() -> list[ImpactLevel]:
    return ["HIGH"]


class NewsConfig(_Strict):
    enabled: bool = False
    blackout_minutes_before: int = Field(default=15, ge=0, le=240)
    blackout_minutes_after: int = Field(default=15, ge=0, le=240)
    impact: list[ImpactLevel] = Field(default_factory=_default_impact)


class RiskConfig(_Strict):
    risk_per_trade_pct: Pct = Decimal("0.5")
    max_risk_per_trade_pct: Pct = Decimal("1.0")
    confidence_threshold: int = Field(default=65, ge=0, le=100)
    confidence_risk_scaling: ConfidenceScaling = ConfidenceScaling()
    vol_regime_scaling: VolRegimeScaling = VolRegimeScaling()
    drawdown_scaling: DrawdownScaling = DrawdownScaling()
    stops: StopsConfig = StopsConfig()
    min_lot_overshoot_pct: Decimal = Field(default=Decimal("0"), ge=0, le=50)
    max_lots_per_symbol: Decimal = Field(default=Decimal("5.0"), gt=0)
    commission_per_lot_roundtrip: Literal["auto"] | Annotated[Decimal, Field(ge=0)] = "auto"
    max_margin_utilisation: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    limits: LimitsConfig = LimitsConfig()
    guards: GuardsConfig = GuardsConfig()
    news: NewsConfig = NewsConfig()

    @model_validator(mode="after")
    def _risk_within_ceiling(self) -> Self:
        if self.risk_per_trade_pct > self.max_risk_per_trade_pct:
            raise ValueError("risk.risk_per_trade_pct must be <= max_risk_per_trade_pct")
        if self.max_risk_per_trade_pct > self.limits.daily_loss_limit_pct:
            raise ValueError("risk.max_risk_per_trade_pct must be <= limits.daily_loss_limit_pct")
        return self


# ---------------------------------------------------------------- position management


class TrailingConfig(_Strict):
    mode: Literal["off", "atr"] = "off"
    k: Multiplier = Decimal("2.0")


class PositionManagementConfig(_Strict):
    break_even_at_r: Decimal | None = Field(default=None, gt=0, le=10)
    trailing: TrailingConfig = TrailingConfig()
    time_stop_bars: int | None = Field(default=48, ge=1, le=10_000)
    # symbols that do not trade weekends: flatten this long before any close that keeps the market shut for
    # at least ``long_close_hours`` (weekends, and Fridays or holidays that close early); None disables
    flatten_before_close_minutes: int | None = Field(default=30, ge=1, le=720)
    no_entries_before_close_minutes: int = Field(default=60, ge=0, le=1440)
    long_close_hours: int = Field(default=24, ge=2, le=240)


# ---------------------------------------------------------------- llm / learning / alerts


class CircuitBreakerConfig(_Strict):
    failures: int = Field(default=5, ge=1, le=100)
    open_minutes: int = Field(default=10, ge=1, le=1440)


class ModelPricing(_Strict):
    """USD per million tokens, from the provider's pricing page. DeepSeek bills cached input separately."""

    input_cache_hit_per_mtok: Decimal = Field(ge=0)
    input_cache_miss_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)


class LLMConfig(_Strict):
    provider: Literal["deepseek"] = "deepseek"
    base_url: str = Field(default="https://api.deepseek.com", pattern=r"^https://")
    analyst_model: str = Field(min_length=1)
    auditor_model: str = Field(min_length=1)
    temperature: Decimal = Field(default=Decimal("0.1"), ge=0, le=2)
    timeout_s: int = Field(default=30, ge=1, le=600)
    auditor_timeout_s: int = Field(default=180, ge=1, le=1800)
    max_retries: int = Field(default=2, ge=0, le=5)
    daily_budget_usd: Decimal = Field(default=Decimal("5.0"), gt=0)
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)  # model id -> prices (for cost and budget)


class LearningConfig(_Strict):
    audit_schedule_utc: str = "00:30"
    audit_min_new_trades: int = Field(default=10, ge=1)
    audit_cooldown_hours: int = Field(default=12, ge=0)
    window_days: int = Field(default=120, ge=7)
    min_matches_total: int = Field(default=20, ge=5)
    min_matches_holdout: int = Field(default=6, ge=2)
    min_effect_r: Decimal = Field(default=Decimal("0.30"), gt=0)
    fdr_q: Decimal = Field(default=Decimal("0.10"), gt=0, lt=1)
    holdout_fraction: Decimal = Field(default=Decimal("0.30"), gt=0, lt=1)
    max_rule_conditions: int = Field(default=3, ge=1, le=5)
    max_rule_coverage: Decimal = Field(default=Decimal("0.30"), gt=0, le=1)
    max_active_rules: int = Field(default=25, ge=1, le=200)
    max_total_penalty: int = Field(default=40, ge=0, le=100)
    shadow_min_matches: int = Field(default=10, ge=1)
    shadow_max_days: int = Field(default=30, ge=1)
    auto_promote_max_penalty: int = Field(default=15, ge=0, le=30)
    review_after_days: int = Field(default=30, ge=1)
    expire_after_days: int = Field(default=90, ge=1)

    _hhmm = field_validator("audit_schedule_utc")(_check_hhmm)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.min_matches_holdout > self.min_matches_total:
            raise ValueError("learning.min_matches_holdout must be <= min_matches_total")
        if self.review_after_days > self.expire_after_days:
            raise ValueError("learning.review_after_days must be <= expire_after_days")
        return self


class AlertsConfig(_Strict):
    telegram: bool = False
    daily_summary_utc: str | None = "21:05"

    _hhmm = field_validator("daily_summary_utc")(_check_hhmm)


# ---------------------------------------------------------------- root


class ResearchConfig(_Strict):
    """Evidence gates and research defaults (docs/09 §7). Change a threshold only with a decisions-log entry:
    the gates exist to reject strategies, and lowering one to let a strategy through defeats them."""

    holdout_fraction: Decimal = Field(default=Decimal("0.25"), ge=Decimal("0.1"), le=Decimal("0.5"))
    folds: int = Field(default=4, ge=2, le=20)
    min_train_signals: int = Field(default=30, ge=5)
    fdr_q: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.2"))
    min_oos_signals: int = Field(default=200, ge=30)
    min_positive_fold_share: Decimal = Field(default=Decimal("0.6"), ge=Decimal("0.5"), le=1)
    min_holdout_signals: int = Field(default=60, ge=20)
    min_forward_signals: int = Field(default=100, ge=30)
    min_paired_signals: int = Field(default=150, ge=30)
    max_months_to_evidence: int = Field(default=6, ge=1, le=36)
    commission_per_lot: Decimal = Field(default=Decimal(0), ge=0)  # round turn, account currency
    slippage_points: int = Field(default=0, ge=0)
    max_hypotheses_per_run: int = Field(default=5, ge=1, le=20)


class TradingConfig(_Strict):
    engine: EngineConfig
    strategy: StrategyConfig = StrategyConfig()
    symbols: list[SymbolConfig] = Field(min_length=1)
    profiles: dict[str, ProfileConfig] = Field(min_length=1)
    risk: RiskConfig = RiskConfig()
    position_management: PositionManagementConfig = PositionManagementConfig()
    sessions: dict[str, SessionConfig] = Field(default_factory=dict)
    llm: LLMConfig
    learning: LearningConfig = LearningConfig()
    alerts: AlertsConfig = AlertsConfig()
    research: ResearchConfig = ResearchConfig()

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        canon = [s.canonical for s in self.symbols]
        broker = [s.broker for s in self.symbols]
        if len(set(canon)) != len(canon):
            raise ValueError("symbols: duplicate canonical names")
        if len(set(broker)) != len(broker):
            raise ValueError("symbols: duplicate broker symbols")
        unknown = sorted({s.profile for s in self.symbols} - self.profiles.keys())
        if unknown:
            raise ValueError(f"symbols reference undefined profiles: {unknown}")
        for s in self.symbols:
            if s.session is None and not s.trade_weekends:
                raise ValueError(f"symbol {s.canonical}: set a session calendar (it does not trade weekends)")
            if s.session is not None and s.session not in self.sessions:
                raise ValueError(f"symbol {s.canonical}: undefined session {s.session!r}")
        if not (self.strategy.analyst_enabled or self.strategy.baseline_enabled):
            raise ValueError("strategy: enable analyst_enabled and/or baseline_enabled")
        if self.strategy.analyst_enabled and self.engine.mode is not Mode.SIM:
            for field in ("analyst_model", "auditor_model"):
                model = getattr(self.llm, field)
                if model.startswith("<"):
                    raise ValueError(f"llm.{field} is still a placeholder; set a real model id")
                if model not in self.llm.pricing:
                    raise ValueError(f"llm.pricing has no prices for {model!r}: the daily budget needs them")
        return self

    def symbol(self, canonical: str) -> SymbolConfig:
        for s in self.symbols:
            if s.canonical == canonical:
                return s
        raise KeyError(canonical)
