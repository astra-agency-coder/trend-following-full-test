"""FastAPI dependencies: state, db session, auth, CSRF, rate limits."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request

from app.webapi.envelope import ApiError
from app.webapi.state import AppState
from app.webapi.security import CSRF_COOKIE, SESSION_COOKIE, RateLimiter, WebUser

_state: AppState | None = None


def set_state(state: AppState) -> None:
    global _state
    _state = state


def get_state() -> AppState:
    assert _state is not None, "application state not initialised"
    return _state


def get_db(state: AppState = Depends(get_state)) -> Iterator:
    with state.database.session() as session:
        yield session


# ------------------------------------------------------------------ auth
def current_user(request: Request, state: AppState = Depends(get_state)) -> WebUser:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or state.session_manager is None:
        raise ApiError(401, "UNAUTHENTICATED", "Authentication required")
    payload = state.session_manager.verify(token)
    if payload is None:
        raise ApiError(401, "SESSION_EXPIRED", "Session expired — please log in again")
    user = state.find_user(payload.get("username", ""))
    if user is None:
        raise ApiError(401, "UNAUTHENTICATED", "Unknown user session")
    return user


def require_owner(user: WebUser = Depends(current_user)) -> WebUser:
    if user.role != "OWNER":
        # Backend-side authorization: VIEWER cannot invoke control APIs
        # even if the UI hides the buttons (frontend spec §17.5 / §22.2).
        raise ApiError(403, "FORBIDDEN", "OWNER role required for control actions")
    return user


def verify_csrf(request: Request, user: WebUser = Depends(current_user)) -> WebUser:
    """Double-submit CSRF check for mutating requests (cookie session auth)."""
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or cookie != header:
        raise ApiError(403, "CSRF_FAILED", "Missing or invalid CSRF token")
    return user


def owner_with_csrf(request: Request, user: WebUser = Depends(verify_csrf)) -> WebUser:
    """Control endpoints: authenticated + CSRF + OWNER role (spec §17.5)."""
    if user.role != "OWNER":
        raise ApiError(403, "FORBIDDEN", "OWNER role required for control actions")
    return user


def rate_limit(bucket: str, limit: int, window_seconds: int):
    def dependency(request: Request, state: AppState = Depends(get_state)) -> None:
        key = f"{bucket}:{request.client.host if request.client else 'unknown'}"
        if not state.rate_limiter.allow(key, limit, window_seconds):
            raise ApiError(429, "RATE_LIMITED", "Too many requests — slow down")
    return dependency
