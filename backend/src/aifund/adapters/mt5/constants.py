"""MetaTrader 5 numeric constants and their domain equivalents.

Values are the documented MQL5 enum values (https://www.mql5.com/en/docs/constants). They are defined here
rather than read from the ``MetaTrader5`` module because (a) some constants — e.g. the SYMBOL_FILLING_*
flags — are missing from the Python package, and (b) the mapping code must run on macOS, where the
package cannot be installed. ``tests/unit/adapters/mt5/test_constants.py`` cross-checks them against the
real module when it is available (Windows CI).
"""

from __future__ import annotations

from typing import Final

from aifund.domain.enums import (
    AccountTradeMode,
    DealEntry,
    DealReason,
    MarginMode,
    Side,
    Timeframe,
)
from aifund.domain.market import FillingMode

# ENUM_TIMEFRAMES
TIMEFRAMES: Final[dict[Timeframe, int]] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 16385,
    Timeframe.H4: 16388,
    Timeframe.D1: 16408,
}

# ENUM_ORDER_TYPE (market orders only) / ENUM_POSITION_TYPE / ENUM_DEAL_TYPE share 0=BUY, 1=SELL
ORDER_TYPE_BUY: Final = 0
ORDER_TYPE_SELL: Final = 1
SIDE_TO_ORDER_TYPE: Final[dict[Side, int]] = {Side.BUY: ORDER_TYPE_BUY, Side.SELL: ORDER_TYPE_SELL}
TYPE_TO_SIDE: Final[dict[int, Side]] = {0: Side.BUY, 1: Side.SELL}

# ENUM_TRADE_REQUEST_ACTIONS
TRADE_ACTION_DEAL: Final = 1
TRADE_ACTION_SLTP: Final = 6

# ENUM_ORDER_TYPE_FILLING (value placed in a request)
ORDER_FILLING: Final[dict[FillingMode, int]] = {FillingMode.FOK: 0, FillingMode.IOC: 1, FillingMode.RETURN: 2}
# SYMBOL_FILLING_* flags (bitmask reported by symbol_info().filling_mode) — absent from the Python package
SYMBOL_FILLING_FOK: Final = 1
SYMBOL_FILLING_IOC: Final = 2

# ENUM_ORDER_TYPE_TIME
ORDER_TIME_GTC: Final = 0

# ENUM_DEAL_ENTRY
DEAL_ENTRY: Final[dict[int, DealEntry]] = {
    0: DealEntry.IN,
    1: DealEntry.OUT,
    2: DealEntry.INOUT,
    3: DealEntry.OUT_BY,
}

# ENUM_DEAL_REASON
DEAL_REASON: Final[dict[int, DealReason]] = {
    0: DealReason.CLIENT,
    1: DealReason.MOBILE,
    2: DealReason.WEB,
    3: DealReason.EXPERT,
    4: DealReason.SL,
    5: DealReason.TP,
    6: DealReason.SO,
    7: DealReason.ROLLOVER,
    8: DealReason.VMARGIN,
    9: DealReason.SPLIT,
}

# ENUM_ACCOUNT_TRADE_MODE / ENUM_ACCOUNT_MARGIN_MODE
ACCOUNT_TRADE_MODE: Final[dict[int, AccountTradeMode]] = {
    0: AccountTradeMode.DEMO,
    1: AccountTradeMode.CONTEST,
    2: AccountTradeMode.REAL,
}
MARGIN_MODE: Final[dict[int, MarginMode]] = {
    0: MarginMode.NETTING,
    1: MarginMode.EXCHANGE,
    2: MarginMode.HEDGING,
}

# ENUM_SYMBOL_TRADE_MODE
SYMBOL_TRADE_MODE_DISABLED: Final = 0
SYMBOL_TRADE_MODE_FULL: Final = 4

# Trade server return codes (order_send). order_check reports success as 0.
ORDER_CHECK_OK: Final = 0
RETCODES: Final[dict[int, str]] = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
    10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS",
    10034: "LIMIT_VOLUME",
    10035: "INVALID_ORDER",
    10036: "POSITION_CLOSED",
    10038: "INVALID_CLOSE_VOLUME",
    10039: "CLOSE_ORDER_EXIST",
    10040: "LIMIT_POSITIONS",
    10041: "REJECT_CANCEL",
    10042: "LONG_ONLY",
    10043: "SHORT_ONLY",
    10044: "CLOSE_ONLY",
    10045: "FIFO_CLOSE",
    10046: "HEDGE_PROHIBITED",
}


def retcode_name(code: int) -> str:
    if code == ORDER_CHECK_OK:
        return "OK"
    return RETCODES.get(code, f"UNKNOWN_{code}")


# (constant name in the MetaTrader5 package, value) — cross-checked on Windows CI.
PACKAGE_CROSSCHECK: Final[list[tuple[str, int]]] = [
    ("TIMEFRAME_M1", 1),
    ("TIMEFRAME_M15", 15),
    ("TIMEFRAME_H1", 16385),
    ("TIMEFRAME_H4", 16388),
    ("TIMEFRAME_D1", 16408),
    ("ORDER_TYPE_BUY", 0),
    ("ORDER_TYPE_SELL", 1),
    ("TRADE_ACTION_DEAL", 1),
    ("TRADE_ACTION_SLTP", 6),
    ("ORDER_FILLING_FOK", 0),
    ("ORDER_FILLING_IOC", 1),
    ("ORDER_FILLING_RETURN", 2),
    ("ORDER_TIME_GTC", 0),
    ("DEAL_ENTRY_IN", 0),
    ("DEAL_ENTRY_OUT", 1),
    ("DEAL_ENTRY_INOUT", 2),
    ("DEAL_ENTRY_OUT_BY", 3),
    ("DEAL_REASON_SL", 4),
    ("DEAL_REASON_TP", 5),
    ("DEAL_REASON_SO", 6),
    ("ACCOUNT_TRADE_MODE_DEMO", 0),
    ("ACCOUNT_TRADE_MODE_REAL", 2),
    ("ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2),
    ("TRADE_RETCODE_DONE", 10009),
    ("TRADE_RETCODE_REQUOTE", 10004),
    ("TRADE_RETCODE_CLIENT_DISABLES_AT", 10027),
    ("TRADE_RETCODE_INVALID_FILL", 10030),
    ("SYMBOL_TRADE_MODE_FULL", 4),
]
