"""Shadow Tracking (spec 11 / 41).

Tracks candidates the router did NOT select, using the same entry/exit
rules the original school proposed — never a better after-known price.
Completely isolated from portfolio balance/risk. Outcomes are computed
only after the price path completes (no router feedback loop).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import Direction, ExitReason, ShadowTrackingStatus
from app.core.logging_setup import get_logger
from app.data.contracts import MarketTick
from app.strategies.base import StrategyEvaluation

logger = get_logger("shadow.tracker")

SHADOW_CALCULATION_VERSION = "1.0.0"


@dataclass
class ShadowRecord:
    id: str
    evaluation: StrategyEvaluation
    rejected_at_utc: datetime
    rejection_stage: str
    rejection_reasons: list[str]
    selected_alternative: str | None
    # entry snapshot at decision time (never revised later)
    entry_bid: float
    entry_ask: float
    # outcome fields (filled later)
    status: ShadowTrackingStatus = ShadowTrackingStatus.TRACKING
    entry_price: float | None = None
    exit_time_utc: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_pnl_per_lot: float | None = None
    mae: float | None = None
    mfe: float | None = None
    resolved: bool = False

    @property
    def direction(self) -> Direction:
        return self.evaluation.direction

    @property
    def stop_loss(self) -> float | None:
        return self.evaluation.stop_loss

    @property
    def take_profit(self) -> float | None:
        return self.evaluation.take_profit


class ShadowTracker:
    """Holds active shadow records and resolves them against live ticks."""

    def __init__(self, *, value_per_unit: float = 100.0, max_hold_hours: float = 48.0):
        self.value_per_unit = value_per_unit
        self.max_hold_hours = max_hold_hours
        self.records: dict[str, ShadowRecord] = {}
        self.completed: list[ShadowRecord] = []

    def register(
        self,
        evaluation: StrategyEvaluation,
        *,
        rejected_at_utc: datetime,
        rejection_stage: str,
        rejection_reasons: list[str],
        selected_alternative: str | None,
        tick: MarketTick,
    ) -> ShadowRecord:
        from app.core.ids import new_uuid

        record = ShadowRecord(
            id=new_uuid(),
            evaluation=evaluation,
            rejected_at_utc=rejected_at_utc,
            rejection_stage=rejection_stage,
            rejection_reasons=rejection_reasons,
            selected_alternative=selected_alternative,
            entry_bid=tick.bid,
            entry_ask=tick.ask,
        )
        # entry at the same instant and side rules as a real fill
        if evaluation.direction == Direction.BUY:
            record.entry_price = tick.ask
        else:
            record.entry_price = tick.bid
        self.records[record.id] = record
        return record

    def update(self, tick: MarketTick, now_utc: datetime) -> list[ShadowRecord]:
        """Advance every active record; returns records resolved this tick."""
        resolved_now: list[ShadowRecord] = []
        for record in list(self.records.values()):
            entry = record.entry_price or 0.0
            if record.direction == Direction.BUY:
                favorable = tick.bid - entry
                record.mfe = max(record.mfe or 0.0, favorable)
                record.mae = min(record.mae or 0.0, favorable)
                exit_hit = None
                if record.stop_loss is not None and tick.bid <= record.stop_loss:
                    exit_hit = (ExitReason.STOP_LOSS, min(record.stop_loss, tick.bid))
                elif record.take_profit is not None and tick.bid >= record.take_profit:
                    exit_hit = (ExitReason.TAKE_PROFIT, record.take_profit)
            else:
                favorable = entry - tick.ask
                record.mfe = max(record.mfe or 0.0, favorable)
                record.mae = min(record.mae or 0.0, favorable)
                exit_hit = None
                if record.stop_loss is not None and tick.ask >= record.stop_loss:
                    exit_hit = (ExitReason.STOP_LOSS, max(record.stop_loss, tick.ask))
                elif record.take_profit is not None and tick.ask <= record.take_profit:
                    exit_hit = (ExitReason.TAKE_PROFIT, record.take_profit)

            if exit_hit is not None:
                reason, price = exit_hit
                self._resolve(record, reason, price, now_utc)
                resolved_now.append(record)
                continue

            age_hours = (now_utc - record.rejected_at_utc).total_seconds() / 3600.0
            valid_until = record.evaluation.valid_until_utc
            if age_hours >= self.max_hold_hours or (valid_until and now_utc > valid_until and record.mfe == 0):
                self._resolve(record, ExitReason.EXPIRED, tick.bid if record.direction == Direction.BUY else tick.ask, now_utc, censored=True)
                resolved_now.append(record)

        return resolved_now

    def _resolve(
        self,
        record: ShadowRecord,
        reason: ExitReason,
        exit_price: float,
        now_utc: datetime,
        *,
        censored: bool = False,
    ) -> None:
        entry = record.entry_price or exit_price
        if record.direction == Direction.BUY:
            gross = exit_price - entry
        else:
            gross = entry - exit_price
        record.gross_pnl_per_lot = round(gross * self.value_per_unit, 2)
        record.exit_price = round(exit_price, 5)
        record.exit_time_utc = now_utc
        record.exit_reason = "CENSORED_" + reason.value if censored else reason.value
        record.status = ShadowTrackingStatus.CENSORED if censored else ShadowTrackingStatus.COMPLETED
        record.resolved = True
        self.records.pop(record.id, None)
        self.completed.append(record)

    # ------------------------------------------------------------------ stats
    def summary(self) -> dict:
        total = len(self.completed)
        if total == 0:
            return {"count": 0, "sample_status": "NO_DATA"}
        tp = sum(1 for r in self.completed if r.exit_reason == ExitReason.TAKE_PROFIT.value)
        sl = sum(1 for r in self.completed if r.exit_reason == ExitReason.STOP_LOSS.value)
        censored = sum(1 for r in self.completed if r.status == ShadowTrackingStatus.CENSORED)
        net = sum(r.gross_pnl_per_lot or 0.0 for r in self.completed)
        return {
            "count": total,
            "reached_tp": tp,
            "reached_sl": sl,
            "censored": censored,
            "gross_pnl_per_lot_sum": round(net, 2),
            "sample_status": "SUFFICIENT" if total >= 30 else "LOW_SAMPLE",
        }

    def by_strategy(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for record in self.completed:
            bucket = out.setdefault(
                record.evaluation.strategy_id,
                {"count": 0, "tp": 0, "sl": 0, "net": 0.0},
            )
            bucket["count"] += 1
            if record.exit_reason == ExitReason.TAKE_PROFIT.value:
                bucket["tp"] += 1
            if record.exit_reason == ExitReason.STOP_LOSS.value:
                bucket["sl"] += 1
            bucket["net"] = round(bucket["net"] + (record.gross_pnl_per_lot or 0.0), 2)
        return out
