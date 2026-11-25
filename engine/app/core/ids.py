"""ID generation helpers (UUID + short correlation ids)."""
from __future__ import annotations

import uuid


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]
