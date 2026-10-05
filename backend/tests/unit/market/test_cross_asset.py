"""Cross-asset features (roadmap 10.2): hand-derived values, closed bars only, closed markets give nulls."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal as D

import numpy as np
import pytest

from aifund.adapters.clock import FakeClock
from aifund.adapters.sim.replay_feed import ReplayFeed
from aifund.domain.enums import ReasonCode, Timeframe
from aifund.domain.market import Bar
from aifund.market import feature_registry as reg
from aifund.market.cross_asset import BASES, CrossAssetError, CrossAssetInput, cross_features
from aifund.market.features import SnapshotError
from aifund.research.history import Series
from tests.integration.test_snapshot_pipeline import SPEC
from tests.unit.market.test_features import AS_OF, LAST_OPEN, linear, snapshot

H1 = Timeframe.H1
HOUR = timedelta(hours=1)
LAST = LAST_OPEN[H1]  # 09:00, closed at 10:00; AS_OF is 10:00:05
AGE = timedelta(minutes=120)


def hourly(log_returns: list[float], *, symbol: str = "X", last: datetime = LAST, start: float = 100.0,
           times: list[datetime] | None = None) -> list[Bar]:  # fmt: skip
    """Bars whose closes follow ``log_returns`` from ``start`` (one bar more than returns), hourly."""
    closes = [start]
    for r in log_returns:
        closes.append(closes[-1] * math.exp(r))
    stamps = times or [last - (len(closes) - 1 - i) * HOUR for i in range(len(closes))]
    out = []
    for t, c in zip(stamps, closes, strict=True):
        close = D(f"{c:.8f}")
        out.append(Bar(symbol=symbol, timeframe=H1, time=t, open=close, high=close * D("1.001"),
                       low=close * D("0.999"), close=close, tick_volume=10))  # fmt: skip
    return out


def noise(n: int, seed: int, scale: float = 0.002) -> list[float]:
    return list(np.random.default_rng(seed).normal(0.0, scale, n))


def features(subject: list[Bar] | None, **others: list[Bar]) -> dict[str, object]:
    return cross_features(subject, others, timeframe=H1, as_of=AS_OF, max_age=AGE)


def test_every_instrument_gets_every_feature_and_the_registry_knows_them() -> None:
    out = features(hourly(noise(299, 1)), eurusd=hourly(noise(299, 2)), btcusd=[])
    assert set(out) == {f"xa.{slug}.{base}" for slug in ("eurusd", "btcusd") for base in BASES}
    for name in out:
        spec = reg.get(name)
        assert spec.source is reg.FeatureSource.CROSS_ASSET
        assert reg.is_rule_usable(name)
    assert all(v is None for k, v in out.items() if k.startswith("xa.btcusd."))  # no bars: unknown


def test_returns_are_scaled_by_the_instruments_own_volatility() -> None:
    rets = noise(299, 3)
    out = features(None, eurusd=hourly(rets))
    vol = float(np.std(rets[-100:], ddof=1))
    assert out["xa.eurusd.ret4_z"] == pytest.approx(sum(rets[-4:]) / (vol * 2), rel=1e-6)
    assert out["xa.eurusd.ret24_z"] == pytest.approx(sum(rets[-24:]) / (vol * math.sqrt(24)), rel=1e-6)
    # no subject: the pair features are unknown, the instrument's own are not
    assert out["xa.eurusd.corr100"] is None
    assert out["xa.eurusd.rel_ret24_z"] is None


def test_trend_state_of_the_other_instrument() -> None:
    up = hourly([0.001] * 299)
    out = features(None, xauusd=up)
    assert out["xa.xauusd.ema_stack"] == "BULL"
    dist = out["xa.xauusd.dist_ema50_atr"]
    assert isinstance(dist, float)
    assert dist > 0
    down = features(None, xauusd=hourly([-0.001] * 299))
    assert down["xa.xauusd.ema_stack"] == "BEAR"


def test_pair_correlation_relative_strength_and_ratio() -> None:
    rets = noise(299, 4)
    same = features(hourly(rets), nas100=hourly(rets, start=20_000.0))
    assert same["xa.nas100.corr100"] == pytest.approx(1.0)
    assert same["xa.nas100.ratio_z100"] is None  # a constant ratio has no spread to score
    assert same["xa.nas100.rel_ret24_z"] == pytest.approx(0.0, abs=1e-6)  # closes rounded to 8 decimals
    inverse = features(hourly(rets), eurusd=hourly([-r for r in rets]))
    assert inverse["xa.eurusd.corr100"] == pytest.approx(-1.0)
    # S outran X over the last day: positive relative strength and a stretched ratio
    lead = rets[:-24] + [0.004] * 24
    ahead = features(hourly(lead), btcusd=hourly(rets))
    assert ahead["xa.btcusd.rel_ret24_z"] > 3
    assert ahead["xa.btcusd.ratio_z100"] > 1


def test_pairs_use_matched_bar_times_only() -> None:
    """X misses 40 of S's hours: the correlation uses the common hours, and below MIN_PAIRS it is unknown."""
    rets = noise(299, 5)
    s = hourly(rets)
    x = [b for i, b in enumerate(hourly(rets)) if i % 7 != 3]  # same closes, some hours missing
    out = features(s, eurusd=x)
    assert out["xa.eurusd.corr100"] == pytest.approx(1.0)
    sparse = [b for i, b in enumerate(hourly(rets)) if i % 5 == 4]  # 60 bars, the last one fresh
    thin = features(s, eurusd=sparse)
    assert thin["xa.eurusd.ret4_z"] is None  # 60 bars: too little history for the volatility scale
    assert thin["xa.eurusd.corr100"] is None  # 60 matched bars < MIN_PAIRS + 1


