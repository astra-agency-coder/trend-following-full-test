"""Append-only audit trail with hash chaining (spec 12.3 / 43.3).

- Every audit event links to the previous one via ``previous_event_hash``.
- ``event_hash = sha256(index|timestamp|actor|event_type|entity|payload|previous_hash)``.
- Trading modules can only append; no update/delete helpers exist here.
- A verification function detects tampering (test 49.5-8).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.enums import Actor
from app.persistence.database import Database, dumps
from app.persistence.models import AuditEventRow


class AuditJournal:
    def __init__(self, database: Database) -> None:
        self.db = database

    @staticmethod
    def _iso(dt: datetime) -> str:
        """Normalise to UTC-aware isoformat (SQLite drops tzinfo on round-trip)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()

    def _hash_event(
        self,
        index: int,
        timestamp: str,
        actor: str,
        event_type: str,
        entity_type: str | None,
        entity_id: str | None,
        payload: str | None,
        previous_hash: str | None,
    ) -> str:
        material = "|".join(
            [
                str(index),
                timestamp,
                actor,
                event_type,
                entity_type or "",
                entity_id or "",
                payload or "",
                previous_hash or "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def append(
        self,
        event_type: str,
        *,
        actor: str = Actor.SYSTEM.value,
        entity_type: str | None = None,
        entity_id: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp_utc: datetime | None = None,
    ) -> int:
        """Append one audit event and return its row id."""
        with self.db.session() as session:
            last = session.execute(
                select(AuditEventRow).order_by(AuditEventRow.id.desc()).limit(1)
            ).scalar_one_or_none()
            previous_hash = last.event_hash if last else None
            next_index = (last.id + 1) if last else 1

            ts = timestamp_utc or datetime.now(UTC)
            ts_iso = self._iso(ts)
            payload_json = dumps(payload) if payload else None

            event_hash = self._hash_event(
                next_index, ts_iso, actor, event_type, entity_type, entity_id, payload_json, previous_hash
            )
            row = AuditEventRow(
                timestamp_utc=ts,
                actor=actor,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                correlation_id=correlation_id,
                payload=payload_json,
                previous_event_hash=previous_hash,
                event_hash=event_hash,
            )
            session.add(row)
            session.flush()
            return row.id

    def verify_chain(self) -> bool:
        """Recompute the full chain; False if any record was altered/removed."""
        with self.db.session() as session:
            rows = session.execute(select(AuditEventRow).order_by(AuditEventRow.id)).scalars().all()
        previous_hash = None
        for expected_index, row in enumerate(rows, start=1):
            if row.previous_event_hash != previous_hash:
                return False
            recomputed = self._hash_event(
                expected_index,
                self._iso(row.timestamp_utc),
                row.actor,
                row.event_type,
                row.entity_type,
                row.entity_id,
                row.payload,
                previous_hash,
            )
            if recomputed != row.event_hash:
                return False
            previous_hash = row.event_hash
        return True

    def count(self) -> int:
        with self.db.session() as session:
            return session.execute(select(func.count(AuditEventRow.id))).scalar_one()
