"""Enumerations shared across the system. Values are stable strings: they are persisted and logged."""

from __future__ import annotations

from enum import StrEnum


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return _TF_MINUTES[self]


_TF_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}


class Mode(StrEnum):
    """Trading mode (docs/01 §9). Orthogonal to EngineState."""

    SIM = "SIM"  # SimBroker + replay
    PAPER = "PAPER"  # live MT5 data, SimBroker fills
    DEMO = "DEMO"  # real orders, account must be a demo account
    LIVE = "LIVE"  # real money; requires allow_live + operator confirmation


class EngineState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    HALTED = "HALTED"
    FLATTENING = "FLATTENING"


class Side(StrEnum):
    """Order side as sent to the broker."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class Direction(StrEnum):
    """Directional view of a proposal or decision."""

    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"

    def to_side(self) -> Side:
        if self is Direction.LONG:
            return Side.BUY
        if self is Direction.SHORT:
            return Side.SELL
        raise ValueError("Direction.NONE has no order side")


class AssetClass(StrEnum):
    FX = "fx"
    METAL = "metal"
    INDEX = "index"
    CRYPTO = "crypto"
    ENERGY = "energy"


class Regime(StrEnum):
    """Market regime of one symbol on its setup timeframe (docs/03 §6, roadmap 1.6)."""

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"
    QUIET = "QUIET"
    UNKNOWN = "UNKNOWN"


class EmaStack(StrEnum):
    BULL = "BULL"  # ema20 > ema50 > ema200
    BEAR = "BEAR"  # ema20 < ema50 < ema200
    MIXED = "MIXED"


class AccountTradeMode(StrEnum):
    DEMO = "DEMO"
    CONTEST = "CONTEST"
    REAL = "REAL"


class MarginMode(StrEnum):
    NETTING = "NETTING"
    EXCHANGE = "EXCHANGE"
    HEDGING = "HEDGING"


class DealEntry(StrEnum):
    """MT5 DEAL_ENTRY_*: whether a deal opens, closes, reverses or closes-by a position."""

    IN = "IN"
    OUT = "OUT"
    INOUT = "INOUT"
    OUT_BY = "OUT_BY"

    @property
    def reduces_position(self) -> bool:
        return self in (DealEntry.OUT, DealEntry.OUT_BY, DealEntry.INOUT)


class DealReason(StrEnum):
    """MT5 DEAL_REASON_*: who or what caused a deal."""

    CLIENT = "CLIENT"
    MOBILE = "MOBILE"
    WEB = "WEB"
    EXPERT = "EXPERT"
    SL = "SL"
    TP = "TP"
    SO = "SO"
    ROLLOVER = "ROLLOVER"
    VMARGIN = "VMARGIN"
    SPLIT = "SPLIT"
    OTHER = "OTHER"


class CommandType(StrEnum):
    """Operator commands submitted by the API and executed by the engine (docs/02 `commands`)."""

    START = "START"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    REARM = "REARM"
    FLATTEN_ALL = "FLATTEN_ALL"
    CLOSE_POSITION = "CLOSE_POSITION"
    RUN_AUDIT = "RUN_AUDIT"
    APPROVE_RULE = "APPROVE_RULE"
    REJECT_RULE = "REJECT_RULE"
    RETIRE_RULE = "RETIRE_RULE"
    RELOAD_CONFIG = "RELOAD_CONFIG"
    SET_MODE = "SET_MODE"


class CommandStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class ReversalMode(StrEnum):
    IGNORE = "ignore"
    CLOSE_ONLY = "close_only"
    CLOSE_AND_REVERSE = "close_and_reverse"


class IntentKind(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    REVERSE_CLOSE = "REVERSE_CLOSE"
    MODIFY_SLTP = "MODIFY_SLTP"
    FLATTEN = "FLATTEN"


class IntentStatus(StrEnum):
    """docs/02 §2.1."""

    PENDING = "PENDING"
    CHECK_FAILED = "CHECK_FAILED"
    SENT = "SENT"
    RETRYING = "RETRYING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (IntentStatus.FILLED, IntentStatus.REJECTED, IntentStatus.CHECK_FAILED)

    @property
    def locks_symbol(self) -> bool:
        """Non-terminal intents lock their symbol for new OPEN intents."""
        return not self.is_terminal


class TradeStatus(StrEnum):
    """docs/02 §2.2."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ORPHAN_OPEN = "ORPHAN_OPEN"
    ORPHAN_CLOSED = "ORPHAN_CLOSED"


