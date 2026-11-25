"""Auth endpoints (frontend spec §18.1 / §22)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.webapi.dependencies import current_user, get_state
from app.webapi.envelope import ApiError, envelope
from app.webapi.security import CSRF_COOKIE, SESSION_COOKIE, new_csrf_token, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, state=Depends(get_state)):
    host = request.client.host if request.client else "unknown"
    user = state.find_user(body.username)
    if user is None or not verify_password(body.password, user.password_hash):
        # rate limit failed attempts: 10 per 5 minutes per host
        state.rate_limiter.allow(f"login-fail:{host}", 10, 300)
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid username or password")
    token = state.session_manager.issue(user)
    csrf = new_csrf_token()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False, max_age=12 * 3600, path="/")
    # readable by JS on purpose — double-submit CSRF token (not a secret)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=False, max_age=12 * 3600, path="/")
    return envelope(request, {"username": user.username, "role": user.role, "csrf_token": csrf})


@router.post("/logout")
def logout(request: Request, response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return envelope(request, {"ok": True})


@router.get("/me")
def me(request: Request, user=Depends(current_user), state=Depends(get_state)):
    return envelope(
        request,
        {
            "username": user.username,
            "role": user.role,
            "permissions": {
                "view": True,
                "control": user.role == "OWNER",
                "export": True,
            },
            "using_default_credentials": state.users_using_defaults,
        },
    )
