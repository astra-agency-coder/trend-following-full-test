"""SSE event stream (frontend spec §20).

Single connection carries all realtime updates. Heartbeat comments keep
proxies from buffering; on reconnect the client replays via REST snapshot
reconciliation (handled client-side).
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.webapi.dependencies import current_user, get_state
from app.webapi.envelope import envelope
from app.webapi.event_bus import make_event

router = APIRouter(prefix="/api/v1/stream", tags=["stream"])


@router.get("/events")
async def stream_events(request: Request, user=Depends(current_user), state=Depends(get_state)):
    queue = await state.event_bus.subscribe()

    async def generator():
        try:
            hello = make_event("system.health.updated", {"status": "stream_connected", "mode": state.run_mode})
            yield f"event: {hello['event_type']}\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            state.event_bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/status")
def stream_status(request: Request, state=Depends(get_state), user=Depends(current_user)):
    return envelope(
        request,
        {
            "bus_version": state.event_bus.version,
            "subscribers": len(state.event_bus._subs),
            "runtime_attached": state.has_runtime,
        },
    )
