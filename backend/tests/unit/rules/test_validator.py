"""Rule validator (roadmap 7.6, docs/04 §6): each check, the severity, the planted pattern passes, noise
fails, an overly broad rule is escalated as a strategy-level finding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aifund.domain.enums import Direction
from aifund.rules import dsl
from aifund.rules.dsl import ActionType
from aifund.rules.miner import Sample
from aifund.rules.validator import ValidatorConfig, jaccard, severity, validate
from tests.unit.rules.synthetic import dataset

CFG = ValidatorConfig(resamples=500)
PLANTED = dsl.parse(
    {
        "rule_id": "R-0001",
        "scope": {"directions": ["LONG"]},
        "conditions": {"all": [{"feature": "ctx.session", "op": "==", "value": "NY"},
                               {"feature": "m15.rsi14", "op": ">", "value": 40}]},
        "action": {"type": "penalty", "points": 10},
    }
)  # fmt: skip
FLAG = dsl.parse(
    {
        "rule_id": "R-0002",
        "conditions": {"all": [{"feature": "m15.nr7", "op": "==", "value": True}]},
        "action": {"type": "penalty", "points": 10},
    }
)
T = datetime(2026, 1, 1, tzinfo=UTC)


def samples(rows: list[tuple[bool, float]], *, virtual: bool = False) -> list[Sample]:
    """(nr7 flag, R) in time order."""
    return [
        Sample(
            f"K{i:03d}",
            T + timedelta(hours=i),
            "XAUUSD",
            Direction.LONG,
            "s",
            "M15",
            r,
            virtual,
            {"m15.nr7": f},
        )
        for i, (f, r) in enumerate(rows)
    ]


def block(flagged: list[float], rest: list[float]) -> list[tuple[bool, float]]:
    """Interleave flagged and unflagged outcomes so both splits get their share."""
    out: list[tuple[bool, float]] = []
    step = max(len(rest) // max(len(flagged), 1), 1)
    it = iter(flagged)
    for i, r in enumerate(rest):
        if i % step == 0:
            nxt = next(it, None)
            if nxt is not None:
                out.append((True, nxt))
        out.append((False, r))
    out += [(True, x) for x in it]
    return out


def losing(n: int, loss: float = -1.0, win_every: int = 5) -> list[float]:
    return [1.5 if i % win_every == 0 else loss for i in range(n)]


def winning(n: int) -> list[float]:
    return [1.5 if i % 2 else -1.0 for i in range(n)]


def test_the_planted_pattern_passes_with_its_evidence() -> None:
    v = validate(PLANTED, dataset(600, seed=1), CFG)
    assert v.passed, v.failures
    e = v.evidence
    assert e["n_matched"] >= 20 and e["n_holdout"] >= 6 and e["effect_discovery"] <= -0.3  # noqa: PT018
    assert e["ci_mean"][1] < 0 and e["coverage"] <= 0.3  # noqa: PT018
    assert v.action.type is ActionType.BLOCK  # mean ≈ -0.75R, n well above 30
    assert v.needs_approval
    assert len(v.matched) == e["n_matched"]


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_the_same_rule_fails_on_noise(seed: int) -> None:
    v = validate(PLANTED, dataset(600, seed=seed, planted=False), CFG)
    assert not v.passed
    assert any("effect" in f or "CI90" in f for f in v.failures)


def test_an_overly_broad_rule_is_a_strategy_level_finding() -> None:
    broad = PLANTED.model_copy(
        update={"conditions": dsl.AllOf(all_=[dsl.Predicate(feature="m15.rsi14", op=dsl.Op.GE, value=5)])}
    )
    v = validate(broad, dataset(600, seed=1), CFG)
    assert not v.passed
    assert any(f.startswith("coverage") for f in v.failures)
    assert v.strategy_finding is not None and "too broad" in v.strategy_finding  # noqa: PT018


def test_too_few_matches_overall_and_in_the_holdout() -> None:
    v = validate(FLAG, samples(block(losing(10), winning(90))), CFG)
    assert "10 matches < 20" in v.failures
    late = samples([(True, -1.0)] * 25 + [(False, x) for x in winning(75)])  # every match in discovery
    assert "0 holdout matches < 6" in validate(FLAG, late, CFG).failures


def test_virtual_heavy_evidence_needs_half_as_much_again() -> None:
    rows = block(losing(25), winning(75))
    real = validate(FLAG, samples(rows), CFG)
    virtual = validate(FLAG, samples(rows, virtual=True), CFG)
    assert not any("matches <" in f for f in real.failures)
    assert "25 matches < 30" in virtual.failures and virtual.evidence["virtual_share"] == 1.0  # noqa: PT018


def test_the_effect_must_hold_in_discovery_and_the_holdout() -> None:
    flip = [(True, -1.0)] * 1 + block(losing(20), winning(50)) + block(winning(15), winning(30))
    v = validate(FLAG, samples(flip), CFG)
    assert any(f.startswith("holdout effect") for f in v.failures)
    mild = block([0.2] * 30, [0.3] * 70)
    v = validate(FLAG, samples(mild), CFG)
    assert any(f.startswith("discovery effect") for f in v.failures)


def test_a_mean_that_is_not_surely_negative_fails() -> None:
    v = validate(FLAG, samples(block([1.5, -1.0, -1.0] * 10, [2.0] * 70)), CFG)
    assert any(f.startswith("CI90 upper") for f in v.failures)


def test_removing_the_matches_must_help_and_must_not_cost_the_winners() -> None:
    big_wins = [8.0 if i % 3 == 0 else -1.0 for i in range(30)]  # a third of the window's winning R
    v = validate(FLAG, samples(block(big_wins, [2.0] * 70)), CFG)
    assert any(f.startswith("removes") for f in v.failures)
    small_wins = validate(FLAG, samples(block([1.0] * 30, [5.0] * 70)), CFG)  # worse than the rest, still > 0
    assert any(f.startswith("removing matches does not improve") for f in small_wins.failures)


def test_complexity_and_duplicates() -> None:
    four = FLAG.model_copy(
        update={
            "conditions": dsl.AllOf(all_=[dsl.Predicate(feature="m15.nr7", op=dsl.Op.EQ, value=True)] * 4)
        }
    )
    rows = samples(block(losing(30), winning(70)))
    assert "4 predicates > 3" in validate(four, rows, CFG).failures
    first = validate(FLAG, rows, CFG)
    assert first.passed, first.failures
    dup = validate(FLAG.model_copy(update={"rule_id": "R-0009"}), rows, CFG, live={"R-0002": first.matched})
    assert dup.duplicate_of == "R-0002" and not dup.passed  # noqa: PT018
    again = validate(FLAG, rows, CFG, live={"R-0002": first.matched})  # re-validating itself is no duplicate
    assert again.passed


def test_severity_bands() -> None:
    assert severity(-0.7, -0.3, 30, -0.9).type is ActionType.BLOCK
    assert severity(-0.7, -0.3, 29, -0.9).type is ActionType.RISK_SCALE  # too few for a block
    assert severity(-0.7, -0.1, 40, -0.9).factor == Decimal("0.5")
    assert severity(-0.2, -0.05, 40, -0.4).points == 16
    assert severity(-0.1, -0.05, 40, -0.05).points == 5
    assert severity(-0.29, None, 40, -2.0).points == 30


def test_jaccard_and_an_empty_window() -> None:
    assert jaccard(frozenset("ab"), frozenset("bc")) == pytest.approx(1 / 3)
    assert jaccard(frozenset(), frozenset()) == 0.0
    v = validate(FLAG, [], CFG)
    assert not v.passed and v.evidence["window"] is None  # noqa: PT018
