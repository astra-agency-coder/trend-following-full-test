"""Order Flow / Auction Market Theory school (spec 35.6).

Status: ``DISABLED_DATA_UNAVAILABLE`` — XAUUSD spot is OTC with no unified
order book; a single broker's tick volume cannot measure delta/depth. The
module ships complete with an ``OrderFlowDataProvider`` adapter interface
so a real source (e.g. COMEX GC futures depth) can be plugged in later.
It never fabricates volume or delta, and its disabled state does NOT make
the rest of the system unhealthy (spec 47.2).
"""
from __future__ import annotations

from typing import Protocol

from app.core.enums import Direction, EvaluationStatus
from app.market_context.engine import MarketSnapshot
from app.strategies.base import (
    StrategyBase,
    StrategyEvaluation,
    TradeManagementAction,
    TradeManagementContext,
)


class OrderFlowDataProvider(Protocol):
    """Adapter contract for a future real order-flow source (COMEX GC etc.)."""

    provider_name: str
    source_market: str  # e.g. "COMEX_GC_FUTURES"
    contract_symbol: str | None

    def available(self) -> bool: ...
    def get_volume_profile(self, symbol: str, start_utc, end_utc) -> dict | None: ...
    def get_delta(self, symbol: str, start_utc, end_utc) -> dict | None: ...


class OrderFlowStrategy(StrategyBase):
    strategy_id = "order_flow"
    strategy_name = "Auction Market / Order Flow"
    strategy_version = "1.0.0"

    def __init__(self, config: dict | None = None, provider: OrderFlowDataProvider | None = None):
        super().__init__(config)
        self.provider = provider

    def evaluate(self, snapshot: MarketSnapshot, bar_service, config: dict) -> StrategyEvaluation:
        started = snapshot.timestamp_utc
        evaluation = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), started)
        evaluation.rejection_reason_codes = ["ORDER_FLOW_DISABLED_DATA_UNAVAILABLE"]
        evaluation.notes = (
            "Order Flow requires a real depth/delta source (e.g. COMEX GC futures); "
            "tick volume of a spot XAUUSD broker feed is not sufficient. Module stays disabled."
        )
        evaluation.status = EvaluationStatus.NO_SETUP
        return self.finish(evaluation, started)

    def manage_open_trade(self, trade_context: TradeManagementContext, config: dict) -> TradeManagementAction:
        return TradeManagementAction(action="HOLD", reason_code="ORDER_FLOW_DISABLED")

    def health_status(self) -> str:
        return "DISABLED_DATA_UNAVAILABLE"

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": [],
            "setups": [],
            "status": "DISABLED_DATA_UNAVAILABLE",
            "required_data": ["bid_ask_aggressor_volume", "footprint", "market_depth", "comex_gc_futures"],
            "note": "adapter interface provided; enable only with a real verified source",
        }
