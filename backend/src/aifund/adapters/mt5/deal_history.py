"""Recorded MT5 deal histories (roadmap 3.2): fixture format and the balance check that validates P&L.

MT5 books every deal's profit, commission, swap and fee straight into the account balance, and deposits,
withdrawals and broker-charged fees are deals too (types BALANCE, CREDIT, COMMISSION, ...). So over an
account's WHOLE history:

    balance == Σ over trade deals, grouped by position, of pnl.aggregate(...).net
             + Σ over non-trade deals of (profit + commission + swap + fee)

exactly, to the cent. Open positions do not break this: their floating P&L and accrued swap are not deals
and not in the balance, while the entry commission is in both. A fixture that satisfies it proves the
production mapping (``deal_from_mt5``) and the P&L aggregation agree with the broker's own ledger.

Fixture files hold the raw deal fields exactly as the terminal reported them (JSON numbers round-trip an
MT5 double exactly) and no account login or owner name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from aifund.adapters.mt5.mapping import deal_from_mt5, dec
from aifund.domain.market import Deal
from aifund.reconcile import pnl

FORMAT_VERSION = 1
# external_id is not kept: it can identify the account at the broker's liquidity provider
DEAL_FIELDS = (
    "ticket", "order", "time", "time_msc", "type", "entry", "magic", "position_id", "reason", "volume",
    "price", "commission", "swap", "profit", "fee", "symbol", "comment",
)  # fmt: skip


@dataclass(frozen=True)
class DealFixture:
    server: str
    currency: str
    trade_mode: str
    server_offset_minutes: int | None
    balance: Decimal
    deals: list[dict[str, Any]]
    note: str = ""

    @property
    def offset(self) -> timedelta:
        return timedelta(minutes=self.server_offset_minutes or 0)

    def to_json(self) -> str:
        """Readable, diff-friendly JSON: header fields one per line, then one deal per line."""
        header = {
            "format_version": FORMAT_VERSION,
            "server": self.server,
            "currency": self.currency,
            "trade_mode": self.trade_mode,
            "server_offset_minutes": self.server_offset_minutes,
            "balance": str(self.balance),
            "note": self.note,
        }
        head = ",\n".join(f" {json.dumps(k)}: {json.dumps(v)}" for k, v in header.items())
        deals = ",\n".join(f"  {json.dumps(d)}" for d in self.deals)
        return "{\n" + head + ',\n "deals": [\n' + deals + "\n ]\n}"

    @classmethod
    def load(cls, path: Path) -> DealFixture:
        data = json.loads(path.read_text())
        if data.get("format_version") != FORMAT_VERSION:
            raise ValueError(f"{path.name}: unsupported format_version {data.get('format_version')}")
        return cls(
            server=data["server"],
            currency=data["currency"],
            trade_mode=data["trade_mode"],
            server_offset_minutes=data["server_offset_minutes"],
            balance=Decimal(data["balance"]),
            deals=data["deals"],
            note=data.get("note", ""),
        )


def keep_fields(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in DEAL_FIELDS if k in raw}


@dataclass(frozen=True)
class LedgerCheck:
    balance: Decimal
    positions: dict[int, pnl.PositionPnl]
    trade_deals: dict[int, list[Deal]]
    non_trade_total: Decimal
    non_trade_count: int

    @property
    def trades_net(self) -> Decimal:
        return sum((p.net for p in self.positions.values()), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.trades_net + self.non_trade_total

    @property
    def mismatch(self) -> Decimal:
        """Balance minus the ledger rebuilt from deals: must be exactly zero."""
        return self.balance - self.total


def check_ledger(fixture: DealFixture) -> LedgerCheck:
    by_position: dict[int, list[Deal]] = {}
    non_trade = Decimal(0)
    count = 0
    for raw in fixture.deals:
        deal = deal_from_mt5(SimpleNamespace(**raw), fixture.offset)
        if deal is None:  # balance / credit / commission / bonus deal: straight into the balance
            non_trade += (
                dec(raw["profit"]) + dec(raw["commission"]) + dec(raw["swap"]) + dec(raw.get("fee", 0.0))
            )
            count += 1
            continue
        by_position.setdefault(deal.position_id, []).append(deal)
    return LedgerCheck(
        balance=fixture.balance,
        positions={pid: pnl.aggregate(deals) for pid, deals in by_position.items()},
        trade_deals=by_position,
        non_trade_total=non_trade,
        non_trade_count=count,
    )
