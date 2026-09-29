"""Condition grammar (roadmap R.5, docs/04 §5, docs/09 §5): every op, validation, canonical form, mirrors.

Mirror correctness is checked against the real snapshot builder: a feature's declared mirror must predict its
partner's value on the PRICE-REFLECTED market (price -> K - price, highs <-> lows) for every feature.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from pydantic import TypeAdapter

from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar, Tick
from aifund.market import conditions as cnd
from aifund.market import feature_registry as reg
from aifund.market.conditions import AllOf, AnyOf, Condition, ConditionError, Predicate
from aifund.market.feature_registry import CATEGORY_MIRROR, FeatureSource, MirrorKind
from aifund.market.features import build_snapshot

COND: TypeAdapter[Condition] = TypeAdapter(Condition)


def c(data: dict[str, Any]) -> Condition:
    return COND.validate_python(data)


def p(feature: str, op: str, value: Any) -> dict[str, Any]:
    return {"feature": feature, "op": op, "value": value}


# ------------------------------------------------------------------ evaluation


@pytest.mark.parametrize(
    ("op", "value", "x", "expected"),
    [
        ("<", 50, 49.9, True), ("<", 50, 50.0, False), ("<=", 50, 50.0, True), ("<=", 50, 50.1, False),
        (">", 50, 50.1, True), (">", 50, 50.0, False), (">=", 50, 50.0, True), (">=", 50, 49.9, False),
        ("==", 50, 50.0, True), ("==", 50, 50.5, False), ("!=", 50, 50.5, True), ("!=", 50, 50.0, False),
        ("in", [30, 50], 50.0, True), ("in", [30, 50], 40.0, False),
        ("not_in", [30, 50], 40.0, True), ("not_in", [30, 50], 30.0, False),
        ("between", [35, 50], 35.0, True), ("between", [35, 50], 50.0, True),
        ("between", [35, 50], 42.0, True),
        ("between", [35, 50], 34.99, False), ("between", [35, 50], 50.01, False),
    ],
)  # fmt: skip
def test_numeric_ops(op: str, value: Any, x: float, expected: bool) -> None:
    cond = c({"all": [p("m15.rsi14", op, value)]})
    cnd.check(cond, max_predicates=6)
    assert cnd.evaluate(cond, {"m15.rsi14": x}) is expected


@pytest.mark.parametrize(
    ("pred", "x", "expected"),
    [
        (p("h4.ema50_above_ema200", "==", True), True, True),
        (p("h4.ema50_above_ema200", "==", True), False, False),
        (p("h4.ema50_above_ema200", "!=", True), False, True),
        (p("h4.ema50_above_ema200", "in", [False]), False, True),
        (p("h4.ema_stack", "==", "BULL"), "BULL", True),
        (p("h4.ema_stack", "!=", "BULL"), "MIXED", True),
        (p("h4.ema_stack", "in", ["BULL", "MIXED"]), "BEAR", False),
        (p("h4.ema_stack", "not_in", ["BEAR"]), "MIXED", True),
        (p("ctx.day_of_week", "==", 2), 2, True),
        (p("ctx.day_of_week", "in", [0, 4]), 4, True),
    ],
)  # fmt: skip
def test_flag_category_and_int_ops(pred: dict[str, Any], x: Any, expected: bool) -> None:
    cond = c({"all": [pred]})
    cnd.check(cond, max_predicates=6)
    assert cnd.evaluate(cond, {pred["feature"]: x}) is expected


def test_a_flag_never_equals_a_number() -> None:
    cond = c({"all": [p("ctx.day_of_week", "==", 1)]})
    assert cnd.evaluate(cond, {"ctx.day_of_week": True}) is False  # True == 1 in Python, not here


def test_all_and_any_groups() -> None:
    cond = c({"all": [p("m15.rsi14", "<", 40), {"any": [p("m15.nr7", "==", True), p("m15.adx14", ">", 25)]}]})
    feats = {"m15.rsi14": 30.0, "m15.nr7": False, "m15.adx14": 30.0}
    assert cnd.evaluate(cond, feats) is True
    assert cnd.evaluate(cond, {**feats, "m15.adx14": 20.0}) is False
    assert cnd.evaluate(cond, {**feats, "m15.rsi14": 45.0}) is False
    top_any = c({"any": [p("m15.nr7", "==", True), p("m15.adx14", ">", 25)]})
    assert cnd.evaluate(top_any, {"m15.nr7": True, "m15.adx14": 10.0}) is True


def test_a_missing_feature_never_matches() -> None:
    cond = c({"any": [p("m15.rsi14", "<", 40), p("m15.adx14", ">", 25)]})
    assert cnd.missing(cond, {"m15.rsi14": 30.0, "m15.adx14": None}) == {"m15.adx14"}
    assert cnd.evaluate(cond, {"m15.rsi14": 30.0, "m15.adx14": None}) is False  # even inside an any
    assert cnd.evaluate(cond, {"m15.rsi14": 30.0}) is False


def test_any_groups_nest_one_level_only() -> None:
    with pytest.raises(ValueError, match="any"):
        c({"any": [{"any": [p("m15.nr7", "==", True)]}]})
    with pytest.raises(ValueError, match="at least 1 item"):
        c({"all": []})
    with pytest.raises(ValueError, match="op"):
        c({"all": [p("m15.rsi14", "~", 1)]})


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("pred", "message"),
    [
        (p("m15.rsi_14", "<", 30), "unknown feature"),
        (p("m15.rsi14", "<", 130), "outside m15.rsi14's range 0..100"),
        (p("m15.atr14_pct_rank100", ">", 1.5), "range 0..1"),
        (p("m15.rsi14", "<", "30"), "numeric"),
        (p("m15.rsi14", "<", True), "numeric"),
        (p("m15.rsi14", "<", float("nan")), "finite"),
        (p("m15.nr7", "==", 1), "flag"),
        (p("m15.nr7", ">", True), "needs a numeric feature"),
        (p("h4.ema_stack", "==", "UP"), "is not one of"),
        (p("h4.ema_stack", "<", "BULL"), "needs a numeric feature"),
        (p("m15.rsi14", "between", [50, 35]), "lo <= hi"),
        (p("m15.rsi14", "between", [35]), "[lo, hi]"),
        (p("m15.rsi14", "between", 35), "[lo, hi]"),
        (p("m15.rsi14", "in", []), "non-empty list"),
        (p("m15.rsi14", "<", [30]), "single value"),
    ],
)  # fmt: skip
def test_invalid_predicates_are_rejected_with_the_reason(pred: dict[str, Any], message: str) -> None:
    with pytest.raises(ConditionError, match=message.replace("[", r"\[").replace("]", r"\]")):
        cnd.check(c({"all": [pred]}), max_predicates=6)


def test_not_entry_time_features_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    late = replace(reg.get("m15.rsi14"), name="m15.future_rsi", available_at_entry=False)
    monkeypatch.setitem(reg._ALL, "m15.future_rsi", late)
    with pytest.raises(ConditionError, match="not available at entry time"):
        cnd.check(c({"all": [p("m15.future_rsi", "<", 30)]}), max_predicates=6)


def test_extra_filter_and_predicate_cap() -> None:
    cond = c({"all": [p("m15.rsi14", "<", 30), p("m15.adx14", ">", 20)]})
    with pytest.raises(ConditionError, match="2 predicates > max 1"):
        cnd.check(cond, max_predicates=1)
    with pytest.raises(ConditionError, match="no oscillators"):
        cnd.check(cond, max_predicates=6, allow=lambda s: "no oscillators" if "rsi" in s.name else None)


# ------------------------------------------------------------------ canonical form


def test_canonical_form_normalises_numbers_and_key_order() -> None:
    a = c({"all": [p("m15.rsi14", "between", [35, 50])]})
    b = COND.validate_json('{"all": [{"value": [35.0, 50.0], "op": "between", "feature": "m15.rsi14"}]}')
    assert cnd.canonical(a) == cnd.canonical(b)
    assert cnd.sha256(a) == cnd.sha256(b)
    assert cnd.sha256(a) != cnd.sha256(c({"all": [p("m15.rsi14", "between", [35, 51])]}))
    assert cnd.canonical(a) == ('{"all":[{"feature":"m15.rsi14","op":"between","value":[35.0,50.0]}]}')
    with pytest.raises(TypeError):
        cnd.canonical({"x": object()})


# ------------------------------------------------------------------ mirrors (grammar)


def test_mirror_transforms_each_kind() -> None:
    long = c(
        {"all": [
            p("h4.ema50_above_ema200", "==", True),
            p("m15.rsi14", "between", [35, 50]),
            p("h1.low_dist_ema50_atr", "<=", 0.25),
            p("h4.ema_stack", "in", ["BULL", "MIXED"]),
            p("ctx.regime", "!=", "TREND_DOWN"),
            p("m15.stoch_cross_up", "==", True),
            {"any": [p("m15.dist_ema20_atr", ">", -0.5), p("m15.adx14", ">=", 25),
                     p("m15.nr7", "in", [True])]},
        ]}
    )  # fmt: skip
    short = cnd.mirror(long)
    assert cnd.canonical(short) == cnd.canonical(
        c(
            {
                "all": [
                    p("h4.ema50_above_ema200", "==", False),
                    p("m15.rsi14", "between", [50, 65]),
                    p("h1.high_dist_ema50_atr", ">=", -0.25),
                    p("h4.ema_stack", "in", ["BEAR", "MIXED"]),
                    p("ctx.regime", "!=", "TREND_UP"),
                    p("m15.stoch_cross_down", "==", True),
                    {
                        "any": [
                            p("m15.dist_ema20_atr", "<", 0.5),
                            p("m15.adx14", ">=", 25),
                            p("m15.nr7", "in", [True]),
                        ]
                    },
                ]
            }
        )
    )
    assert cnd.canonical(cnd.mirror(short)) == cnd.canonical(long)  # an involution
    assert isinstance(cnd.mirror(c({"any": [p("m15.rsi14", "in", [30, 70])]})), AnyOf)
    assert cnd.mirror_predicate(Predicate(feature="m15.dist_ema50_atr", op="==", value=0)).value == 0.0


def test_a_feature_without_a_mirror_cannot_be_mirrored() -> None:
    with pytest.raises(ConditionError, match="no declared mirror"):
        cnd.mirror(AllOf(all_=[Predicate(feature="m15.close", op=">", value=1)]))
    with pytest.raises(ConditionError, match="no declared mirror"):
        cnd.mirror_predicate(Predicate(feature="ctx.drawdown_pct", op=">", value=1))


# ------------------------------------------------------------------ mirrors (registry, against real data)

LAST = datetime(2026, 9, 28, 9, 45, tzinfo=UTC)  # a Monday
TFS = (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
K = D("2000")


def walk(tf: Timeframe, seed: int, n: int = 320) -> list[Bar]:
    rng = random.Random(f"{seed}-{tf}")
    span = timedelta(minutes=tf.minutes)
    last_open = {
        Timeframe.D1: datetime(2026, 9, 27, tzinfo=UTC),
        Timeframe.H4: datetime(2026, 9, 28, 4, tzinfo=UTC),
        Timeframe.H1: datetime(2026, 9, 28, 9, tzinfo=UTC),
        Timeframe.M15: LAST,
    }[tf]
    price, drift = 1000.0, rng.uniform(-0.6, 0.6)
    out = []
    for i in range(n):
        o = price
        price = max(price + drift + rng.gauss(0, 3), 500.0)
        hi = max(o, price) + abs(rng.gauss(0, 1.5))
        lo = min(o, price) - abs(rng.gauss(0, 1.5))
        out.append(
            Bar(symbol="X", timeframe=tf, time=last_open - (n - 1 - i) * span, open=exact(o), high=exact(hi),
                low=exact(lo), close=exact(price), tick_volume=rng.randint(50, 500))
        )  # fmt: skip
    return out


def exact(x: float) -> D:
    """Prices on a 1/64 grid are exact binary floats, so K - price loses nothing and ties (e.g. equal +DM
    and -DM in ADX) break the same way on both sides; 0.01 steps would not."""
    return D(round(x * 64)) / 64


def reflect(b: Bar) -> Bar:
    return b.model_copy(
        update={"open": K - b.open, "close": K - b.close, "high": K - b.low, "low": K - b.high}
    )


def snap(bars: dict[Timeframe, list[Bar]], tick: Tick):  # type: ignore[no-untyped-def]
    return build_snapshot(
        symbol="X", trigger_tf=Timeframe.M15, setup_tf=Timeframe.H1, context_tfs=[Timeframe.H4, Timeframe.D1],
        bars=bars, as_of=LAST + timedelta(minutes=15, seconds=2), tick=tick,
    )  # fmt: skip


# Bollinger width is 2k·σ / SMA: relative to the price LEVEL, which reflection changes, so its rank moves a
# little. Volatility compression is direction-neutral by intent, so its declared mirror is SAME anyway.
APPROXIMATE = (".bb_width_pct_rank100",)


def expected(kind: MirrorKind, v: Any) -> Any:
    if v is None or kind is MirrorKind.SAME:
        return v
    if kind is MirrorKind.NEGATE:
        return -v
    if kind is MirrorKind.COMPLEMENT:
        return 100 - v
    if kind is MirrorKind.NOT:
        return not v
    return CATEGORY_MIRROR.get(v, v)


@pytest.mark.parametrize("seed", range(12))
def test_every_declared_mirror_matches_the_reflected_market(seed: int) -> None:
    bars = {tf: walk(tf, seed) for tf in TFS}
    last = bars[Timeframe.M15][-1].close
    tick = Tick(symbol="X", time=LAST, bid=last, ask=last + D("0.3"))
    original = snap(bars, tick)
    mirrored = snap(
        {tf: [reflect(b) for b in bs] for tf, bs in bars.items()},
        Tick(symbol="X", time=LAST, bid=K - tick.ask, ask=K - tick.bid),
    )
    checked = 0
    for name, value in original.features.items():
        found = reg.mirror_of(name)
        if found is None or name.endswith(APPROXIMATE):
            continue
        partner, kind = found
        want, got = expected(kind, value), mirrored.features[partner]
        if isinstance(want, float) and isinstance(got, float):
            assert got == pytest.approx(want, rel=1e-6, abs=1e-6), name
        else:
            assert got == want, name
        checked += 1
    assert checked >= 120  # 4 timeframes x 30 bar features + context


def test_mirrors_are_declared_for_every_snapshot_feature_and_are_mutual() -> None:
    for name, spec in reg.registry().items():
        if spec.source in (FeatureSource.PORTFOLIO, FeatureSource.PROPOSAL):
            continue
        if name.endswith(".close"):
            assert spec.mirror is None, name  # a price level has no mirror
            continue
        found = reg.mirror_of(name)
        assert found is not None, f"{name} has no declared mirror"
        partner, kind = found
        assert reg.mirror_of(partner) == (name, kind), f"{name} <-> {partner} is not mutual"


def test_describe_is_compact() -> None:
    cond = c(
        {"all": [p("m15.rsi14", "between", [35, 50.5]), p("h4.ema_stack", "in", ["BULL", "MIXED"]),
                 {"any": [p("m15.nr7", "==", True), p("m15.adx14", ">", 25.0)]}]}
    )  # fmt: skip
    assert cnd.describe(cond) == (
        "m15.rsi14 in 35..50.5 AND h4.ema_stack in [BULL, MIXED] AND (m15.nr7 == true OR m15.adx14 > 25)"
    )
    assert cnd.describe(c({"any": [p("m15.nr7", "!=", False)]})) == "m15.nr7 != false"


def test_a_category_without_declared_names_still_needs_a_name() -> None:
    with pytest.raises(ConditionError, match="is a category"):
        cnd.check(c({"all": [p("prop.setup_tag", "==", 5)]}), max_predicates=6)
