"""Telegram client — thin HTTP wrapper over the Bot API (long polling).

Never logs the token; never contains trading logic (spec 44.1). The client
is injected everywhere so tests can pass a MockTelegramClient.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from app.core.exceptions import TelegramError
from app.core.logging_setup import get_logger

logger = get_logger("telegram.client")

API_BASE = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    user_id: str | None
    chat_id: str | None
    text: str


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 15.0):
        self.token = token
        self.timeout = timeout
        self._base = f"{API_BASE}/bot{token}"
        self._offset = 0

    def _call(self, method: str, payload: dict | None = None) -> dict:
        try:
            response = requests.post(f"{self._base}/{method}", json=payload or {}, timeout=self.timeout)
            data = response.json()
        except requests.RequestException as exc:
            raise TelegramError(f"network error calling {method}: {exc}") from exc
        except ValueError as exc:
            raise TelegramError(f"invalid JSON from {method}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description', 'unknown error')}")
        return data["result"]

    def send_message(self, chat_id: str, text: str) -> dict:
        return self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )

    def get_updates(self, timeout: float = 10.0) -> list[TelegramUpdate]:
        try:
            result = self._call("getUpdates", {"offset": self._offset, "timeout": timeout})
        except TelegramError:
            return []
        updates: list[TelegramUpdate] = []
        for item in result:
            self._offset = max(self._offset, item["update_id"] + 1)
            message = item.get("message") or item.get("edited_message") or {}
            updates.append(
                TelegramUpdate(
                    update_id=item["update_id"],
                    user_id=str(message.get("from", {}).get("id")) if message.get("from") else None,
                    chat_id=str(message.get("chat", {}).get("id")) if message.get("chat") else None,
                    text=message.get("text", ""),
                )
            )
        return updates

    def set_commands(self, commands: list[dict]) -> None:
        self._call("setMyCommands", {"commands": commands})


class MockTelegramClient:
    """In-memory client for tests: records sent messages, serves scripted updates."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.updates: list[TelegramUpdate] = []
        self.fail_next = False

    def send_message(self, chat_id: str, text: str) -> dict:
        if self.fail_next:
            self.fail_next = False
            raise TelegramError("mock failure")
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def get_updates(self, timeout: float = 10.0) -> list[TelegramUpdate]:
        out, self.updates = self.updates, []
        return out

    def set_commands(self, commands: list[dict]) -> None:
        pass

    def push_update(self, user_id: str, chat_id: str, text: str) -> None:
        self.updates.append(
            TelegramUpdate(update_id=len(self.updates) + 1, user_id=user_id, chat_id=chat_id, text=text)
        )
