"""Classification of trade-server return codes (docs/03 §12)."""

from __future__ import annotations

from enum import StrEnum


class RetcodeClass(StrEnum):
    FILLED = "FILLED"  # DONE / DONE_PARTIAL / PLACED
    RETRYABLE = "RETRYABLE"  # the price moved: re-price and resend (bounded)
    PAUSE = "PAUSE"  # rejected for a reason that will repeat: stop new entries and alert
    REJECTED = "REJECTED"  # definitive rejection of this request


FILLED_CODES = {10008, 10009, 10010}  # PLACED, DONE, DONE_PARTIAL
RETRYABLE_CODES = {10004, 10020, 10021}  # REQUOTE, PRICE_CHANGED, PRICE_OFF
PAUSE_CODES = {
    10017,  # TRADE_DISABLED
    10018,  # MARKET_CLOSED
    10019,  # NO_MONEY
    10026,  # SERVER_DISABLES_AT
    10027,  # CLIENT_DISABLES_AT (AutoTrading button off)
    10031,  # CONNECTION
    10032,  # ONLY_REAL
}


def classify(retcode: int) -> RetcodeClass:
    if retcode in FILLED_CODES:
        return RetcodeClass.FILLED
    if retcode in RETRYABLE_CODES:
        return RetcodeClass.RETRYABLE
    if retcode in PAUSE_CODES:
        return RetcodeClass.PAUSE
    return RetcodeClass.REJECTED
