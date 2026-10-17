"""The pooled momentum study (roadmap 11.15): loading, pooling, and ``research.py momentum`` end to end."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from aifund.adapters import history_store as hs
from aifund.domain.enums import Side, SwapMode, Timeframe
from aifund.domain.market import Bar, SymbolSpec
from aifund.research.basket import momentum as mom
from aifund.research.basket.swap import SwapUnavailable
from aifund.research.ledger import Ledger, Split
from aifund.research.signals import CostModel
from tests.unit.research.test_research_script import load
from tests.unit.risk.test_stops import XAU

T0 = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
SWAP = {"swap_long": D("-1"), "swap_short": D("-1"), "swap_mode": SwapMode.POINTS, "swap_rollover3days": 3}


def spec(symbol: str, *, swap: bool = True) -> SymbolSpec:
    return XAU.model_copy(update={"symbol": symbol, **(SWAP if swap else {})})


def bars(symbol: str, tf: Timeframe, price: Callable[[float], float], start: datetime, n: int,
         *, pad: float = 0.05, keep: Callable[[datetime], bool] = lambda t: True) -> list[Bar]:  # fmt: skip
    """Bar k opens at price(hours since T0) and closes at the next bar's open."""
    step = timedelta(minutes=tf.minutes)
    out = []
    for k in range(n):
        t = start + k * step
        if not keep(t):
            continue
        h0 = (t - T0) / timedelta(hours=1)
        o, c = price(h0), price(h0 + tf.minutes / 60)
        out.append(Bar(symbol=symbol, timeframe=tf, time=t, open=D(f"{o:.2f}"), close=D(f"{c:.2f}"),
                       high=D(f"{max(o, c) + pad:.2f}"), low=D(f"{min(o, c) - pad:.2f}"), tick_volume=1,
                       spread_points=10))  # fmt: skip
    return out


def trend(h: float) -> float:
    return 100 * math.exp(0.002 * h / 24)  # +0.2% a day, every day


def flat(h: float) -> float:
    return 100.0


def export(root: Path, price: Callable[[float], float], *, days: int = 240, swap: bool = True) -> Path:
    """Two symbols quoting around the clock (no weekend flatten), H1 paths and D1 bars."""
    for sym in ("XAUUSD", "XAGUSD"):
        hs.write_bars(root, sym, Timeframe.H1, bars(sym, Timeframe.H1, price, T0, days * 24))
        hs.write_bars(root, sym, Timeframe.D1, bars(sym, Timeframe.D1, price, T0, days, pad=0.3))
    hs.write_specs(root, {s: spec(s, swap=swap) for s in ("XAUUSD", "XAGUSD")})
    manifest = hs.Manifest(exported_at=T0 + timedelta(days=days), server="Test", account_trade_mode="DEMO",
                           server_offset_minutes=0, start=T0, end=T0 + timedelta(days=days),
                           rows={"XAUUSD": {"H1": days * 24}})  # fmt: skip
    hs.write_manifest(root, manifest)
    return root


def test_load_momentum_needs_d1_and_swap_for_the_daily_family(tmp_path: Path) -> None:
    root = export(tmp_path / "h", trend, days=40)
    md = mom.load_momentum(root, "XAUUSD", spec("XAUUSD"), 0, "tsmom_daily")
    assert md is not None
    assert (md.path_tfs, md.weekends, md.swap is not None) == (["H1"], True, True)
    with pytest.raises(SwapUnavailable):
        mom.load_momentum(root, "XAUUSD", spec("XAUUSD", swap=False), 0, "tsmom_daily")
    assert mom.load_momentum(root, "NOPE", spec("NOPE"), 0, "tsmom_daily") is None  # no D1
    assert mom.load_momentum(root, "XAUUSD", spec("XAUUSD"), 0, "intraday_momentum") is None  # no M5 path
    assert mom.load_momentum(tmp_path / "empty", "XAUUSD", spec("XAUUSD"), 0, "tsmom_daily") is None
    hs.write_bars(root, "D1ONLY", Timeframe.D1, bars("D1ONLY", Timeframe.D1, trend, T0, 40))
    assert mom.load_momentum(root, "D1ONLY", spec("D1ONLY"), 0, "tsmom_daily") is None  # no path to trade on


def test_pooling_sorts_every_symbols_outcomes_and_clusters_them_by_date(tmp_path: Path) -> None:
    root = export(tmp_path / "h", trend, days=60)
    universe = [
        m for s in ("XAUUSD", "XAGUSD") if (m := mom.load_momentum(root, s, spec(s), 0, "tsmom_daily"))
    ]
    strat = mom.FAMILIES["tsmom_daily"]["d28"]
    outs = mom.pooled(strat, universe, costs=CostModel(), offset=0, start=T0, end=T0 + timedelta(days=60))
    assert outs == sorted(outs, key=lambda o: (o.entry_time, o.symbol))
    assert {o.symbol for o in outs} == {"XAUUSD", "XAGUSD"}
    assert {o.direction.value for o in outs} == {"LONG"}
    by_date = {mom.date_cluster(o) for o in outs}
    assert len(by_date) * 2 == len(outs)  # both symbols decide on the same D1 closes
    assert mom.clusters_per_year(outs) == pytest.approx(365.25, rel=0.05)  # one decision every calendar day
    assert mom.clusters_per_year(outs[:1]) == 0.0
    assert mom.per_month(30, T0, T0 + timedelta(days=30.44)) == pytest.approx(30)
    forced = mom.pooled(strat, universe, costs=CostModel(), offset=0, start=T0, end=T0 + timedelta(days=60),
                        force=Side.SELL)  # fmt: skip
    assert {o.direction.value for o in forced} == {"SHORT"}


