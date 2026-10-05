"""MT5 gateway — the only code that talks to the MetaTrader 5 terminal (docs/03 §1).

The ``MetaTrader5`` package is not thread-safe and holds one IPC connection per process, so every call
runs on a single dedicated worker thread; asyncio callers await it with a timeout. A timed-out call
cannot be interrupted — the thread finishes it in the background and later calls queue behind it — so
timeouts are surfaced as errors and, for ``order_send``, as an UNKNOWN outcome (``None``) that the
caller must resolve against positions/deals instead of resending.

All timestamps leaving this module are UTC; broker server time is converted here and only here.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from aifund.adapters.mt5 import constants as c
from aifund.adapters.mt5 import mapping as m
from aifund.adapters.mt5.server_time import OffsetUnavailable, estimate_offset, utc_to_server_epoch
from aifund.domain.enums import AccountTradeMode, MarginMode, Mode, Side, Timeframe
from aifund.domain.market import (
    AccountInfo,
    Bar,
    Deal,
    OrderRequest,
    OrderResult,
    Position,
    SymbolSpec,
    Tick,
)
from aifund.ports.broker import BrokerError, BrokerUnavailable
from aifund.ports.system import ClockPort

T = TypeVar("T")
RES_S_OK = 1  # MetaTrader5.last_error() success code


class GatewayError(BrokerError):
    """The terminal returned an error or an unusable result."""


class GatewayTimeout(GatewayError):
    """A terminal call did not complete within its timeout."""


class GatewayDisconnected(GatewayError, BrokerUnavailable):
    """The terminal is not connected to the broker (or the IPC link is down)."""


class HistoryNotSynced(GatewayError):
    """The terminal's local bar history for a symbol/timeframe is far behind its live quote."""


class AccountMismatch(GatewayError):
    """The terminal is logged into a different account/server than configured. Never trade it."""


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"


@dataclass(frozen=True)
class MT5Credentials:
    login: int
    password: str = field(repr=False)
    server: str
    path: Path | None = None
    portable: bool = False


@dataclass
class StartupReport:
    """Result of startup checks. ``ok`` is False if any problem would make trading unsafe."""

    account: AccountInfo | None = None
    specs: dict[str, SymbolSpec] = field(default_factory=dict)
    server_offset: timedelta | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _load_metatrader5() -> Any:
    return importlib.import_module("MetaTrader5")


