"""Pattern miner (roadmap 7.4, docs/04 §3): the planted losing pattern is found, pure noise yields nothing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aifund.domain.enums import Direction
from aifund.market import conditions as cond
from aifund.market.conditions import Op
from aifund.rules import miner
from aifund.rules.miner import MinerConfig, Sample
from tests.unit.rules.synthetic import dataset

FAST = MinerConfig(resamples=500, permutations=500)


def test_the_planted_losing_pattern_is_found_on_discovery_data_only() -> None:
    samples = dataset(600, seed=1)
    result = miner.mine(samples, FAST, label="C-2026-09-30")
    assert (result.n_samples, result.n_discovery) == (600, 420)
    assert result.discovery_end == sorted(s.time for s in samples)[419]
    assert result.clusters, "the planted pattern must survive FDR"
    top = result.clusters[0]
    assert top.id == "C-2026-09-30-01" and top.survived  # noqa: PT018
    used = cond.features_of(top.condition)
    assert used & {"m15.rsi14", "ctx.session"}
    assert top.effect <= -0.3 and top.mean_r < 0 and top.p_value < 0.05  # noqa: PT018
    assert top.ci_mean is not None and top.ci_mean[1] < 0.5  # noqa: PT018
    assert all(c.effect <= -FAST.min_effect_r and c.n >= FAST.min_matches_total for c in result.clusters)
    assert result.tested > 100
    body = result.to_json()
    assert body["clusters"][0]["text"] == top.describe()
    assert body["tables"]["session"]
    assert body["tag_frequency"]["GOOD_TRADE_BAD_OUTCOME"]["wins"] == 0.0


@pytest.mark.parametrize("seed", [11, 12, 13, 14, 15])
def test_pure_noise_yields_no_survivors(seed: int) -> None:
    result = miner.mine(dataset(600, seed=seed, planted=False), FAST)
    assert result.clusters == [], [c.describe() for c in result.clusters]
    assert all(not w.survived for w in result.weak)


def test_too_little_data_mines_nothing_but_still_describes() -> None:
    result = miner.mine(dataset(20, seed=2), FAST)
    assert (result.clusters, result.weak, result.tested) == ([], [], 0)
    assert result.tables["symbol"][0].n == 14


def test_bins_for_each_kind_of_feature() -> None:
    rsi = miner.feature_bins("m15.rsi14", [float(v) for v in range(100)])
    assert [p.op for p in rsi] == [Op.LE, Op.BETWEEN, Op.BETWEEN, Op.BETWEEN, Op.GE]
    assert rsi[0].value == 19.8 and rsi[-1].value == 79.2  # noqa: PT018
    assert [p.value for p in miner.feature_bins("m15.nr7", [True, False, None])] == [False, True]
    assert miner.feature_bins("ctx.session", ["NY", "NY"]) == []  # one value: nothing to compare
    assert [p.value for p in miner.feature_bins("ctx.htf_trend_score", [1, 2, 2, -1])] == [-1, 1, 2]
    assert miner.feature_bins("m15.rsi14", [None, None]) == []
    assert miner.feature_bins("m15.rsi14", [50.0, 50.0]) == []
    assert miner.feature_bins("m15.rsi14", [1.0] * 9 + [2.0]) == []  # every edge sits on an extreme


def test_only_rule_usable_features_are_mined() -> None:
    assert miner.minable("m15.rsi14") and miner.minable("prop.htf_alignment")  # noqa: PT018
    assert not miner.minable("prop.rr_target")  # known only after the stop plan
    assert not miner.minable("prop.direction")  # that is the scope's job
    assert not miner.minable("m15.nope")


def test_scopes_need_enough_samples() -> None:
    samples = dataset(60, seed=3)
    few_btc = [replace(s, symbol="BTCUSD") for s in samples[:5]]
    scopes = miner.scopes(samples + few_btc, 20)
    assert scopes[0] == miner.RuleScope()
    assert miner.RuleScope(symbols=("XAUUSD",), directions=(Direction.LONG,)) in scopes
    assert all("BTCUSD" not in s.symbols for s in scopes)
    assert any(s.setup_tags == ("mtf_trend_pullback",) for s in scopes)


def test_split_is_by_time_and_a_sample_becomes_a_rule_context() -> None:
    samples = dataset(10, seed=4)
    discovery, holdout = miner.split(list(reversed(samples)), 0.3)
    assert [s.key for s in discovery] == [s.key for s in samples[:7]]
    assert [s.key for s in holdout] == [s.key for s in samples[7:]]
    ctx = samples[0].context()
    assert (ctx.symbol, ctx.trigger_tf, ctx.features["m15.rsi14"]) == (
        "XAUUSD",
        "M15",
        samples[0].features["m15.rsi14"],
    )


def test_confidence_buckets() -> None:
    assert [miner.confidence_bucket(v) for v in (72, 0, None, True, "x")] == [
        "70-79",
        "0-9",
        "unknown",
        "unknown",
        "unknown",
    ]


def test_a_sample_is_hashable_data() -> None:
    s = dataset(1, seed=5)[0]
    assert isinstance(s, Sample) and s.r in (1.5, -1.0)  # noqa: PT018
