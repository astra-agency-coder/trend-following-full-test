"""SQLAlchemy models — the full schema from spec sections 13 & 43.

Design notes:
- UUID primary keys as ``String(36)`` for SQLite/PostgreSQL portability.
- Prices/money as ``Numeric(18, 5)`` (never float, spec 43.2).
- JSON payloads as ``Text`` storing canonical JSON (portable across both
  databases; SQLAlchemy's JSON type is not needed here and keeps the
  migration surface identical between backends).
- Append-only audit table with a hash chain (spec 12.3).
"""
from __future__ import annotations

import uuid
from datetime import datetime, UTC

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

func_now = lambda: datetime.now(UTC)  # noqa: E731


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- run / config
class SystemRun(Base):
    __tablename__ = "system_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    stopped_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    app_version: Mapped[str] = mapped_column(String(32))
    host_name: Mapped[str] = mapped_column(String(255))
    run_status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_version_id: Mapped[str | None] = mapped_column(String(36), index=True)


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    config_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    source: Mapped[str] = mapped_column(String(32), default="FILE")
    config_json: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- symbols
class SymbolSpec(Base):
    __tablename__ = "symbol_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    broker_name: Mapped[str] = mapped_column(String(128), default="UNKNOWN")
    source_symbol: Mapped[str] = mapped_column(String(64))
    canonical_symbol: Mapped[str] = mapped_column(String(32), index=True)
    digits: Mapped[int] = mapped_column(Integer)
    point: Mapped[float] = mapped_column(Numeric(18, 8))
    tick_size: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    tick_value: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    contract_size: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume_min: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume_max: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    volume_step: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    currency_base: Mapped[str | None] = mapped_column(String(16), nullable=True)
    currency_profit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    captured_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    raw_spec_json: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- market data
class MarketTickRow(Base):
    __tablename__ = "market_ticks"
    __table_args__ = (UniqueConstraint("source_symbol", "timestamp_utc", "bid", "ask", name="uq_tick"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_symbol: Mapped[str] = mapped_column(String(64), index=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    bid: Mapped[float] = mapped_column(Numeric(18, 5))
    ask: Mapped[float] = mapped_column(Numeric(18, 5))
    last: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    tick_volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)


class MarketBarRow(Base):
    __tablename__ = "market_bars"
    __table_args__ = (
        UniqueConstraint("canonical_symbol", "timeframe", "open_time_utc", name="uq_bar"),
        Index("ix_bars_symbol_tf_time", "canonical_symbol", "timeframe", "open_time_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    open_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Numeric(18, 5))
    high: Mapped[float] = mapped_column(Numeric(18, 5))
    low: Mapped[float] = mapped_column(Numeric(18, 5))
    close: Mapped[float] = mapped_column(Numeric(18, 5))
    tick_volume: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    spread: Mapped[float | None] = mapped_column(Numeric(18, 5), nullable=True)
    source_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    source_type: Mapped[str] = mapped_column(String(32), default="BROKER_BAR")


class DataImportJobRow(Base):
    __tablename__ = "data_import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func_now)
    completed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    records_seen: Mapped[int] = mapped_column(Integer, default=0)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    gaps_detected: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
