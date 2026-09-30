"""2B reversal / failure test (playbook card: config/playbooks/failure_test_2b.yaml).

Sources: TRADING BRAIN [[sperandeo_2b_reversal_playbook]] and
[[grimes_failure_test_and_false_breakout_playbook]], mapped onto the profile's roles: the "prior significant
low" → the last confirmed setup-TF swing low; the probe and the failure → one trigger-TF bar. LONG rules (a
bullish 2B / spring; SHORT mirrors as the bearish 2B / upthrust):

1. Probe (trigger TF): the bar trades below the setup swing low, by at most ``max_overshoot_atr`` × setup ATR
   (Sperandeo: the new low exceeds the old one only "slightly").
2. Failure (trigger TF): the same bar closes back above the swing low, as a bullish candle.
3. Context: the higher timeframes are not ALL stacked against the trade (``ctx.htf_trend_score`` above its
   most bearish value) — the docs/03 §6 "context not strongly trending against".
4. Levels: invalidation = the probe's low − ``stop_buffer_atr`` × trigger ATR (Sperandeo: just beyond the
   false breakout wick with a 0.5 ATR buffer); target = the opposite setup swing, at least ``min_target_r`` R
   away (Sperandeo: prior swing extreme on the opposite side, or minimum 2:1).

APPROXIMATIONS: the pattern is completed inside one trigger bar (Grimes allows the failure within 1-3 bars);
the snapshot does not say how many times the level was touched ("significant", "multi-touch"), so any
confirmed swing counts; the reversal bar's close stands in for the note's entry on a break of its extreme;
the trend-age filter and volume confirmation are not implemented. The notes' win rates are book claims.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction
from aifund.domain.values import to_decimal
from aifund.market.conditions import sha256
from aifund.strategies.base import TfRoles, candle, num

SETUP_TAG = "failure_test_2b"
PLAYBOOK_ID = "failure_test_2b"


@dataclass(frozen=True)
class FailureTestParams:
    max_overshoot_atr: float = 0.5
    stop_buffer_atr: float = 0.5
    min_target_r: float = 2.0


class FailureTest2B:
    setup_tag = SETUP_TAG
    playbook_id = PLAYBOOK_ID
    version = "1"

    def __init__(self, roles: TfRoles, params: FailureTestParams | None = None) -> None:
        self.roles = roles
        self.params = params or FailureTestParams()

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

        # the setup-TF swing the probe runs, and the opposite one (the target)
        st = r.setup
        st_close, st_atr = num(s, st, "close"), num(s, st, "atr14")
        near = num(s, st, "dist_swing_low_atr" if long else "dist_swing_high_atr")
        far = num(s, st, "dist_swing_high_atr" if long else "dist_swing_low_atr")
        if st_close is None or st_atr is None or st_atr <= 0 or near is None:
            return None
        level = st_close - sign * near * st_atr

        # 1-2. one trigger bar probes beyond the level (slightly) and closes back inside, in our direction
        bar = candle(s, r.trigger)
        tr_atr = num(s, r.trigger, "atr14")
        if bar is None or tr_atr is None or tr_atr <= 0:
            return None
        extreme = bar.low if long else bar.high
        overshoot = sign * (level - extreme)
        if overshoot <= 0 or overshoot > p.max_overshoot_atr * st_atr:
            return None
        if sign * (bar.close - level) <= 0 or sign * (bar.close - bar.open) <= 0:
            return None

        # 3. not against a fully aligned higher-timeframe trend
        score = s.features.get("ctx.htf_trend_score")
        if not isinstance(score, int) or sign * score <= -(1 + len(r.context)):
            return None

        # 4. levels (prices)
        wick = num(s, r.trigger, "lower_wick_to_range" if long else "upper_wick_to_range")  # the rejection
        entry = bar.close
        invalidation = extreme - sign * p.stop_buffer_atr * tr_atr
        risk = sign * (entry - invalidation)
        target = entry + sign * p.min_target_r * risk
        swing_target = st_close + sign * far * st_atr if far is not None else None
        if swing_target is not None and sign * (swing_target - target) > 0:
            target = swing_target
        return SetupCandidate(
            setup_tag=SETUP_TAG,
            playbook_id=PLAYBOOK_ID,
            direction_hint=d,
            key_levels={
                "entry_ref": to_decimal(entry),
                "invalidation": to_decimal(round(invalidation, 10)),
                "target": to_decimal(round(target, 10)),
                "swept_level": to_decimal(round(level, 10)),
            },
            strength=0.5
            + (0.25 if wick is not None and wick >= 0.5 else 0.0)
            + (0.25 if sign * score > 0 else 0.0),
            notes=f"{r.trigger.value} {'low' if long else 'high'} ran the {st.value} swing by "
            f"{overshoot / st_atr:.2f} ATR and closed back inside",
        )
