"""Strategy module contract (spec 7.1 / 30.1 / 35.7).

A school NEVER opens trades itself. ``evaluate`` returns a
``StrategyEvaluation``; the Hybrid Decision Engine alone decides who trades.
``manage_open_trade`` lets the *origin* school keep managing its own open
trades even after the router moves on (spec 8.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.core.enums import (
    Direction,
    EntryType,
    EvaluationStatus,
    SetupStage,
)
from app.market_context.engine import MarketSnapshot


@dataclass(frozen=True)
class ConditionCheck:
    """One condition with its observed value — reconstructable decisions."""

    condition_id: str
    observed_value: float | str | None
    operator: str
    threshold: float | str | None
    passed: bool
    timeframe: str | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "observed_value": self.observed_value,
            "operator": self.operator,
            "threshold": self.threshold,
            "passed": self.passed,
            "timeframe": self.timeframe,
            "note": self.note,
        }


@dataclass(frozen=True)
class ExitPlan:
    """Machine-executable exit plan proposed by a school."""

    take_profit: float | None = None
    exit_policy: str = "FIXED_TP"  # FIXED_TP | TRAILING_STRUCTURE | TRAILING_ATR | MEAN_TARGET | STRUCTURE_EXIT
    trailing_atr_period: int | None = None
    trailing_atr_mult: float | None = None
    break_even_at_r: float | None = None
    partial_close_at_r: float | None = None
    partial_close_fraction: float | None = None
    time_exit_hours: float | None = None


@dataclass
class StrategyEvaluation:
    evaluation_id: str
    strategy_id: str
    strategy_version: str
    config_version: str
    evaluation_timestamp_utc: datetime
    market_snapshot_id: str | None
    status: EvaluationStatus
    direction: Direction = Direction.NONE
    setup_type: str | None = None
    setup_stage: SetupStage = SetupStage.NONE
    entry_type: EntryType = EntryType.MARKET
    entry_price_proposal: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    exit_plan: ExitPlan | None = None
    risk_reward_estimate: float | None = None
    initial_risk_distance: float | None = None
    valid_until_utc: datetime | None = None
    timeframes_used: list[str] = field(default_factory=list)
    conditions_passed: list[ConditionCheck] = field(default_factory=list)
    conditions_failed: list[ConditionCheck] = field(default_factory=list)
    market_fit_score: float = 0.0
    setup_quality_score: float | None = None
    score_explanation: list[str] = field(default_factory=list)
    rejection_reason_codes: list[str] = field(default_factory=list)
    feature_snapshot: dict[str, Any] = field(default_factory=dict)
    opportunity_key: str | None = None
    notes: str = ""
    evaluation_started_at_utc: datetime | None = None
    evaluation_completed_at_utc: datetime | None = None

    @property
    def has_candidate(self) -> bool:
        return self.status == EvaluationStatus.CANDIDATE

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "config_version": self.config_version,
            "evaluation_timestamp_utc": self.evaluation_timestamp_utc.isoformat(),
            "market_snapshot_id": self.market_snapshot_id,
            "status": self.status.value,
            "direction": self.direction.value,
            "setup_type": self.setup_type,
            "setup_stage": self.setup_stage.value,
            "entry_type": self.entry_type.value,
            "entry_price_proposal": self.entry_price_proposal,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_reward_estimate": self.risk_reward_estimate,
            "valid_until_utc": self.valid_until_utc.isoformat() if self.valid_until_utc else None,
            "timeframes_used": self.timeframes_used,
            "conditions_passed": [c.to_dict() for c in self.conditions_passed],
            "conditions_failed": [c.to_dict() for c in self.conditions_failed],
            "market_fit_score": self.market_fit_score,
            "setup_quality_score": self.setup_quality_score,
            "score_explanation": self.score_explanation,
            "rejection_reason_codes": self.rejection_reason_codes,
            "feature_snapshot": self.feature_snapshot,
            "opportunity_key": self.opportunity_key,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TradeManagementContext:
    """What the trade manager shows the school each cycle."""

    trade_id: str
    side: Direction
    entry_price: float
    stop_loss: float
    take_profit: float | None
    opened_at_utc: datetime
    current_bid: float
    current_ask: float
    highest_bid_since_entry: float
    lowest_ask_since_entry: float
    unrealized_pnl: float
    r_multiple_now: float
    snapshot: MarketSnapshot


@dataclass(frozen=True)
class TradeManagementAction:
    action: str  # HOLD | MOVE_SL | CLOSE | PARTIAL_CLOSE
    new_stop_loss: float | None = None
    new_take_profit: float | None = None
    close_fraction: float | None = None
    reason_code: str = "HOLD"
    reason_text: str = ""


@runtime_checkable
class StrategyModule(Protocol):
    strategy_id: str
    strategy_name: str
    strategy_version: str

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        bar_service: Any,
        config: dict,
    ) -> StrategyEvaluation: ...

    def manage_open_trade(
        self,
        trade_context: TradeManagementContext,
        config: dict,
    ) -> TradeManagementAction: ...

    def health_status(self) -> str: ...

    def describe_capabilities(self) -> dict: ...


# --------------------------------------------------------------------------- base class
class StrategyBase:
    """Shared plumbing: condition bookkeeping, scoring helpers, id versioning."""

    strategy_id: str = "base"
    strategy_name: str = "Base"
    strategy_version: str = "1.0.0"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def check(
        condition_id: str,
        observed: float | str | None,
        operator: str,
        threshold: float | str | None,
        *,
        timeframe: str | None = None,
        note: str | None = None,
    ) -> ConditionCheck:
        ops = {
            ">": lambda a, b: a is not None and b is not None and a > b,
            ">=": lambda a, b: a is not None and b is not None and a >= b,
            "<": lambda a, b: a is not None and b is not None and a < b,
            "<=": lambda a, b: a is not None and b is not None and a <= b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
            "in": lambda a, b: a in b,
            "not_in": lambda a, b: a not in b,
            "is_not_none": lambda a, b: a is not None,
            "is_none": lambda a, b: a is None,
        }
        passed = bool(ops[operator](observed, threshold))
        return ConditionCheck(
            condition_id=condition_id,
            observed_value=observed,
            operator=operator,
            threshold=threshold,
            passed=passed,
            timeframe=timeframe,
            note=note,
        )

    def record(self, evaluation: StrategyEvaluation, check: ConditionCheck) -> None:
        (evaluation.conditions_passed if check.passed else evaluation.conditions_failed).append(check)

    def base_evaluation(
        self,
        snapshot: MarketSnapshot,
        config_version: str,
        started_at: datetime,
    ) -> StrategyEvaluation:
        from datetime import UTC

        from app.core.ids import new_uuid

        return StrategyEvaluation(
            evaluation_id=new_uuid(),
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_version=config_version,
            evaluation_timestamp_utc=started_at,
            evaluation_started_at_utc=started_at,
            market_snapshot_id=snapshot.snapshot_id,
            status=EvaluationStatus.NO_SETUP,
        )

    def finish(self, evaluation: StrategyEvaluation, completed_at: datetime) -> StrategyEvaluation:
        evaluation.evaluation_completed_at_utc = completed_at
        return evaluation

    # -- default management ----------------------------------------------
    def manage_open_trade(
        self,
        trade_context: TradeManagementContext,
        config: dict,
    ) -> TradeManagementAction:
        return TradeManagementAction(action="HOLD", reason_code="NO_ACTIVE_POLICY")

    def health_status(self) -> str:
        return "HEALTHY"

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": [],
            "setups": [],
        }
