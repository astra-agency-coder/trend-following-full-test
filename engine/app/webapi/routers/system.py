"""System health & version endpoints (frontend spec §18.11 / §15)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.webapi.dependencies import current_user, get_db, get_state
from app.webapi.envelope import envelope
from app.webapi import queries

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/health")
def system_health(request: Request, session=Depends(get_db), state=Depends(get_state), user=Depends(current_user)):
    return envelope(request, queries.system_health(session, state))


@router.get("/metrics")
def system_metrics(request: Request, state=Depends(get_state), user=Depends(current_user)):
    return envelope(request, queries.server_metrics(state))


@router.get("/incidents")
def system_incidents(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    session=Depends(get_db),
    user=Depends(current_user),
):
    return envelope(request, {"items": queries.incidents(session, limit=limit)})


@router.get("/version")
def system_version(request: Request, state=Depends(get_state)):
    return envelope(request, queries.version_info(state))


@router.get("/engine-config")
def engine_config(request: Request, state=Depends(get_state), user=Depends(current_user)):
    """Read-only system settings for the Settings page (spec §16.2).

    Returns operational summaries only — no secrets, no credentials, no file paths.
    """
    cfg = state.config
    risk = cfg.get("risk", {})
    return envelope(
        request,
        {
            "canonical_symbol": cfg.get("app", {}).get("canonical_symbol", "XAUUSD"),
            "paper_only": cfg.get("app", {}).get("paper_only", True),
            "initial_capital": risk.get("initial_balance"),
            "currency": risk.get("currency", "USD"),
            "risk_profile": {
                "risk_per_trade_pct": risk.get("risk_per_trade_pct"),
                "max_total_open_risk_pct": risk.get("max_total_open_risk_pct"),
                "max_concurrent_positions": risk.get("max_concurrent_positions"),
                "max_open_positions_per_strategy": risk.get("max_open_positions_per_strategy"),
                "daily_loss_limit_pct": risk.get("daily_loss_limit_pct"),
                "daily_loss_limit_enabled": risk.get("daily_loss_limit_enabled"),
            },
            "spread_guard": {"max_spread_points": risk.get("max_spread_points")},
            "enabled_strategies": [
                {
                    "strategy_id": sid,
                    "enabled": body.get("enabled", True),
                    "display_name": body.get("display_name") or sid.replace("_", " ").title(),
                }
                for sid, body in (cfg.get("strategies", {}) or {}).items()
            ],
            "sessions": [
                {"name": name, "timezone": body.get("timezone"), "local_start": body.get("local_start"), "local_end": body.get("local_end")}
                for name, body in (cfg.get("sessions", {}) or {}).items()
            ],
            "router": {
                "weights": state.runtime.router.weights if state.has_runtime else None,
                "min_candidate_score": cfg.get("hybrid_router", {}).get("min_candidate_score"),
            },
            "editable_in_ui": False,
            "note": "System configuration is read-only in the web UI (frontend spec §16.3). Changes follow a versioned configuration workflow outside the dashboard.",
        },
    )