def test_a_closed_or_stale_instrument_gives_nulls_never_old_values() -> None:
    rets = noise(299, 6)
    friday = hourly(rets, last=LAST - timedelta(hours=60))  # EURUSD shut for the weekend
    out = features(hourly(noise(299, 7)), eurusd=friday)
    assert all(out[f"xa.eurusd.{b}"] is None for b in BASES)
    edge = hourly(rets, last=LAST - timedelta(hours=2))  # closed 08:00: 2 h 0 m 5 s before AS_OF
    assert features(None, eurusd=edge)["xa.eurusd.ret4_z"] is None
    fresh = hourly(rets, last=LAST - HOUR)  # closed 09:00: 1 h 0 m 5 s before
    assert features(None, eurusd=fresh)["xa.eurusd.ret4_z"] is not None


def test_a_bar_that_had_not_closed_is_a_lookahead_bug() -> None:
    forming = hourly(noise(299, 8), last=LAST + HOUR)  # opens 10:00, closes 11:00 > AS_OF
    with pytest.raises(CrossAssetError) as exc:
        features(None, eurusd=forming)
    assert exc.value.reason is ReasonCode.FORMING_BAR
    with pytest.raises(CrossAssetError):
        features(forming, eurusd=hourly(noise(299, 9)))


def test_the_weekend_move_of_a_24_7_market_while_this_one_was_shut() -> None:
    """NAS100 (S) shut Friday 21:00 -> Sunday 22:00; BTC (X) trades through it and rises 3%."""
    s_times = [LAST - i * HOUR for i in range(10)][::-1]  # 10 bars since the reopen
    reopen = s_times[0]
    shut = reopen - timedelta(hours=49)
    s_times = [shut - (290 - i) * HOUR for i in range(290)] + s_times  # bars before: last one closes at shut
    subject = hourly(noise(299, 10), times=s_times)
    x_rets = noise(400, 11, scale=0.001)
    x = hourly(x_rets)  # hourly through the weekend, last bar 09:00
    k_shut = next(i for i, b in enumerate(x) if b.time + HOUR == shut)
    k_reopen = next(i for i, b in enumerate(x) if b.time + HOUR == reopen)
    move = math.log(float(x[k_reopen].close) / float(x[k_shut].close))
    vol = float(np.std(np.diff(np.log([float(b.close) for b in x]))[-100:], ddof=1))
    out = features(subject, btcusd=x)
    assert out["xa.btcusd.gap_ret_z"] == pytest.approx(move / (vol * math.sqrt(49)), rel=1e-6)
    # BTC's path between the two closes does not enter the move (only the volatility scale)
    k = (k_shut + k_reopen) // 2
    wild = [*x[:k], x[k].model_copy(update={"close": x[k].close * 3}), *x[k + 1 :]]
    vol_wild = float(np.std(np.diff(np.log([float(b.close) for b in wild]))[-100:], ddof=1))
    assert features(subject, btcusd=wild)["xa.btcusd.gap_ret_z"] == pytest.approx(
        move / (vol_wild * math.sqrt(49)), rel=1e-6
    )
    # a symbol that never shut (24/7) has no gap to measure
    assert features(hourly(noise(299, 12)), btcusd=x)["xa.btcusd.gap_ret_z"] is None


def test_the_cache_gives_identical_values() -> None:
    s, x = hourly(noise(299, 13)), hourly(noise(299, 14))
    cache: dict = {}  # type: ignore[type-arg]
    first = cross_features(s, {"eurusd": x}, timeframe=H1, as_of=AS_OF, max_age=AGE, cache=cache)
    assert len(cache) == 1
    again = cross_features(s, {"eurusd": x}, timeframe=H1, as_of=AS_OF, max_age=AGE, cache=cache)
    assert again == first == features(s, eurusd=x)
    later = AS_OF + timedelta(hours=3)  # the same bars, now stale: the cache never revives them
    stale = cross_features(s, {"eurusd": x}, timeframe=H1, as_of=later, max_age=AGE, cache=cache)
    assert stale["xa.eurusd.ret4_z"] is None


