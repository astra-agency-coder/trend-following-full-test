"""Portfolio / trades / risk / shadow / outbox / audit models (spec 13 & 43)."""
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


class PaperPortfolioRow(Base):
    __tablename__ = "paper_portfolios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    base_currency: Mapped[str] = mapped_column(String(8), default="USD")
    initial_balance: Mapped[float] = mapped_column(Numeric(18, 2))
    current_balance: Mapped[float] = mapped_column(Numeric(18, 2))
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    is_active: Mapped[bool] = mapped_column(String(1), default="1")


class PaperOrderRow(Base):
    __tablename__ = "paper_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trade_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    order_type: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    requested_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    expires_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    fill_time_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_model_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PaperTradeRow(Base):
    __tablename__ = "paper_trades"
    __table_args__ = (
        Index("ix_trades_status_opened", "status", "opened_at_utc"),
        Index("ix_trades_strategy_closed", "origin_strategy_id", "closed_at_utc"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    portfolio_id: Mapped[str] = mapped_column(ForeignKey("paper_portfolios.id"), index=True)
    origin_strategy_id: Mapped[str] = mapped_column(String(32))
    strategy_version: Mapped[str] = mapped_column(String(16))
    management_policy_version: Mapped[str] = mapped_column(String(16))
    router_decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    entry_evaluation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    opportunity_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(24))
    order_type: Mapped[str] = mapped_column(String(16), default="MARKET")
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    opened_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    stop_loss: Mapped[float] = mapped_column(Numeric(18, 5))
    take_profit: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    volume_lots: Mapped[float] = mapped_column(Numeric(18, 4))
    planned_risk_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    planned_risk_pct: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    gross_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    commission: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    swap: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    slippage_cost: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_spread_points: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    execution_model_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_calculation_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_calendar_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    entry_session_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_session_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    management_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class TradeEventRow(Base):
    __tablename__ = "trade_events"
    __table_args__ = (Index("ix_trade_events_trade_time", "trade_id", "timestamp_utc"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    trade_id: Mapped[str] = mapped_column(ForeignKey("paper_trades.id"), index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    event_type: Mapped[str] = mapped_column(String(48))
    price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    old_values: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_values: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor: Mapped[str] = mapped_column(String(32), default="SYSTEM")


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    router_decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    evaluation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    approved: Mapped[bool] = mapped_column(String(1))
    risk_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    risk_pct: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    calculated_volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    post_rounding_risk: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    checks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_config_version: Mapped[str | None] = mapped_column(String(16), nullable=True)


class RejectedOpportunityRow(Base):
    __tablename__ = "rejected_opportunities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    strategy_evaluation_id: Mapped[str] = mapped_column(String(36), index=True)
    router_decision_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    rejected_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    rejection_stage: Mapped[str] = mapped_column(String(16))
    rejection_reason_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_alternative_strategy_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shadow_tracking_status: Mapped[str] = mapped_column(String(16), default="PENDING")


class ShadowOutcomeRow(Base):
    __tablename__ = "shadow_outcomes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    rejected_opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("rejected_opportunities.id"), index=True
    )
    tracked_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    simulated_entry_time_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    simulated_entry_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    simulated_exit_time_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    simulated_exit_price: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    simulated_exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    simulated_gross_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    estimated_costs: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    simulated_net_pnl: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    simulated_r_multiple: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    max_favorable_excursion: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    max_adverse_excursion: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    calculation_version: Mapped[str | None] = mapped_column(String(16), nullable=True)


class TelegramOutboxRow(Base):
    __tablename__ = "telegram_outbox"
    __table_args__ = (
        UniqueConstraint("message_key", name="uq_outbox_message_key"),
        Index("ix_outbox_status_next", "status", "next_attempt_at_utc"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    chat_id: Mapped[str] = mapped_column(String(64))
    message_type: Mapped[str] = mapped_column(String(48))
    message_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    sent_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_time_type_corr", "timestamp_utc", "event_type", "correlation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    actor: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SystemHealthEventRow(Base):
    __tablename__ = "system_health_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    component: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)


class ServiceComponentRow(Base):
    __tablename__ = "service_components"

    component_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    component_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(String(1), default="1")
    last_heartbeat_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="STOPPED")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReportRunRow(Base):
    __tablename__ = "report_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    report_type: Mapped[str] = mapped_column(String(32))
    period_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    config_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    export_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_outbox_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class EconomicEventRow(Base):
    __tablename__ = "economic_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32))
    external_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    impact: Mapped[str | None] = mapped_column(String(16), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
