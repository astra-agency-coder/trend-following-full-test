"""Analytics endpoints (frontend spec §18.6 / §18.7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import ApiError, envelope
from app.webapi import queries
from app.webapi.params import period_window

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.get("/strategies")
def strategies(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    return envelope(request, queries.strategy_analytics(session, state, start, end))


@router.get("/strategies/{strategy_id}")
def strategy_detail(
    request: Request,
    strategy_id: str,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    dto = queries.strategy_detail(session, state, strategy_id, start, end)
    if dto is None:
        raise ApiError(404, "NOT_FOUND", "Unknown strategy")
    return envelope(request, dto)


@router.get("/strategies/{strategy_id}/performance")
def strategy_performance(
    request: Request,
    strategy_id: str,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    dto = queries.strategy_detail(session, state, strategy_id, start, end)
    if dto is None:
        raise ApiError(404, "NOT_FOUND", "Unknown strategy")
    return envelope(
        request,
        {
            "strategy_id": strategy_id,
            "performance_summary": dto["performance_summary"],
            "by_session": dto["by_session"],
            "by_regime": dto["by_regime"],
            "sample_status": dto["sample_status"],
        },
    )


@router.get("/regimes")
def regimes(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    portfolio = queries.get_portfolio(session)
    if portfolio is None:
        return envelope(request, {"by_regime": {}, "insufficient_data": True})
    trades = queries._closed_trades_in_period(state, portfolio.id, start, end)
    return envelope(
        request,
        {
            "by_regime": queries.performance_by_regime(session, state, trades),
            "insufficient_data": not trades,
            "period": {"from": queries.to_iso(start), "to": queries.to_iso(end)},
        },
    )


@router.get("/sessions")
def sessions(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    return envelope(request, queries.session_analytics_full(session, state, start, end))


@router.get("/time-heatmap")
def time_heatmap(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    timezone: str | None = Query(None, description="display timezone (IANA) — informational"),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    portfolio = queries.get_portfolio(session)
    if portfolio is None:
        return envelope(request, {"cells": [], "insufficient_data": True})
    trades = queries._closed_trades_in_period(state, portfolio.id, start, end)
    return envelope(
        request,
        {
            "cells": queries.time_heatmap(trades),
            "basis_timezone": "UTC",
            "display_timezone": timezone,
            "insufficient_data": not trades,
        },
    )


@router.get("/session-strategy")
def session_strategy(
    request: Request,
    period: str | None = Query(None),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    start, end = period_window(period, from_, to)
    portfolio = queries.get_portfolio(session)
    if portfolio is None:
        return envelope(request, {"rows": [], "insufficient_data": True})
    trades = queries._closed_trades_in_period(state, portfolio.id, start, end)
    buckets: dict[tuple[str, str], list] = {}
    for t in trades:
        key = (t.entry_session_name or "UNKNOWN", t.origin_strategy_id)
        buckets.setdefault(key, []).append(t)
    from app.reporting import metrics as perf

    rows = []
    for (sess, sid), group in buckets.items():
        m = perf.compute_metrics(group)
        rows.append(
            {
                "session": sess,
                "strategy_id": sid,
                "trades": m.total_trades,
                "net_pnl": m.net_profit if m.total_trades else None,
                "win_rate": m.win_rate,
                "expectancy": m.expectancy,
                "sample_status": m.sample_status,
            }
        )
    return envelope(request, {"rows": rows, "insufficient_data": not trades})
