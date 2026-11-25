"""Strategy registry — instantiates enabled schools from config."""
from __future__ import annotations

from app.core.enums import StrategyModuleState
from app.core.logging_setup import get_logger
from app.strategies.base import StrategyBase
from app.strategies.breakout.strategy import BreakoutStrategy
from app.strategies.mean_reversion.strategy import MeanReversionStrategy
from app.strategies.order_flow.strategy import OrderFlowStrategy
from app.strategies.smc_ict.strategy import SmcIctStrategy
from app.strategies.trend_following.strategy import TrendFollowingStrategy
from app.strategies.wyckoff.strategy import WyckoffStrategy

logger = get_logger("strategies.registry")

STRATEGY_CLASSES: dict[str, type[StrategyBase]] = {
    "smc_ict": SmcIctStrategy,
    "wyckoff": WyckoffStrategy,
    "trend_following": TrendFollowingStrategy,
    "mean_reversion": MeanReversionStrategy,
    "breakout": BreakoutStrategy,
    "order_flow": OrderFlowStrategy,
}


class StrategyRegistry:
    def __init__(self, config: dict):
        self.config = config
        self.strategies: dict[str, StrategyBase] = {}
        self.states: dict[str, StrategyModuleState] = {}
        self._build()

    def _build(self) -> None:
        strategy_cfgs = self.config.get("strategies", {})
        for strategy_id, cls in STRATEGY_CLASSES.items():
            cfg = strategy_cfgs.get(strategy_id, {})
            enabled = bool(cfg.get("enabled", False))
            if not enabled:
                disable_reason = cfg.get("disable_reason", "DISABLED_CONFIG")
                if strategy_id == "order_flow" and disable_reason == "DATA_SOURCE_NOT_CONFIGURED":
                    self.states[strategy_id] = StrategyModuleState.DISABLED_DATA_UNAVAILABLE
                else:
                    self.states[strategy_id] = StrategyModuleState.DISABLED_CONFIG
                continue
            try:
                instance = cls(config=cfg)
                self.strategies[strategy_id] = instance
                if isinstance(instance, OrderFlowStrategy):
                    # enabled but no verified provider -> still data-unavailable
                    self.states[strategy_id] = StrategyModuleState.DISABLED_DATA_UNAVAILABLE
                    self.strategies.pop(strategy_id)
                else:
                    self.states[strategy_id] = StrategyModuleState.ENABLED
            except Exception:
                logger.exception(f"failed to initialize strategy {strategy_id}")
                self.states[strategy_id] = StrategyModuleState.ERROR

    def enabled_strategies(self) -> list[StrategyBase]:
        return list(self.strategies.values())

    def state_of(self, strategy_id: str) -> StrategyModuleState:
        return self.states.get(strategy_id, StrategyModuleState.DISABLED_CONFIG)

    def summary(self) -> dict[str, str]:
        return {sid: state.value for sid, state in self.states.items()}
