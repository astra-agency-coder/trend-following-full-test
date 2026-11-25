"""Shared constants (spec sections 0/5/30)."""
from __future__ import annotations

APP_NAME = "GOLD_HYBRID_ENGINE"
APP_VERSION = "1.0.0"

CANONICAL_SYMBOL = "XAUUSD"

# Known broker-specific spellings for gold; resolved at runtime via config.
DEFAULT_SYMBOL_CANDIDATES = ["XAUUSD", "XAUUSDm", "XAUUSD.a", "GOLD", "XAUUSD.x", "XAUUSDraw"]

TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

DEFAULT_TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4"]

# Versions of the deterministic rule components (spec 20.2 / 32).
CONTEXT_VERSION = "1.0.0"
ROUTER_VERSION = "1.0.0"
RISK_CALCULATION_VERSION = "1.0.0"
EXECUTION_MODEL_VERSION = "1.0.0"
SHADOW_CALCULATION_VERSION = "1.0.0"
SESSION_CALENDAR_VERSION = "1.0.0"
FEATURE_SET_VERSION = "1.0.0"

# UTC storage everywhere (spec 17).
UTC = "UTC"

# Hard safety defaults (overridable in config).
MAX_DATA_AGE_SECONDS = 10
MIN_BARS_FOR_CONTEXT = 60

# Rate limit for telegram commands (per user, sliding window).
TELEGRAM_RATE_LIMIT_PER_MINUTE = 20

# Forbidden MT5 order functions — importing/calling any of these is a
# PAPER-ONLY violation (spec 23 / 33.1 / test H).
FORBIDDEN_MT5_ORDER_FUNCTIONS = (
    "order_send",
    "orders_send",
    "order_check",
    "positions_add",
    "positions_close",
    "positions_modify",
    "orders_add",
    "orders_delete",
    "orders_modify",
)
