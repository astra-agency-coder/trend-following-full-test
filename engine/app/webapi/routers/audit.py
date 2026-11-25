"""Audit log endpoints (frontend spec §18.9) — strictly read-only.

No update/delete endpoints exist by design: the audit chain is append-only
and the UI must never modify it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.webapi.dependencies import current_user, get_db, get_state, rate_limit
from app.webapi.envelope import ApiError, envelope
from app.webapi import queries

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get("/events")
def audit_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    event_type: str | None = Query(None),
    severity: str | None = Query(None),
    entity_id: str | None = Query(None),
    session=Depends(get_db),
    user=Depends(current_user),
):
    data = queries.audit_events(
        session, limit=limit, cursor=cursor, event_type=event_type, severity=severity, entity_id=entity_id
    )
    return envelope(request, data)


@router.get("/events/{event_id}")
def audit_event_detail(
    request: Request,
    event_id: int,
    session=Depends(get_db),
    user=Depends(current_user),
):
    from app.persistence.models import AuditEventRow

    row = session.get(AuditEventRow, event_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Audit event not found")
    return envelope(request, queries.audit_event_dto(row))


@router.get("/export")
def audit_export(
    request: Request,
    format: str = Query("json", pattern="^(json|csv)$"),
    limit: int = Query(500, ge=1, le=5000),
    user=Depends(current_user),
    session=Depends(get_db),
    state=Depends(get_state),
):
    # rate-limited, authenticated export (spec §22.3)
    limiter = rate_limit("audit-export", 6, 60)
    limiter(request, state)
    data = queries.audit_events(session, limit=limit, cursor=None, event_type=None, severity=None, entity_id=None)
    items = data["items"]
    if format == "json":
        import json

        body = json.dumps({"audit_events": items, "exported_at": queries.to_iso(__import__("datetime").datetime.now(__import__("datetime").UTC))}, ensure_ascii=False, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="audit_export.json"'},
        )
    lines = ["event_id,event_time,actor,event_type,entity_type,entity_id,correlation_id"]
    for it in items:
        lines.append(
            ",".join(
                [
                    it["event_id"],
                    it["event_time"] or "",
                    it["actor"] or "",
                    it["event_type"],
                    it["entity_type"] or "",
                    it["entity_id"] or "",
                    it["correlation_id"] or "",
                ]
            )
        )
    return PlainTextResponse(
        content="\n".join(lines),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit_export.csv"', "X-Content-Type-Options": "nosniff"},
    )
