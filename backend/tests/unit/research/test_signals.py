"""Signal study (docs/09 §2) on a synthetic world whose every number can be derived by hand.

Every bar on every timeframe is 100.00 / 101.00 / 99.00 / 100.00 (O/H/L/C) with a 10-point spread (0.10),
so Wilder ATR14 is exactly 2.00 everywhere. A stub detector fires LONG (or SHORT) on chosen trigger bars with
no levels, so the stop is k_sl_default x ATR = 1.5 x 2.00 = 3.00 and the target rr_default x 3.00 = 6.00. The
quote at a bar close is the last M1 close, 100.00 / 100.10. A BUY enters at the next M1 bar's open at the ask,
100.10: SL 97.10, TP 106.10. Shock M1 bars after the entry decide the exit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from aifund.config.trading_config import PositionManagementConfig, SessionConfig, StopsConfig, SymbolConfig
from aifund.domain.decision import FeatureSnapshot, SetupCandidate
from aifund.domain.enums import AssetClass, CloseReason, Direction, Timeframe, VirtualStatus
from aifund.domain.market import Bar
from aifund.market.sessions import SessionCalendar
from aifund.research.history import History
from aifund.research.signals import CostModel, Skip, StudySpec, run_study
from aifund.strategies.base import TfRoles
from tests.unit.risk.test_stops import XAU

T0 = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)  # a Monday
START = T0 + timedelta(days=7)  # Monday 9 March: every timeframe has >= 30 closed bars (H4: 6 a day)
ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))
SYMBOL = SymbolConfig(
    canonical="XAUUSD", broker="XAUUSD", asset_class=AssetClass.METAL, profile="p", correlation_bucket="B",
    max_spread_points=50, max_spread_to_atr=D("0.3"), trade_weekends=True,
)  # fmt: skip


def flat(tf: Timeframe, days: int = 14, spread: int = 10) -> list[Bar]:
    span = timedelta(minutes=tf.minutes)
    n = int(timedelta(days=days) / span)
    return [
        Bar(
            symbol="XAUUSD",
            timeframe=tf,
            time=T0 + i * span,
            open=D("100.00"),
            high=D("101.00"),
            low=D("99.00"),
            close=D("100.00"),
            tick_volume=10,
            spread_points=spread,
        )
        for i in range(n)
    ]


def world(shocks: dict[datetime, dict[str, str]] | None = None, *, m1: bool = True) -> History:
    """``shocks``: M1 (or M5) bar open time -> fields to override."""
    bars = {("XAUUSD", tf): flat(tf) for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)}
    if m1:
        bars[("XAUUSD", Timeframe.M1)] = flat(Timeframe.M1)
    fine = ("XAUUSD", Timeframe.M1 if m1 else Timeframe.M5)
    for when, fields in (shocks or {}).items():
        series = bars[fine]
        i = next(i for i, b in enumerate(series) if b.time == when)
        series[i] = series[i].model_copy(update={k: D(v) for k, v in fields.items()})
    return History.from_bars(bars, {"XAUUSD": XAU})


class Stub:
    setup_tag = "stub_setup"
    playbook_id = "stub"
    version = "1"

    def __init__(self, fire: dict[datetime, Direction]) -> None:
        self.fire = fire  # trigger bar OPEN time -> direction

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        direction = self.fire.get(snapshot.bar_time)
        if direction is None:
            return []
        return [
            SetupCandidate(setup_tag="stub_setup", playbook_id="stub", direction_hint=direction, strength=0.5)
        ]


def spec(**over: object) -> StudySpec:
    base: dict[str, object] = dict(
        symbol=SYMBOL, roles=ROLES, bars_per_tf=30, stops=StopsConfig(),
        position_management=PositionManagementConfig(time_stop_bars=4), session=None,
    )  # fmt: skip
    base.update(over)
    return StudySpec(**base)  # type: ignore[arg-type]


BAR = START + timedelta(hours=2)  # the trigger bar that fires (opens 02:00, closes 02:15 = entry time)
ENTRY = BAR + timedelta(minutes=15)


def study(history: History, fire: dict[datetime, Direction] | None = None, **over: object):  # type: ignore[no-untyped-def]
    return run_study(
        history, spec(**over), [Stub(fire or {BAR: Direction.LONG})], START, START + timedelta(days=2)
    )


def test_stop_loss_is_exactly_minus_one_r() -> None:
    result = study(world({ENTRY + timedelta(minutes=10): {"low": "97.00"}}))
    (o,) = result.outcomes
    assert (o.entry_time, o.entry_price, o.sl_distance, o.tp_distance) == (
        ENTRY,
        D("100.10"),
        D("3.00"),
        D("6.00"),
    )
    assert (o.status, o.exit_reason, o.exit_time) == (
        VirtualStatus.CLOSED,
        CloseReason.SL,
        ENTRY + timedelta(minutes=10),
    )
    assert (o.r_gross, o.r_net) == (D("-1.0000"), D("-1.0000"))
    assert (o.direction, o.detector, o.resolution) == (Direction.LONG, "stub@1", Timeframe.M1)
    assert o.bar_time == BAR
    assert o.features["m15.atr14"] == 2.0
    assert result.candidates == 1


def test_take_profit_is_exactly_the_planned_reward() -> None:
    (o,) = study(world({ENTRY + timedelta(minutes=10): {"high": "106.50"}})).outcomes
    assert (o.exit_reason, o.r_gross) == (CloseReason.TP, D("2.0000"))


def test_short_mirrors_on_ask_prices() -> None:
    # SELL enters at the bid 100.00: SL 103.00, TP 94.00; an ask high of 103.00 (bid 102.90 + 0.10) stops it
    history = world({ENTRY + timedelta(minutes=5): {"high": "102.90"}})
    (o,) = study(history, {BAR: Direction.SHORT}).outcomes
    assert (o.entry_price, o.exit_reason, o.r_gross) == (D("100.00"), CloseReason.SL, D("-1.0000"))


def test_expiry_at_the_time_stop_costs_the_spread() -> None:
    (o,) = study(world()).outcomes
    # 4 trigger bars after entry; exits at the last bar's bid close 100.00: -0.10 / 3.00
    assert (o.status, o.exit_reason) == (VirtualStatus.EXPIRED, CloseReason.TIME_STOP)
    assert o.exit_time == ENTRY + timedelta(hours=1)
    assert o.r_gross == D("-0.0333")


@pytest.mark.parametrize(
    ("shock", "costs", "r_net"),
    [
        # commission 7.00 per lot round turn on XAU (tick 0.01 = 1.00 per lot) = 0.07 in price: -0.07 / 3
        ({"low": "97.00"}, CostModel(commission_per_lot=D("7")), D("-1.0233")),
        # 10 points slippage in and out at a stop: -0.20 / 3
        ({"low": "97.00"}, CostModel(slippage_points=10), D("-1.0667")),
        # a target is a limit: slippage only at entry, -0.10 / 3
        ({"high": "106.50"}, CostModel(slippage_points=10), D("1.9667")),
    ],
)
def test_costs_in_r(shock: dict[str, str], costs: CostModel, r_net: D) -> None:
    (o,) = study(world({ENTRY + timedelta(minutes=10): shock}), costs=costs).outcomes
    assert o.r_net == r_net


def test_non_overlapping_by_default() -> None:
    fire = {BAR: Direction.LONG, BAR + timedelta(minutes=15): Direction.LONG}
    result = study(world(), fire)
    assert len(result.outcomes) == 1
    assert result.skipped[Skip.OVERLAP] == 1
    assert len(study(world(), fire, non_overlapping=False).outcomes) == 2
    later = {BAR: Direction.LONG, BAR + timedelta(hours=1): Direction.LONG}  # after the first expired
    assert len(study(world(), later).outcomes) == 2


def test_m5_is_used_when_there_is_no_m1_history() -> None:
    (o,) = study(world({ENTRY + timedelta(minutes=10): {"low": "97.00"}}, m1=False)).outcomes
    assert (o.resolution, o.exit_reason, o.exit_time) == (
        Timeframe.M5,
        CloseReason.SL,
        ENTRY + timedelta(minutes=10),
    )


def test_spread_gates() -> None:
    wide = world()
    m1 = wide.series("XAUUSD", Timeframe.M1)
    i = m1.opens.index(ENTRY - timedelta(minutes=1))  # the quote's bar
    m1.bars[i] = m1.bars[i].model_copy(update={"spread_points": 60})
    assert study(wide).skipped[Skip.SPREAD_POINTS] == 1
    wide_atr = spec().symbol.model_copy(update={"max_spread_to_atr": D("0.01")})  # 0.10 / 2.00 = 0.05
    assert study(world(), symbol=wide_atr).skipped[Skip.SPREAD_ATR] > 0


def test_session_gates_follow_the_calendar() -> None:
    weekday = SYMBOL.model_copy(update={"trade_weekends": False, "session": "us"})
    calendar = SessionCalendar.from_config(SessionConfig(closed_days=[date(2026, 3, 13)]))  # Friday closed
    fire = {START + timedelta(days=3, hours=21): Direction.LONG}  # Thursday 21:00 UTC (EDT): daily break
    result = run_study(
        world(), spec(symbol=weekday, session=calendar), [Stub(fire)], START, START + timedelta(days=4)
    )
    assert result.skipped[Skip.SESSION_CLOSED] > 0
    assert result.skipped[Skip.NEAR_CLOSE] > 0  # the hour before Thursday's close starts a long closure
    assert result.outcomes == []


def test_snapshot_errors_are_counted_not_raised() -> None:
    result = run_study(world(), spec(bars_per_tf=500), [Stub({})], START, START + timedelta(hours=1))
    assert result.skipped["SNAPSHOT_INSUFFICIENT_BARS"] == result.trigger_bars == 4


def test_no_fine_data_and_open_at_the_end() -> None:
    history = world()
    m1 = history.series("XAUUSD", Timeframe.M1)
    m5 = history.series("XAUUSD", Timeframe.M5)
    late = History.from_bars(
        {
            ("XAUUSD", tf): history.series("XAUUSD", tf).bars
            for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        }
        | {("XAUUSD", Timeframe.M1): [b for b in m1.bars if b.time >= ENTRY + timedelta(hours=5)]},
        {"XAUUSD": XAU},
    )
    assert study(late).skipped[Skip.NO_FINE_DATA] == 1
    short = History.from_bars(
        {
            ("XAUUSD", tf): history.series("XAUUSD", tf).bars
            for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4)
        }
        | {("XAUUSD", Timeframe.M5): [b for b in m5.bars if b.time < ENTRY + timedelta(minutes=30)]},
        {"XAUUSD": XAU},
    )
    assert study(short).skipped[Skip.OPEN_AT_END] == 1


def test_deterministic() -> None:
    history = world({ENTRY + timedelta(minutes=10): {"low": "97.00"}})
    fire = {BAR + timedelta(hours=h): Direction.LONG for h in range(0, 20, 3)}
    assert study(history, fire).outcomes == study(history, fire).outcomes


# ---------------------------------------------------------------- every skip reason and the data plumbing


def without(history: History, tf: Timeframe, keep) -> History:  # type: ignore[no-untyped-def]
    bars = {
        ("XAUUSD", t): [b for b in history.series("XAUUSD", t).bars if t is not tf or keep(b)]
        for t in (Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
    }
    return History.from_bars(bars, {"XAUUSD": XAU})


def test_no_entry_when_the_fine_history_has_a_hole_at_the_entry() -> None:
    hole = without(
        world(),
        Timeframe.M1,
        lambda b: not ENTRY - timedelta(minutes=1) <= b.time < ENTRY + timedelta(minutes=10),
    )
    assert study(hole).skipped[Skip.NO_ENTRY] == 1


def test_no_stop_when_even_the_minimum_stop_is_below_zero() -> None:
    cheap = History.from_bars(
        {
            ("XAUUSD", tf): [
                b.model_copy(
                    update={"open": D("1.00"), "high": D("5.00"), "low": D("0.50"), "close": D("1.00")}
                )
                for b in flat(tf)
            ]
            for tf in (Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.H4)
        },
        {"XAUUSD": XAU},
    )
    wide = SYMBOL.model_copy(update={"max_spread_to_atr": D("1")})
    assert study(cheap, symbol=wide).skipped[Skip.NO_STOP] == 1  # ATR 4.50: 1.5 ATR below 1.10 is < 0


def test_no_atr_on_a_market_that_never_moves() -> None:
    dead = History.from_bars(
        {
            ("XAUUSD", tf): [b.model_copy(update={"high": D("100.00"), "low": D("100.00")}) for b in flat(tf)]
            for tf in (Timeframe.M1, Timeframe.M15, Timeframe.H1, Timeframe.H4)
        },
        {"XAUUSD": XAU},
    )
    assert study(dead).skipped[Skip.NO_ATR] == 1


def test_a_signal_after_the_pre_close_flatten_is_not_a_trade() -> None:
    weekday = SYMBOL.model_copy(update={"trade_weekends": False, "session": "us"})
    calendar = SessionCalendar.from_config(SessionConfig(closed_days=[date(2026, 3, 13)]))
    pm = PositionManagementConfig(time_stop_bars=4, no_entries_before_close_minutes=0)
    fire = {
        START + timedelta(days=3, hours=20, minutes=30): Direction.LONG
    }  # enters 20:45, flatten was 20:30
    result = run_study(
        world(), spec(symbol=weekday, session=calendar, position_management=pm), [Stub(fire)],
        START, START + timedelta(days=4),
    )  # fmt: skip
    assert result.skipped[Skip.FLATTEN_AT_ONCE] == 1


def test_stops_on_the_setup_timeframe_fall_back_to_the_trigger_atr() -> None:
    from aifund.research.signals import _quote, _stop_atr

    snap = FeatureSnapshot(
        symbol="XAUUSD", trigger_tf=Timeframe.M15, bar_time=BAR, feature_set_version=1,
        features={"m15.atr14": 2.0, "h1.atr14": None}, bars_ref={},
    )  # fmt: skip
    assert _stop_atr(snap, ROLES, StopsConfig(atr_tf="setup")) == D("2.0")
    empty = world().series("XAUUSD", Timeframe.M1)
    tick = _quote("XAUUSD", T0, empty, D("123.45"), XAU.point)  # nothing closed yet: the trigger close
    assert (tick.bid, tick.ask) == (D("123.45"), D("123.45"))


def test_history_loads_the_parquet_export(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from aifund.adapters import history_store as hs

    hs.write_specs(tmp_path, {"XAUUSD": XAU})
    for tf in (Timeframe.M15, Timeframe.H1):
        hs.write_bars(tmp_path, "XAUUSD", tf, flat(tf, days=1))
    loaded = History.load(tmp_path, ["XAUUSD"], [Timeframe.M15, Timeframe.H1, Timeframe.H4])
    assert loaded.series("XAUUSD", Timeframe.M15).bars == flat(Timeframe.M15, days=1)
    assert loaded.get("XAUUSD", Timeframe.H4) is None  # no file: absent, not an error
    with pytest.raises(hs.HistoryError, match="no H4 history"):
        loaded.series("XAUUSD", Timeframe.H4)
    with pytest.raises(hs.HistoryError, match="no specs"):
        History.load(tmp_path, ["BTCUSD"], [Timeframe.M15])


def test_spec_from_the_trading_config() -> None:
    from aifund.config.loader import load_trading_config
    from tests.unit.config.test_trading_config import EXAMPLE

    cfg = load_trading_config(EXAMPLE).config
    gold = next(s for s in cfg.symbols if s.broker == "XAUUSD")
    s = StudySpec.from_config(cfg, gold, costs=CostModel(slippage_points=3))
    assert (s.roles.trigger, s.bars_per_tf, s.costs.slippage_points) == (Timeframe.M15, 300, 3)
    assert s.session is not None
    assert s.timeframes == [Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1]


def test_first_evaluated_bar_and_r_net_list() -> None:
    result = study(world({ENTRY + timedelta(minutes=10): {"low": "97.00"}}))
    assert result.first_evaluated == START + timedelta(minutes=15)
    assert result.r_net == [D("-1.0000")]


class Recorder:
    setup_tag = "recorder"
    playbook_id = "recorder"
    version = "1"

    def __init__(self) -> None:
        self.seen: list[FeatureSnapshot] = []

    def detect(self, snapshot: FeatureSnapshot) -> list[SetupCandidate]:
        self.seen.append(snapshot)
        return []


def test_the_study_sees_the_other_instruments_as_the_engine_does() -> None:
    """Roadmap 10.2: the study reads each reference's bars closed at the trigger close; an instrument without
    history gets null features, as a reference the terminal cannot serve does live."""
    import random

    from aifund.config.loader import load_trading_config
    from aifund.config.settings import PROJECT_ROOT
    from aifund.research.signals import CrossAssetSpec

    rng = random.Random(3)
    closes = [1.1]
    for _ in range(14 * 24 - 1):
        closes.append(round(closes[-1] * (1 + rng.gauss(0, 0.001)), 5))
    eurusd = [
        Bar(symbol="EURUSD", timeframe=Timeframe.H1, time=T0 + i * timedelta(hours=1), open=D(str(c)),
            high=D(str(c)) + D("0.001"), low=D(str(c)) - D("0.001"), close=D(str(c)), tick_volume=1)
        for i, c in enumerate(closes)
    ]  # fmt: skip
    base = world()
    history = History(
        {**base._series, ("EURUSD", Timeframe.H1): History.from_bars({("EURUSD", Timeframe.H1): eurusd}, {})
         .series("EURUSD", Timeframe.H1)}, base.specs,
    )  # fmt: skip
    others = (("eurusd", "EURUSD"), ("btcusd", "BTCUSD"))
    cross = CrossAssetSpec(Timeframe.H1, 120, timedelta(hours=2), others)
    recorder = Recorder()
    run_study(history, spec(cross=cross), [recorder], START, START + timedelta(hours=6))
    assert recorder.seen
    for snap in recorder.seen:
        close = snap.bar_time + timedelta(minutes=15)
        last = [b for b in eurusd if b.time + timedelta(hours=1) <= close][-1]
        expected = cross.input(history, history.get("XAUUSD", Timeframe.H1), close)
        assert expected.others["eurusd"][-1] == last  # closed bars only
        assert isinstance(snap.features["xa.eurusd.ret4_z"], float)
        assert all(v is None for k, v in snap.features.items() if k.startswith("xa.btcusd."))

    cfg = load_trading_config(PROJECT_ROOT / "config" / "trading.example.yaml").config
    xau = CrossAssetSpec.from_config(cfg, cfg.symbol("XAUUSD"))
    assert xau is not None
    assert xau.others == (("eurusd", "EURUSD"), ("btcusd", "BTCUSD"), ("nas100", "NAS100.r"))
    off = cfg.model_copy(update={"cross_asset": cfg.cross_asset.model_copy(update={"enabled": False})})
    assert CrossAssetSpec.from_config(off, cfg.symbol("XAUUSD")) is None