class TradeOutcome(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


class VirtualStatus(StrEnum):
    """Counterfactual trade for a blocked signal (docs/03 §14.4)."""

    PENDING = "PENDING"  # waiting for the next trigger bar's open (its entry)
    OPEN = "OPEN"
    CLOSED = "CLOSED"  # stop or target hit
    EXPIRED = "EXPIRED"  # time stop or pre-close flatten, exited at the market
    NO_ENTRY = "NO_ENTRY"  # the entry bar never arrived (market closed, no data)


class CloseReason(StrEnum):
    SL = "SL"
    TP = "TP"
    STOP_OUT = "STOP_OUT"
    ENGINE = "ENGINE"
    OPERATOR = "OPERATOR"
    MANUAL_EXTERNAL = "MANUAL_EXTERNAL"
    REVERSAL = "REVERSAL"
    TIME_STOP = "TIME_STOP"
    FLATTEN = "FLATTEN"


class RuleStatus(StrEnum):
    """docs/02 §2.3."""

    CANDIDATE = "CANDIDATE"
    REJECTED = "REJECTED"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class DecisionOutcome(StrEnum):
    """Terminal outcome of one decision-pipeline run (docs/03 §3).

    INVALID is distinct from HOLD: HOLD is a decision the analyst made; INVALID means no readable
    decision was produced (LLM error, schema failure). Keeping them apart stops failures from
    polluting HOLD statistics and confidence calibration.
    """

    SKIPPED = "SKIPPED"
    NO_SETUP = "NO_SETUP"
    HOLD = "HOLD"
    INVALID = "INVALID"
    RULE_BLOCKED = "RULE_BLOCKED"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    RISK_REJECTED = "RISK_REJECTED"
    ORDERED = "ORDERED"
    DRY_RUN = "DRY_RUN"  # the Risk Manager approved an order; dry-run mode recorded it and sent nothing
    ERROR = "ERROR"


class ReasonCode(StrEnum):
    """Why a decision stopped where it did. Never scatter string literals for these."""

    # pre-flight (docs/03 §4)
    ENGINE_NOT_RUNNING = "ENGINE_NOT_RUNNING"
    INTENT_IN_FLIGHT = "INTENT_IN_FLIGHT"
    MARKET_CLOSED = "MARKET_CLOSED"
    STALE_TICK = "STALE_TICK"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    NEWS_BLACKOUT = "NEWS_BLACKOUT"
    LOSS_LIMIT = "LOSS_LIMIT"
    COOLDOWN = "COOLDOWN"
    FLIP_FLOP = "FLIP_FLOP"
    DAILY_SYMBOL_CAP = "DAILY_SYMBOL_CAP"
    POSITION_EXISTS = "POSITION_EXISTS"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_BUDGET_EXHAUSTED = "LLM_BUDGET_EXHAUSTED"
    # data
    STALE_DATA = "STALE_DATA"
    INSUFFICIENT_BARS = "INSUFFICIENT_BARS"
    FORMING_BAR = "FORMING_BAR"  # a bar that had not closed at decision time reached the feature builder
    # analyst
    LLM_INVALID_OUTPUT = "LLM_INVALID_OUTPUT"
    LLM_ERROR = "LLM_ERROR"
    SETUP_MISMATCH = "SETUP_MISMATCH"
    ANALYST_HOLD = "ANALYST_HOLD"
    # rules / portfolio
    RULE_BLOCK = "RULE_BLOCK"
    BELOW_THRESHOLD = "BELOW_THRESHOLD"
    # guards (docs/03 §9)
    DUPLICATE_IDEMPOTENCY = "DUPLICATE_IDEMPOTENCY"
    DUPLICATE_SAME_DIRECTION = "DUPLICATE_SAME_DIRECTION"
    FOREIGN_POSITION = "FOREIGN_POSITION"
    MAX_PER_SYMBOL = "MAX_PER_SYMBOL"
    REVERSAL_WEAK = "REVERSAL_WEAK"
    REVERSAL_TOO_EARLY = "REVERSAL_TOO_EARLY"
    REVERSAL_CAP = "REVERSAL_CAP"
    # sizing / limits (docs/03 §11)
    RISK_BELOW_MIN_LOT = "RISK_BELOW_MIN_LOT"
    MARGIN = "MARGIN"
    LEVERAGE_CAP = "LEVERAGE_CAP"
    PORTFOLIO_HEAT = "PORTFOLIO_HEAT"
    BUCKET_HEAT = "BUCKET_HEAT"
    MAX_POSITIONS = "MAX_POSITIONS"
    PRICE_MOVED = "PRICE_MOVED"
    # execution
    BROKER_REJECTED = "BROKER_REJECTED"
    UNKNOWN_NOT_EXECUTED = "UNKNOWN_NOT_EXECUTED"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EventType(StrEnum):
    """``events.type`` values (the dashboard's live feed, docs/05)."""

    TRADE_OPENED = "trade.opened"
    TRADE_ORPHAN = "trade.orphan"
    TRADE_PARTIAL_CLOSE = "trade.partial_close"
    TRADE_CLOSED = "trade.closed"
    TRADE_VANISHED = "trade.vanished"
    RISK_LIMIT_BREACH = "risk.limit_breach"
