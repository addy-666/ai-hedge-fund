"""NR7 volatility breakout (playbook card: config/playbooks/nr7_breakout.yaml).

Source: TRADING BRAIN [[high_probability_nr7_volatility_breakout]] (Link, after Crabel), mapped onto the
profile's roles (note → here): daily trend filter → highest context TF; the NR7 bar (daily in the note) → the
last closed setup-TF bar; the day-8 breakout → the first trigger-TF bar that closes through the NR7 bar's
extreme. LONG rules (SHORT mirrors):

1. Trend filter (context): EMA50 > EMA200 and close > EMA50.
2. NR7 (setup TF): the last closed setup bar has the narrowest range of the last seven.
3. Location (setup TF): the NR7 bar rests at value — its close within ``location_atr`` ATR of EMA20 or EMA50 —
   and EMA50 is rising (slope > 0).
4. Breakout (trigger TF): the trigger bar opens at or below the NR7 high and closes above it (the bar-close
   equivalent of the note's buy stop one tick above the high), with relative tick volume above
   ``min_rel_volume`` (docs/03 §6: > 1.2).
5. Levels: invalidation = NR7 low − ``stop_buffer_atr`` × setup ATR (the note's table: 0.25 ATR); target =
   entry + ``target_r`` × the entry-to-invalidation distance (the note's first target, +2R).

APPROXIMATIONS: the snapshot has ratios, not raw prices, so the NR7 bar's high/low are rebuilt from its close,
ATR, range and wick ratios; for a DOJI (sign of the body unknown) the widest possible high/low are used, which
asks slightly more of the breakout and puts the stop slightly further away. The note's scale-out and trailing
runner are not implemented (single target). Its win rate / profit factor are book claims, not validated here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction
from aifund.domain.values import to_decimal
from aifund.market.conditions import sha256
from aifund.strategies.base import TfRoles, candle, envelope, feature, num, trend_aligned

SETUP_TAG = "nr7_breakout"
PLAYBOOK_ID = "nr7_breakout"


@dataclass(frozen=True)
class Nr7Params:
    location_atr: float = 1.0
    min_rel_volume: float = 1.2
    stop_buffer_atr: float = 0.25
    target_r: float = 2.0


class Nr7Breakout:
    setup_tag = SETUP_TAG
    playbook_id = PLAYBOOK_ID
    version = "1"

    def __init__(self, roles: TfRoles, params: Nr7Params | None = None) -> None:
        self.roles = roles
        self.params = params or Nr7Params()

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

        # 1. higher-timeframe trend
        if not trend_aligned(s, r.trend_filter, long):
            return None

        # 2-3. an NR7 setup bar at value, EMA50 sloping with the trend
        st = r.setup
        if feature(s, st, "nr7") is not True:
            return None
        d20, d50, slope = (
            num(s, st, "dist_ema20_atr"),
            num(s, st, "dist_ema50_atr"),
            num(s, st, "ema50_slope_atr"),
        )
        st_atr, box = num(s, st, "atr14"), envelope(s, st)
        if d20 is None or d50 is None or slope is None or st_atr is None or st_atr <= 0 or box is None:
            return None
        if min(abs(d20), abs(d50)) > p.location_atr or sign * slope <= 0:
            return None
        nr7_high, nr7_low = box
        level, far = (nr7_high, nr7_low) if long else (nr7_low, nr7_high)

        # 4. the first trigger bar to close through the NR7 extreme, on expanding volume
        bar = candle(s, r.trigger)
        volume = num(s, r.trigger, "rel_tick_volume20")
        if bar is None or volume is None or volume <= p.min_rel_volume:
            return None
        if sign * (bar.open - level) > 0 or sign * (bar.close - level) <= 0:
            return None

        # 5. levels (prices)
        entry = bar.close
        invalidation = far - sign * p.stop_buffer_atr * st_atr
        risk = sign * (entry - invalidation)
        target = entry + sign * p.target_r * risk
        strength = 0.5
        if feature(s, r.trend_filter, "ema_stack") == ("BULL" if long else "BEAR"):
            strength += 0.25
        wick = num(s, r.trigger, "upper_wick_to_range" if long else "lower_wick_to_range")
        if wick is not None and wick <= 0.25:  # closes near its extreme: an authentic expansion bar
            strength += 0.25
        return SetupCandidate(
            setup_tag=SETUP_TAG,
            playbook_id=PLAYBOOK_ID,
            direction_hint=d,
            key_levels={
                "entry_ref": to_decimal(entry),
                "invalidation": to_decimal(round(invalidation, 10)),
                "target": to_decimal(round(target, 10)),
                "nr7_high": to_decimal(round(nr7_high, 10)),
                "nr7_low": to_decimal(round(nr7_low, 10)),
            },
            strength=strength,
            notes=f"{st.value} NR7 at value, {r.trigger.value} close through it on {volume:.2f}x volume",
        )
