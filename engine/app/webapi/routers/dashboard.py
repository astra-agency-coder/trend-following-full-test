"""Overview / dashboard endpoints (frontend spec §18.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import envelope
from app.webapi import queries
from app.webapi.params import period_window

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    return envelope(request, queries.dashboard_summary(session, state, start, end))


@router.get("/equity-curve")
def equity_curve(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    return envelope(request, queries.equity_curve(session, state, start, end))


@router.get("/recent-activity")
def recent_activity(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    session=Depends(get_db),
    user=Depends(current_user),
):
    return envelope(request, {"items": queries.recent_activity(session, limit)})


@router.get("/open-trades-preview")
def open_trades_preview(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    portfolio = queries.get_portfolio(session)
    snap = queries.latest_snapshot(session)
    rows = queries.open_trades(session, portfolio.id if portfolio else None)[:limit]
    return envelope(request, {"items": [queries.trade_list_dto(r, session, snap) for r in rows]})
