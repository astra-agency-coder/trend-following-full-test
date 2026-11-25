"""Structured JSON logging (spec 4.1 / 12.1-A / 12.4).

- Every log record is a single JSON line.
- Secrets are scrubbed by key name before serialisation.
- Rotating file handler for the operational log.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"
SECRET_KEYS = {
    "token",
    "bot_token",
    "password",
    "mt5_password",
    "db_password",
    "database_password",
    "api_key",
    "secret",
    "authorization",
}


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if str(k).lower() in SECRET_KEYS else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if extra:
            payload["context"] = scrub(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path | str = "logs", level: str = "INFO", console: bool = True) -> logging.Logger:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("gold_hybrid")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "engine.log", maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(JsonFormatter())
        root.addHandler(console_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"gold_hybrid.{name}")
