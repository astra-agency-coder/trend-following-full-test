"""Central enumerations shared across the whole system.

All values follow the Arabic specification (section 6/7/8/10/30/36/37/39).
Names are kept in English per the spec's language convention.
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum base so members serialise cleanly to JSON/DB."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# ---------------------------------------------------------------- trend / context
class TrendState(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    RANGING = "RANGING"
    TRANSITIONAL = "TRANSITIONAL"
    UNCLEAR = "UNCLEAR"


class VolatilityState(StrEnum):
    VERY_LOW = "VERY_LOW"
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class ActivityState(StrEnum):
    QUIET = "QUIET"
    NORMAL = "NORMAL"
    ACTIVE = "ACTIVE"
    VERY_ACTIVE = "VERY_ACTIVE"


class LiquidityProxyState(StrEnum):
    LOW_PROXY = "LOW_PROXY"
    NORMAL_PROXY = "NORMAL_PROXY"
    HIGH_PROXY = "HIGH_PROXY"
    DISLOCATED = "DISLOCATED"


class MarketStructureEvent(StrEnum):
    HH_HL_SEQUENCE = "HH_HL_SEQUENCE"
    LH_LL_SEQUENCE = "LH_LL_SEQUENCE"
    RANGE_STRUCTURE = "RANGE_STRUCTURE"
    BOS_CONFIRMED = "BOS_CONFIRMED"
    CHOCH_CONFIRMED = "CHOCH_CONFIRMED"
    LIQUIDITY_SWEEP_CANDIDATE = "LIQUIDITY_SWEEP_CANDIDATE"
    COMPRESSION = "COMPRESSION"
    EXPANSION = "EXPANSION"
    UNKNOWN = "UNKNOWN"


class DataQuality(StrEnum):
    VALID = "VALID"
    STALE = "STALE"
    INVALID = "INVALID"
    MISSING = "MISSING"


class SessionPhase(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    OPENING_WINDOW = "OPENING_WINDOW"
    EARLY_SESSION = "EARLY_SESSION"
    MID_SESSION = "MID_SESSION"
    LATE_SESSION = "LATE_SESSION"
    CLOSING_WINDOW = "CLOSING_WINDOW"
    CLOSED = "CLOSED"


class NewsContextState(StrEnum):
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"
    AVAILABLE = "AVAILABLE"


# ---------------------------------------------------------------- strategies
class EvaluationStatus(StrEnum):
    NO_SETUP = "NO_SETUP"
    CANDIDATE = "CANDIDATE"
    INVALID_DATA = "INVALID_DATA"
    ERROR = "ERROR"


class SetupStage(StrEnum):
    NONE = "NONE"
    WATCH = "WATCH"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NONE = "NONE"


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class StrategyModuleState(StrEnum):
    ENABLED = "ENABLED"
    DISABLED_CONFIG = "DISABLED_CONFIG"
    DISABLED_DATA_UNAVAILABLE = "DISABLED_DATA_UNAVAILABLE"
    ERROR = "ERROR"


# ---------------------------------------------------------------- router
class RouterDecisionStatus(StrEnum):
    """Distinguish *why* nothing was opened (spec 36.5)."""

    SELECTED = "SELECTED"
    TRADE_OPENED = "TRADE_OPENED"
    SELECTED_PENDING_ENTRY = "SELECTED_PENDING_ENTRY"
    NO_CANDIDATE = "NO_CANDIDATE"
    CANDIDATE_REJECTED_BY_ROUTER = "CANDIDATE_REJECTED_BY_ROUTER"
    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"
    BLOCKED_BY_DATA = "BLOCKED_BY_DATA"
    BLOCKED_BY_SYSTEM = "BLOCKED_BY_SYSTEM"
    NO_TRADE = "NO_TRADE"


class RejectionStage(StrEnum):
    STRATEGY = "STRATEGY"
    ROUTER = "ROUTER"
    RISK = "RISK"
    DATA = "DATA"
    DUPLICATE = "DUPLICATE"


# ---------------------------------------------------------------- trades
class TradeStatus(StrEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_TRAILING_STOP = "CLOSED_TRAILING_STOP"
    CLOSED_STRATEGY_EXIT = "CLOSED_STRATEGY_EXIT"
    CLOSED_TIME_EXIT = "CLOSED_TIME_EXIT"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class ExitReason(StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    TIME_EXIT = "TIME_EXIT"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# ---------------------------------------------------------------- circuit breakers
class CircuitBreakerType(StrEnum):
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    MAX_DRAWDOWN_LIMIT = "MAX_DRAWWOWN_LIMIT"
    DATA_QUALITY_BLOCK = "DATA_QUALITY_BLOCK"
    EXECUTION_MODEL_ERROR = "EXECUTION_MODEL_ERROR"
    MANUAL_PAUSE = "MANUAL_PAUSE"
    SYSTEM_EMERGENCY_STOP = "SYSTEM_EMERGENCY_STOP"


# ---------------------------------------------------------------- operational
class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    STOPPED = "STOPPED"
    DISABLED = "DISABLED"


class Actor(StrEnum):
    SYSTEM = "SYSTEM"
    TELEGRAM_USER = "TELEGRAM_USER"
    SCHEDULER = "SCHEDULER"
    TRADE_MANAGER = "TRADE_MANAGER"


class ConfigSource(StrEnum):
    FILE = "FILE"
    TELEGRAM_ADMIN = "TELEGRAM_ADMIN"
    MIGRATION = "MIGRATION"


class BarSource(StrEnum):
    BROKER_BAR = "BROKER_BAR"
    AGGREGATED_TICKS = "AGGREGATED_TICKS"
    IMPORTED_HISTORY = "IMPORTED_HISTORY"


class AmbiguousBarResolution(StrEnum):
    CONSERVATIVE_SL_FIRST = "CONSERVATIVE_SL_FIRST"
    AMBIGUOUS_BAR_EXIT = "AMBIGUOUS_BAR_EXIT"


class SlippageModelType(StrEnum):
    FIXED_POINTS = "FIXED_POINTS"
    SPREAD_AND_VOLATILITY_BASED = "SPREAD_AND_VOLATILITY_BASED"
    OBSERVED_SLIPPAGE = "OBSERVED_SLIPPAGE"


class CostModelStatus(StrEnum):
    UNKNOWN_COST_MODEL = "UNKNOWN_COST_MODEL"
    CONFIGURED = "CONFIGURED"
    INCOMPLETE = "INCOMPLETE"


# ---------------------------------------------------------------- shadow
class ShadowTrackingStatus(StrEnum):
    PENDING = "PENDING"
    TRACKING = "TRACKING"
    COMPLETED = "COMPLETED"
    CENSORED = "CENSORED"


# ---------------------------------------------------------------- outbox / reports
class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class ReportType(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    SESSION_TODAY = "SESSION_TODAY"
    PERFORMANCE = "PERFORMANCE"
