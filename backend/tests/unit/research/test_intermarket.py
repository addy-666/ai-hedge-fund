"""Roadmap 10.3: the curated intermarket hypotheses and the research history of cross-asset instruments."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aifund.adapters import history_store as hs
from aifund.config.loader import load_trading_config
from aifund.config.settings import PROJECT_ROOT
from aifund.domain.enums import Timeframe
from aifund.domain.market import Bar
from aifund.research.history import History
from aifund.research.loop import HistoryEvaluator
from aifund.research.signals import StudySpec
from aifund.strategies.base import TfRoles
from aifund.strategies.dsl_detector import DslDetector, EntryHypothesis
from tests.integration.test_snapshot_pipeline import SPEC

IDEAS = sorted((PROJECT_ROOT / "config" / "research").glob("intermarket_*.json"))
ROLES = TfRoles(trigger=Timeframe.M15, setup=Timeframe.H1, context=(Timeframe.H4,))  # research.py's default
T0 = datetime(2026, 3, 2, tzinfo=UTC)


def test_there_is_a_file_per_traded_symbol() -> None:
    cfg = load_trading_config(PROJECT_ROOT / "config" / "trading.example.yaml").config
    assert [p.stem for p in IDEAS] == sorted(f"intermarket_{s.canonical.lower()}" for s in cfg.symbols)


@pytest.mark.parametrize("path", IDEAS, ids=lambda p: p.stem)
def test_every_idea_is_a_valid_cross_asset_hypothesis_for_the_research_profile(path: Path) -> None:
    cfg = load_trading_config(PROJECT_ROOT / "config" / "trading.example.yaml").config
    target = path.stem.removeprefix("intermarket_")
    slugs = {i.slug for i in cfg.instruments()} - {target}  # what that symbol can see
    docs = json.loads(path.read_text(encoding="utf-8"))
    ids = [d["id"] for d in docs]
    assert len(set(ids)) == len(ids)
    for doc in docs:
        h = EntryHypothesis.model_validate(doc)
        DslDetector(h, ROLES)  # the timeframes belong to the profile
        assert h.instruments(), h.id  # every one is about another market
        assert h.instruments() <= slugs, h.id  # and never about the symbol itself


def bar(symbol: str, tf: Timeframe, i: int) -> Bar:
    return Bar(symbol=symbol, timeframe=tf, time=T0 + i * timedelta(minutes=tf.minutes), open=D(1), high=D(2),
               low=D("0.5"), close=D("1.5"), tick_volume=1)  # fmt: skip


def test_research_history_reads_references_on_their_timeframe_only(tmp_path: Path) -> None:
    for tf in (Timeframe.M15, Timeframe.H1):
        hs.write_bars(tmp_path, "XAUUSD", tf, [bar("XAUUSD", tf, i) for i in range(3)])
    hs.write_bars(tmp_path, "EURUSD", Timeframe.H1, [bar("EURUSD", Timeframe.H1, i) for i in range(3)])
    hs.write_bars(tmp_path, "EURUSD", Timeframe.M15, [bar("EURUSD", Timeframe.M15, i) for i in range(3)])
    hs.write_specs(tmp_path, {s: SPEC.model_copy(update={"symbol": s}) for s in ("XAUUSD", "EURUSD")})
    history = History.load(tmp_path, ["XAUUSD"], [Timeframe.M15, Timeframe.H1],
                           references=["EURUSD", "XAGUSD", "XAUUSD"], reference_tf=Timeframe.H1)  # fmt: skip
    assert history.get("EURUSD", Timeframe.H1) is not None
    assert history.get("EURUSD", Timeframe.M15) is None  # references: their timeframe only
    assert history.get("XAUUSD", Timeframe.M15) is not None  # a studied symbol keeps all of its own
    assert set(history.specs) == {"XAUUSD", "EURUSD"}  # XAGUSD was never exported: skipped

    cfg = load_trading_config(PROJECT_ROOT / "config" / "trading.example.yaml").config
    spec = StudySpec.from_config(cfg, cfg.symbol("XAUUSD"))
    evaluator = HistoryEvaluator(history, [spec])
    assert evaluator.instruments == ["eurusd"]  # BTCUSD and NAS100 have no history here: not offered
