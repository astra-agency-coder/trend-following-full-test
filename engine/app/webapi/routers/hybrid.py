"""Hybrid Intelligence endpoints (frontend spec §18.4)."""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import ApiError, envelope
from app.webapi import queries
from app.webapi.params import period_window

router = APIRouter(prefix="/api/v1/hybrid", tags=["hybrid"])


@router.get("/current")
def hybrid_current(
    request: Request,
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    names = queries.strategy_names(state.config)
    row = session.execute(
        sa.select(queries.RouterDecisionRow).order_by(queries.RouterDecisionRow.timestamp_utc.desc()).limit(1)
    ).scalars().first()
    if row is None:
        return envelope(request, {"decision": None, "insufficient_data": True})
    dto = queries.decision_dto(row, session, names, light=False)
    return envelope(request, {"decision": dto, "insufficient_data": False})


@router.get("/decisions")
def hybrid_decisions(
    request: Request,
    limit: int = Query(25, ge=1, le=100),
    cursor: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    names = queries.strategy_names(state.config)
    return envelope(request, queries.decision_list(session, names, limit=limit, cursor=cursor))


@router.get("/decisions/{decision_id}")
def hybrid_decision_detail(
    request: Request,
    decision_id: str,
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    names = queries.strategy_names(state.config)
    row = session.get(queries.RouterDecisionRow, decision_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Decision not found")
    dto = queries.decision_dto(row, session, names, light=False)
    # related trade if any
    trade = session.execute(
        sa.select(queries.PaperTradeRow).where(queries.PaperTradeRow.router_decision_id == decision_id).limit(1)
    ).scalars().first()
    dto["related_trade_id"] = trade.id if trade else None
    return envelope(request, dto)


@router.get("/strategy-evaluations")
def strategy_evaluations(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    strategy_id: str | None = Query(None),
    status: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    names = queries.strategy_names(state.config)
    stmt = sa.select(queries.StrategyEvaluationRow).order_by(queries.StrategyEvaluationRow.timestamp_utc.desc())
    if strategy_id:
        stmt = stmt.where(queries.StrategyEvaluationRow.strategy_id == strategy_id)
    if status:
        stmt = stmt.where(queries.StrategyEvaluationRow.status == status.upper())
    start, end = queries.parse_dt(from_), queries.parse_dt(to)
    if start:
        stmt = stmt.where(queries.StrategyEvaluationRow.timestamp_utc >= start)
    if end:
        stmt = stmt.where(queries.StrategyEvaluationRow.timestamp_utc <= end)
    rows = session.execute(stmt.limit(limit)).scalars().all()
    return envelope(request, {"items": [queries.evaluation_dto(r, names) for r in rows]})


@router.get("/strategy-switches")
def strategy_switches(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    names = queries.strategy_names(state.config)
    return envelope(request, {"items": queries.strategy_switches(session, names, limit=limit)})
