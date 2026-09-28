"""Multi-timeframe trend pullback (playbook card: config/playbooks/mtf_trend_pullback.yaml).

Source: TRADING BRAIN [[high_probability_multi_timeframe_trend_pullback]] (Link), mapped onto the profile's
roles (note → here): Daily trend filter → highest context TF; 60-min setup → setup TF; 5-min trigger →
trigger TF. LONG rules (SHORT mirrors):

1. Trend filter (context): EMA50 > EMA200 and close > EMA50.
2. Setup (setup TF): the last closed bar's low reached the EMA50 value zone
   (low within ``value_zone_atr`` ATR above EMA50, or below it), the close is not more than
   ``max_close_below_ema50_atr`` ATR under EMA50, and the close is above EMA200 — the note's invalidation
   rule ("closes below the 200 EMA on the 60-minute chart → cancel").
3. Trigger (trigger TF): Slow Stochastics(14,3,3) %K crosses above %D while %K < ``oversold``
   (the note's text says 20, its reference code uses 30; default 30, configurable).
4. Confirmation (trigger TF): a bullish candle that is a hammer (lower wick >= 50% of range) or a strong
   body (>= 60% of range). APPROXIMATION: the note asks for a hammer or engulfing bar; engulfing needs the
   previous candle, which the snapshot does not carry, so a strong bullish body stands in for it.
5. Levels: invalidation = setup-bar low − ``stop_buffer_atr`` × setup ATR (the note: below the 60-min
   pullback low − 0.5 ATR); target = the last confirmed setup-TF swing high if above entry (the note's
   first target, "prior 60-min swing high").

The note's quoted win rate / profit factor are book claims, NOT validated here; the replay backtest
measures them on real data.
"""

from __future__ import annotations

from dataclasses import dataclass

from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction
from aifund.domain.values import to_decimal
from aifund.strategies.base import TfRoles, feature, num

SETUP_TAG = "mtf_trend_pullback"
PLAYBOOK_ID = "mtf_trend_pullback"


@dataclass(frozen=True)
class PullbackParams:
    value_zone_atr: float = 0.25
    max_close_below_ema50_atr: float = 0.5
    oversold: float = 30.0
    require_candle: bool = True
    hammer_wick: float = 0.5
    strong_body: float = 0.6
    stop_buffer_atr: float = 0.5


class MtfTrendPullback:
    setup_tag = SETUP_TAG
    playbook_id = PLAYBOOK_ID
    version = "1"

    def __init__(self, roles: TfRoles, params: PullbackParams | None = None) -> None:
        self.roles = roles
        self.params = params or PullbackParams()

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        out = []
        for direction in (Direction.LONG, Direction.SHORT):
            candidate = self._detect(snapshot, direction)
            if candidate is not None:
                out.append(candidate)
        return out

    def _detect(self, s: FeatureSnapshot, d: Direction) -> SetupCandidate | None:
        p, r = self.params, self.roles
        long = d is Direction.LONG
        sign = 1 if long else -1

        # 1. higher-timeframe trend filter
        htf = r.trend_filter
        trend = feature(s, htf, "ema50_above_ema200")
        htf_dist = num(s, htf, "dist_ema50_atr")
        if trend is None or htf_dist is None or trend is not long or sign * htf_dist <= 0:
            return None

        # 2. pullback into the setup-TF EMA50 value zone, structure intact
        st = r.setup
        edge = num(s, st, "low_dist_ema50_atr" if long else "high_dist_ema50_atr")
        close_dist = num(s, st, "dist_ema50_atr")
        above_200 = feature(s, st, "close_above_ema200")
        if edge is None or close_dist is None or above_200 is None:
            return None
        if sign * edge > p.value_zone_atr or sign * close_dist < -p.max_close_below_ema50_atr:
            return None
        if above_200 is not long:
            return None

        # 3. stochastic trigger
        tr = r.trigger
        crossed = feature(s, tr, "stoch_cross_up" if long else "stoch_cross_down")
        k = num(s, tr, "stoch_k")
        if crossed is not True or k is None:
            return None
        if (long and k >= p.oversold) or (not long and k <= 100 - p.oversold):
            return None

        # 4. candle confirmation
        candle = feature(s, tr, "candle_dir")
        body = num(s, tr, "body_to_range") or 0.0
        wick = num(s, tr, "lower_wick_to_range" if long else "upper_wick_to_range") or 0.0
        confirmed = candle == ("BULL" if long else "BEAR") and (
            wick >= p.hammer_wick or body >= p.strong_body
        )
        if p.require_candle and not confirmed:
            return None

        # 5. levels (prices)
        entry = num(s, tr, "close")
        st_close, st_atr = num(s, st, "close"), num(s, st, "atr14")
        if entry is None or st_close is None or st_atr is None or st_atr <= 0:
            return None
        ema50 = st_close - close_dist * st_atr
        pullback_extreme = ema50 + edge * st_atr  # setup-bar low (long) / high (short)
        invalidation = pullback_extreme - sign * p.stop_buffer_atr * st_atr
        if sign * (entry - invalidation) <= 0:
            return None  # price already beyond the invalidation level
        levels = {"entry_ref": to_decimal(entry), "invalidation": to_decimal(round(invalidation, 10))}
        swing_dist = num(s, st, "dist_swing_high_atr" if long else "dist_swing_low_atr")
        if swing_dist is not None:
            target = st_close + sign * swing_dist * st_atr
            if sign * (target - entry) > 0:
                levels["target"] = to_decimal(round(target, 10))

        full_stack = feature(s, htf, "ema_stack") == ("BULL" if long else "BEAR")
        strength = 0.5 + (0.25 if full_stack else 0.0) + (0.25 if body >= p.strong_body else 0.0)
        return SetupCandidate(
            setup_tag=SETUP_TAG,
            playbook_id=PLAYBOOK_ID,
            direction_hint=d,
            key_levels=levels,
            strength=strength,
            notes=f"{htf.value} trend, {st.value} EMA50 pullback, {tr.value} stochastic cross at {k:.1f}",
        )
