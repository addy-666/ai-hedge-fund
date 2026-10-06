"""Learning with cross-asset features (roadmap 10.5): a losing pattern that lives in another market is mined,
becomes a valid rule, and is enforced only when that market's value is known."""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from aifund.domain.enums import Direction
from aifund.market import conditions as cond
from aifund.rules import dsl, miner
from aifund.rules.dsl import RuleContext
from aifund.rules.miner import MinerConfig, Sample
from tests.unit.rules.synthetic import dataset

FAST = MinerConfig(resamples=500, permutations=500)
FEATURE = "xa.eurusd.ret24_z"


def with_dollar(samples: list[Sample], seed: int) -> list[Sample]:
    """Adds EURUSD's 24-bar move (null a fifth of the time: its market shut) and plants the loss: gold longs
    lose 90% of the time while the dollar is rallying hard (EURUSD down more than 1 sigma)."""
    rng = np.random.default_rng(seed)
    out = []
    for s in samples:
        move = None if rng.random() < 0.2 else round(float(rng.uniform(-3, 3)), 3)
        hit = s.direction is Direction.LONG and move is not None and move < -1
        r = (1.5 if rng.random() < 0.1 else -1.0) if hit else s.r
        out.append(replace(s, r=r, features={**s.features, FEATURE: move}))
    return out


def test_a_losing_pattern_in_another_market_is_mined_and_enforced() -> None:
    samples = with_dollar(dataset(600, seed=3, planted=False), seed=3)
    result = miner.mine(samples, FAST, label="C-XA")
    assert result.clusters, "the planted cross-asset pattern must survive FDR"
    top = result.clusters[0]
    assert FEATURE in cond.features_of(top.condition)
    assert top.effect <= -0.3

    rule = dsl.parse({
        "rule_id": "R-0100", "scope": {"symbols": ["XAUUSD"], "directions": ["LONG"]},
        "conditions": {"all": [{"feature": FEATURE, "op": "<", "value": -1}]},
        "action": {"type": "penalty", "points": 15}, "hypothesis": "gold longs into a dollar rally",
        "cited_clusters": [top.id],
    })  # fmt: skip
    dsl.validate(rule, max_conditions=3, symbols=["XAUUSD"])  # a registry feature, known before the rules run
    long = {"symbol": "XAUUSD", "direction": Direction.LONG, "setup_tag": "t", "trigger_tf": "M15"}
    assert dsl.matches(rule, RuleContext(**long, features={FEATURE: -1.8}))
    assert not dsl.matches(rule, RuleContext(**long, features={FEATURE: 0.4}))
    shut = RuleContext(**long, features={FEATURE: None})  # EURUSD closed: the rule cannot judge, it stays out
    assert not dsl.matches(rule, shut)
    assert dsl.missing(rule, shut) == {FEATURE}


def test_noise_in_another_market_mines_nothing() -> None:
    samples = dataset(600, seed=12, planted=False)
    rng = np.random.default_rng(12)
    noisy = [
        replace(s, features={**s.features, FEATURE: round(float(rng.uniform(-3, 3)), 3)}) for s in samples
    ]
    result = miner.mine(noisy, FAST)
    assert all(FEATURE not in cond.features_of(c.condition) for c in result.clusters)
