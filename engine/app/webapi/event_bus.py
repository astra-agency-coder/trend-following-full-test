"""In-process event bus feeding the SSE stream (frontend spec §20).

The engine runner publishes a small, spec-shaped envelope per event;
SSE subscribers receive them with automatic fan-out. Nothing here holds
trading state — it is a transport only.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any


def make_event(event_type: str, data: dict[str, Any], *, correlation_id: str | None = None) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:16]}",
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "schema_version": "1.0",
        "correlation_id": correlation_id,
        "data": data,
    }


class EventBus:
    """Fan-out pub/sub with bounded per-subscriber queues."""

    def __init__(self, queue_size: int = 256):
        self._subs: set[asyncio.Queue] = set()
        self._queue_size = queue_size
        self._last_events: list[dict] = []  # small replay buffer for reconnects
        self._version = 0

    def publish(self, event: dict) -> None:
        self._version += 1
        self._last_events.append(event)
        del self._last_events[:-100]
        for queue in list(self._subs):
            if queue.full():
                try:
                    queue.get_nowait()  # drop oldest — slow consumer
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subs.discard(queue)

    def recent(self, after_version: int = 0) -> list[dict]:
        return list(self._last_events)

    @property
    def version(self) -> int:
        return self._version
