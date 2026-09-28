"""A scriptable stand-in for the MetaTrader5 module (which only exists on Windows).

Shapes follow the real package: namedtuple-like objects with attribute access, rates as rows with item
access, ``last_error()`` as ``(code, message)``. Every call records the calling thread so tests can
assert that the gateway only touches the module from its worker thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace as NS
from typing import Any

RES_S_OK = (1, "Success")
IPC_FAIL = (-10004, "No IPC connection")


def make_account(**over: Any) -> NS:
    base = dict(
        login=12345678,
        server="Broker-Demo",
        trade_mode=0,
        margin_mode=2,
        currency="USD",
        leverage=100,
        balance=10000.0,
        equity=10050.25,
        margin=120.5,
        margin_free=9929.75,
        trade_allowed=True,
        trade_expert=True,
    )
    return NS(**{**base, **over})


def make_symbol(name: str = "XAUUSDm", **over: Any) -> NS:
    base = dict(
        name=name,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        trade_contract_size=100.0,
        volume_min=0.01,
        volume_max=200.0,
        volume_step=0.01,
        trade_stops_level=0,
        trade_freeze_level=0,
        filling_mode=3,
        currency_profit="USD",
        currency_margin="USD",
        trade_mode=4,
    )
    return NS(**{**base, **over})


@dataclass
class FakeMT5:
    account: NS | None = field(default_factory=make_account)
    terminal: NS | None = field(default_factory=lambda: NS(connected=True, trade_allowed=True))
    symbols: dict[str, NS] = field(default_factory=lambda: {"XAUUSDm": make_symbol()})
    ticks: dict[str, NS] = field(default_factory=dict)
    rates: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    positions: list[NS] = field(default_factory=list)
    deals: list[NS] = field(default_factory=list)
    error: tuple[int, str] = RES_S_OK
    initialize_failures: int = 0
    hang: dict[str, float] = field(default_factory=dict)
    none_for: set[str] = field(default_factory=set)
    order_send_result: NS | None = field(
        default_factory=lambda: NS(
            retcode=10009, order=111, deal=222, volume=0.1, price=2350.12, comment="Request executed"
        )
    )
    order_check_result: NS | None = field(default_factory=lambda: NS(retcode=0, comment="Done"))
    calls: list[tuple[str, str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    initialized: bool = False
    tick_advance_ms: int = 0
    """Simulate a live feed: every symbol_info_tick call moves that symbol's tick forward by this much."""

    # ---------------------------------------------------------------- helpers
    def _enter(self, name: str, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((name, threading.current_thread().name, args, kwargs))
        if name in self.hang:
            time.sleep(self.hang[name])
        return name in self.none_for

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # ---------------------------------------------------------------- API surface
    def initialize(self, **kwargs: Any) -> bool:
        if self._enter("initialize", **kwargs):
            return False
        if self.initialize_failures > 0:
            self.initialize_failures -= 1
            self.error = IPC_FAIL
            return False
        self.error = RES_S_OK
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self._enter("shutdown")
        self.initialized = False

    def last_error(self) -> tuple[int, str]:
        return self.error

    def terminal_info(self) -> NS | None:
        return None if self._enter("terminal_info") else self.terminal

    def account_info(self) -> NS | None:
        return None if self._enter("account_info") else self.account

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return not self._enter("symbol_select", symbol) and symbol in self.symbols

    def symbol_info(self, symbol: str) -> NS | None:
        return None if self._enter("symbol_info", symbol) else self.symbols.get(symbol)

    def symbol_info_tick(self, symbol: str) -> NS | None:
        if self._enter("symbol_info_tick", symbol):
            return None
        tick = self.ticks.get(symbol)
        if tick is not None and self.tick_advance_ms:
            tick.time_msc += self.tick_advance_ms
            tick.time = tick.time_msc // 1000
        return tick

    def copy_rates_from_pos(
        self, symbol: str, tf: int, start: int, count: int
    ) -> list[dict[str, Any]] | None:
        if self._enter("copy_rates_from_pos", symbol, tf, start, count):
            return None
        rows = self.rates.get((symbol, tf), [])
        # position 0 = newest (forming) bar; return `count` bars ending `start` bars back, oldest first
        end = len(rows) - start
        return rows[max(0, end - count) : max(0, end)]

    def copy_rates_range(
        self, symbol: str, tf: int, date_from: int, date_to: int
    ) -> list[dict[str, Any]] | None:
        if self._enter("copy_rates_range", symbol, tf, date_from, date_to):
            return None
        return [r for r in self.rates.get((symbol, tf), []) if date_from <= r["time"] <= date_to]

    def positions_get(self, symbol: str | None = None, **kw: Any) -> tuple[NS, ...] | None:
        if self._enter("positions_get", symbol=symbol, **kw):
            return None
        return tuple(p for p in self.positions if symbol is None or p.symbol == symbol)

    def history_deals_get(self, *args: Any, **kwargs: Any) -> tuple[NS, ...] | None:
        if self._enter("history_deals_get", *args, **kwargs):
            return None
        if "position" in kwargs:
            return tuple(d for d in self.deals if d.position_id == kwargs["position"])
        start, end = args
        return tuple(d for d in self.deals if start <= d.time <= end)

    def order_calc_profit(
        self, otype: int, symbol: str, volume: float, p_open: float, p_close: float
    ) -> float | None:
        if self._enter("order_calc_profit", otype, symbol, volume, p_open, p_close):
            return None
        direction = 1 if otype == 0 else -1
        return round((p_close - p_open) * direction * volume * 100.0, 2)

    def order_calc_margin(self, otype: int, symbol: str, volume: float, price: float) -> float | None:
        if self._enter("order_calc_margin", otype, symbol, volume, price):
            return None
        return round(volume * 100.0 * price / 100.0, 2)

    def order_check(self, request: dict[str, Any]) -> NS | None:
        return None if self._enter("order_check", request) else self.order_check_result

    def order_send(self, request: dict[str, Any]) -> NS | None:
        return None if self._enter("order_send", request) else self.order_send_result
