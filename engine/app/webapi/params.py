"""Shared query-parameter helpers (period → datetime window)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.webapi.envelope import ApiError

PERIODS = {
    "today": 1,
    "24h": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}


def period_window(period: str | None, from_str: str | None, to_str: str | None) -> tuple[datetime, datetime]:
    """Resolve ?period=/?from=/?to= into a UTC window. Filters compose (spec §2.4)."""
    now = datetime.now(UTC).replace(microsecond=0)
    if from_str or to_str:
        try:
            start = datetime.fromisoformat(from_str.replace("Z", "+00:00")) if from_str else now - timedelta(days=7)
            end = datetime.fromisoformat(to_str.replace("Z", "+00:00")) if to_str else now
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
            return start.astimezone(UTC), end.astimezone(UTC)
        except ValueError:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid date range", {"field": "from"})
    days = PERIODS.get((period or "7d").lower(), 7)
    if days is None:
        return datetime(2000, 1, 1, tzinfo=UTC), now
    if (period or "7d").lower() == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    return now - timedelta(days=days), now
