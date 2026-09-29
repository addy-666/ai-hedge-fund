"""Feature snapshots: hand-derived values on synthetic series, validation, registry coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.domain.enums import ReasonCode, Timeframe
from aifund.domain.market import Bar, Tick
from aifund.market import feature_registry as reg
from aifund.market.features import PortfolioContext, SnapshotError, build_snapshot, session_of

AS_OF = datetime(2026, 9, 28, 10, 0, 5, tzinfo=UTC)  # a Monday, 5 s after the 09:45 M15 bar closed
TFS = (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
LAST_OPEN = {  # open time of the last CLOSED bar at AS_OF
    Timeframe.M15: datetime(2026, 9, 28, 9, 45, tzinfo=UTC),
    Timeframe.H1: datetime(2026, 9, 28, 9, 0, tzinfo=UTC),
    Timeframe.H4: datetime(2026, 9, 28, 4, 0, tzinfo=UTC),
    Timeframe.D1: datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
}


def linear(tf: Timeframe, n: int = 300, step: float = 0.1, start: float = 100.0) -> list[Bar]:
    """close_i = start + step*i ; open = close - step/2 ; high/low = body +/- 0.1 (range 0.25) ; vol 100."""
    span = timedelta(minutes=tf.minutes)
    out = []
    for i in range(n):
        c = D(str(round(start + step * i, 6)))
        o = c - D(str(step / 2))
        out.append(
            Bar(
                symbol="X",
                timeframe=tf,
                time=LAST_OPEN[tf] - (n - 1 - i) * span,
                open=o,
                high=max(o, c) + D("0.1"),
                low=min(o, c) - D("0.1"),
                close=c,
                tick_volume=100,
            )
        )
    return out


def snapshot(bars: dict[Timeframe, list[Bar]] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = dict(
        symbol="X",
        trigger_tf=Timeframe.M15,
        setup_tf=Timeframe.H1,
        context_tfs=[Timeframe.H4, Timeframe.D1],
        bars=bars or {tf: linear(tf) for tf in TFS},
        as_of=AS_OF,
    )
    args.update(kw)
    return build_snapshot(**args)  # type: ignore[arg-type]


def test_uptrend_features_match_hand_derived_values() -> None:
    tick = Tick(symbol="X", time=AS_OF, bid=D("129.90"), ask=D("129.92"))
    f = snapshot(tick=tick).features
    # every bar spans 0.25 (body 0.05 + 0.1 wick each side) and TR = max(0.25, |h-prev c| = 0.2, ...) = 0.25
    atr = 0.25
    assert f["m15.atr14"] == pytest.approx(atr)
    assert f["m15.range_to_atr"] == pytest.approx(1.0)
    # linear series: SMA-seeded EMA sits at its steady-state lag step*(n-1)/2 from the start
    assert f["m15.dist_ema20_atr"] == pytest.approx(0.1 * 19 / 2 / atr)
    assert f["m15.dist_ema50_atr"] == pytest.approx(0.1 * 49 / 2 / atr)
    assert f["m15.dist_ema200_atr"] == pytest.approx(0.1 * 199 / 2 / atr)
    assert f["m15.ema50_slope_atr"] == pytest.approx(5 * 0.1 / atr)
    assert f["m15.ema_stack"] == "BULL"
    assert f["m15.close_above_ema200"] is True
    assert f["m15.rsi14"] == 100.0
    assert f["m15.rsi14_slope3"] == 0.0
    assert f["m15.adx14"] == pytest.approx(100.0)  # +DM every bar, -DM never
    assert f["m15.macd_hist_z"] == 0.0  # MACD line constant -> histogram 0 -> z 0
    assert f["m15.atr14_pct_rank100"] == 0.5  # flat ATR history ranks mid, not "extreme"
    assert f["m15.bb_width_pct_rank100"] == pytest.approx(0.005)  # width shrinks as the mean rises
    assert f["m15.nr7"] is False
    assert f["m15.rel_tick_volume20"] == pytest.approx(1.0)
    # candle: open = close - 0.05, high = close + 0.1, low = open - 0.1 -> range 0.25
    assert f["m15.body_to_range"] == pytest.approx(0.05 / 0.25)
    assert f["m15.upper_wick_to_range"] == pytest.approx(0.1 / 0.25)
    assert f["m15.lower_wick_to_range"] == pytest.approx(0.1 / 0.25)
    assert f["m15.candle_dir"] == "BULL"
    # a monotonic series has no confirmed fractal swings
    assert f["m15.dist_swing_high_atr"] is None
    assert f["m15.dist_swing_low_atr"] is None
    assert f["m15.bars_since_swing_break"] is None
    # feature set v2
    close = float(linear(Timeframe.M15)[-1].close)
    assert f["m15.close"] == close
    assert f["m15.ema50_above_ema200"] is True
    ema50 = close - 0.1 * 49 / 2
    assert f["m15.low_dist_ema50_atr"] == pytest.approx((close - 0.15 - ema50) / atr)
    assert f["m15.high_dist_ema50_atr"] == pytest.approx((close + 0.1 - ema50) / atr)
    # stochastics: highest high (last) = close + 0.1 ; lowest low (13 bars back) = close - 1.3 - 0.15
    raw_k = 100 * (close - (close - 1.45)) / (close + 0.1 - (close - 1.45))
    assert f["m15.stoch_k"] == pytest.approx(raw_k)
    assert f["m15.stoch_d"] == pytest.approx(raw_k)
    assert f["m15.stoch_cross_up"] is False
    assert f["m15.stoch_cross_down"] is False
    # context
    assert f["ctx.session"] == "LONDON"
    assert f["ctx.day_of_week"] == 0
    assert f["ctx.regime"] == "TREND_UP"
    assert f["ctx.htf_trend_score"] == 3
    assert f["ctx.spread_to_atr"] == pytest.approx(0.02 / atr)
    d1_last = linear(Timeframe.D1)[-1]
    m15_close = float(linear(Timeframe.M15)[-1].close)
    assert f["ctx.dist_pdh_atr"] == pytest.approx((float(d1_last.high) - m15_close) / atr)
    assert f["ctx.dist_pdl_atr"] == pytest.approx((m15_close - float(d1_last.low)) / atr)
    assert f["ctx.minutes_to_next_high_impact_news"] is None
    assert f["ctx.open_positions_count"] is None


def test_downtrend_mirrors() -> None:
    f = snapshot({tf: linear(tf, step=-0.1, start=200.0) for tf in TFS}).features
    assert f["h1.ema_stack"] == "BEAR"
    assert f["m15.rsi14"] == 0.0
    assert f["ctx.regime"] == "TREND_DOWN"
    assert f["ctx.htf_trend_score"] == -3


def test_snapshot_metadata_and_registry_coverage() -> None:
    snap = snapshot(portfolio=PortfolioContext(2, 0.5, 1.2, 1, 3.4))
    assert snap.bar_time == LAST_OPEN[Timeframe.M15]
    assert snap.bars_ref == LAST_OPEN
    assert snap.feature_set_version == reg.FEATURE_SET_VERSION
    names = reg.registry(TFS)
    expected = {n for n, s in names.items() if s.source is not reg.FeatureSource.PROPOSAL}
    assert set(snap.features) == expected  # the builder emits exactly the registry, nothing else
    assert snap.features["ctx.portfolio_heat_pct"] == 1.2


def test_degenerate_last_bar_yields_none_not_nan() -> None:
    bars = {tf: linear(tf) for tf in TFS}
    last = bars[Timeframe.M15][-1]
    bars[Timeframe.M15][-1] = last.model_copy(
        update={"open": last.close, "high": last.close, "low": last.close, "tick_volume": 0}
    )
    f = snapshot(bars).features
    assert f["m15.body_to_range"] is None
    assert f["m15.candle_dir"] is None
    assert f["m15.rel_tick_volume20"] == 0.0


# ---------------------------------------------------------------- validation


def test_insufficient_bars_and_missing_timeframe() -> None:
    bars = {tf: linear(tf) for tf in TFS}
    bars[Timeframe.H4] = linear(Timeframe.H4, n=299)
    with pytest.raises(SnapshotError) as exc:
        snapshot(bars)
    assert exc.value.reason is ReasonCode.INSUFFICIENT_BARS
    del bars[Timeframe.H4]
    with pytest.raises(SnapshotError, match="no bars supplied"):
        snapshot(bars)


def test_a_forming_bar_is_rejected() -> None:
    bars = {tf: linear(tf) for tf in TFS}
    with pytest.raises(SnapshotError) as exc:
        snapshot(bars, as_of=AS_OF - timedelta(seconds=10))  # the 09:45 M15 bar has not closed yet
    assert exc.value.reason is ReasonCode.FORMING_BAR


@pytest.mark.parametrize("problem", ["duplicate", "gap", "stale"])
def test_stale_or_broken_data_is_rejected(problem: str) -> None:
    bars = {tf: linear(tf) for tf in TFS}
    kwargs: dict[str, object] = {}
    if problem == "duplicate":
        bars[Timeframe.H1][100] = bars[Timeframe.H1][100].model_copy(
            update={"time": bars[Timeframe.H1][99].time}
        )
    elif problem == "gap":
        bars[Timeframe.M15] = [
            b.model_copy(update={"time": b.time - timedelta(days=6)}) if i < 10 else b
            for i, b in enumerate(bars[Timeframe.M15])
        ]
    else:
        kwargs["as_of"] = AS_OF + timedelta(minutes=45)  # trigger bar closed 45 min ago on M15
    with pytest.raises(SnapshotError) as exc:
        snapshot(bars, **kwargs)
    assert exc.value.reason is ReasonCode.STALE_DATA


@pytest.mark.parametrize(
    ("hour", "session"),
    [(0, "ASIA"), (6, "ASIA"), (7, "LONDON"), (12, "OVERLAP"), (16, "NY"), (21, "OFF"), (23, "ASIA")],
)
def test_sessions(hour: int, session: str) -> None:
    assert session_of(datetime(2026, 9, 28, hour, tzinfo=UTC)) == session


# ---------------------------------------------------------------- registry


def test_registry_names_and_lookup() -> None:
    assert reg.get("m15.rsi14").dtype is reg.FeatureType.FLOAT
    assert reg.get("d1.ema_stack").categories == ("BULL", "BEAR", "MIXED")
    assert reg.get("prop.htf_alignment").source is reg.FeatureSource.PROPOSAL
    assert reg.is_rule_usable("h4.adx14")
    assert not reg.is_rule_usable("m15.mae_r")  # outcomes are never rule inputs
    with pytest.raises(KeyError):
        reg.get("m15.not_a_feature")
    assert len(reg.registry()) == len(reg.TF_FEATURES) * len(Timeframe) + len(reg.CTX_FEATURES) + len(
        reg.PROP_FEATURES
    )


def test_the_timeframe_cache_changes_nothing_and_is_reused() -> None:
    cache: dict[tuple[Timeframe, datetime, int], dict[str, object]] = {}
    fresh = snapshot()
    first = snapshot(tf_cache=cache)
    assert first.features == fresh.features
    assert len(cache) == len(TFS)
    marker = {**cache[(Timeframe.H1, LAST_OPEN[Timeframe.H1], 300)], "rsi14": -1.0}
    cache[(Timeframe.H1, LAST_OPEN[Timeframe.H1], 300)] = marker  # a hit is used as-is
    assert snapshot(tf_cache=cache).features["h1.rsi14"] == -1.0
    other = {tf: linear(tf, n=301) for tf in TFS}  # a different window is a miss
    assert snapshot(other, tf_cache=cache).features["h1.rsi14"] != -1.0