class MT5Gateway:
    def __init__(
        self,
        credentials: MT5Credentials,
        *,
        clock: ClockPort,
        module_loader: Callable[[], Any] = _load_metatrader5,
        call_timeout_s: float = 10.0,
        order_timeout_s: float = 15.0,
        init_timeout_ms: int = 60_000,
        max_backoff_s: float = 60.0,
        history_sync_attempts: int = 4,
    ) -> None:
        self._creds = credentials
        self._clock = clock
        self._loader = module_loader
        self._mt5: Any = None  # only ever touched on the worker thread
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-gateway")
        self._call_timeout = call_timeout_s
        self._order_timeout = order_timeout_s
        self._init_timeout_ms = init_timeout_ms
        self._max_backoff = max_backoff_s
        self._history_sync_attempts = max(1, history_sync_attempts)
        self._offset: timedelta | None = None
        self._specs: dict[str, SymbolSpec] = {}
        self.state = ConnectionState.DISCONNECTED

    # ------------------------------------------------------------------ worker-thread plumbing

    def _invoke(self, fn: Callable[[Any], T]) -> T:
        if self._mt5 is None:
            self._mt5 = self._loader()
        return fn(self._mt5)

    async def _run(self, fn: Callable[[Any], T], timeout: float | None = None) -> T:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, self._invoke, fn)
        try:
            return await asyncio.wait_for(future, timeout or self._call_timeout)
        except TimeoutError as exc:
            raise GatewayTimeout(f"MT5 call exceeded {timeout or self._call_timeout}s") from exc

    @staticmethod
    def _error(mt5: Any) -> tuple[int, str]:
        err = mt5.last_error()
        return (int(err[0]), str(err[1])) if err else (0, "")

    def _fail(self, mt5: Any, what: str) -> GatewayError:
        """Build the right exception for a None result, updating connection state."""
        code, msg = self._error(mt5)
        term = mt5.terminal_info()
        if term is None or not bool(term.connected):
            self.state = ConnectionState.DISCONNECTED
            return GatewayDisconnected(f"{what}: terminal disconnected ({code} {msg})")
        return GatewayError(f"{what} failed ({code} {msg})")

    async def close(self) -> None:
        with contextlib.suppress(GatewayError):
            await self._run(lambda mt5: mt5.shutdown(), timeout=5)
        self.state = ConnectionState.DISCONNECTED
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ connection lifecycle

    async def connect(self) -> AccountInfo:
        creds = self._creds

        def _init(mt5: Any) -> tuple[bool, Any]:
            kwargs: dict[str, Any] = {
                "login": creds.login,
                "password": creds.password,
                "server": creds.server,
                "timeout": self._init_timeout_ms,
                "portable": creds.portable,
            }
            if creds.path is not None:
                kwargs["path"] = str(creds.path)
            if not mt5.initialize(**kwargs):
                return False, self._error(mt5)
            return True, mt5.account_info()

        ok, payload = await self._run(_init, timeout=self._init_timeout_ms / 1000 + 5)
        if not ok:
            self.state = ConnectionState.DISCONNECTED
            raise GatewayDisconnected(f"initialize failed: {payload}")
        if payload is None:
            self.state = ConnectionState.DISCONNECTED
            raise GatewayDisconnected("initialize succeeded but account_info() is unavailable")
        account = m.account_from_mt5(payload)
        if account.login != creds.login or account.server.casefold() != creds.server.casefold():
            await self._run(lambda mt5: mt5.shutdown())
            self.state = ConnectionState.DISCONNECTED
            raise AccountMismatch(
                f"terminal is on account {account.login}@{account.server}, "
                f"configured {creds.login}@{creds.server}"
            )
        self.state = ConnectionState.CONNECTED
        return account

    async def reconnect(self, *, max_attempts: int) -> bool:
        """shutdown + initialize with exponential backoff (1, 2, 4 … max_backoff s)."""
        delay = 1.0
        for attempt in range(1, max_attempts + 1):
            with contextlib.suppress(GatewayError):
                await self._run(lambda mt5: mt5.shutdown(), timeout=5)
            try:
                await self.connect()
                return True
            except AccountMismatch:
                raise
            except GatewayError:
                if attempt == max_attempts:
                    break
                await self._clock.sleep(delay)
                delay = min(delay * 2, self._max_backoff)
        return False

    async def is_connected(self) -> bool:
        try:
            term = await self._run(lambda mt5: mt5.terminal_info())
        except GatewayError:
            term = None
        connected = term is not None and bool(term.connected)
        self.state = ConnectionState.CONNECTED if connected else ConnectionState.DISCONNECTED
        return connected

    # ------------------------------------------------------------------ server time

    @property
    def server_offset(self) -> timedelta:
        if self._offset is None:
            raise GatewayError(
                "server UTC offset unknown: call refresh_server_offset() or set_server_offset()"
            )
        return self._offset

    def set_server_offset(self, offset: timedelta) -> None:
        """Restore a persisted offset (e.g. at a weekend start, when no tick is fresh)."""
        self._offset = offset

    async def refresh_server_offset(
        self, symbols: list[str], *, sample_interval_s: float = 5.0
    ) -> tuple[timedelta, bool]:
        """Re-estimate the offset from a LIVE tick. Returns (offset, changed).

        A tick's age cannot be read from its timestamp (that is what we are solving for), and a stale
        tick whose age is close to a multiple of 15 minutes would silently yield a wrong offset. So the
        ticks are sampled twice; only a symbol whose tick time advanced between the samples is used —
        its tick is at most ``sample_interval_s`` + one tick gap old. No advancing symbol (closed market)
        → OffsetUnavailable, and the previous/persisted offset stays in force.
        """

        def _tick_times(mt5: Any) -> dict[str, int]:
            out: dict[str, int] = {}
            for sym in symbols:
                t = mt5.symbol_info_tick(sym)
                if t is not None:
                    out[sym] = int(getattr(t, "time_msc", 0) or int(t.time) * 1000)
            return out

        first = await self._run(_tick_times)
        await self._clock.sleep(sample_interval_s)
        second = await self._run(_tick_times)
        live = [msc for sym, msc in second.items() if sym in first and msc > first[sym]]
        if not live:
            raise OffsetUnavailable("no symbol ticked during sampling (market closed or feed stalled)")
        offset = estimate_offset(max(live) // 1000, self._clock.now())
        changed = self._offset is not None and offset != self._offset
        self._offset = offset
        return offset, changed

    # ------------------------------------------------------------------ startup checks

    async def startup_checks(
        self, symbols: list[str], *, mode: Mode, require_hedging: bool, references: Sequence[str] = ()
    ) -> StartupReport:
        """``references``: data-only instruments (cross-asset features, roadmap 10.1). One the terminal cannot
        select is a warning, not a problem: its features are null, nothing is traded on it."""
        report = StartupReport()
        term = await self._run(lambda mt5: mt5.terminal_info())
        if term is None or not bool(term.connected):
            report.problems.append("terminal is not connected to the trade server")
        elif not bool(term.trade_allowed):
            report.problems.append("AutoTrading is disabled in the terminal (Algo Trading button)")

        account = await self.account_info()
        report.account = account
        if not account.trade_allowed:
            report.problems.append("trading is disabled for this account")
        if not account.trade_expert:
            report.problems.append("expert/algorithmic trading is disabled for this account")
        if mode is Mode.DEMO and account.trade_mode is not AccountTradeMode.DEMO:
            report.problems.append(f"mode DEMO but the account is {account.trade_mode}")
        if mode is Mode.LIVE and account.trade_mode is not AccountTradeMode.REAL:
            report.problems.append(f"mode LIVE but the account is {account.trade_mode}")
        if mode in (Mode.SIM, Mode.PAPER) and account.trade_mode is AccountTradeMode.REAL:
            report.warnings.append("connected to a REAL account in a non-trading mode")
        if require_hedging and account.margin_mode is not MarginMode.HEDGING:
            report.problems.append(f"hedging account required, account is {account.margin_mode}")

        for sym in symbols:
            try:
                report.specs[sym] = await self.refresh_symbol_spec(sym)
            except (GatewayError, m.MappingError) as exc:
                report.problems.append(f"symbol {sym}: {exc}")
                continue
            if not report.specs[sym].trade_allowed:
                report.warnings.append(f"symbol {sym}: not open for new entries (trade mode)")
        for ref in references:
            try:
                await self.refresh_symbol_spec(ref)
            except (GatewayError, m.MappingError) as exc:
                report.warnings.append(f"reference {ref}: {exc} (its cross-asset features stay null)")

        try:
            report.server_offset, _ = await self.refresh_server_offset(symbols)
        except OffsetUnavailable as exc:
            if self._offset is None:
                report.warnings.append(f"server UTC offset unknown ({exc}); restore a persisted offset")
            else:
                report.server_offset = self._offset
        return report

    # ------------------------------------------------------------------ market data (MarketDataPort)

    async def refresh_symbol_spec(self, symbol: str) -> SymbolSpec:
        def _spec(mt5: Any) -> Any:
            if not mt5.symbol_select(symbol, True):
                raise self._fail(mt5, f"symbol_select({symbol})")
            info = mt5.symbol_info(symbol)
            if info is None:
                raise self._fail(mt5, f"symbol_info({symbol})")
            return info

        spec = m.symbol_spec_from_mt5(await self._run(_spec))
        self._specs[symbol] = spec
        return spec

    async def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._specs.get(symbol) or await self.refresh_symbol_spec(symbol)

    async def tick(self, symbol: str) -> Tick | None:
        spec = await self.symbol_spec(symbol)
        raw = await self._run(lambda mt5: mt5.symbol_info_tick(symbol))
        if raw is None or float(raw.bid) <= 0 or float(raw.ask) <= 0:
            return None
        return m.tick_from_mt5(raw, symbol, spec.digits, self.server_offset)

    async def closed_bars(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        """Last ``count`` closed bars. Position 0 is the forming bar, so reading starts at position 1."""
        if count <= 0:
            return []
        spec = await self.symbol_spec(symbol)
        tf = c.TIMEFRAMES[timeframe]

        def _rates(mt5: Any) -> Any:
            rates = mt5.copy_rates_from_pos(symbol, tf, 1, count)
            if rates is None:
                raise self._fail(mt5, f"copy_rates_from_pos({symbol}, {timeframe})")
            return rates

        delay = 1.0
        for attempt in range(1, self._history_sync_attempts + 1):
            bars = m.bars_from_mt5(
                await self._run(_rates), symbol, timeframe, spec.digits, self.server_offset
            )
            lag = await self._history_lag(symbol, timeframe, bars)
            if lag is None:
                return bars
            if attempt < self._history_sync_attempts:
                await self._clock.sleep(delay)  # asking for the bars makes the terminal download them
                delay *= 2
        raise HistoryNotSynced(
            f"{symbol} {timeframe}: last closed bar is {lag} behind the live quote — the terminal's local "
            f"history is not synchronised. Open a {symbol} {timeframe} chart in MT5 (or wait) and retry."
        )

    async def _history_lag(self, symbol: str, timeframe: Timeframe, bars: list[Bar]) -> timedelta | None:
        """How far the last closed bar trails a LIVE quote, if that is implausibly far; else None.

        The threshold (max(4 days, 3 bars)) is deliberately wide: weekends, holidays and daily breaks
        never trip it. It only catches a stale local cache (seen live: EURUSD M15 returning 2024 bars
        on the first request). Finer staleness is the feature builder's job (STALE_DATA).
        """
        if not bars:
            return None
        tick = await self.tick(symbol)
        if tick is None or self._clock.now() - tick.time > timedelta(minutes=10):
            return None  # market closed or quiet: nothing to compare against
        lag = tick.time - (bars[-1].time + timedelta(minutes=timeframe.minutes))
        threshold = max(timedelta(days=4), timedelta(minutes=3 * timeframe.minutes))
        return lag if lag > threshold else None

    async def common_files_dir(self) -> Path | None:
        """``<commondata_path>/Files/aifund``: where the Guardian EA and the engine exchange files."""
        term = await self._run(lambda mt5: mt5.terminal_info())
        root = getattr(term, "commondata_path", None) if term is not None else None
        return Path(str(root)) / "Files" / "aifund" if root else None

    async def max_bars(self) -> int | None:
        """The terminal's "Max bars in chart" (``terminal_info().maxbars``): every copy_rates_* call returns
        at most this many bars per symbol and timeframe, whatever range is asked for."""
        term = await self._run(lambda mt5: mt5.terminal_info())
        value = getattr(term, "maxbars", None) if term is not None else None
        return int(value) if isinstance(value, int) and value > 0 else None

    async def bars_range(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        """Bars with open time in [start, end] (UTC). Used by the history exporter."""
        spec = await self.symbol_spec(symbol)
        tf = c.TIMEFRAMES[timeframe]
        s_epoch = utc_to_server_epoch(start, self.server_offset)
        e_epoch = utc_to_server_epoch(end, self.server_offset)

        def _rates(mt5: Any) -> Any:
            rates = mt5.copy_rates_range(symbol, tf, s_epoch, e_epoch)
            if rates is None:
                raise self._fail(mt5, f"copy_rates_range({symbol}, {timeframe})")
            return rates

        return m.bars_from_mt5(
            await self._run(_rates, timeout=120), symbol, timeframe, spec.digits, self.server_offset
        )

    # ------------------------------------------------------------------ account & positions (BrokerPort)

    async def account_info(self) -> AccountInfo:
        def _acc(mt5: Any) -> Any:
            info = mt5.account_info()
            if info is None:
                raise self._fail(mt5, "account_info")
            return info

        return m.account_from_mt5(await self._run(_acc))

    async def positions(self, symbol: str | None = None) -> list[Position]:
        def _pos(mt5: Any) -> Any:
            raw = mt5.positions_get(symbol=symbol) if symbol is not None else mt5.positions_get()
            if raw is None:
                code, _ = self._error(mt5)
                if code != RES_S_OK:
                    raise self._fail(mt5, "positions_get")
                return ()
            return raw

        raw_positions = await self._run(_pos)
        result = []
        for p in raw_positions:
            spec = await self.symbol_spec(str(p.symbol))
            result.append(m.position_from_mt5(p, spec.digits, self.server_offset))
        return result

    async def deals_for_position(self, position_id: int) -> list[Deal]:
        def _deals(mt5: Any) -> Any:
            raw = mt5.history_deals_get(position=position_id)
            if raw is None:
                code, _ = self._error(mt5)
                if code != RES_S_OK:
                    raise self._fail(mt5, f"history_deals_get(position={position_id})")
                return ()
            return raw

        return self._map_deals(await self._run(_deals))

    async def deals_between(self, start: datetime, end: datetime) -> list[Deal]:
        s_epoch = utc_to_server_epoch(start, self.server_offset)
        e_epoch = utc_to_server_epoch(end, self.server_offset)

        def _deals(mt5: Any) -> Any:
            raw = mt5.history_deals_get(s_epoch, e_epoch)
            if raw is None:
                code, _ = self._error(mt5)
                if code != RES_S_OK:
                    raise self._fail(mt5, "history_deals_get(range)")
                return ()
            return raw

        return self._map_deals(await self._run(_deals))

    async def raw_deals_between(self, start_server_epoch: int, end_server_epoch: int) -> list[dict[str, Any]]:
        """Every deal in a server-time window as MT5 returns it (incl. balance / credit / fee deals).

        For recording fixtures (scripts/capture_deals.py): values are left exactly as the terminal reports
        them so a test can run them through the production mapping.
        """

        def _deals(mt5: Any) -> Any:
            raw = mt5.history_deals_get(start_server_epoch, end_server_epoch)
            if raw is None:
                code, _ = self._error(mt5)
                if code != RES_S_OK:
                    raise self._fail(mt5, "history_deals_get(range)")
                return ()
            return [d._asdict() if hasattr(d, "_asdict") else dict(vars(d)) for d in raw]

        return list(await self._run(_deals))

    def _map_deals(self, raw: Any) -> list[Deal]:
        deals = [d for d in (m.deal_from_mt5(r, self.server_offset) for r in raw) if d is not None]
        deals.sort(key=lambda d: (d.time, d.ticket))
        return deals

    async def calc_profit(
        self, side: Side, symbol: str, volume: Decimal, price_open: Decimal, price_close: Decimal
    ) -> Decimal:
        otype = c.SIDE_TO_ORDER_TYPE[side]

        def _calc(mt5: Any) -> Any:
            value = mt5.order_calc_profit(otype, symbol, float(volume), float(price_open), float(price_close))
            if value is None:
                raise self._fail(mt5, f"order_calc_profit({symbol})")
            return value

        return m.dec(await self._run(_calc))

    async def calc_margin(self, side: Side, symbol: str, volume: Decimal, price: Decimal) -> Decimal:
        otype = c.SIDE_TO_ORDER_TYPE[side]

        def _calc(mt5: Any) -> Any:
            value = mt5.order_calc_margin(otype, symbol, float(volume), float(price))
            if value is None:
                raise self._fail(mt5, f"order_calc_margin({symbol})")
            return value

        return m.dec(await self._run(_calc))

    # ------------------------------------------------------------------ orders

    async def order_check(self, request: OrderRequest) -> OrderResult:
        payload = m.request_to_mt5(request)

        def _check(mt5: Any) -> Any:
            result = mt5.order_check(payload)
            if result is None:
                raise self._fail(mt5, "order_check")
            return result

        return m.order_result_from_mt5(await self._run(_check))

    async def order_send(self, request: OrderRequest) -> OrderResult | None:
        """Returns None when the outcome is UNKNOWN (no result, timeout, lost connection)."""
        payload = m.request_to_mt5(request)
        try:
            raw = await self._run(lambda mt5: mt5.order_send(payload), timeout=self._order_timeout)
        except GatewayError:
            return None
        return None if raw is None else m.order_result_from_mt5(raw)
