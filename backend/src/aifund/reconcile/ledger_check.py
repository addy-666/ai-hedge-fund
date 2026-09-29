"""Ledger verification (roadmap 5.7c, used by 3.6 and 9.3): the database's trades against MT5's deal history.

The reconciler builds trades from what it SEES; this checks what it may have missed. For every position of
the engine magic in the broker's deal history (a position is the engine's when any of its deals carries the
magic; only positions whose entry falls inside the window count, so a truncated history raises nothing):

- UNRECORDED   the broker has it, the database does not — typically a position opened AND closed while the
               engine was down (``positions_get`` never showed it);
- NOT_AT_BROKER  a database trade (not an orphan) whose position has no deal at the broker in the window;
- STATUS       open/closed disagree (the deals cover the whole volume, the trade is still OPEN, or back);
- VOLUME       the opened volume differs;
- NET          the net P&L differs by more than a cent (every deal counts: commission, swap, fees, partials).

Pure: facts in, differences out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from aifund.domain.enums import DealEntry, TradeStatus
from aifund.domain.market import Deal
from aifund.reconcile import pnl

CENT = Decimal("0.01")
CLOSED = {TradeStatus.CLOSED, TradeStatus.ORPHAN_CLOSED}


class DiffKind(StrEnum):
    UNRECORDED = "UNRECORDED"
    NOT_AT_BROKER = "NOT_AT_BROKER"
    STATUS = "STATUS"
    VOLUME = "VOLUME"
    NET = "NET"


@dataclass(frozen=True)
class TradeFacts:
    position_id: int
    symbol: str
    status: TradeStatus
    open_time: datetime
    volume_opened: Decimal
    net_pnl: Decimal | None


@dataclass(frozen=True)
class Diff:
    kind: DiffKind
    position_id: int
    symbol: str
    detail: str

    def render(self) -> str:
        return f"{self.kind.value:<14} #{self.position_id} {self.symbol}: {self.detail}"


def diff_ledger(
    trades: Sequence[TradeFacts], deals: Sequence[Deal], *, magic: int, since: datetime
) -> list[Diff]:
    by_position: dict[int, list[Deal]] = {}
    for d in deals:
        by_position.setdefault(d.position_id, []).append(d)
    ours = {pid: ds for pid, ds in by_position.items() if any(d.magic == magic for d in ds)}
    recorded = {t.position_id: t for t in trades}
    out: list[Diff] = []
    for pid, ds in sorted(ours.items()):
        entry = [d for d in ds if d.entry is DealEntry.IN]
        if not entry or min(d.time for d in entry) < since:
            continue  # opened before the window (or its entry was cut off): not judged here
        s = pnl.aggregate(ds)
        closed = s.last_exit is not None and s.volume_out >= s.volume_in
        trade = recorded.get(pid)
        symbol = ds[0].symbol
        if trade is None:
            state = f"closed {s.close_time:%Y-%m-%d %H:%M} UTC, net {s.net}" if closed else "still open"
            out.append(
                Diff(
                    DiffKind.UNRECORDED,
                    pid,
                    symbol,
                    f"opened {min(d.time for d in entry):%Y-%m-%d %H:%M} UTC, {state}",
                )
            )
            continue
        if closed != (trade.status in CLOSED):
            out.append(
                Diff(
                    DiffKind.STATUS,
                    pid,
                    symbol,
                    f"broker {'closed' if closed else 'open'}, database {trade.status.value}",
                )
            )
        if trade.volume_opened != s.volume_in:
            out.append(
                Diff(DiffKind.VOLUME, pid, symbol, f"broker {s.volume_in}, database {trade.volume_opened}")
            )
        if closed and trade.net_pnl is not None and abs(trade.net_pnl - s.net) > CENT:
            out.append(Diff(DiffKind.NET, pid, symbol, f"broker {s.net}, database {trade.net_pnl}"))
    for t in trades:
        orphan = t.status in (TradeStatus.ORPHAN_OPEN, TradeStatus.ORPHAN_CLOSED)
        if t.position_id not in ours and t.open_time >= since and not orphan:
            out.append(
                Diff(
                    DiffKind.NOT_AT_BROKER,
                    t.position_id,
                    t.symbol,
                    f"database {t.status.value}, no deal at the broker",
                )
            )
    return out