def run_script(root: Path, out: Path, *extra: str) -> int:
    script = load()
    code: int = script.main(["momentum", "--history", str(root), "--out", str(out), *extra])
    return code


def test_momentum_needs_a_family_swap_and_a_symbol_it_trades(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "r"
    assert run_script(export(tmp_path / "h", trend, days=40, swap=False), out) == 2
    assert "momentum needs --family" in capsys.readouterr().err
    assert run_script(tmp_path / "h", out, "--family", "tsmom_daily") == 2
    assert "refused: XAGUSD: no swap in the export" in capsys.readouterr().err
    assert run_script(tmp_path / "h", out, "--family", "intraday_momentum", "--symbols", "XAUUSD,NOPE") == 2
    printed = capsys.readouterr()
    assert "NOPE: not in the export" in printed.out
    assert "XAUUSD: not in intraday_momentum" in printed.out
    assert "can run intraday_momentum" in printed.err


def test_a_trending_pool_passes_e1_and_spends_the_holdout_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, out = export(tmp_path / "h", trend), tmp_path / "r"
    assert run_script(root, out, "--family", "tsmom_daily", "--spend-holdout") == 0
    printed = capsys.readouterr().out
    assert "gate E1: PASSED" in printed
    assert "BELOW the frequency target of 100" in printed  # two symbols x one decision a day = ~61 a month
    assert "power: could detect" in printed
    splits = [t.split for t in Ledger(out / "ledger.jsonl").trials()]
    assert splits == [Split.IN_SAMPLE] * 3 + [Split.WALK_FORWARD, Split.HOLDOUT]
    (report,) = out.glob("report_momentum_tsmom_daily_*.json")
    data = json.loads(report.read_text())
    assert data["chosen"] in mom.FAMILIES["tsmom_daily"]
    assert data["below_frequency_target"] is True
    assert set(data["controls"]) == {"always_long", "long_only", "short_only", "stress_every_boundary"}
    assert set(data["per_symbol"]) == {"XAUUSD", "XAGUSD"}
    assert data["swap"] == {"XAUUSD": True, "XAGUSD": True}
    assert data["holdout"]["checks"][0]["passed"] is True
    assert run_script(root, out, "--family", "tsmom_daily", "--spend-holdout") == 0
    assert "already had its one look" in capsys.readouterr().out


def test_a_flat_pool_fails_and_keeps_the_holdout_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, out = export(tmp_path / "h", flat, days=120), tmp_path / "r"
    assert run_script(root, out, "--family", "tsmom_daily", "--spend-holdout") == 0
    printed = capsys.readouterr().out
    assert "holdout NOT spent: the walk-forward part of E1 failed" in printed
    assert "power:" not in printed  # no outcome: nothing to measure
    assert run_script(root, out, "--family", "tsmom_daily") == 0
    assert "verdict: FAILED (E1 walk-forward part; holdout unspent)" in capsys.readouterr().out


def test_intraday_runs_on_five_minute_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, out = tmp_path / "h", tmp_path / "r"
    start = T0 + timedelta(days=35)  # D1 history first, for the ATR

    def weekday(t: datetime) -> bool:
        return t.weekday() < 5

    hs.write_bars(root, "NAS100.r", Timeframe.D1, bars("NAS100.r", Timeframe.D1, trend, T0, 70, pad=0.3))
    hs.write_bars(root, "NAS100.r", Timeframe.M5,
                  bars("NAS100.r", Timeframe.M5, trend, start, 35 * 288, keep=weekday))  # fmt: skip
    hs.write_specs(root, {"NAS100.r": spec("NAS100.r", swap=False)})
    manifest = hs.Manifest(exported_at=start + timedelta(days=35), server="Test", account_trade_mode="DEMO",
                           server_offset_minutes=0, start=start, end=start + timedelta(days=35),
                           rows={"NAS100.r": {"M5": 1}})  # fmt: skip
    hs.write_manifest(root, manifest)
    assert run_script(root, out, "--family", "intraday_momentum") == 0
    printed = capsys.readouterr().out
    assert "paths NAS100.r M5" in printed
    (report,) = out.glob("report_momentum_intraday_momentum_*.json")
    data = json.loads(report.read_text())
    assert data["swap"] == {"NAS100.r": False}  # flat before every rollover: no swap needed
    assert data["trades_per_month"] > 15  # one session a weekday
