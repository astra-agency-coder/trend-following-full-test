"""Clock abstraction.

Live runtime uses the wall clock; the backtester injects a VirtualClock so
no component can ever read "now" from the machine during replay (spec 46.2).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    """Real wall clock (timezone-aware UTC)."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class VirtualClock:
    """Deterministic clock for backtests / tests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("VirtualClock requires a timezone-aware start datetime")
        self._now = start

    def now_utc(self) -> datetime:
        return self._now

    def advance_to(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("advance_to requires a timezone-aware datetime")
        if moment < self._now:
            raise ValueError("VirtualClock cannot move backwards")
        self._now = moment

    def advance_seconds(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
