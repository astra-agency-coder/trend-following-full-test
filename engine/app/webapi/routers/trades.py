"""Trades endpoints (frontend spec §18.5) — read-only, no manual order creation."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request

from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import ApiError, envelope
from app.webapi import queries
from app.webapi.params import period_window

router = APIRouter(prefix="/api/v1/trades", tags=["trades"])


@router.get("")
def list_trades(
    request: Request,
    status: str | None = Query(None),  # open | closed | all
    strategy_id: str | None = Query(None),
    side: str | None = Query(None),
    session_name: str | None = Query(None),
    exit_reason: str | None = Query(None),
    result: str | None = Query(None),  # winning | losing | breakeven
    trade_id: str | None = Query(None),
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    portfolio = queries.get_portfolio(session)
    snap = queries.latest_snapshot(session)
    stmt = sa.select(queries.PaperTradeRow).order_by(queries.PaperTradeRow.opened_at_utc.desc().nullslast())
    if portfolio:
        stmt = stmt.where(queries.PaperTradeRow.portfolio_id == portfolio.id)
    if status == "open":
        stmt = stmt.where(queries.PaperTradeRow.status.in_(["OPEN", "PENDING", "PARTIALLY_CLOSED"]))
    elif status == "closed":
        stmt = stmt.where(queries.PaperTradeRow.status.like("CLOSED%"))
    if strategy_id:
        stmt = stmt.where(queries.PaperTradeRow.origin_strategy_id == strategy_id)
    if side:
        stmt = stmt.where(queries.PaperTradeRow.side == side.upper())
    if session_name:
        stmt = stmt.where(queries.PaperTradeRow.entry_session_name == session_name)
    if exit_reason:
        stmt = stmt.where(queries.PaperTradeRow.exit_reason == exit_reason.upper())
    if trade_id:
        stmt = stmt.where(queries.PaperTradeRow.id.contains(trade_id))
    if start:
        stmt = stmt.where(queries.PaperTradeRow.opened_at_utc >= start)
    if end:
        stmt = stmt.where(queries.PaperTradeRow.opened_at_utc <= end)
    total = session.execute(sa.select(sa.func.count()).select_from(stmt.subquery())).scalar() or 0
    rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    dtos = [queries.trade_list_dto(r, session, snap) for r in rows]
    if result == "winning":
        dtos = [d for d in dtos if (d["realized_pnl"] or 0) > 0]
    elif result == "losing":
        dtos = [d for d in dtos if (d["realized_pnl"] or 0) < 0]
    elif result == "breakeven":
        dtos = [d for d in dtos if d["realized_pnl"] is not None and d["realized_pnl"] == 0]
    return envelope(
        request,
        {
            "items": dtos,
            "total_count": total,
            "offset": offset,
            "limit": limit,
        },
    )


@router.get("/{trade_id}")
def trade_detail(
    request: Request,
    trade_id: str,
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    names = queries.strategy_names(state.config)
    snap = queries.latest_snapshot(session)
    dto = queries.trade_detail(session, trade_id, snap, names)
    if dto is None:
        raise ApiError(404, "NOT_FOUND", "Trade not found")
    return envelope(request, dto)


@router.get("/{trade_id}/events")
def trade_events(
    request: Request,
    trade_id: str,
    session=Depends(get_db),
    user=Depends(current_user),
):
    import sqlalchemy as sa

    rows = session.execute(
        sa.select(queries.TradeEventRow)
        .where(queries.TradeEventRow.trade_id == trade_id)
        .order_by(queries.TradeEventRow.timestamp_utc.asc())
    ).scalars().all()
    return envelope(
        request,
        {
            "items": [
                {
                    "event_id": r.id,
                    "time": queries.to_iso(r.timestamp_utc),
                    "event_type": r.event_type,
                    "price": queries.fl(r.price),
                    "old_values": queries.jload(r.old_values),
                    "new_values": queries.jload(r.new_values),
                    "reason": r.reason,
                    "actor": r.actor,
                }
                for r in rows
            ]
        },
    )


@router.get("/{trade_id}/chart")
def trade_chart(
    request: Request,
    trade_id: str,
    timeframe: str = Query("M15"),
    session=Depends(get_db),
    user=Depends(current_user),
):
    data = queries.trade_chart(session, trade_id, timeframe)
    if data is None:
        raise ApiError(404, "NOT_FOUND", "Trade not found or has no chart data")
    available = queries.available_timeframes(session, data["trade"]["symbol"] if "trade" in data else "XAUUSD")
    data["available_timeframes"] = available
    return envelope(request, data)
