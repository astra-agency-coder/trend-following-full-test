"""Unit tests: config loader, validators, indicators, sessions, audit chain."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import ConfigLoader, compute_config_hash
from app.core.exceptions import ConfigurationError
from app.data.validators import detect_bar_gaps, validate_bar, validate_tick
from app.journal.audit import AuditJournal
from app.market_context.indicators import atr, ema, rsi, sma, swings, z_score
from app.market_context.sessions import SessionEngine
from app.persistence.models import AuditEventRow


# ---------------------------------------------------------------- config
class TestConfig:
    def test_loads_and_validates(self):
        cv = ConfigLoader("config").load()
        assert cv.data["app"]["paper_only"] is True
        assert cv.data["app"]["canonical_symbol"] == "XAUUSD"
        assert set(cv.data["strategies"]) >= {"smc_ict", "wyckoff", "trend_following", "mean_reversion", "breakout"}

    def test_hash_is_deterministic(self):
        a = {"x": 1, "y": [1, 2]}
        assert compute_config_hash(a) == compute_config_hash(dict(a))

    def test_rejects_real_execution(self, config):
        bad = dict(config)
        bad["paper_execution"] = {**bad.get("paper_execution", {}), "real_execution_enabled": True}
        from app.core.config import ConfigValidator

        errors = ConfigValidator.validate(bad)
        assert any("real_execution" in e for e in errors)

    def test_rejects_martingale(self, config):
        bad = dict(config)
        bad["risk"] = {**bad["risk"], "martingale_enabled": True}
        from app.core.config import ConfigValidator

        assert any("martingale" in e for e in ConfigValidator.validate(bad))

    def test_env_override(self, config):
        from app.core.config import _apply_env_overrides

        merged = _apply_env_overrides({"app": {"paper_only": True}}, {"PAPER_ONLY": "true"})
        assert merged["app"]["paper_only"] is True
        merged = _apply_env_overrides({}, {"TELEGRAM_ALLOWED_USER_IDS": "111,222"})
        assert merged["telegram"]["allowed_user_ids"] == ["111", "222"]


# ---------------------------------------------------------------- validators
class TestValidators:
    def _tick(self, bid=2000.0, ask=2000.3, age_seconds=0):
        from app.data.replay_provider import make_mock_tick

        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        return make_mock_tick(time_utc=now - timedelta(seconds=age_seconds), bid=bid, ask=ask)

    def test_valid_tick(self):
        ok, flags = validate_tick(self._tick(), max_age_seconds=10, now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
        assert ok and not flags

    def test_stale_tick(self):
        ok, flags = validate_tick(self._tick(age_seconds=60), max_age_seconds=10, now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
        assert not ok and "STALE_TICK" in flags

    def test_crossed_quotes(self):
        ok, flags = validate_tick(self._tick(bid=2000.5, ask=2000.2), max_age_seconds=10, now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
        assert not ok and "ASK_BID_CROSSED" in flags

    def test_non_positive(self):
        ok, flags = validate_tick(self._tick(bid=0, ask=0.3), max_age_seconds=10, now_utc=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
        assert not ok and "NON_POSITIVE_PRICE" in flags

    def test_bar_ohlc_consistency(self, market_data):
        ok, _ = validate_bar(market_data["m15"][10])
        assert ok
        bad = market_data["m15"][10]
        from dataclasses import replace

        broken = replace(bad, high=bad.low - 5)
        ok, flags = validate_bar(broken)
        assert not ok and "OHLC_INCONSISTENT_HIGH" in flags

    def test_gap_detection(self, market_data):
        bars = market_data["m15"][:50]
        from app.data.replay_provider import generate_synthetic_bars

        # remove one bar -> exactly one gap
        with_hole = bars[:10] + bars[11:]
        gaps = detect_bar_gaps(with_hole, "M15")
        assert len(gaps) == 1
        assert gaps[0][0] == bars[10].open_time_utc
        assert not detect_bar_gaps(bars, "M15")


# ---------------------------------------------------------------- indicators
class TestIndicators:
    def test_sma(self):
        assert sma([1, 2, 3, 4, 5], 5) == 3.0
        assert sma([1, 2], 5) is None

    def test_ema_converges(self):
        series = ema([float(i) for i in range(1, 60)], 10)
        assert series is not None and len(series) == 50

    def test_atr_positive(self, market_data):
        value = atr(market_data["m15"], 14)
        assert value is not None and value > 0

    def test_rsi_bounds(self, market_data):
        value = rsi(market_data["m15"], 14)
        assert value is not None and 0 <= value <= 100

    def test_zscore_zero_for_flat(self, market_data):
        from dataclasses import replace

        bars = [replace(b, close=100.0, high=100.5, low=99.5, open=100.0) for b in market_data["m15"][:30]]
        assert z_score(bars, 20) == 0.0

    def test_swings_confirmation_no_lookahead(self, market_data):
        bars = market_data["m15"][:60]
        pivots = swings(bars, left=2, right=2)
        for idx, kind, price, pivot_bar, confirm_bar in pivots:
            # the pivot can only be known after `right` more bars closed
            assert bars.index(confirm_bar) == idx + 2


# ---------------------------------------------------------------- sessions / DST
class TestSessions:
    def _engine(self):
        from tests.fixtures.conftest_fixtures import SESSIONS_CFG

        return SessionEngine.from_config(SESSIONS_CFG)

    def test_london_open(self):
        engine = self._engine()
        info = engine.evaluate(datetime(2026, 3, 27, 10, 0, tzinfo=UTC))  # before DST switch
        assert info.session_name == "LONDON"
        assert info.minutes_from_open == 120

    def test_dst_spring_forward_changes_utc_offset(self):
        engine = self._engine()
        before = engine.evaluate(datetime(2026, 3, 27, 10, 0, tzinfo=UTC))
        after = engine.evaluate(datetime(2026, 3, 30, 10, 0, tzinfo=UTC))  # after Mar 29 switch
        # same local 10:00 open, but UTC minutes differ by the DST hour
        assert after.minutes_from_open - before.minutes_from_open == 60

    def test_dst_fall_back(self):
        engine = self._engine()
        before = engine.evaluate(datetime(2026, 10, 23, 14, 0, tzinfo=UTC))
        after = engine.evaluate(datetime(2026, 10, 26, 14, 0, tzinfo=UTC))  # after Oct 25 switch
        assert before.minutes_from_open - after.minutes_from_open == 60

    def test_ny_london_overlap(self):
        engine = self._engine()
        info = engine.evaluate(datetime(2026, 3, 27, 14, 30, tzinfo=UTC))
        assert info.is_overlap
        assert "NEW_YORK" in info.active_sessions and "LONDON" in info.active_sessions

    def test_weekend_closed(self):
        engine = self._engine()
        info = engine.evaluate(datetime(2026, 1, 10, 12, 0, tzinfo=UTC))  # Saturday
        assert info.session_name == "CLOSED"


# ---------------------------------------------------------------- audit chain
class TestAuditChain:
    def test_chain_intact_and_tamper_detected(self, db):
        journal = AuditJournal(db)
        journal.append("SYSTEM_STARTED", payload={"v": 1})
        journal.append("TRADE_OPENED", entity_type="trade", entity_id="t1", payload={"side": "BUY"})
        journal.append("TRADE_CLOSED", entity_type="trade", entity_id="t1", payload={"net": 12.5})
        assert journal.count() == 3
        assert journal.verify_chain() is True

        with db.session() as session:
            row = session.query(AuditEventRow).filter(AuditEventRow.id == 2).one()
            row.payload = '{"side":"SELL"}'  # tamper
        assert journal.verify_chain() is False

    def test_delete_breaks_chain(self, db):
        journal = AuditJournal(db)
        for i in range(5):
            journal.append(f"E{i}")
        with db.session() as session:
            session.query(AuditEventRow).filter(AuditEventRow.id == 3).delete()
        assert journal.verify_chain() is False
