"""Shared application state for the Web API layer.

``AppState`` owns the Database, optional embedded HybridRuntime (when the
API runs inside the engine process) and the SSE event bus. Query endpoints
read the database directly — the database is the source of truth; control
endpoints act on the embedded runtime and always write an audit event.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.persistence.database import Database
from app.webapi.event_bus import EventBus
from app.webapi.security import RateLimiter, SessionManager, WebUser, load_users, resolve_secret


@dataclass
class AppState:
    database: Database
    config: dict
    run_mode: str = "STANDALONE"  # EMBEDDED_ENGINE | STANDALONE | BACKTEST
    runtime: object | None = None  # HybridRuntime | None
    event_bus: EventBus = field(default_factory=EventBus)
    session_manager: SessionManager | None = None
    users: list[WebUser] = field(default_factory=list)
    users_using_defaults: bool = False
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    started_at_utc: str | None = None
    engine_thread: threading.Thread | None = None
    engine_stop: threading.Event = field(default_factory=threading.Event)
    engine_error: str | None = None

    def find_user(self, username: str) -> WebUser | None:
        for user in self.users:
            if user.username == username:
                return user
        return None

    @property
    def has_runtime(self) -> bool:
        return self.runtime is not None


def build_state(database: Database, config: dict, run_mode: str = "STANDALONE", runtime=None) -> AppState:
    users, using_defaults = load_users()
    return AppState(
        database=database,
        config=config,
        run_mode=run_mode,
        runtime=runtime,
        session_manager=SessionManager(resolve_secret()),
        users=users,
        users_using_defaults=using_defaults,
    )
