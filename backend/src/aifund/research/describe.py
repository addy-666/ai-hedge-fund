"""Descriptive in-sample tables for the LLM researcher (docs/09 §6): how the market behaves, not hypotheses.

A PROBE detector alternates LONG and SHORT on every trigger bar (default ATR stop and RR target), so the
signal study measures what an undirected entry earns in each condition. The tables group those signals by
session, setup-TF regime and higher-timeframe alignment. The loop only ever passes signals that ENTERED
before the holdout starts: nothing here may describe the holdout.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence

from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import Direction
from aifund.market.conditions import sha256
from aifund.research.signals import SignalOutcome

PROBE_TAG = "probe_every_bar"


class Probe:
    setup_tag = PROBE_TAG
    playbook_id = PROBE_TAG
    version = "1"

    def params_sha256(self) -> str:
        return sha256({"probe": PROBE_TAG})

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        index = int(snapshot.bar_time.timestamp()) // (
            snapshot.trigger_tf.minutes * 60
        )  # bars since the epoch
        direction = Direction.LONG if index % 2 == 0 else Direction.SHORT
        return [
            SetupCandidate(setup_tag=PROBE_TAG, playbook_id=PROBE_TAG, direction_hint=direction, strength=0.5)
        ]


def alignment(o: SignalOutcome) -> str:
    score = o.features.get("ctx.htf_trend_score")
    if not isinstance(score, int) or isinstance(score, bool):
        return "unknown"
    aligned = score * (1 if o.direction is Direction.LONG else -1)
    return "with the HTF trend" if aligned > 0 else "against it" if aligned < 0 else "HTF mixed"


def feature_key(name: str) -> Callable[[SignalOutcome], str]:
    def key(o: SignalOutcome) -> str:
        value = o.features.get(name)
        return "unknown" if value is None else str(value)

    return key


def breakdown(outcomes: Sequence[SignalOutcome], key: Callable[[SignalOutcome], str]) -> list[str]:
    groups: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        groups[key(o)].append(float(o.r_net))
    rows = []
    for name in sorted(groups):
        r = groups[name]
        wins = sum(1 for x in r if x > 0)
        rows.append(f"{name}: n={len(r)} mean {sum(r) / len(r):+.3f}R win {wins / len(r):.0%}")
    return rows


TABLES: tuple[tuple[str, Callable[[SignalOutcome], str]], ...] = (
    ("by direction", lambda o: o.direction.value),
    ("by session (UTC)", feature_key("ctx.session")),
    ("by setup-TF regime", feature_key("ctx.regime")),
    ("by higher-timeframe alignment", alignment),
)
