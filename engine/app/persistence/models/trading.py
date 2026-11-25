"""Market context / strategy / router / trade models (spec 13 & 43)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.models.base import Base, _uuid, func_now


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_snapshots_time_session", "timestamp_utc", "session_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    symbol: Mapped[str] = mapped_column(String(32))
    source_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_quality: Mapped[str] = mapped_column(String(16))
    trend_state: Mapped[str] = mapped_column(String(16))
    volatility_state: Mapped[str] = mapped_column(String(16))
    activity_state: Mapped[str] = mapped_column(String(16))
    liquidity_proxy_state: Mapped[str] = mapped_column(String(16))
    market_structure: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    session_name: Mapped[str] = mapped_column(String(32), index=True)
    session_phase: Mapped[str] = mapped_column(String(32))
    session_local_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_session_overlap: Mapped[bool] = mapped_column(String(1), default="0")
    spread_points: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    bid: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    ask: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    feature_values: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    news_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_version: Mapped[str] = mapped_column(String(16))
    config_version_id: Mapped[str | None] = mapped_column(String(36))


class SessionInstanceRow(Base):
    __tablename__ = "session_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_name: Mapped[str] = mapped_column(String(32))
    timezone_name: Mapped[str] = mapped_column(String(64))
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    local_date: Mapped[str] = mapped_column(String(16))
    calendar_version: Mapped[str] = mapped_column(String(16))
    is_overlap: Mapped[bool] = mapped_column(String(1), default="0")


class StrategyEvaluationRow(Base):
    __tablename__ = "strategy_evaluations"
    __table_args__ = (Index("ix_eval_strategy_time_status", "strategy_id", "timestamp_utc", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    market_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    strategy_id: Mapped[str] = mapped_column(String(32))
    strategy_version: Mapped[str] = mapped_column(String(16))
    config_version_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))
    setup_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    setup_stage: Mapped[str] = mapped_column(String(16), default="NONE")
    market_fit_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    setup_quality_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    proposed_entry: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    proposed_sl: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    proposed_tp: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    risk_reward: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    valid_until_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timeframes: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditions_passed: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditions_failed: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_breakdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    feature_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_result: Mapped[str | None] = mapped_column(Text, nullable=True)


class RouterDecisionRow(Base):
    __tablename__ = "router_decisions"
    __table_args__ = (
        Index("ix_router_time_status_strategy", "timestamp_utc", "decision_status", "selected_strategy_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    market_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    router_version: Mapped[str] = mapped_column(String(16))
    config_version_id: Mapped[str | None] = mapped_column(String(36))
    opportunity_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    selected_strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    selected_evaluation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decision_status: Mapped[str] = mapped_column(String(32))
    ranked_candidates: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    hard_blocks: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation_ar: Mapped[str | None] = mapped_column(Text, nullable=True)


class RouterCandidateRankingRow(Base):
    __tablename__ = "router_candidate_rankings"
    __table_args__ = (UniqueConstraint("router_decision_id", "evaluation_id", name="uq_ranking"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    router_decision_id: Mapped[str] = mapped_column(ForeignKey("router_decisions.id"), index=True)
    evaluation_id: Mapped[str] = mapped_column(String(36), index=True)
    strategy_id: Mapped[str] = mapped_column(String(32))
    rank_position: Mapped[int] = mapped_column(Integer)
    eligible: Mapped[bool] = mapped_column(String(1), default="1")
    score_total: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    score_components: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
