"""Feature registry — the single source of feature names (docs/02 §3).

It is the contract between the feature builder, the learned-rule DSL, the pattern miner and the LLM
auditor: rules may only reference names listed here with ``available_at_entry=True``. Per-timeframe
features are named ``<tf>.<base>`` (``m15.rsi14``, ``h1.adx14``); context features ``ctx.<name>``;
proposal fields ``prop.<name>`` (filled at rule-evaluation time, not by the snapshot builder).

Changing what a feature means or adding one = bump FEATURE_SET_VERSION.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum

from aifund.domain.enums import Direction, EmaStack, Regime, Timeframe

FEATURE_SET_VERSION = 2  # v2 (2026-09-28): close, ema50_above_ema200, *_dist_ema50_atr, stochastics


class FeatureType(StrEnum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    CATEGORY = "category"


class MirrorKind(StrEnum):
    """How a feature's value transforms when the price series is reflected (price -> K - price, highs <->
    lows): the SHORT side of a symmetric hypothesis is the LONG side on the reflected market (docs/09 §5).
    Every kind is its own inverse."""

    SAME = "same"  # direction-neutral (ATR rank, ADX, session)
    NEGATE = "negate"  # value -> -value (signed distances, slopes)
    COMPLEMENT = "complement"  # value -> 100 - value (0-100 oscillators: RSI, stochastics)
    NOT = "not"  # bool -> not bool
    CATEGORY = "category"  # BULL <-> BEAR, TREND_UP <-> TREND_DOWN; other categories unchanged


CATEGORY_MIRROR = {"BULL": "BEAR", "BEAR": "BULL", "TREND_UP": "TREND_DOWN", "TREND_DOWN": "TREND_UP"}


@dataclass(frozen=True)
class Mirror:
    """On the reflected market, ``partner`` (this feature when None) takes the value ``kind`` maps this
    feature's value to. Partners are mutual: low_dist_ema50_atr <-> high_dist_ema50_atr (NEGATE)."""

    kind: MirrorKind
    partner: str | None = None


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
    mirror: Mirror | None = None  # None: no declared mirror (a "mirror" short side may not use it)

    @property
    def bounds(self) -> tuple[float, float] | None:
        """Declared numeric range for bounded units ("0-100", "0-1"); thresholds must lie inside it."""
        m = re.fullmatch(r"(\d+)-(\d+)", self.unit)
        return (float(m.group(1)), float(m.group(2))) if m else None


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
    # --- feature set v2
    ("close", _F, "price", "last close (a price level: not scale-free, avoid in learned rules)", None),
    ("ema50_above_ema200", _B, "", "EMA50 > EMA200", None),
    ("low_dist_ema50_atr", _F, "ATR", "(last bar low - EMA50) / ATR14", None),
    ("high_dist_ema50_atr", _F, "ATR", "(last bar high - EMA50) / ATR14", None),
    ("stoch_k", _F, "0-100", "Slow Stochastics(14,3,3) %K", None),
    ("stoch_d", _F, "0-100", "Slow Stochastics(14,3,3) %D", None),
    ("stoch_cross_up", _B, "", "%K crossed above %D on the last bar", None),
    ("stoch_cross_down", _B, "", "%K crossed below %D on the last bar", None),
)
_V2 = {
    "close",
    "ema50_above_ema200",
    "low_dist_ema50_atr",
    "high_dist_ema50_atr",
    "stoch_k",
    "stoch_d",
    "stoch_cross_up",
    "stoch_cross_down",
}

_M = Mirror
_SAME, _NEG, _C100, _NOT, _CAT = (
    _M(MirrorKind.SAME), _M(MirrorKind.NEGATE), _M(MirrorKind.COMPLEMENT), _M(MirrorKind.NOT),
    _M(MirrorKind.CATEGORY),
)  # fmt: skip
TF_MIRRORS: dict[str, Mirror] = {  # base name -> mirror (partners are base names on the same timeframe)
    "dist_ema20_atr": _NEG, "dist_ema50_atr": _NEG, "dist_ema200_atr": _NEG, "ema50_slope_atr": _NEG,
    "ema_stack": _CAT, "adx14": _SAME, "close_above_ema200": _NOT, "rsi14": _C100, "rsi14_slope3": _NEG,
    "macd_hist_z": _NEG, "atr14": _SAME, "atr14_pct_rank100": _SAME,
    "bb_width_pct_rank100": _SAME,  # approximate: width / SMA depends on the price level (direction-neutral)
    "range_to_atr": _SAME, "nr7": _SAME, "rel_tick_volume20": _SAME,
    "dist_swing_high_atr": _M(MirrorKind.SAME, "dist_swing_low_atr"),
    "dist_swing_low_atr": _M(MirrorKind.SAME, "dist_swing_high_atr"),
    "bars_since_swing_break": _SAME, "body_to_range": _SAME,
    "upper_wick_to_range": _M(MirrorKind.SAME, "lower_wick_to_range"),
    "lower_wick_to_range": _M(MirrorKind.SAME, "upper_wick_to_range"),
    "candle_dir": _CAT, "ema50_above_ema200": _NOT,
    "low_dist_ema50_atr": _M(MirrorKind.NEGATE, "high_dist_ema50_atr"),
    "high_dist_ema50_atr": _M(MirrorKind.NEGATE, "low_dist_ema50_atr"),
    "stoch_k": _C100, "stoch_d": _C100,
    "stoch_cross_up": _M(MirrorKind.SAME, "stoch_cross_down"),
    "stoch_cross_down": _M(MirrorKind.SAME, "stoch_cross_up"),
    # "close" has none: a price level does not reflect into anything meaningful
}  # fmt: skip

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

_CTX_MIRRORS: dict[str, Mirror] = {  # full names; portfolio features are not snapshot features (no mirror)
    "ctx.session": _SAME, "ctx.day_of_week": _SAME, "ctx.minutes_to_next_high_impact_news": _SAME,
    "ctx.minutes_since_last_high_impact_news": _SAME, "ctx.spread_to_atr": _SAME, "ctx.regime": _CAT,
    "ctx.htf_trend_score": _NEG, "ctx.dist_pdh_atr": _M(MirrorKind.SAME, "ctx.dist_pdl_atr"),
    "ctx.dist_pdl_atr": _M(MirrorKind.SAME, "ctx.dist_pdh_atr"),
}  # fmt: skip
CTX_FEATURES = tuple(replace(s, mirror=_CTX_MIRRORS.get(s.name)) for s in CTX_FEATURES)

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


def _prefixed(mirror: Mirror | None, prefix: str) -> Mirror | None:
    if mirror is None or mirror.partner is None:
        return mirror
    return Mirror(mirror.kind, f"{prefix}.{mirror.partner}")


def tf_feature_specs(tf: Timeframe) -> tuple[FeatureSpec, ...]:
    p = tf_prefix(tf)
    return tuple(
        FeatureSpec(
            f"{p}.{base}",
            dtype,
            unit,
            f"{tf.value}: {desc}",
            FeatureSource.BARS,
            cats,
            since_version=2 if base in _V2 else 1,
            mirror=_prefixed(TF_MIRRORS.get(base), p),
        )
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


def mirror_of(name: str) -> tuple[str, MirrorKind] | None:
    """(feature whose value mirrors ``name`` on the reflected market, how it transforms), or None."""
    spec = _ALL.get(name)
    if spec is None or spec.mirror is None:
        return None
    return (spec.mirror.partner or name, spec.mirror.kind)


def is_rule_usable(name: str) -> bool:
    spec = _ALL.get(name)
    return spec is not None and spec.available_at_entry
