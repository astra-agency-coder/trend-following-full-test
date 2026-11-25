"""Session engine with IANA time zones and DST awareness (spec 17 / 42).

Sessions are defined in *local* time per IANA zone; conversion happens via
``zoneinfo`` so DST transitions are handled by the tz database, never by
fixed UTC offsets. Phase boundaries are configurable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.enums import SessionPhase

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


@dataclass(frozen=True)
class SessionDefinition:
    name: str
    timezone_name: str
    local_start: time
    local_end: time
    weekdays: frozenset[int]

    @classmethod
    def from_config(cls, name: str, cfg: dict) -> "SessionDefinition":
        start = time.fromisoformat(cfg["local_start"])
        end = time.fromisoformat(cfg["local_end"])
        weekdays = frozenset(WEEKDAYS[d] for d in cfg.get("weekdays", ["MON", "TUE", "WED", "THU", "FRI"]))
        return cls(name=name, timezone_name=cfg["timezone"], local_start=start, local_end=end, weekdays=weekdays)


@dataclass(frozen=True)
class SessionInfo:
    session_name: str
    phase: SessionPhase
    timezone_name: str
    local_time: str
    minutes_from_open: int
    minutes_to_close: int
    is_overlap: bool
    active_sessions: tuple[str, ...]
    session_start_utc: datetime
    session_end_utc: datetime


class SessionEngine:
    def __init__(self, definitions: list[SessionDefinition], phase_minutes: dict[str, float] | None = None):
        self.definitions = {d.name: d for d in definitions}
        # configurable phase boundaries (fractions/minutes of session)
        self.phase_minutes = phase_minutes or {
            "opening_window": 30.0,   # first N minutes
            "early_until_frac": 0.33,
            "late_from_frac": 0.66,
            "closing_window": 30.0,  # last N minutes
        }

    @classmethod
    def from_config(cls, cfg: dict) -> "SessionEngine":
        defs = [SessionDefinition.from_config(name, body) for name, body in cfg.items()]
        return cls(defs)

    def _local_session_bounds(self, definition: SessionDefinition, moment_utc: datetime) -> tuple[datetime, datetime] | None:
        """UTC bounds of the session instance covering ``moment_utc`` (or None)."""
        tz = ZoneInfo(definition.timezone_name)
        local = moment_utc.astimezone(tz)
        if local.weekday() not in definition.weekdays:
            return None
        start_local = local.replace(hour=definition.local_start.hour, minute=definition.local_start.minute, second=0, microsecond=0)
        end_local = local.replace(hour=definition.local_end.hour, minute=definition.local_end.minute, second=0, microsecond=0)
        if end_local <= start_local:  # overnight session
            if local.time() < definition.local_start.hour and local.hour >= definition.local_end.hour:
                return None
            if local < start_local or local >= end_local + timedelta(days=1):
                # before session start today or after end
                if start_local <= local < end_local:
                    pass
                else:
                    return None
            if local >= start_local:
                return start_local.astimezone(ZoneInfo("UTC")), (end_local + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
            return None
        if start_local <= local < end_local:
            return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))
        return None

    def evaluate(self, moment_utc: datetime) -> SessionInfo:
        active: dict[str, tuple[datetime, datetime]] = {}
        for name, definition in self.definitions.items():
            bounds = self._local_session_bounds(definition, moment_utc)
            if bounds:
                active[name] = bounds

        if not active:
            return SessionInfo(
                session_name="CLOSED",
                phase=SessionPhase.CLOSED,
                timezone_name="UTC",
                local_time=moment_utc.strftime("%Y-%m-%d %H:%M"),
                minutes_from_open=0,
                minutes_to_close=0,
                is_overlap=False,
                active_sessions=(),
                session_start_utc=moment_utc,
                session_end_utc=moment_utc,
            )

        # primary session = earliest-opening active session
        primary_name = min(active, key=lambda n: active[n][0])
        start_utc, end_utc = active[primary_name]
        definition = self.definitions[primary_name]
        tz = ZoneInfo(definition.timezone_name)
        local_time = moment_utc.astimezone(tz)

        total_minutes = (end_utc - start_utc).total_seconds() / 60
        elapsed = (moment_utc - start_utc).total_seconds() / 60
        remaining = total_minutes - elapsed

        pm = self.phase_minutes
        if elapsed <= pm["opening_window"]:
            phase = SessionPhase.OPENING_WINDOW
        elif elapsed <= total_minutes * pm["early_until_frac"]:
            phase = SessionPhase.EARLY_SESSION
        elif remaining <= pm["closing_window"]:
            phase = SessionPhase.CLOSING_WINDOW
        elif elapsed >= total_minutes * pm["late_from_frac"]:
            phase = SessionPhase.LATE_SESSION
        else:
            phase = SessionPhase.MID_SESSION

        return SessionInfo(
            session_name=primary_name.upper(),
            phase=phase,
            timezone_name=definition.timezone_name,
            local_time=local_time.strftime("%Y-%m-%d %H:%M"),
            minutes_from_open=int(elapsed),
            minutes_to_close=int(remaining),
            is_overlap=len(active) > 1,
            active_sessions=tuple(n.upper() for n in sorted(active)),
            session_start_utc=start_utc,
            session_end_utc=end_utc,
        )

    def session_bars(
        self,
        bars: list,
        session_info: SessionInfo,
        *,
        max_bars: int = 200,
    ) -> list:
        """Closed bars belonging to the current session instance only."""
        return [
            b
            for b in bars
            if session_info.session_start_utc <= b.open_time_utc < session_info.session_end_utc
        ][-max_bars:]
