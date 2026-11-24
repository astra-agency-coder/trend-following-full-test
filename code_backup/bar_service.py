"""Multi-timeframe bar buffers + persistence (spec 5.2 / 33).

``BarService`` keeps in-memory rolling windows of closed bars per timeframe
(no recalculation of full history on every tick — spec 51.5) and upserts
them into ``market_bars`` with unique (symbol, timeframe, open_time).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.constants import TIMEFRAME_SECONDS
from app.core.logging_setup import get_logger
from app.data.contracts import MarketBar
from app.data.validators import detect_bar_gaps, validate_bar
from app.persistence.database import Database
from app.persistence.models import MarketBarRow

logger = get_logger("data.bar_service")

MAX_BUFFER = 600  # bars per timeframe in memory


class BarService:
    def __init__(self, database: Database | None = None, persist: bool = True) -> None:
        self.database = database
        self.persist = persist and database is not None
        self._buffers: dict[str, dict[datetime, MarketBar]] = defaultdict(dict)
        self._gaps: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)

    def ingest_bars(self, bars: list[MarketBar], *, detect_gaps: bool = True) -> dict:
        """Add bars to buffers; returns stats {added, updated, invalid, gaps}."""
        stats = {"added": 0, "updated": 0, "invalid": 0, "gaps": 0}
        by_tf: dict[str, list[MarketBar]] = defaultdict(list)
        for bar in bars:
            ok, flags = validate_bar(bar)
            if not ok:
                stats["invalid"] += 1
                logger.warning("invalid bar rejected", extra={"context": {"bar_id": bar.bar_id, "flags": flags}})
                continue
            by_tf[bar.timeframe].append(bar)

        for timeframe, tf_bars in by_tf.items():
            buffer = self._buffers[timeframe]
            for bar in tf_bars:
                existing = buffer.get(bar.open_time_utc)
                if existing is None:
                    stats["added"] += 1
                elif (
                    existing.open == bar.open
                    and existing.high == bar.high
                    and existing.low == bar.low
                    and existing.close == bar.close
                ):
                    continue  # exact duplicate — ignore
                else:
                    stats["updated"] += 1  # data revision
                buffer[bar.open_time_utc] = bar
            # trim buffer
            if len(buffer) > MAX_BUFFER:
                keep = sorted(buffer.keys())[-MAX_BUFFER:]
                self._buffers[timeframe] = {k: buffer[k] for k in keep}
            if detect_gaps:
                new_gaps = self._recompute_gaps(timeframe)
                stats["gaps"] += len(new_gaps)

        if self.persist and by_tf:
            self._upsert(list(bar for bars_ in by_tf.values() for bar in bars_))
        return stats

    def _recompute_gaps(self, timeframe: str) -> list[tuple[datetime, datetime]]:
        bars = self.closed_bars(timeframe)
        gaps = detect_bar_gaps(bars, timeframe)
        self._gaps[timeframe] = gaps
        return gaps

    def closed_bars(self, timeframe: str, *, as_of: datetime | None = None) -> list[MarketBar]:
        """Bars closed strictly before ``as_of`` (defaults: all complete)."""
        buffer = self._buffers.get(timeframe, {})
        bars = [b for b in buffer.values() if b.is_complete]
        if as_of is not None:
            bars = [b for b in bars if b.close_time_utc <= as_of]
        return sorted(bars, key=lambda b: b.open_time_utc)

    def last_bar(self, timeframe: str) -> MarketBar | None:
        bars = self.closed_bars(timeframe)
        return bars[-1] if bars else None

    def gaps(self, timeframe: str) -> list[tuple[datetime, datetime]]:
        return list(self._gaps.get(timeframe, []))

    def _upsert(self, bars: list[MarketBar]) -> None:
        assert self.database is not None
        with self.database.session() as session:
            for bar in bars:
                row = session.execute(
                    select(MarketBarRow).where(
                        MarketBarRow.canonical_symbol == bar.canonical_symbol,
                        MarketBarRow.timeframe == bar.timeframe,
                        MarketBarRow.open_time_utc == bar.open_time_utc,
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        MarketBarRow(
                            canonical_symbol=bar.canonical_symbol,
                            timeframe=bar.timeframe,
                            open_time_utc=bar.open_time_utc,
                            close_time_utc=bar.close_time_utc,
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            tick_volume=bar.tick_volume,
                            spread=bar.spread,
                            source_symbol=bar.source_symbol,
                            is_complete=bar.is_complete,
                            source_type=bar.source_type,
                        )
                    )
                else:
                    row.high = bar.high
                    row.low = bar.low
                    row.close = bar.close
                    row.open = bar.open
                    row.tick_volume = bar.tick_volume
                    row.spread = bar.spread
                    row.is_complete = bar.is_complete

    def load_history_from_db(
        self,
        canonical_symbol: str,
        timeframe: str,
        *,
        limit: int = MAX_BUFFER,
    ) -> int:
        """Restore buffers after restart from persisted bars."""
        assert self.database is not None
        with self.database.session() as session:
            rows = (
                session.execute(
                    select(MarketBarRow)
                    .where(
                        MarketBarRow.canonical_symbol == canonical_symbol,
                        MarketBarRow.timeframe == timeframe,
                    )
                    .order_by(MarketBarRow.open_time_utc.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        for row in reversed(rows):
            self._buffers[timeframe][row.open_time_utc] = MarketBar(
                canonical_symbol=row.canonical_symbol,
                source_symbol=row.source_symbol or "UNKNOWN",
                timeframe=row.timeframe,
                open_time_utc=row.open_time_utc,
                close_time_utc=row.close_time_utc,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                tick_volume=float(row.tick_volume) if row.tick_volume else None,
                spread=float(row.spread) if row.spread else None,
                is_complete=bool(row.is_complete),
                source_type=row.source_type,
            )
        return len(rows)
