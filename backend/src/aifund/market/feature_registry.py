"""Feature registry — the single source of feature names (docs/02 §3).

It is the contract between the feature builder, the learned-rule DSL, the pattern miner and the LLM
auditor: rules may only reference names listed here with ``available_at_entry=True``. Per-timeframe
features are named ``<tf>.<base>`` (``m15.rsi14``, ``h1.adx14``); context features ``ctx.<name>``;
proposal fields ``prop.<name>`` (filled at rule-evaluation time, not by the snapshot builder).

Changing what a feature means or adding one = bump FEATURE_SET_VERSION.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from aifund.domain.enums import Direction, EmaStack, Regime, Timeframe

FEATURE_SET_VERSION = 1


class FeatureType(StrEnum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    CATEGORY = "category"


class FeatureSource(StrEnum):
    BARS = "bars"  # computed from closed bars of one timeframe
    CONTEXT = "context"  # time, quote, cross-timeframe, calendar
    PORTFOLIO = "portfolio"  # account/positions state at decision time (filled by the pipeline)
    PROPOSAL = "proposal"  # the analyst's proposal (filled at rule evaluation)


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    dtype: FeatureType
    unit: str
    description: str
    source: FeatureSource
    categories: tuple[str, ...] | None = None
    available_at_entry: bool = True
    since_version: int = 1


_F, _I, _B, _C = FeatureType.FLOAT, FeatureType.INT, FeatureType.BOOL, FeatureType.CATEGORY
_STACK = tuple(s.value for s in EmaStack)
_CANDLE = ("BULL", "BEAR", "DOJI")
SESSIONS = ("ASIA", "LONDON", "OVERLAP", "NY", "OFF")

# Base names for every timeframe; the full name is "<tf>.<base>".
TF_FEATURES: tuple[tuple[str, FeatureType, str, str, tuple[str, ...] | None], ...] = (
    ("dist_ema20_atr", _F, "ATR", "(close - EMA20) / ATR14", None),
    ("dist_ema50_atr", _F, "ATR", "(close - EMA50) / ATR14", None),
    ("dist_ema200_atr", _F, "ATR", "(close - EMA200) / ATR14", None),
    ("ema50_slope_atr", _F, "ATR", "EMA50 change over 5 bars / ATR14", None),
    ("ema_stack", _C, "", "BULL: EMA20>EMA50>EMA200, BEAR: reverse, else MIXED", _STACK),
    ("adx14", _F, "0-100", "Wilder ADX(14)", None),
    ("close_above_ema200", _B, "", "close > EMA200", None),
    ("rsi14", _F, "0-100", "Wilder RSI(14)", None),
    ("rsi14_slope3", _F, "RSI points", "RSI14 change over 3 bars", None),
    ("macd_hist_z", _F, "z", "MACD(12,26,9) histogram z-score over 100 bars", None),
    ("atr14", _F, "price", "Wilder ATR(14)", None),
    ("atr14_pct_rank100", _F, "0-1", "mid-rank percentile of ATR14 within the last 100 bars", None),
    ("bb_width_pct_rank100", _F, "0-1", "mid-rank percentile of Bollinger(20,2) width within 100 bars", None),
    ("range_to_atr", _F, "ATR", "last bar (high - low) / ATR14", None),
    ("nr7", _B, "", "last bar range strictly narrower than each of the previous six", None),
    ("rel_tick_volume20", _F, "x", "tick volume / mean tick volume of the previous 20 bars", None),
    ("dist_swing_high_atr", _F, "ATR", "(last confirmed 5-bar swing high - close) / ATR14", None),
    ("dist_swing_low_atr", _F, "ATR", "(close - last confirmed 5-bar swing low) / ATR14", None),
    ("bars_since_swing_break", _I, "bars", "bars since a close broke the then-current swing high/low", None),
    ("body_to_range", _F, "0-1", "|close - open| / (high - low) of the last bar", None),
    ("upper_wick_to_range", _F, "0-1", "(high - max(open, close)) / range", None),
    ("lower_wick_to_range", _F, "0-1", "(min(open, close) - low) / range", None),
    ("candle_dir", _C, "", "BULL / BEAR, or DOJI when body < 10% of range", _CANDLE),
)

CTX_FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "ctx.session", _C, "", "UTC session of the trigger bar close", FeatureSource.CONTEXT, SESSIONS
    ),
    FeatureSpec(
        "ctx.day_of_week", _I, "0=Mon", "weekday (UTC) of the trigger bar close", FeatureSource.CONTEXT
    ),
    FeatureSpec(
        "ctx.minutes_to_next_high_impact_news",
        _I,
        "min",
        "calendar (Phase 8); null until then",
        FeatureSource.CONTEXT,
    ),
    FeatureSpec(
        "ctx.minutes_since_last_high_impact_news",
        _I,
        "min",
        "calendar (Phase 8); null until then",
        FeatureSource.CONTEXT,
    ),
    FeatureSpec("ctx.spread_to_atr", _F, "ATR", "current spread / trigger-TF ATR14", FeatureSource.CONTEXT),
    FeatureSpec(
        "ctx.regime",
        _C,
        "",
        "setup-TF regime (market/regime.py)",
        FeatureSource.CONTEXT,
        tuple(r.value for r in Regime),
    ),
    FeatureSpec(
        "ctx.htf_trend_score",
        _I,
        "-n..+n",
        "sum over setup+context TFs of +1 BULL / -1 BEAR EMA stack",
        FeatureSource.CONTEXT,
    ),
    FeatureSpec(
        "ctx.dist_pdh_atr",
        _F,
        "ATR",
        "(previous day high - close) / trigger ATR14 (needs D1)",
        FeatureSource.CONTEXT,
    ),
    FeatureSpec(
        "ctx.dist_pdl_atr",
        _F,
        "ATR",
        "(close - previous day low) / trigger ATR14 (needs D1)",
        FeatureSource.CONTEXT,
    ),
    FeatureSpec("ctx.open_positions_count", _I, "", "engine positions open", FeatureSource.PORTFOLIO),
    FeatureSpec(
        "ctx.symbol_open_risk_pct",
        _F,
        "% equity",
        "initial risk open on this symbol",
        FeatureSource.PORTFOLIO,
    ),
    FeatureSpec(
        "ctx.portfolio_heat_pct", _F, "% equity", "sum of open initial risk", FeatureSource.PORTFOLIO
    ),
    FeatureSpec(
        "ctx.consecutive_losses_symbol",
        _I,
        "",
        "losing trades in a row on this symbol",
        FeatureSource.PORTFOLIO,
    ),
    FeatureSpec("ctx.drawdown_pct", _F, "%", "equity drawdown from peak", FeatureSource.PORTFOLIO),
)

PROP_FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "prop.direction",
        _C,
        "",
        "proposed direction",
        FeatureSource.PROPOSAL,
        tuple(d.value for d in Direction if d is not Direction.NONE),
    ),
    FeatureSpec("prop.setup_tag", _C, "", "detected setup the proposal is based on", FeatureSource.PROPOSAL),
    FeatureSpec("prop.llm_confidence", _I, "0-100", "analyst raw confidence", FeatureSource.PROPOSAL),
    FeatureSpec(
        "prop.sl_atr_multiple", _F, "ATR", "stop distance / ATR after clamping", FeatureSource.PROPOSAL
    ),
    FeatureSpec("prop.rr_target", _F, "R", "target distance / stop distance", FeatureSource.PROPOSAL),
    FeatureSpec(
        "prop.htf_alignment",
        _I,
        "-n..+n",
        "ctx.htf_trend_score x (+1 LONG / -1 SHORT)",
        FeatureSource.PROPOSAL,
    ),
)


def tf_prefix(tf: Timeframe) -> str:
    return tf.value.lower()


def tf_feature_specs(tf: Timeframe) -> tuple[FeatureSpec, ...]:
    p = tf_prefix(tf)
    return tuple(
        FeatureSpec(f"{p}.{base}", dtype, unit, f"{tf.value}: {desc}", FeatureSource.BARS, cats)
        for base, dtype, unit, desc, cats in TF_FEATURES
    )


def registry(timeframes: Iterable[Timeframe] = tuple(Timeframe)) -> dict[str, FeatureSpec]:
    specs: list[FeatureSpec] = [s for tf in timeframes for s in tf_feature_specs(tf)]
    specs += [*CTX_FEATURES, *PROP_FEATURES]
    return {s.name: s for s in specs}


_ALL = registry()


def get(name: str) -> FeatureSpec:
    """Look up any registered feature (all timeframes). KeyError if it does not exist."""
    return _ALL[name]


def is_rule_usable(name: str) -> bool:
    spec = _ALL.get(name)
    return spec is not None and spec.available_at_entry
