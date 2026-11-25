"""Market endpoints (frontend spec §18.3)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request

from app.core.constants import CANONICAL_SYMBOL
from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import envelope
from app.webapi import queries

router = APIRouter(prefix="/api/v1/market", tags=["market"])


@router.get("/snapshot")
def market_snapshot(
    request: Request,
    symbol: str = Query(CANONICAL_SYMBOL),
    session=Depends(get_db),
    state=Depends(get_state),
    user=Depends(current_user),
):
    snap = queries.latest_snapshot(session, symbol)
    return envelope(
        request,
        {
            "snapshot": snap,
            "symbol": symbol,
            "available_timeframes": queries.available_timeframes(session, symbol),
            "demo_data": queries.version_info(state)["demo_data"],
        },
    )


@router.get("/bars")
def market_bars(
    request: Request,
    symbol: str = Query(CANONICAL_SYMBOL),
    timeframe: str = Query("M15"),
    limit: int = Query(400, ge=10, le=1000),
    user=Depends(current_user),
    session=Depends(get_db),
):
    available = queries.available_timeframes(session, symbol)
    if timeframe not in available:
        return envelope(
            request,
            {"bars": [], "timeframe": timeframe, "available_timeframes": available, "insufficient_data": True},
        )
    data = queries.bars(session, symbol, timeframe, limit=limit)
    return envelope(request, {"bars": data, "timeframe": timeframe, "available_timeframes": available})


@router.get("/annotations")
def market_annotations(
    request: Request,
    symbol: str = Query(CANONICAL_SYMBOL),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
    user=Depends(current_user),
    session=Depends(get_db),
):
    def _dt(v: str | None) -> datetime | None:
        if not v:
            return None
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d.replace(tzinfo=UTC) if d.tzinfo is None else d
        except ValueError:
            return None

    annotations = queries.market_annotations(session, symbol, _dt(from_), _dt(to))
    return envelope(request, {"annotations": annotations[:200]})


@router.get("/sessions/current")
def sessions_current(
    request: Request,
    state=Depends(get_state),
    user=Depends(current_user),
    session=Depends(get_db),
):
    from app.market_context.sessions import SessionEngine

    engine = SessionEngine.from_config(state.config.get("sessions", {}))
    info = engine.evaluate(datetime.now(UTC))
    snap = queries.latest_snapshot(session)
    return envelope(
        request,
        {
            "session_name": info.session_name,
            "phase": info.phase.value,
            "local_time": info.local_time,
            "timezone_name": info.timezone_name,
            "is_overlap": info.is_overlap,
            "active_sessions": list(info.active_sessions),
            "session_start_utc": queries.to_iso(info.session_start_utc),
            "session_end_utc": queries.to_iso(info.session_end_utc),
            "definitions": [
                {
                    "name": d.name,
                    "timezone": d.timezone_name,
                    "local_start": d.local_start.isoformat(),
                    "local_end": d.local_end.isoformat(),
                }
                for d in engine.definitions.values()
            ],
            "last_snapshot_session": snap.get("session") if snap else None,
        },
    )


@router.get("/sessions/calendar")
def sessions_calendar(
    request: Request,
    days: int = Query(2, ge=1, le=14),
    state=Depends(get_state),
    user=Depends(current_user),
):
    """Session boundaries derived from the backend session engine (IANA/DST aware)."""
    from app.market_context.sessions import SessionEngine

    engine = SessionEngine.from_config(state.config.get("sessions", {}))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    segments: dict[str, list] = {}
    previous: set[str] = set()
    moment = now
    for _ in range(days * 24 * 4):  # 15-minute resolution
        info = engine.evaluate(moment)
        active = set(info.active_sessions)
        for name in active:
            if name in previous:
                segments[name][-1]["end"] = moment.isoformat()
            else:
                segments.setdefault(name, []).append({"start": moment.isoformat(), "end": moment.isoformat()})
        previous = active
        moment += timedelta(minutes=15)
    return envelope(request, {"segments": segments, "generated_for_days": days})
