"""Rejected opportunities & shadow tracking endpoints (frontend spec §18.8).

All shadow results are marked ``counterfactual: true`` — they must never be
mixed into portfolio equity/P&L (spec §12.2).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import ApiError, envelope
from app.webapi import queries
from app.webapi.params import period_window

router = APIRouter(prefix="/api/v1/opportunities", tags=["opportunities"])


@router.get("/rejected")
def rejected_opportunities(
    request: Request,
    strategy_id: str | None = Query(None),
    outcome: str | None = Query(None),  # favorable | unfavorable | incomplete
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    return envelope(
        request,
        queries.rejected_opportunities(
            session, state, start, end, limit=limit, cursor=cursor, strategy_id=strategy_id, outcome=outcome
        ),
    )


@router.get("/{opportunity_id}")
def opportunity_detail(
    request: Request,
    opportunity_id: str,
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    dto = queries.opportunity_detail(session, state, opportunity_id)
    if dto is None:
        raise ApiError(404, "NOT_FOUND", "Opportunity not found")
    return envelope(request, dto)


@router.get("/{opportunity_id}/shadow")
def opportunity_shadow(
    request: Request,
    opportunity_id: str,
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    dto = queries.opportunity_detail(session, state, opportunity_id)
    if dto is None:
        raise ApiError(404, "NOT_FOUND", "Opportunity not found")
    return envelope(request, {"shadow_outcome": dto.get("shadow_outcome"), "shadow_status": dto.get("shadow_status"), "counterfactual": True})
