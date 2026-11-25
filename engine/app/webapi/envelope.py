"""Web API envelope + error handling (frontend spec §2.4 / §23).

Every response is wrapped in the unified envelope:

    {"data": ..., "meta": {request_id, generated_at, schema_version}, "error": null}

Errors never leak stack traces, SQL or secrets — only a stable code, a
human message and optional safe details.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

SCHEMA_VERSION = "1.0"


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def envelope(request: Request, data) -> dict:
    return {
        "data": data,
        "meta": {
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "generated_at": now_iso(),
            "schema_version": SCHEMA_VERSION,
        },
        "error": None,
    }


def error_envelope(request: Request, code: str, message: str, details: dict | None = None) -> dict:
    return {
        "data": None,
        "meta": {
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "generated_at": now_iso(),
            "schema_version": SCHEMA_VERSION,
        },
        "error": {"code": code, "message": message, "details": details or None},
    }


class ApiError(Exception):
    """Internal API error mapped to a safe envelope (never a stack trace)."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(request, exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        fields = {}
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", []) if p != "query")
            fields[loc] = err.get("msg", "invalid")
        return JSONResponse(
            status_code=422,
            content=error_envelope(request, "VALIDATION_ERROR", "Invalid request parameters", fields),
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(request: Request, exc: Exception):
        # Log server-side only; client gets a generic safe message.
        import logging

        logging.getLogger("webapi").exception("unhandled API error")
        return JSONResponse(
            status_code=500,
            content=error_envelope(request, "INTERNAL_ERROR", "Unexpected server error"),
        )
