"""Web API authentication & security (frontend spec §18.1 / §22).

- Passwords hashed with hashlib.scrypt (stdlib, no extra dependency).
- Session token: signed payload + HMAC-SHA256, carried in an HttpOnly
  SameSite=Lax cookie. No long-lived token is exposed to the client.
- CSRF: a readable cookie holds a nonce; every mutating request must send
  the same nonce in the ``X-CSRF-Token`` header (double-submit pattern).
- Users come from ``WEB_AUTH_USERS`` env JSON. If unset, development
  defaults are created and a warning audit event is logged (frontend spec
  forbids shipping secrets in the client; defaults exist for local runs).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

SESSION_COOKIE = "gold_hybrid_session"
CSRF_COOKIE = "gold_hybrid_csrf"
SESSION_TTL_SECONDS = 12 * 3600

_DEFAULT_USERS = [
    {"username": "admin", "password": "admin123", "role": "OWNER"},
    {"username": "viewer", "password": "viewer123", "role": "VIEWER"},
]


@dataclass
class WebUser:
    username: str
    role: str  # OWNER | VIEWER
    password_hash: str

    def to_payload(self) -> dict:
        return {"username": self.username, "role": self.role}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _scheme, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


class SessionManager:
    """HMAC-signed session payloads; secret persisted beside the DB."""

    def __init__(self, secret: str):
        self._secret = secret.encode()

    def _sign(self, material: bytes) -> str:
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def issue(self, user: WebUser, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
        payload = json.dumps(
            {**user.to_payload(), "exp": int(time.time()) + ttl_seconds, "jti": secrets.token_hex(8)}
        ).encode()
        body = base64.urlsafe_b64encode(payload).decode()
        return f"{body}.{self._sign(body.encode())}"

    def verify(self, token: str) -> dict | None:
        try:
            body, signature = token.rsplit(".", 1)
            if not hmac.compare_digest(self._sign(body.encode()), signature):
                return None
            payload = json.loads(base64.urlsafe_b64decode(body.encode()))
            if payload.get("exp", 0) < time.time():
                return None
            return payload
        except Exception:
            return None


class RateLimiter:
    """Simple in-process sliding-window limiter (login/control/export)."""

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        bucket = [t for t in self._events.get(key, []) if now - t < window_seconds]
        if len(bucket) >= limit:
            self._events[key] = bucket
            return False
        bucket.append(now)
        self._events[key] = bucket
        return True


def load_users() -> tuple[list[WebUser], bool]:
    """Parse WEB_AUTH_USERS; returns (users, using_defaults)."""
    raw = os.environ.get("WEB_AUTH_USERS", "").strip()
    if raw:
        entries = json.loads(raw)
        users = [
            WebUser(username=e["username"], role=e.get("role", "VIEWER"), password_hash=hash_password(e["password"]))
            for e in entries
        ]
        return users, False
    return [
        WebUser(username=u["username"], role=u["role"], password_hash=hash_password(u["password"]))
        for u in _DEFAULT_USERS
    ], True


def resolve_secret() -> str:
    """WEB_SESSION_SECRET or a persisted random secret (survives restarts)."""
    secret = os.environ.get("WEB_SESSION_SECRET", "").strip()
    if secret:
        return secret
    path = Path(os.environ.get("WEB_SECRET_FILE", ".web_session_secret"))
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        secret = secrets.token_hex(32)
        path.write_text(secret, encoding="utf-8")
        return secret
    except OSError:
        return secrets.token_hex(32)  # ephemeral fallback


def new_csrf_token() -> str:
    return secrets.token_hex(16)
