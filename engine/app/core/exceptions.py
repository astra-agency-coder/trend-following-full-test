"""Application-wide exception hierarchy.

Recoverable errors carry `retryable=True`; anything else must halt trade
opening but should never crash the whole engine (spec 51.2).
"""
from __future__ import annotations


class GoldHybridError(Exception):
    """Base class for all domain errors."""

    retryable: bool = False
    code: str = "GOLD_HYBRID_ERROR"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict = details or {}


class ConfigurationError(GoldHybridError):
    """Invalid / missing configuration. Never open trades in this state."""

    code = "CONFIG_ERROR"


class DatabaseError(GoldHybridError):
    code = "DATABASE_ERROR"
    retryable = True


class MarketDataError(GoldHybridError):
    code = "MARKET_DATA_ERROR"


class DataQualityError(MarketDataError):
    """Tick/bar failed validation (stale, crossed, non-numeric...)."""

    code = "DATA_QUALITY_ERROR"


class ProviderConnectionError(MarketDataError):
    code = "PROVIDER_CONNECTION_ERROR"
    retryable = True


class SymbolError(MarketDataError):
    code = "SYMBOL_ERROR"


class StrategyError(GoldHybridError):
    code = "STRATEGY_ERROR"


class RouterError(GoldHybridError):
    code = "ROUTER_ERROR"


class RiskError(GoldHybridError):
    code = "RISK_ERROR"


class ExecutionError(GoldHybridError):
    code = "EXECUTION_ERROR"


class TradeStateError(GoldHybridError):
    code = "TRADE_STATE_ERROR"


class TelegramError(GoldHybridError):
    code = "TELEGRAM_ERROR"
    retryable = True


class PaperOnlyViolation(GoldHybridError):
    """Raised when any real-order execution path is attempted."""

    code = "PAPER_ONLY_VIOLATION"


class RealExecutionDisabledError(PaperOnlyViolation):
    """Startup assertion: the app refuses to run with real execution enabled."""

    code = "REAL_EXECUTION_DISABLED"
