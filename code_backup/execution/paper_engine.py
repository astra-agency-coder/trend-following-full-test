"""Paper Trading Engine + Trade Manager (spec 10 / 37 / 38).

One hybrid paper portfolio. Trades keep their origin school & management
policy until close (spec 8.4). The state machine only allows documented
transitions. Nothing here can send a real order — the engine talks to the
database and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.enums import (
    Actor,
    Direction,
    ExitReason,
    TradeStatus,
)
from app.core.exceptions import TradeStateError
from app.core.logging_setup import get_logger
from app.data.contracts import MarketTick, SymbolSpecification
from app.execution.fill_models import CostModel, PaperFillEngine
from app.market_context.engine import MarketSnapshot
from app.strategies.base import StrategyEvaluation, TradeManagementAction, TradeManagementContext
from app.strategies.registry import StrategyRegistry

logger = get_logger("execution.paper")

VALID_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"PENDING", "OPEN", "CANCELLED", "EXPIRED"},
    "PENDING": {"OPEN", "CANCELLED", "EXPIRED"},
    "OPEN": {"PARTIALLY_CLOSED", "CLOSED_TP", "CLOSED_SL", "CLOSED_TRAILING_STOP", "CLOSED_STRATEGY_EXIT", "CLOSED_TIME_EXIT", "ERROR"},
    "PARTIALLY_CLOSED": {"CLOSED_TP", "CLOSED_SL", "CLOSED_TRAILING_STOP", "CLOSED_STRATEGY_EXIT", "CLOSED_TIME_EXIT"},
}


@dataclass
class PaperTrade:
    id: str
    portfolio_id: str
    origin_strategy_id: str
    strategy_version: str
    management_policy_version: str
    router_decision_id: str | None
    entry_evaluation_id: str | None
    opportunity_key: str | None
    symbol: str
    side: Direction
    status: TradeStatus
    order_type: str
    created_at_utc: datetime
    opened_at_utc: datetime | None = None
    closed_at_utc: datetime | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    initial_stop_loss: float | None = None
    volume_lots: float = 0.0
    remaining_volume_lots: float = 0.0
    planned_risk_amount: float | None = None
    gross_pnl: float | None = None
    commission: float | None = None
    swap: float | None = None
    slippage_cost: float | None = None
    net_pnl: float | None = None
    r_multiple: float | None = None
    exit_reason: str | None = None
    entry_spread_points: float | None = None
    exit_resolution_method: str | None = None
    entry_session_name: str | None = None
    exit_session_name: str | None = None
    entry_snapshot_id: str | None = None
    highest_bid_since_entry: float | None = None
    lowest_ask_since_entry: float | None = None
    management_policy: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def unrealized_pnl(self, bid: float, ask: float, value_per_unit: float) -> float:
        if self.entry_price is None:
            return 0.0
        close_side_price = bid if self.side == Direction.BUY else ask
        diff = (close_side_price - self.entry_price) if self.side == Direction.BUY else (self.entry_price - close_side_price)
        return round(self.remaining_volume_lots * diff * value_per_unit, 2)

    def current_r(self, bid: float, ask: float, value_per_unit: float) -> float:
        if self.entry_price is None or not self.planned_risk_amount or self.planned_risk_amount <= 0:
            return 0.0
        pnl = self.unrealized_pnl(bid, ask, value_per_unit)
        return pnl / self.planned_risk_amount


class PaperTradingEngine:
    """Owns the paper portfolio, fills and closes virtual trades."""

    def __init__(
        self,
        *,
        spec: SymbolSpecification,
        fill_engine: PaperFillEngine,
        cost_model: CostModel,
        strategy_registry: StrategyRegistry,
        portfolio_id: str,
        initial_balance: float,
    ):
        self.spec = spec
        self.fills = fill_engine
        self.costs = cost_model
        self.registry = strategy_registry
        self.portfolio_id = portfolio_id
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.open_trades: dict[str, PaperTrade] = {}
        self.closed_trades: list[PaperTrade] = []
        self.value_per_unit = self._value_per_unit()

    def _value_per_unit(self) -> float:
        tick_size = self.spec.tick_size or self.spec.point
        if self.spec.tick_value and tick_size:
            return self.spec.tick_value / tick_size
        return self.spec.contract_size or 100.0

    # ------------------------------------------------------------- transitions
    @staticmethod
    def _transition(trade: PaperTrade, new_status: TradeStatus) -> None:
        allowed = VALID_TRANSITIONS.get(trade.status.value, set())
        if new_status.value not in allowed:
            raise TradeStateError(
                f"illegal transition {trade.status.value} -> {new_status.value} for trade {trade.id}"
            )
        trade.status = new_status

    # ------------------------------------------------------------- open
    def open_trade(
        self,
        *,
        evaluation: StrategyEvaluation,
        decision_id: str,
        volume: float,
        risk_amount: float,
        tick: MarketTick,
        snapshot: MarketSnapshot,
        now_utc: datetime,
        slippage_cost: float | None = None,
    ) -> PaperTrade:
        from app.core.ids import new_uuid

        fill = self.fills.entry_fill(evaluation.direction.value, tick, snapshot.atr_values.get("M15"))
        trade = PaperTrade(
            id=new_uuid(),
            portfolio_id=self.portfolio_id,
            origin_strategy_id=evaluation.strategy_id,
            strategy_version=evaluation.strategy_version,
            management_policy_version=evaluation.strategy_version,
            router_decision_id=decision_id,
            entry_evaluation_id=evaluation.evaluation_id,
            opportunity_key=evaluation.opportunity_key,
            symbol=self.spec.canonical_symbol,
            side=evaluation.direction,
            status=TradeStatus.CREATED,
            order_type=evaluation.entry_type.value,
            created_at_utc=now_utc,
            stop_loss=evaluation.stop_loss,
            initial_stop_loss=evaluation.stop_loss,
            take_profit=evaluation.take_profit,
            volume_lots=volume,
            remaining_volume_lots=volume,
            planned_risk_amount=risk_amount,
            entry_spread_points=round(tick.spread_points, 2),
            entry_session_name=snapshot.session.session_name,
            entry_snapshot_id=snapshot.snapshot_id,
            highest_bid_since_entry=tick.bid,
            lowest_ask_since_entry=tick.ask,
            management_policy={
                "exit_policy": evaluation.exit_plan.exit_policy if evaluation.exit_plan else "FIXED_TP",
                "trailing_atr_mult": evaluation.exit_plan.trailing_atr_mult if evaluation.exit_plan else None,
                "break_even_at_r": evaluation.exit_plan.break_even_at_r if evaluation.exit_plan else None,
            },
            metadata={
                "setup_type": evaluation.setup_type,
                "fill_price_requested": fill.requested_price,
                "slippage_price": fill.slippage_price,
                "slippage_cost": slippage_cost,
            },
        )
        self._transition(trade, TradeStatus.OPEN)
        trade.entry_price = fill.fill_price
        trade.opened_at_utc = now_utc
        # re-verify risk at actual fill (spec 38.2 step 6)
        actual_risk = abs(fill.fill_price - (evaluation.stop_loss or fill.fill_price)) * volume * self.value_per_unit
        if self.planned_risk_amount_exceeded(actual_risk, risk_amount):
            trade.metadata["risk_at_fill_note"] = f"actual risk {actual_risk:.2f} vs planned {risk_amount:.2f} (slippage)"
        self.open_trades[trade.id] = trade
        return trade

    @staticmethod
    def planned_risk_amount_exceeded(actual: float, planned: float) -> bool:
        return actual > planned * 1.15  # tolerance for slippage; logged not rejected

    # ------------------------------------------------------------- management cycle
    def manage_trades(
        self,
        *,
        tick: MarketTick,
        snapshot: MarketSnapshot,
        now_utc: datetime,
        config: dict,
        on_event=None,
    ) -> list[PaperTrade]:
        """SL/TP monitor + origin-strategy management for every open trade.

        Runs BEFORE strategy evaluation in the runtime loop (spec 18 note).
        Returns trades closed this cycle.
        closed: list of PaperTrade closed during this cycle.
        """
        closed: list[PaperTrade] = []
        for trade in list(self.open_trades.values()):
            # update excursions
            trade.highest_bid_since_entry = max(trade.highest_bid_since_entry or tick.bid, tick.bid)
            trade.lowest_ask_since_entry = min(trade.lowest_ask_since_entry or tick.ask, tick.ask)

            # 1. hard SL / TP on executable prices
            exit_hit = self._check_sl_tp(trade, tick)
            if exit_hit is not None:
                reason, price = exit_hit
                self.close_trade(trade, exit_price=price, reason=reason, tick=tick, snapshot=snapshot, now_utc=now_utc, on_event=on_event)
                closed.append(trade)
                continue

            # 2. origin strategy management policy (trailing / BE)
            strategy = self.registry.strategies.get(trade.origin_strategy_id)
            if strategy is not None:
                ctx = TradeManagementContext(
                    trade_id=trade.id,
                    side=trade.side,
                    entry_price=trade.entry_price or 0.0,
                    stop_loss=trade.stop_loss or 0.0,
                    take_profit=trade.take_profit,
                    opened_at_utc=trade.opened_at_utc or now_utc,
                    current_bid=tick.bid,
                    current_ask=tick.ask,
                    highest_bid_since_entry=trade.highest_bid_since_entry or tick.bid,
                    lowest_ask_since_entry=trade.lowest_ask_since_entry or tick.ask,
                    unrealized_pnl=trade.unrealized_pnl(tick.bid, tick.ask, self.value_per_unit),
                    r_multiple_now=trade.current_r(tick.bid, tick.ask, self.value_per_unit),
                    snapshot=snapshot,
                )
                try:
                    action = strategy.manage_open_trade(ctx, config)
                except Exception:
                    logger.exception(f"management policy failed for {trade.id}; keeping HOLD")
                    action = TradeManagementAction(action="HOLD", reason_code="POLICY_ERROR")
                if action.action == "MOVE_SL" and action.new_stop_loss is not None:
                    self.move_stop(trade, action.new_stop_loss, reason=action.reason_code, on_event=on_event, snapshot_id=snapshot.snapshot_id, now_utc=now_utc)
                elif action.action == "CLOSE":
                    self.close_trade(trade, exit_price=None, reason=ExitReason.STRATEGY_EXIT, tick=tick, snapshot=snapshot, now_utc=now_utc, on_event=on_event, strategy_reason=action.reason_text)
                    closed.append(trade)
                    continue

            # 3. time exit
            policy = trade.management_policy or {}
            max_hours = policy.get("max_hold_hours")
            if max_hours and trade.opened_at_utc and (now_utc - trade.opened_at_utc).total_seconds() > max_hours * 3600:
                self.close_trade(trade, exit_price=None, reason=ExitReason.TIME_EXIT, tick=tick, snapshot=snapshot, now_utc=now_utc, on_event=on_event)
                closed.append(trade)

        return closed

    def _check_sl_tp(self, trade: PaperTrade, tick: MarketTick) -> tuple[ExitReason, float] | None:
        """Executable-price SL/TP check (BUY exits on Bid, SELL on Ask)."""
        if trade.side == Direction.BUY:
            close_price = tick.bid
            if trade.stop_loss is not None and close_price <= trade.stop_loss:
                # gap: fill at min(SL, bid) — first available price
                fill_price = min(trade.stop_loss, close_price)
                return ExitReason.STOP_LOSS, fill_price
            if trade.take_profit is not None and close_price >= trade.take_profit:
                return ExitReason.TAKE_PROFIT, trade.take_profit
        else:
            close_price = tick.ask
            if trade.stop_loss is not None and close_price >= trade.stop_loss:
                fill_price = max(trade.stop_loss, close_price)
                return ExitReason.STOP_LOSS, fill_price
            if trade.take_profit is not None and close_price <= trade.take_profit:
                return ExitReason.TAKE_PROFIT, trade.take_profit
        return None

    def move_stop(self, trade: PaperTrade, new_sl: float, *, reason: str, on_event=None, snapshot_id: str | None = None, now_utc: datetime | None = None) -> bool:
        """Only tighten stops — never widen (no hidden risk increase)."""
        if trade.side == Direction.BUY and new_sl <= (trade.stop_loss or float("-inf")):
            return False
        if trade.side == Direction.SELL and new_sl >= (trade.stop_loss or float("inf")):
            return False
        old = trade.stop_loss
        trade.stop_loss = round(new_sl, 5)
        if on_event:
            on_event(
                trade.id,
                "SL_MODIFIED",
                {"old_values": {"stop_loss": old}, "new_values": {"stop_loss": trade.stop_loss}, "reason": reason},
                now_utc=now_utc,
                snapshot_id=snapshot_id,
                actor=Actor.TRADE_MANAGER.value,
            )
        return True

    # ------------------------------------------------------------- close
    def close_trade(
        self,
        trade: PaperTrade,
        *,
        exit_price: float | None,
        reason: ExitReason,
        tick: MarketTick,
        snapshot: MarketSnapshot | None = None,
        now_utc: datetime,
        on_event=None,
        strategy_reason: str | None = None,
    ) -> PaperTrade:
        if exit_price is None:
            fill = self.fills.exit_fill(trade.side.value, tick)
            exit_price = fill.fill_price
            trade.exit_resolution_method = fill.exit_price_used
        else:
            trade.exit_resolution_method = "GAP_FIRST_AVAILABLE" if reason == ExitReason.STOP_LOSS else "LEVEL"

        diff = (
            (exit_price - trade.entry_price)
            if trade.side == Direction.BUY
            else (trade.entry_price - exit_price)
        )
        gross = trade.remaining_volume_lots * diff * self.value_per_unit
        commission = self.costs.commission(trade.volume_lots)
        days_held = ((now_utc - trade.opened_at_utc).total_seconds() / 86400.0) if trade.opened_at_utc else 0.0
        swap = self.costs.swap(trade.volume_lots, trade.side.value, days_held)
        slippage_entry = float(trade.metadata.get("slippage_price") or 0.0) * trade.volume_lots * self.value_per_unit
        net = gross - commission - swap

        trade.gross_pnl = round(gross, 2)
        trade.commission = round(commission, 2)
        trade.swap = round(swap, 2)
        trade.slippage_cost = round(slippage_entry, 2)
        trade.net_pnl = round(net, 2)
        trade.r_multiple = round(net / trade.planned_risk_amount, 4) if trade.planned_risk_amount else None
        trade.exit_price = round(exit_price, 5)
        trade.closed_at_utc = now_utc
        trade.exit_reason = reason.value
        if snapshot is not None:
            trade.exit_session_name = snapshot.session.session_name
        if strategy_reason:
            trade.metadata["strategy_exit_reason"] = strategy_reason

        status_map = {
            ExitReason.TAKE_PROFIT: TradeStatus.CLOSED_TP,
            ExitReason.STOP_LOSS: TradeStatus.CLOSED_SL,
            ExitReason.TRAILING_STOP: TradeStatus.CLOSED_TRAILING_STOP,
            ExitReason.STRATEGY_EXIT: TradeStatus.CLOSED_STRATEGY_EXIT,
            ExitReason.TIME_EXIT: TradeStatus.CLOSED_TIME_EXIT,
            ExitReason.SETUP_INVALIDATED: TradeStatus.CLOSED_STRATEGY_EXIT,
            ExitReason.CANCELLED: TradeStatus.CANCELLED,
            ExitReason.EXPIRED: TradeStatus.EXPIRED,
        }
        self._transition(trade, status_map[reason])
        self.balance = round(self.balance + net, 2)
        self.open_trades.pop(trade.id, None)
        self.closed_trades.append(trade)

        if on_event:
            on_event(
                trade.id,
                "TRADE_CLOSED",
                {
                    "exit_price": trade.exit_price,
                    "exit_reason": trade.exit_reason,
                    "net_pnl": trade.net_pnl,
                    "r_multiple": trade.r_multiple,
                    "gross_pnl": trade.gross_pnl,
                    "commission": trade.commission,
                    "swap": trade.swap,
                },
                now_utc=now_utc,
                snapshot_id=snapshot.snapshot_id if snapshot else None,
            )
        return trade

    # ------------------------------------------------------------- queries
    def equity(self, tick: MarketTick | None) -> float:
        unrealized = sum(
            t.unrealized_pnl(tick.bid, tick.ask, self.value_per_unit) for t in self.open_trades.values()
        ) if tick else 0.0
        return round(self.balance + unrealized, 2)

    def day_realized_pnl(self, day_start_utc: datetime) -> float:
        return round(
            sum(
                (t.net_pnl or 0.0)
                for t in self.closed_trades
                if t.closed_at_utc and t.closed_at_utc >= day_start_utc
            ),
            2,
        )

    def restore_open_trade(self, trade: PaperTrade) -> None:
        """Restart recovery: rehydrate an open trade from DB (spec 22.3)."""
        if trade.status in (TradeStatus.OPEN, TradeStatus.PENDING, TradeStatus.PARTIALLY_CLOSED):
            self.open_trades[trade.id] = trade
