"""Fill, slippage and cost models for paper execution (spec 38).

- Prices: BUY enters on Ask / exits on Bid; SELL the mirror (spec 38.1).
- Slippage: FIXED_POINTS or SPREAD_AND_VOLATILITY_BASED, always adverse;
  deterministic (seeded) when randomness is required in backtests.
- Commission: per-lot-per-side or round-turn; status UNKNOWN_COST_MODEL
  when unconfigured (never silently zero — spec 10.4).
- Spread is embedded in the Bid/Ask fill (counted once, spec 38.7).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from app.core.enums import CostModelStatus, SlippageModelType
from app.data.contracts import MarketTick

EXECUTION_MODEL_VERSION = "1.0.0"


@dataclass(frozen=True)
class FillResult:
    fill_price: float
    requested_price: float
    slippage_price: float
    spread_price: float
    exit_price_used: str  # BID / ASK


class SlippageModel:
    """Adverse slippage in price units (spec 38.4)."""

    def __init__(self, cfg: dict, *, seed: int | None = None):
        self.model_type = SlippageModelType(cfg.get("slippage_model", "FIXED_POINTS"))
        self.fixed_points = float(cfg.get("slippage_fixed_points", 2.0))
        self.point = float(cfg.get("point", 0.01))
        self.spread_mult = float(cfg.get("slippage_spread_mult", 0.25))
        self.vol_mult = float(cfg.get("slippage_vol_mult", 0.05))
        self._rng = random.Random(seed) if seed is not None else None

    def adverse_slippage(self, tick: MarketTick, atr_value: float | None = None) -> float:
        """Always >= 0 price units worsening the fill."""
        if self.model_type == SlippageModelType.FIXED_POINTS:
            return self.fixed_points * self.point
        if self.model_type == SlippageModelType.SPREAD_AND_VOLATILITY_BASED:
            base = self.spread_mult * tick.spread_price
            vol_part = self.vol_mult * (atr_value or 0.0)
            total = base + vol_part
            if self._rng is not None:
                total *= 0.5 + self._rng.random()  # deterministic when seeded
            return max(total, self.point)
        return 0.0


class CostModel:
    """Commission & swap (spec 38.5)."""

    def __init__(self, cfg: dict):
        self.mode = cfg.get("commission_mode", "PER_LOT_PER_SIDE")  # | ROUND_TURN | NONE
        self.commission_per_lot_side = float(cfg.get("commission_per_lot_side", 7.0))
        self.swap_long_per_lot_day = float(cfg.get("swap_long_per_lot_day", -3.5))
        self.swap_short_per_lot_day = float(cfg.get("swap_short_per_lot_day", -2.0))
        self.swap_enabled = bool(cfg.get("swap_enabled", False))
        if self.mode == "NONE":
            self.status = CostModelStatus.CONFIGURED
        elif cfg.get("commission_explicitly_configured"):
            self.status = CostModelStatus.CONFIGURED
        else:
            # defaults are experimental assumptions, not broker facts
            self.status = CostModelStatus.INCOMPLETE

    def commission(self, volume_lots: float, *, sides: int = 2) -> float:
        """Round-trip commission by default (entry + exit)."""
        if self.mode == "NONE":
            return 0.0
        if self.mode == "ROUND_TURN":
            return self.commission_per_lot_side * volume_lots
        return self.commission_per_lot_side * volume_lots * sides

    def swap(self, volume_lots: float, side: str, days_held: float) -> float:
        if not self.swap_enabled or days_held <= 0:
            return 0.0
        rate = self.swap_long_per_lot_day if side == "BUY" else self.swap_short_per_lot_day
        return rate * volume_lots * days_held


class PaperFillEngine:
    """Computes realistic entry/exit fills. NEVER sends anything anywhere."""

    def __init__(self, slippage: SlippageModel, costs: CostModel, point: float = 0.01):
        self.slippage = slippage
        self.costs = costs
        self.point = point

    def entry_fill(self, side: str, tick: MarketTick, atr_value: float | None = None) -> FillResult:
        slip = self.slippage.adverse_slippage(tick, atr_value)
        spread = tick.ask - tick.bid
        if side == "BUY":
            price = tick.ask + slip
            used = "ASK"
        else:
            price = tick.bid - slip
            used = "BID"
        return FillResult(
            fill_price=round(price, 5),
            requested_price=round(tick.ask if side == "BUY" else tick.bid, 5),
            slippage_price=round(slip, 5),
            spread_price=round(spread, 5),
            exit_price_used=used,
        )

    def exit_fill(self, side: str, tick: MarketTick) -> FillResult:
        """Closing a BUY sells at Bid; closing a SELL buys at Ask."""
        if side == "BUY":
            price = tick.bid
            used = "BID"
        else:
            price = tick.ask
            used = "ASK"
        return FillResult(
            fill_price=round(price, 5),
            requested_price=round(price, 5),
            slippage_price=0.0,
            spread_price=round(tick.ask - tick.bid, 5),
            exit_price_used=used,
        )

    def gap_exit_fill(self, side: str, first_available_price: float) -> FillResult:
        """SL hit through a gap: execute at the first available price (spec 38.6)."""
        return FillResult(
            fill_price=round(first_available_price, 5),
            requested_price=round(first_available_price, 5),
            slippage_price=0.0,
            spread_price=0.0,
            exit_price_used="GAP_FIRST_AVAILABLE",
        )
