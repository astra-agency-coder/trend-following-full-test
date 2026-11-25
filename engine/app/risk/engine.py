"""Risk Engine (spec sections 9 / 39).

Position sizing from symbol specification (tick value / contract size —
never a fixed pip value), floor-rounding to volume step, aggregate risk
limits, same-direction exposure cap, and the daily loss circuit breaker.
Every decision is returned as a RiskResult with full checks journal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import CircuitBreakerType, Direction
from app.core.logging_setup import get_logger
from app.data.contracts import SymbolSpecification
from app.strategies.base import StrategyEvaluation

logger = get_logger("risk.engine")

RISK_CALCULATION_VERSION = "1.0.0"


@dataclass
class RiskCheck:
    check_id: str
    passed: bool
    observed: float | str | None
    limit: float | str | None
    note: str = ""

    def to_dict(self) -> dict:
        return {"check_id": self.check_id, "passed": self.passed, "observed": self.observed, "limit": self.limit, "note": self.note}


@dataclass
class RiskResult:
    approved: bool
    risk_amount: float
    risk_pct: float
    calculated_volume: float | None = None
    post_rounding_risk: float | None = None
    rejection_codes: list[str] = field(default_factory=list)
    checks: list[RiskCheck] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "risk_amount": self.risk_amount,
            "risk_pct": self.risk_pct,
            "calculated_volume": self.calculated_volume,
            "post_rounding_risk": self.post_rounding_risk,
            "rejection_codes": self.rejection_codes,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class OpenPositionRisk:
    trade_id: str
    side: Direction
    strategy_id: str
    risk_amount: float
    entry_price: float
    stop_loss: float
    volume_lots: float
    opportunity_key: str | None = None


class RiskEngine:
    def __init__(self, config: dict, spec: SymbolSpecification):
        self.config = config
        self.spec = spec
        rcfg = config.get("risk", {})
        self.risk_per_trade_pct = float(rcfg.get("risk_per_trade_pct", 0.5))
        self.max_total_open_risk_pct = float(rcfg.get("max_total_open_risk_pct", 1.5))
        self.max_concurrent_positions = int(rcfg.get("max_concurrent_positions", 3))
        self.max_positions_per_strategy = int(rcfg.get("max_open_positions_per_strategy", 1))
        self.max_same_direction_positions = int(rcfg.get("max_same_direction_positions", 2))
        self.daily_loss_limit_pct = float(rcfg.get("daily_loss_limit_pct", 2.0))
        self.daily_loss_limit_enabled = bool(rcfg.get("daily_loss_limit_enabled", True))

    # -------------------------------------------------------------- sizing
    def value_per_price_unit_per_lot(self) -> float:
        """Monetary value of a 1.0 price move for 1.0 lot.

        XAUUSD convention: contract 100 oz -> $100 per $1 move per lot.
        Uses tick_size/tick_value when present, else contract_size.
        """
        tick_size = self.spec.tick_size or (self.spec.point or 0.01)
        tick_value = self.spec.tick_value
        if tick_value and tick_size and tick_size > 0:
            return tick_value / tick_size
        contract = self.spec.contract_size or 100.0
        return contract

    def compute_volume(self, entry: float, stop: float, risk_amount: float) -> tuple[float | None, float, str | None]:
        """Volume in lots so that SL hit loses ~risk_amount.

        Returns (volume, actual_risk, error_code). Volume floors to the
        volume step (never up — spec 39.3).
        """
        distance = abs(entry - stop)
        if distance <= 0:
            return None, 0.0, "INVALID_STOP_DISTANCE"
        per_unit = self.value_per_price_unit_per_lot()
        if per_unit <= 0:
            return None, 0.0, "INVALID_SYMBOL_VALUE_MODEL"
        raw_volume = risk_amount / (distance * per_unit)

        step = self.spec.volume_step or 0.01
        volume = math.floor(raw_volume / step) * step
        volume = round(volume, 4)

        vmin = self.spec.volume_min or 0.01
        if volume < vmin:
            return None, 0.0, "MIN_VOLUME_EXCEEDS_RISK"
        vmax = self.spec.volume_max or 100.0
        if volume > vmax:
            volume = round(math.floor(vmax / step) * step, 4)

        actual_risk = volume * distance * per_unit
        return volume, round(actual_risk, 2), None

    # -------------------------------------------------------------- approval
    def approve(
        self,
        evaluation: StrategyEvaluation,
        *,
        equity: float,
        balance: float,
        open_positions: list[OpenPositionRisk],
        day_realized_pnl: float,
        now_utc: datetime,
    ) -> RiskResult:
        checks: list[RiskCheck] = []
        codes: list[str] = []

        def add(check_id: str, passed: bool, observed, limit, note=""):
            checks.append(RiskCheck(check_id, passed, observed, limit, note))
            if not passed:
                codes.append(check_id)

        entry = evaluation.entry_price_proposal
        stop = evaluation.stop_loss

        # 1. SL validity & side
        add(
            "SL_VALID_SIDE",
            stop is not None and entry is not None and (
                (evaluation.direction == Direction.BUY and stop < entry)
                or (evaluation.direction == Direction.SELL and stop > entry)
            ),
            f"{stop}",
            "correct side of entry",
        )

        risk_amount = equity * self.risk_per_trade_pct / 100.0
        volume: float | None = None
        post_risk: float | None = None
        if stop is not None and entry is not None:
            # 2-6. sizing with floor rounding + re-verification
            volume, post_risk, err = self.compute_volume(entry, stop, risk_amount)
            add("VOLUME_COMPUTABLE", volume is not None, err or "ok", "volume > 0")
            if volume is not None and post_risk is not None:
                add(
                    "POST_ROUNDING_RISK_OK",
                    post_risk <= risk_amount * 1.0001,
                    post_risk,
                    risk_amount,
                    "risk after floor rounding must not exceed plan",
                )

        # 7. per-trade risk %
        add("RISK_PCT_PER_TRADE", self.risk_per_trade_pct <= 100, self.risk_per_trade_pct, 100)

        # 8. aggregate open risk
        open_risk = sum(p.risk_amount for p in open_positions)
        max_total = equity * self.max_total_open_risk_pct / 100.0
        projected = open_risk + (post_risk or 0.0)
        add("MAX_TOTAL_OPEN_RISK", projected <= max_total + 1e-9, round(projected, 2), round(max_total, 2))

        # 9. max positions
        add("MAX_CONCURRENT_POSITIONS", len(open_positions) < self.max_concurrent_positions, len(open_positions), self.max_concurrent_positions)

        # per-strategy positions
        same_strategy = sum(1 for p in open_positions if p.strategy_id == evaluation.strategy_id)
        add("MAX_POSITIONS_PER_STRATEGY", same_strategy < self.max_positions_per_strategy, same_strategy, self.max_positions_per_strategy)

        # 10. same-direction exposure
        same_dir = sum(1 for p in open_positions if p.side == evaluation.direction)
        add("MAX_SAME_DIRECTION", same_dir < self.max_same_direction_positions, same_dir, self.max_same_direction_positions)

        # same opportunity cluster (spec 39.4)
        if evaluation.opportunity_key:
            same_key = sum(1 for p in open_positions if p.opportunity_key == evaluation.opportunity_key)
            add("SAME_OPPORTUNITY_CLUSTER", same_key == 0, same_key, 0, "correlated entries on one level")

        # 11. daily loss circuit breaker
        if self.daily_loss_limit_enabled:
            daily_limit = balance * self.daily_loss_limit_pct / 100.0
            add("DAILY_LOSS_LIMIT", day_realized_pnl > -daily_limit, round(day_realized_pnl, 2), round(-daily_limit, 2))

        approved = not codes and volume is not None
        return RiskResult(
            approved=approved,
            risk_amount=round(risk_amount, 2),
            risk_pct=self.risk_per_trade_pct,
            calculated_volume=volume,
            post_rounding_risk=post_risk,
            rejection_codes=codes,
            checks=checks,
        )
