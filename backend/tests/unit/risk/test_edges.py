"""Risk-layer edge paths (R.0 coverage): invalid inputs fail closed, safety rules that fall through."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from aifund.config.trading_config import GuardsConfig, LimitsConfig, RiskConfig, StopsConfig
from aifund.domain.enums import ReasonCode, Side
from aifund.risk.guards import evaluate_guards
from aifund.risk.limits import Exposure, check_new_exposure
from aifund.risk.position_manager import ActionKind
from aifund.risk.sizing import SizingRejected, size_position
from aifund.risk.stops import StopError, plan_stops
from tests.unit.risk import test_guards as g
from tests.unit.risk import test_manager as m
from tests.unit.risk import test_position_manager as pm
from tests.unit.risk import test_sizing as sz
from tests.unit.risk.test_stops import XAU

# ---------------------------------------------------------------- pure checks


def test_zero_positions_per_symbol_blocks_every_entry() -> None:
    verdict = evaluate_guards(
        g.BASE, GuardsConfig(), max_positions_per_symbol=0, max_trades_per_symbol_per_day=5
    )
    assert verdict.rejection is not None
    assert verdict.rejection.reason is ReasonCode.MAX_PER_SYMBOL


def test_exposure_check_refuses_non_positive_equity() -> None:
    new = Exposure("XAUUSD", "USD_INVERSE", D("50"), D("10000"))
    rejection = check_new_exposure(new, [], D(0), LimitsConfig())
    assert rejection is not None
    assert rejection.reason is ReasonCode.PORTFOLIO_HEAT


def test_sizing_refuses_non_positive_equity() -> None:
    with pytest.raises(SizingRejected) as exc:
        size_position(sz.inputs(equity=D(0)), XAU, RiskConfig())
    assert exc.value.reason is ReasonCode.RISK_BELOW_MIN_LOT
    assert "rejected" in exc.value.worksheet


@pytest.mark.parametrize(("entry", "spread"), [(D(0), D("0.28")), (D("4150"), D("-0.01"))])
def test_stops_refuse_invalid_entry_or_spread(entry: D, spread: D) -> None:
    with pytest.raises(StopError, match="invalid entry or spread"):
        plan_stops(side=Side.BUY, entry_ref=entry, spread=spread, atr=D("9.67"), spec=XAU, cfg=StopsConfig())


# ---------------------------------------------------------------- Risk Manager


async def test_no_valid_stop_rejects_the_trade() -> None:
    # an ATR so large that even the minimum stop lands below zero
    outcome = await m.manager().evaluate(m.request(atr=D("5000")))
    assert outcome.intent is None
    assert outcome.rejection is not None
    assert outcome.rejection.reason is ReasonCode.INTERNAL_ERROR
    assert "no valid stop" in outcome.rejection.detail


def test_counterfactual_stops_need_a_direction_an_atr_and_a_valid_stop() -> None:
    manager = m.manager()
    assert manager.counterfactual_stops(m.DECISION, m.TICK, D("9.67"), XAU) is not None
    assert manager.counterfactual_stops(m.DECISION, m.TICK, D(0), XAU) is None
    assert manager.counterfactual_stops(m.DECISION, m.TICK, D("5000"), XAU) is None
    none = m.DECISION.model_copy(update={"direction": m.Direction.NONE})
    assert manager.counterfactual_stops(none, m.TICK, D("9.67"), XAU) is None


# ---------------------------------------------------------------- position manager


def test_missing_sl_with_no_way_to_size_one_closes() -> None:
    action = pm.manager().plan(pm.facts(pm.pos(sl=None), planned_sl_distance=None, atr=None), pm.MON)
    assert action is not None
    assert action.kind is ActionKind.STOP_BREACHED
    assert "no way to size" in action.detail


def test_realignment_is_skipped_when_the_planned_level_is_already_crossed() -> None:
    # SL 16 away (planned 11.93) but the bid already sits below the planned level 4138.35: leave it
    t = pm.tick(bid="4137.00", ask="4137.28")
    assert pm.manager().plan(pm.facts(pm.pos(sl="4134.28"), t), pm.MON) is None


def test_break_even_never_loosens_a_stop_already_past_entry() -> None:
    t = pm.tick(bid="4162.30", ask="4162.58")  # +1R
    assert pm.manager(break_even_at_r=D("1.0")).plan(pm.facts(pm.pos(sl="4151.00"), t), pm.MON) is None


def test_orphan_with_a_stop_has_no_planned_distance_to_realign_to() -> None:
    assert pm.manager().plan(pm.facts(pm.pos(sl="4134.28"), planned_sl_distance=None), pm.MON) is None
