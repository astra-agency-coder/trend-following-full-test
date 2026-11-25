"""GOLD HYBRID ENGINE Web API — FastAPI application factory.

The API is part of the Python backend (frontend spec §2.3): it reads the
same database, exposes versioned ``/api/v1`` DTOs and forwards control
actions to the embedded runtime. No trading logic lives here.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer  # noqa: F401  (OpenAPI doc helper)

from app.webapi.dependencies import set_state
from app.webapi.envelope import SCHEMA_VERSION, envelope, register_error_handlers
from app.webapi.state import AppState, build_state
from app.webapi.envelope import RequestIDMiddleware


def create_app(
    database=None,
    config: dict | None = None,
    runtime=None,
    run_mode: str = "STANDALONE",
    cors_origins: list[str] | None = None,
) -> FastAPI:
    from app.persistence.database import Database
    from app.core.config import ConfigLoader

    if database is None:
        import os

        database = Database(os.environ.get("DATABASE_URL", "sqlite:///gold_hybrid.db"))
    if config is None:
        config = ConfigLoader().load().data

    state = build_state(database, config, run_mode=run_mode, runtime=runtime)
    set_state(state)

    from datetime import UTC, datetime

    state.started_at_utc = datetime.now(UTC).isoformat()

    app = FastAPI(
        title="GOLD HYBRID ENGINE Web API",
        version=SCHEMA_VERSION,
        description="Read-only dashboard API + safe operational control for the XAUUSD hybrid paper-trading engine.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    if cors_origins is None:
        cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "Idempotency-Key"],
        )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    app.add_middleware(RequestIDMiddleware)
    register_error_handlers(app)

    # ---------------------------------------------------------------- routers
    from app.webapi.routers import (
        analytics,
        audit,
        auth,
        control,
        dashboard,
        hybrid,
        market,
        opportunities,
        reports,
        stream,
        system,
        trades,
    )

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(market.router)
    app.include_router(hybrid.router)
    app.include_router(trades.router)
    app.include_router(analytics.router)
    app.include_router(opportunities.router)
    app.include_router(audit.router)
    app.include_router(reports.router)
    app.include_router(system.router)
    app.include_router(control.router)
    app.include_router(stream.router)

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        import os

        return {
            "status": "ok",
            "app": "GOLD_HYBRID_ENGINE",
            "paper_only": True,
            "pid": os.getpid(),
        }

    @app.get("/api/v1/system/state")
    def system_state():
        return {
            "run_mode": state.run_mode,
            "runtime_attached": state.has_runtime,
            "using_default_credentials": state.users_using_defaults,
        }

    @app.on_event("shutdown")
    def _stop_engine():
        if state.engine_thread is not None and state.engine_thread.is_alive():
            state.engine_thread.shutdown()
            state.engine_thread.join(timeout=10)

    return app
