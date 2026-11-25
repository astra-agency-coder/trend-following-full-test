"""Shared fixtures: deterministic data, config, database."""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import ConfigLoader, ConfigVersion
from app.data.contracts import SymbolSpecification
from app.data.replay_provider import ReplayProvider, bars_to_ticks, generate_synthetic_bars
from app.persistence.database import Database

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

SESSIONS_CFG = {
    "asia": {"timezone": "Asia/Tokyo", "local_start": "09:00", "local_end": "15:00",
             "weekdays": ["MON", "TUE", "WED", "THU", "FRI"]},
    "london": {"timezone": "Europe/London", "local_start": "08:00", "local_end": "17:00",
               "weekdays": ["MON", "TUE", "WED", "THU", "FRI"]},
    "new_york": {"timezone": "America/New_York", "local_start": "08:00", "local_end": "17:00",
                 "weekdays": ["MON", "TUE", "WED", "THU", "FRI"]},
}


def make_spec() -> SymbolSpecification:
    return SymbolSpecification(
        canonical_symbol="XAUUSD", source_symbol="XAUUSD", broker_name="TEST",
        digits=2, point=0.01, contract_size=100.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_size=0.01, tick_value=1.0,
    )


def make_config(tmp_path=None) -> dict:
    """Load the real project config (validation included)."""
    loader = ConfigLoader(CONFIG_DIR)
    cv = loader.load()
    return cv.data


@pytest.fixture()
def spec():
    return make_spec()


@pytest.fixture()
def config():
    return make_config()


@pytest.fixture()
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path}/test.db")
    database.create_all()
    yield database
    database.drop_all()


@pytest.fixture()
def market_data():
    """Deterministic multi-timeframe bars + ticks (fixed seeds)."""
    start = datetime(2026, 1, 5, tzinfo=UTC)  # Monday
    m5 = generate_synthetic_bars(timeframe="M5", count=600, start=start, trend=0.08, volatility=1.8, seed=11)
    m15 = generate_synthetic_bars(timeframe="M15", count=250, start=start, trend=0.08, volatility=2.8, seed=12)
    h1 = generate_synthetic_bars(timeframe="H1", count=90, start=start, trend=0.08, volatility=5.0, seed=13)
    ticks = bars_to_ticks(m15)
    return {"m5": m5, "m15": m15, "h1": h1, "ticks": ticks, "start": start}


@pytest.fixture()
def replay_provider(market_data, spec):
    return ReplayProvider(spec, bars=market_data["m5"] + market_data["m15"] + market_data["h1"], ticks=market_data["ticks"])


@pytest.fixture()
def config_version(config):
    from datetime import datetime as dt

    return ConfigVersion(
        config_version_id="test-cfg-0001",
        config_hash="hash0001",
        loaded_at_utc=dt.now(UTC),
        source="FILE",
        data=config,
    )
