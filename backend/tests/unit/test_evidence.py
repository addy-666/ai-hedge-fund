"""Evidence gate E1 (roadmap R.7, docs/09 §7): outside SIM a detector needs matching, passing evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aifund.config.evidence import (
    Deployment,
    EvidenceError,
    EvidenceGate,
    EvidenceRecord,
    load_evidence,
    profile_key,
    require_evidence,
    write_evidence,
)
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT
from aifund.config.trading_config import StrategyConfig, SymbolConfig
from aifund.domain.enums import Mode, Timeframe
from aifund.engine.equity import EquityTracker
from aifund.engine.pipeline import DecisionPipeline
from aifund.strategies.base import TfRoles
from aifund.strategies.mtf_trend_pullback import MtfTrendPullback, PullbackParams

ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))
PROFILE = profile_key(Timeframe.M15, Timeframe.H1, [Timeframe.H4])
DETECTOR = MtfTrendPullback(ROLES)
M5_PROFILE = profile_key(Timeframe.M5, Timeframe.H1, [Timeframe.H4])


def record(**over: Any) -> EvidenceRecord:
    base: dict[str, Any] = dict(
        detector="mtf_trend_pullback", version="1", params_sha256=DETECTOR.params_sha256(),
        gate=EvidenceGate.E1_PASSED, symbols=["XAUUSD"], profile=PROFILE, walk_forward={"n": 250},
        holdout={"n": 80}, created_at=datetime(2026, 9, 29, tzinfo=UTC),
    )  # fmt: skip
    return EvidenceRecord(**{**base, **over})


def deploy(detector: Any = DETECTOR, symbol: str = "XAUUSD") -> list[Deployment]:
    return [Deployment(detector, symbol, PROFILE)]


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.DEMO, Mode.LIVE])
def test_a_matching_passing_record_lets_a_detector_run(mode: Mode) -> None:
    require_evidence(deploy(), [record()], mode=mode, dry_run=False)
    require_evidence(deploy(), [record(gate=EvidenceGate.E2_PASSED)], mode=mode, dry_run=False)


@pytest.mark.parametrize(
    ("records", "detector", "symbol", "message"),
    [
        ([], DETECTOR, "XAUUSD", "no evidence record"),
        ([record(version="0")], DETECTOR, "XAUUSD", "no evidence record"),
        ([record()], MtfTrendPullback(ROLES, PullbackParams(oversold=20)), "XAUUSD", "STALE"),
        ([record(gate=EvidenceGate.E1_FAILED)], DETECTOR, "XAUUSD", "E1_FAILED, not E1_PASSED"),
        ([record()], DETECTOR, "BTCUSD", "not on BTCUSD"),
        ([record(profile=M5_PROFILE)], DETECTOR, "XAUUSD", "profile"),
    ],
)  # fmt: skip
def test_missing_stale_failed_or_mismatched_evidence_is_refused(
    records: list[EvidenceRecord], detector: Any, symbol: str, message: str
) -> None:
    with pytest.raises(EvidenceError, match=message):
        require_evidence(deploy(detector, symbol), records, mode=Mode.DEMO, dry_run=False)


def test_every_problem_is_listed_at_once() -> None:
    both = deploy() + deploy(symbol="BTCUSD")
    with pytest.raises(EvidenceError) as err:
        require_evidence(both, [], mode=Mode.PAPER, dry_run=False)
    assert str(err.value).count("no evidence record") == 2
    assert "refuses to start in PAPER" in str(err.value)


def test_sim_and_dry_runs_are_exempt() -> None:
    require_evidence(deploy(), [], mode=Mode.SIM, dry_run=False)
    require_evidence(deploy(), [], mode=Mode.DEMO, dry_run=True)  # nothing is ever sent in a dry run


def test_records_round_trip_through_files(tmp_path: Path) -> None:
    path = write_evidence(tmp_path, record())
    assert path.name == f"mtf_trend_pullback_v1_{DETECTOR.params_sha256()[:12]}.json"
    assert load_evidence(tmp_path) == [record()]
    assert load_evidence(tmp_path / "missing") == []
    (tmp_path / "broken.json").write_text('{"detector": "x"}')
    with pytest.raises(EvidenceError, match=r"broken\.json: invalid evidence record"):
        load_evidence(tmp_path)


def test_the_parameter_hash_follows_the_parameters() -> None:
    assert (
        MtfTrendPullback(ROLES).params_sha256() == MtfTrendPullback(ROLES, PullbackParams()).params_sha256()
    )
    assert (
        MtfTrendPullback(ROLES).params_sha256()
        != MtfTrendPullback(ROLES, PullbackParams(oversold=20)).params_sha256()
    )


def pipeline(mode: Mode, *, evidence: bool, dry_run: bool = False) -> DecisionPipeline:
    cfg = load_trading_config(PROJECT_ROOT / "config" / "trading.example.yaml").config
    cfg = cfg.model_copy(
        update={
            "engine": cfg.engine.model_copy(update={"mode": mode}),
            "symbols": [s for s in cfg.symbols if s.broker == "XAUUSD"],
            "strategy": StrategyConfig(analyst_enabled=False, baseline_enabled=True, dry_run=dry_run),
        }
    )
    profile = cfg.profiles[cfg.symbols[0].profile]

    def detectors(_s: SymbolConfig, roles: TfRoles) -> list[MtfTrendPullback]:
        return [MtfTrendPullback(roles)]

    good = record(profile=profile_key(profile.trigger_tf, profile.setup_tf, profile.context_tfs))
    return DecisionPipeline(
        cfg, broker=None, market=None, factory=None, clock=None, executor=None, risk=None,  # type: ignore[arg-type]
        detectors=detectors, equity=EquityTracker.for_engine(cfg.engine), account_id="acc",
        evidence=[good] if evidence else [],
    )  # fmt: skip


def test_the_pipeline_refuses_to_start_without_evidence_outside_sim() -> None:
    pipeline(Mode.SIM, evidence=False)  # replay and research
    pipeline(Mode.PAPER, evidence=False, dry_run=True)  # shadow decisions only
    pipeline(Mode.PAPER, evidence=True)
    with pytest.raises(EvidenceError, match="mtf_trend_pullback v1 on XAUUSD: no evidence record"):
        pipeline(Mode.PAPER, evidence=False)
