"""Operational control endpoints (frontend spec §18.12 / §17).

OWNER-only, CSRF-protected, rate-limited, idempotent and always audited.
The UI state must never change before the backend confirms (§22.4) — the
endpoints return the authoritative control state after every action.

Emergency Stop has NO endpoint by design: its contract (what happens to
open trades/services) is not defined in the backend, so no button and no
API is exposed (spec §17.4).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel

from app.persistence.database import dumps
from app.webapi.dependencies import current_user as current_user_dep
from app.webapi.dependencies import get_db, get_state, owner_with_csrf
from app.webapi.envelope import ApiError, envelope
from app.webapi import queries
from app.webapi.event_bus import make_event

router = APIRouter(prefix="/api/v1/control", tags=["control"])

_recent_idempotency: dict[str, float] = {}


class ControlRequest(BaseModel):
    reason: str | None = None


def _check_idempotency(key: str | None) -> None:
    if not key:
        return
    now = time.time()
    for k, ts in list(_recent_idempotency.items()):
        if now - ts > 600:
            _recent_idempotency.pop(k, None)
    if key in _recent_idempotency:
        raise ApiError(409, "IDEMPOTENT_REPLAY", "This command was already processed (idempotency key reuse)")
    _recent_idempotency[key] = now


@router.post("/pause")
def control_pause(
    request: Request,
    body: ControlRequest | None = None,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(owner_with_csrf),
):
    state.rate_limiter.allow(f"control:{user.username}", 10, 60)
    _check_idempotency(idempotency_key)
    if not state.has_runtime:
        raise ApiError(503, "NO_RUNTIME", "No engine runtime is attached to this API — pause requires the embedded engine")
    before = "paused" if state.runtime.paused else "running"
    reason = (body.reason if body else None) or "WEB_PAUSE"
    state.runtime.pause_opening(reason=f"{reason}:{user.username}")
    with state.database.session() as write_session:
        from app.persistence.models import AuditEventRow

        write_session.add(
            AuditEventRow(
                actor=f"WEB:{user.username}",
                event_type="WEB_PAUSE",
                entity_type="control",
                payload=dumps({"reason": reason, "idempotency_key": idempotency_key}),
            )
        )
    state.event_bus.publish(
        make_event("system.control.changed", {"system_state": "paused", "by": user.username, "reason": reason})
    )
    return envelope(request, {"action": "pause", "confirmed": True, **queries.control_state(state, session), "before_state": before})


@router.post("/resume")
def control_resume(
    request: Request,
    body: ControlRequest | None = None,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(owner_with_csrf),
):
    state.rate_limiter.allow(f"control:{user.username}", 10, 60)
    _check_idempotency(idempotency_key)
    if not state.has_runtime:
        raise ApiError(503, "NO_RUNTIME", "No engine runtime is attached to this API — resume requires the embedded engine")
    if not state.runtime.paused:
        return envelope(request, {"action": "resume", "confirmed": False, **queries.control_state(state, session), "before_state": "running", "message": "System is not paused"})
    ok, error = state.runtime.resume_opening()
    if not ok:
        raise ApiError(409, "RESUME_BLOCKED", error or "Resume rejected by the engine")
    with state.database.session() as write_session:
        from app.persistence.models import AuditEventRow

        write_session.add(
            AuditEventRow(
                actor=f"WEB:{user.username}",
                event_type="WEB_RESUME",
                entity_type="control",
                payload=dumps({"idempotency_key": idempotency_key}),
            )
        )
    state.event_bus.publish(
        make_event("system.control.changed", {"system_state": "running", "by": user.username})
    )
    return envelope(request, {"action": "resume", "confirmed": True, **queries.control_state(state, session), "before_state": "paused"})


@router.get("/status")
def control_status(request: Request, session=Depends(get_db), state=Depends(get_state), user=Depends(current_user_dep)):
    return envelope(request, queries.control_state(state, session))
