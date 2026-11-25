"""Database session management and migration runner.

The spec mandates PostgreSQL + Alembic for production. For development and
CI (this sandbox), the same schema runs on SQLite; the repository layer is
backend-agnostic. Migrations are applied through a lightweight, ordered SQL
script runner that works on both backends, and an Alembic setup is provided
for PostgreSQL deployments (``deploy/alembic``).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core.exceptions import DatabaseError
from app.persistence.models import Base, create_engine, sessionmaker

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # pragma: no cover - driver glue
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def _configure_postgres(engine: Engine) -> None:
    # NUMERIC with asdecimal handled by SQLAlchemy; nothing extra needed.
    return None


def create_db_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})
        _configure_sqlite(engine)
    else:
        engine = create_engine(url, pool_pre_ping=True)
    return engine


class Database:
    """Owns the engine/session factory and schema lifecycle."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_db_engine(url)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Any]:
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    def healthy(self) -> bool:
        try:
            with self.session() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------- migrations
class MigrationRunner:
    """Ordered, checkpointed migrations from app/persistence/migrations/*.sql.

    Each file must be named ``NNNN_description.sql`` and may contain
    ``-- +migrate`` separated statements. Applied migrations are recorded in
    ``schema_migrations``; re-running is a no-op (idempotent for tests).
    """

    def __init__(self, engine: Engine, migrations_dir: Path | None = None) -> None:
        self.engine = engine
        self.migrations_dir = migrations_dir or MIGRATIONS_DIR

    def _ensure_table(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "  version VARCHAR(32) PRIMARY KEY,"
                    "  applied_at_utc TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
            )

    def applied(self) -> list[str]:
        self._ensure_table()
        with self.engine.begin() as conn:
            rows = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).fetchall()
        return [r[0] for r in rows]

    def pending(self) -> list[Path]:
        applied = set(self.applied())
        out: list[Path] = []
        for path in sorted(self.migrations_dir.glob("*.sql")):
            if path.stem not in applied:
                out.append(path)
        return out

    def upgrade(self) -> list[str]:
        """Apply pending migrations; returns the applied version names."""
        self._ensure_table()
        applied_now: list[str] = []
        for path in self.pending():
            statements = [s.strip() for s in path.read_text(encoding="utf-8").split(";") if s.strip()]
            with self.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
                conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                    {"v": path.stem},
                )
            applied_now.append(path.stem)
        return applied_now

    @staticmethod
    def ensure_schema(engine: Engine) -> None:
        """Fallback for fresh DBs: create everything from metadata.

        Used by tests and first boot before SQL migrations exist.
        """
        Base.metadata.create_all(engine)


def dumps(value: Any) -> str:
    """Canonical JSON for Text columns (stable key order)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def loads(raw: str | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