def test_the_snapshot_carries_them_only_when_asked() -> None:
    plain = snapshot()
    assert not any(k.startswith("xa.") for k in plain.features)
    cross = CrossAssetInput(timeframe=H1, max_age=AGE, others={"eurusd": hourly(noise(299, 15))})
    snap = snapshot(cross_asset=cross)
    assert snap.features["xa.eurusd.corr100"] is not None  # the subject came from the snapshot's H1 bars
    assert {k: v for k, v in snap.features.items() if not k.startswith("xa.")} == plain.features
    forming = hourly(noise(299, 16), last=LAST + HOUR)
    bad = CrossAssetInput(timeframe=H1, max_age=AGE, others={"eurusd": forming})
    with pytest.raises(SnapshotError) as exc:
        snapshot(cross_asset=bad)
    assert exc.value.reason is ReasonCode.FORMING_BAR


async def test_engine_and_research_read_the_same_closed_bars() -> None:
    """The replay feed (engine) and the research Series see the same bars at the same moment, even with
    future bars in the data: identical features."""
    x = hourly(noise(305, 17), last=LAST + 5 * HOUR)  # five bars beyond AS_OF
    s = [b.model_copy(update={"symbol": "S"}) for b in linear(H1, n=305)]
    s = [b.model_copy(update={"time": LAST - (304 - i) * HOUR + 5 * HOUR}) for i, b in enumerate(s)]
    clock = FakeClock(AS_OF)
    spec_s, spec_x = SPEC.model_copy(update={"symbol": "S"}), SPEC
    feed = ReplayFeed({("S", H1): s, ("X", H1): x}, {"S": spec_s, "X": spec_x}, clock)
    live = features(await feed.closed_bars("S", H1, 300), eurusd=await feed.closed_bars("X", H1, 300))
    research = features(Series.of(H1, s).closed_at(AS_OF, 300), eurusd=Series.of(H1, x).closed_at(AS_OF, 300))
    assert live == research
    assert live["xa.eurusd.ret4_z"] is not None


def test_registry_resolves_any_instrument_and_declares_mirrors() -> None:
    assert reg.get("xa.xagusd.ret24_z").description.startswith("XAGUSD: ")
    assert reg.mirror_of("xa.eurusd.ret4_z") == ("xa.eurusd.ret4_z", reg.MirrorKind.NEGATE)
    assert reg.mirror_of("xa.eurusd.corr100") == ("xa.eurusd.corr100", reg.MirrorKind.SAME)
    assert reg.mirror_of("xa.eurusd.ema_stack") == ("xa.eurusd.ema_stack", reg.MirrorKind.CATEGORY)
    for bad in ("xa.eurusd.nope", "xa.EURUSD.ret4_z", "xa.eurusd", "xa.e.ret4_z"):
        with pytest.raises(KeyError):
            reg.get(bad)
        assert not reg.is_rule_usable(bad)
        assert reg.mirror_of(bad) is None
    listed = reg.registry([H1], instruments=["eurusd"])
    assert {n for n in listed if n.startswith("xa.")} == {f"xa.eurusd.{b}" for b in BASES}
    assert reg.FEATURE_SET_VERSION == 3


def test_short_or_disjoint_histories_give_nulls() -> None:
    short = hourly(noise(40, 18))  # 41 bars: no EMA50, no 100-bar volatility
    out = features(hourly(noise(299, 19)), eurusd=short)
    assert out["xa.eurusd.dist_ema50_atr"] is None
    assert out["xa.eurusd.ema_stack"] is None
    assert out["xa.eurusd.ret24_z"] is None
    assert out["xa.eurusd.gap_ret_z"] is None
    # no common bar time at all: the pair is unknown (S's bars are on the half hour)
    s = [b.model_copy(update={"time": b.time - timedelta(minutes=30)}) for b in hourly(noise(299, 20))]
    assert features(s, eurusd=hourly(noise(299, 21)))["xa.eurusd.corr100"] is None


def test_a_gap_before_the_instruments_history_is_unknown() -> None:
    """S shut 200 h before its reopen; X's 151 bars all start after that: its close at the shut is unknown."""
    s_times = [LAST - i * HOUR for i in range(5)][::-1]
    shut = s_times[0] - timedelta(hours=200)
    subject = hourly(noise(299, 22), times=[shut - (294 - i) * HOUR for i in range(295)] + s_times)
    x = hourly(noise(150, 23))
    assert x[0].time + HOUR > shut
    out = features(subject, btcusd=x)
    assert out["xa.btcusd.ret24_z"] is not None  # X itself is fine
    assert out["xa.btcusd.gap_ret_z"] is None


def test_a_flat_instrument_has_no_correlation() -> None:
    flat = hourly([0.0] * 299)
    out = features(hourly(noise(299, 24)), eurusd=flat)
    assert out["xa.eurusd.corr100"] is None
    assert out["xa.eurusd.ret4_z"] is None  # no movement: no volatility scale
