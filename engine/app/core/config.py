"""Configuration loading & validation.

Layering (spec 20 / 25 / 30):
- YAML files under ``config/`` hold every tunable value (no thresholds in code).
- Environment variables override file values (secrets come only from env).
- Every loaded configuration gets a deterministic ``config_hash`` and is
  persisted as an immutable ``config_versions`` row (versioning).

Atomicity (spec 48.5): a config load either fully validates or raises; the
caller keeps the previously loaded version on failure.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import yaml

from app.core.exceptions import ConfigurationError

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


ENV_OVERRIDES: dict[str, str] = {
    # dotted config path -> environment variable
    "app.paper_only": "PAPER_ONLY",
    "market_data.provider": "MARKET_DATA_PROVIDER",
    "market_data.source_symbol": "SOURCE_SYMBOL",
    "database.url": "DATABASE_URL",
    "telegram.bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram.enabled": "TELEGRAM_ENABLED",
    "telegram.allowed_user_ids": "TELEGRAM_ALLOWED_USER_IDS",
    "telegram.allowed_chat_ids": "TELEGRAM_ALLOWED_CHAT_IDS",
    "mt5.login": "MT5_LOGIN",
    "mt5.password": "MT5_PASSWORD",
    "mt5.server": "MT5_SERVER",
    "mt5.terminal_path": "MT5_TERMINAL_PATH",
}


def _cast_env(raw: str) -> Any:
    low = raw.strip().lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", ""):
        return None
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _apply_env_overrides(config: dict, environ: dict[str, str] | None = None) -> dict:
    env = environ if environ is not None else dict(os.environ)
    for dotted, var in ENV_OVERRIDES.items():
        if var in env and env[var] != "":
            value = env[var]
            # comma separated lists -> list of strings
            if dotted.endswith("_ids"):
                value = [part.strip() for part in env[var].split(",") if part.strip()]
            else:
                value = _cast_env(value)
            node = config
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
    return config


class ConfigValidator:
    """Structural validation of the merged config (spec 20.3)."""

    @staticmethod
    def validate(config: dict) -> list[str]:
        errors: list[str] = []

        app = config.get("app", {})
        if not app.get("canonical_symbol"):
            errors.append("app.canonical_symbol is required")
        if app.get("paper_only") is not True:
            errors.append("app.paper_only must be true in this version (paper trading only)")
        if app.get("enable_ai") not in (None, False):
            errors.append("app.enable_ai must be false (rule-based only)")

        risk = config.get("risk", {})
        if not (0 < float(risk.get("risk_per_trade_pct", 0)) <= 100):
            errors.append("risk.risk_per_trade_pct must be in (0, 100]")
        if not (0 < float(risk.get("max_total_open_risk_pct", 0)) <= 100):
            errors.append("risk.max_total_open_risk_pct must be in (0, 100]")
        if risk.get("martingale_enabled") or risk.get("grid_enabled") or risk.get("averaging_down_enabled"):
            errors.append("martingale/grid/averaging_down must be disabled")
        if float(risk.get("initial_balance", 0)) <= 0:
            errors.append("risk.initial_balance must be positive")

        md = config.get("market_data", {})
        timeframes = md.get("timeframes", [])
        if not timeframes:
            errors.append("market_data.timeframes must not be empty")
        from app.core.constants import TIMEFRAME_SECONDS

        for tf in timeframes:
            if tf not in TIMEFRAME_SECONDS:
                errors.append(f"unknown timeframe {tf!r}")

        paper = config.get("paper_execution", {})
        if paper.get("enabled") is not True:
            errors.append("paper_execution.enabled must be true (paper only version)")
        if paper.get("real_execution_enabled") is True:
            errors.append("paper_execution.real_execution_enabled must never be true")

        sessions = config.get("sessions", {})
        if not sessions:
            errors.append("sessions must define at least one trading session")

        return errors


@dataclass(frozen=True)
class ConfigVersion:
    config_version_id: str
    config_hash: str
    loaded_at_utc: datetime
    source: str = "FILE"
    data: dict[str, Any] = field(default_factory=dict)


def compute_config_hash(config: dict) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfigLoader:
    """Loads config files, applies env overrides, validates, versions."""

    def __init__(self, config_dir: Path | str | None = None) -> None:
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR

    def load(self, environ: dict[str, str] | None = None) -> ConfigVersion:
        try:
            merged: dict[str, Any] = {}
            app_file = self.config_dir / "app.yaml"
            if app_file.exists():
                merged.update(yaml.safe_load(app_file.read_text(encoding="utf-8")) or {})

            for name in (
                "market_data.yaml",
                "sessions.yaml",
                "market_context.yaml",
                "router.yaml",
                "risk.yaml",
                "paper_execution.yaml",
                "telegram.yaml",
                "database.yaml",
                "logging.yaml",
                "reporting.yaml",
                "backtesting.yaml",
            ):
                path = self.config_dir / name
                if path.exists():
                    section = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    if not isinstance(section, dict):
                        raise ConfigurationError(f"{name} must be a YAML mapping")
                    merged = _deep_merge(merged, section)

            strategies_dir = self.config_dir / "strategies"
            if strategies_dir.exists():
                strategy_cfgs: dict[str, Any] = merged.setdefault("strategies", {})
                for path in sorted(strategies_dir.glob("*.yaml")):
                    body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    # allow either `smc_ict: {...}` or the flat settings body
                    if isinstance(body, dict) and path.stem in body and isinstance(body[path.stem], dict):
                        body = body[path.stem]
                    if not isinstance(body, dict):
                        raise ConfigurationError(f"{path.name} must be a YAML mapping")
                    strategy_cfgs[path.stem] = body
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"invalid YAML: {exc}") from exc
        except OSError as exc:
            raise ConfigurationError(f"cannot read config files: {exc}") from exc

        merged = _apply_env_overrides(merged, environ)

        errors = ConfigValidator.validate(merged)
        if errors:
            raise ConfigurationError(
                "configuration validation failed",
                details={"errors": errors},
            )

        from app.core.ids import new_uuid

        return ConfigVersion(
            config_version_id=new_uuid(),
            config_hash=compute_config_hash(merged),
            loaded_at_utc=datetime.now(UTC),
            source="FILE",
            data=merged,
        )


def get(config: dict, dotted: str, default: Any = None) -> Any:
    """Dotted-path getter used across services."""
    node: Any = config
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node
