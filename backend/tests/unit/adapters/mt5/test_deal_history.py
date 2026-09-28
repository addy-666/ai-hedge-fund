"""Recorded MT5 deal histories must rebuild the broker's balance exactly (roadmap 3.2).

Every file in tests/fixtures/mt5_deals/ is checked: real recordings from scripts/capture_deals.py and the
hand-derived synthetic example. The example's expected values are derived in the comments of the script
that wrote it (see the file's "note") and repeated here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from aifund.adapters.mt5.deal_history import DealFixture, check_ledger, keep_fields
from aifund.domain.enums import CloseReason, DealReason
from aifund.reconcile import pnl

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "mt5_deals"
ALL = sorted(FIXTURES.glob("*.json"))
EXAMPLE = FIXTURES / "example_synthetic.json"


@pytest.mark.parametrize("path", ALL, ids=[p.stem for p in ALL])
def test_deals_rebuild_the_mt5_balance_to_the_cent(path: Path) -> None:
    check = check_ledger(DealFixture.load(path))
    assert check.mismatch == 0, (
        f"{path.name}: balance {check.balance} but deals sum to {check.total} "
        f"(trades {check.trades_net}, balance/fee deals {check.non_trade_total})"
    )


@pytest.mark.parametrize("path", ALL, ids=[p.stem for p in ALL])
def test_every_closed_position_is_consistent(path: Path) -> None:
    fixture = DealFixture.load(path)
    tickets = [d["ticket"] for d in fixture.deals]
    assert len(tickets) == len(set(tickets)), "a deal recorded twice"
    check = check_ledger(fixture)
    for pid, s in check.positions.items():
        assert s.net == s.gross + s.commission + s.swap + s.fee
        if not s.fully_closed(s.volume_in):
            continue
        assert s.last_exit is not None
        assert s.close_price_vwap is not None
        exits = [d.price for d in check.trade_deals[pid] if d.entry.reduces_position]
        assert min(exits) <= s.close_price_vwap <= max(exits), pid
        assert s.close_time == max(d.time for d in check.trade_deals[pid] if d.entry.reduces_position)
        assert isinstance(pnl.close_reason(s.last_exit, None), CloseReason)


def test_example_positions_match_the_hand_derivation() -> None:
    check = check_ledger(DealFixture.load(EXAMPLE))
    nets = {pid: s.net for pid, s in check.positions.items()}
    assert nets == {101: D("1.68"), 102: D("0.27"), 103: D("5.00"), 104: D("-0.18"), 105: D("-0.07")}
    assert check.non_trade_total == D("9999.50")  # 10000 deposit, 0.50 platform fee
    assert check.balance == D("10006.20")

    manual, partial, tp, overnight, still_open = (check.positions[p] for p in (101, 102, 103, 104, 105))
    assert pnl.close_reason(manual.last_exit, None) is CloseReason.MANUAL_EXTERNAL  # type: ignore[arg-type]
    assert partial.close_price_vwap == D("4160.2750")  # (4165.55 + 4155.00) / 2
    assert partial.last_exit is not None and partial.last_exit.reason is DealReason.SL  # noqa: PT018
    assert pnl.close_reason(tp.last_exit, None) is CloseReason.TP  # type: ignore[arg-type]
    assert (overnight.gross, overnight.swap) == (D("1.05"), D("-1.23"))
    assert not still_open.fully_closed(still_open.volume_in)
    assert still_open.commission == D("-0.07")  # booked at entry, already in the balance


def test_a_wrong_balance_is_detected() -> None:
    fixture = DealFixture.load(EXAMPLE)
    assert check_ledger(replace(fixture, balance=D("10006.21"))).mismatch == D("0.01")


def test_round_trip_and_format(tmp_path: Path) -> None:
    fixture = DealFixture.load(EXAMPLE)
    out = tmp_path / "f.json"
    out.write_text(fixture.to_json())
    assert DealFixture.load(out) == fixture
    assert json.loads(out.read_text())["format_version"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**json.loads(out.read_text()), "format_version": 99}))
    with pytest.raises(ValueError, match="format_version"):
        DealFixture.load(bad)


def test_only_known_fields_are_kept() -> None:
    raw = {**DealFixture.load(EXAMPLE).deals[1], "external_id": "LP-123456", "login": 12345678}
    kept = keep_fields(raw)
    assert "external_id" not in kept
    assert "login" not in kept
    assert kept["ticket"] == 2


def test_fixtures_hold_no_account_identity() -> None:
    for path in ALL:
        data = json.loads(path.read_text())
        assert not {"login", "name", "password"} & set(data), path.name
        assert all(not {"login", "external_id"} & set(d) for d in data["deals"]), path.name
