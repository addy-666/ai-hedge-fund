"""Support / resistance fade in a range (playbook card: config/playbooks/sr_fade_range.yaml).

Source: TRADING BRAIN [[high_probability_support_resistance_fade]] (Link), mapped onto the profile's roles:
the range → the setup TF (its regime and its last confirmed swings as the range walls); the note's 60-minute
oscillator → the setup TF; the rejection candle → the trigger bar. LONG rules (fade at support; SHORT mirrors
at resistance):

1. Range regime (setup TF): ``ctx.regime`` is RANGE and EMA50 is flat (|slope| ≤ ``max_ema50_slope_atr``).
2. Room: the range (swing high − swing low) is at least ``min_range_atr`` setup ATR tall.
3. Test (trigger TF): the bar's low reaches support — within ``touch_atr`` above it, or at most ``pierce_atr``
   below it (further below is a failure test, not a fade) — and the bar closes above support.
4. Oscillator (setup TF): Stochastics %K ≤ ``oversold`` or RSI14 ≤ ``rsi_oversold`` (the note: < 20 / < 30).
5. Rejection candle (trigger TF): bullish, with a hammer wick (≥ ``hammer_wick`` of the range) or a strong
   body (≥ ``strong_body``) standing in for the engulfing bar.
6. Levels: invalidation = the rejection low − ``stop_buffer_atr`` × trigger ATR (the note: 0.25 ATR); target =
   the range midpoint (the note's first target), which must lie beyond the entry.

APPROXIMATIONS: the snapshot cannot count prior reactions at the level (the note wants two) or see oscillator
divergence, so neither is required; the entry is the rejection bar's close instead of a stop above its high;
one target (no scale-out). The note's win rate / profit factor are book claims, not validated here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction, Regime
from aifund.domain.values import to_decimal
from aifund.market.conditions import sha256
from aifund.strategies.base import TfRoles, candle, num

SETUP_TAG = "sr_fade_range"
PLAYBOOK_ID = "sr_fade_range"


@dataclass(frozen=True)
class FadeParams:
    max_ema50_slope_atr: float = 0.25
    min_range_atr: float = 2.0
    touch_atr: float = 0.25
    pierce_atr: float = 0.25
    oversold: float = 20.0
    rsi_oversold: float = 30.0
    hammer_wick: float = 0.5
    strong_body: float = 0.6
    stop_buffer_atr: float = 0.25


class SrFadeRange:
    setup_tag = SETUP_TAG
    playbook_id = PLAYBOOK_ID
    version = "1"

    def __init__(self, roles: TfRoles, params: FadeParams | None = None) -> None:
        self.roles = roles
        self.params = params or FadeParams()

    def params_sha256(self) -> str:
        return sha256(asdict(self.params))

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        out = []
        for direction in (Direction.LONG, Direction.SHORT):
            found = self._detect(snapshot, direction)
            if found is not None:
                out.append(found)
        return out

    def _detect(self, s: FeatureSnapshot, d: Direction) -> SetupCandidate | None:
        p, r = self.params, self.roles
        long = d is Direction.LONG
        sign = 1 if long else -1

        # 1. a flat, ranging setup timeframe
        st = r.setup
        slope = num(s, st, "ema50_slope_atr")
        if s.features.get("ctx.regime") != Regime.RANGE.value or slope is None:
            return None
        if abs(slope) > p.max_ema50_slope_atr:
            return None

        # 2. the range walls: the last confirmed setup swings
        st_close, st_atr = num(s, st, "close"), num(s, st, "atr14")
        to_high, to_low = num(s, st, "dist_swing_high_atr"), num(s, st, "dist_swing_low_atr")
        if st_close is None or st_atr is None or st_atr <= 0 or to_high is None or to_low is None:
            return None
        resistance, support = st_close + to_high * st_atr, st_close - to_low * st_atr
        if resistance - support < p.min_range_atr * st_atr:
            return None
        wall, midpoint = (support if long else resistance), (support + resistance) / 2

        # 3. the trigger bar tests the wall and closes back inside
        bar = candle(s, r.trigger)
        tr_atr = num(s, r.trigger, "atr14")
        if bar is None or tr_atr is None or tr_atr <= 0:
            return None
        extreme = bar.low if long else bar.high
        reach = sign * (extreme - wall)  # > 0: stayed inside the range, < 0: pierced the wall
        if reach > p.touch_atr * st_atr or reach < -p.pierce_atr * st_atr:
            return None
        if sign * (bar.close - wall) <= 0:
            return None

        # 4. the oscillator is stretched toward the wall
        k, rsi = num(s, st, "stoch_k"), num(s, st, "rsi14")
        stoch_ok = k is not None and (k <= p.oversold if long else k >= 100 - p.oversold)
        rsi_ok = rsi is not None and (rsi <= p.rsi_oversold if long else rsi >= 100 - p.rsi_oversold)
        if not (stoch_ok or rsi_ok):
            return None

        # 5. a rejection candle
        wick = num(s, r.trigger, "lower_wick_to_range" if long else "upper_wick_to_range") or 0.0
        body = num(s, r.trigger, "body_to_range") or 0.0
        if sign * (bar.close - bar.open) <= 0 or (wick < p.hammer_wick and body < p.strong_body):
            return None

        # 6. levels (prices)
        entry = bar.close
        invalidation = extreme - sign * p.stop_buffer_atr * tr_atr
        if sign * (midpoint - entry) <= 0:
            return None  # already past the middle of the range: nothing left to fade into
        return SetupCandidate(
            setup_tag=SETUP_TAG,
            playbook_id=PLAYBOOK_ID,
            direction_hint=d,
            key_levels={
                "entry_ref": to_decimal(entry),
                "invalidation": to_decimal(round(invalidation, 10)),
                "target": to_decimal(round(midpoint, 10)),
                "support": to_decimal(round(support, 10)),
                "resistance": to_decimal(round(resistance, 10)),
            },
            strength=0.5 + (0.25 if stoch_ok and rsi_ok else 0.0) + (0.25 if wick >= p.hammer_wick else 0.0),
            notes=f"{st.value} range {(resistance - support) / st_atr:.1f} ATR tall, "
            f"{r.trigger.value} rejection at {'support' if long else 'resistance'}",
        )
