"""Telegram authorization + rate limiting (spec 14.4 / 44)."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from app.core.logging_setup import get_logger

logger = get_logger("telegram.auth")


class TelegramAuthorizer:
    """Whitelist by numeric user id (never by username) + sliding rate limit."""

    def __init__(self, allowed_user_ids: list[str], allowed_chat_ids: list[str], *, rate_per_minute: int = 20):
        self.allowed_users = {str(u).strip() for u in allowed_user_ids if str(u).strip()}
        self.allowed_chats = {str(c).strip() for c in allowed_chat_ids if str(c).strip()}
        self.rate_per_minute = rate_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def authorize(self, user_id: str | None, chat_id: str | None) -> tuple[bool, str]:
        if not user_id:
            return False, "UNAUTHORIZED_NO_USER_ID"
        if self.allowed_users and user_id not in self.allowed_users:
            logger.warning("unauthorized telegram user rejected", extra={"context": {"user_id": user_id}})
            return False, "UNAUTHORIZED_USER"
        if chat_id and self.allowed_chats and chat_id not in self.allowed_chats:
            return False, "UNAUTHORIZED_CHAT"
        if not self._rate_ok(user_id):
            return False, "RATE_LIMITED"
        return True, "OK"

    def _rate_ok(self, user_id: str) -> bool:
        now = time.monotonic()
        hits = self._hits[user_id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self.rate_per_minute:
            return False
        hits.append(now)
        return True
