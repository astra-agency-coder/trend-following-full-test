"""Database tests: schema, constraints, persistence round-trip, migrations."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import inspect, select

from app.persistence.database import Database, MigrationRunner, dumps, loads
from app.persistence.models import (
    MarketBarRow,
    PaperTradeRow,
    RouterDecisionRow,
    StrategyEvaluationRow,
    TelegramOutboxRow,
)


class TestSchema:
    def test_all_tables_exist(self, db):
        tables = set(inspect(db.engine).get_table_names())
        required = {
            "market_bars", "market_ticks", "market_snapshots", "symbol_specs",
            "strategy_evaluations", "router_decisions", "router_candidate_rankings",
            "paper_portfolios", "paper_orders", "paper_trades", "trade_events",
            "risk_decisions", "rejected_opportunities", "shadow_outcomes",
            "telegram_outbox", "audit_events", "system_health_events",
            "system_runs", "config_versions", "data_import_jobs", "economic_events",
            "session_instances", "report_runs", "service_components",
        }
        assert required <= tables

    def test_bar_unique_constraint(self, db):
        ts = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        row = MarketBarRow(
            canonical_symbol="XAUUSD", timeframe="M15", open_time_utc=ts, close_time_utc=ts,
            open=2000, high=2001, low=1999, close=2000.5,
        )
        with db.session() as session:
            session.add(row)
        import pytest
        from sqlalchemy.exc import IntegrityError

        dup = MarketBarRow(
            canonical_symbol="XAUUSD", timeframe="M15", open_time_utc=ts, close_time_utc=ts,
            open=2000, high=2001, low=1999, close=2000.5,
        )
        with pytest.raises(IntegrityError):
            with db.session() as session:
                session.add(dup)

    def test_trade_requires_stop_loss(self, db):
        import pytest
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            with db.session() as session:
                session.add(
                    PaperTradeRow(
                        portfolio_id="p1", origin_strategy_id="smc_ict", strategy_version="1.0.0",
                        management_policy_version="1.0.0", symbol="XAUUSD", side="BUY",
                        status="OPEN", order_type="MARKET", created_at_utc=datetime.now(UTC),
                        volume_lots=0.1, stop_loss=None,  # must not be null
                    )
                )

    def test_outbox_unique_message_key(self, db):
        import pytest
        from sqlalchemy.exc import IntegrityError

        base = dict(chat_id="1", message_type="T", message_key="k1", message_text="x")
        with db.session() as session:
            session.add(TelegramOutboxRow(**base))
        with pytest.raises(IntegrityError):
            with db.session() as session:
                session.add(TelegramOutboxRow(**base))


class TestRoundTrip:
    def test_evaluation_json_fields(self, db):
        row = StrategyEvaluationRow(
            id="ev-1", timestamp_utc=datetime.now(UTC), strategy_id="smc_ict",
            strategy_version="1.0.0", status="CANDIDATE", direction="BUY",
            setup_stage="TRIGGERED", conditions_passed=dumps([{"condition_id": "c1", "passed": True}]),
            feature_snapshot=dumps({"atr": 2.5}),
        )
        with db.session() as session:
            session.add(row)
        with db.session() as session:
            loaded = session.get(StrategyEvaluationRow, "ev-1")
            assert loads(loaded.conditions_passed)[0]["condition_id"] == "c1"
            assert loads(loaded.feature_snapshot)["atr"] == 2.5

    def test_migration_runner_idempotent(self, tmp_path):
        database = Database(f"sqlite:///{tmp_path}/mig.db")
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "0001_init.sql").write_text(
            "CREATE TABLE IF NOT EXISTS mig_test (id INTEGER PRIMARY KEY, name TEXT)", encoding="utf-8"
        )
        runner = MigrationRunner(database.engine, migrations_dir)
        applied1 = runner.upgrade()
        applied2 = runner.upgrade()
        assert applied1 == ["0001_init"]
        assert applied2 == []  # idempotent
        assert "0001_init" in runner.applied()


class TestPaperOnlyGuards:
    def test_no_real_order_tables_or_functions(self):
        """Spec test H: no real-order execution surface anywhere in app code."""
        import subprocess

        root = "/home/z/my-project/gold_hybrid_engine/app"
        result = subprocess.run(
            ["grep", "-rn", "-E", r"order_send\(|OrderSend\(|TRADE_ACTION_DEAL|\.positions_add\(", root],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "", f"forbidden execution call found:\n{result.stdout}"

    def test_mt5_adapter_has_no_order_methods(self):
        from app.data.mt5_adapter import Mt5Adapter

        adapter = Mt5Adapter()
        forbidden = [
            m for m in dir(adapter)
            if any(f in m.lower() for f in ("order", "position", "deal"))
            and not m.startswith("_")  # exclude private helpers
        ]
        assert forbidden == [], f"MT5 adapter exposes execution-like methods: {forbidden}"

    def test_startup_refuses_real_execution(self, config, db, replay_provider, config_version):
        import pytest

        from app.bootstrap import HybridRuntime
        from app.core.config import ConfigVersion
        from app.core.exceptions import RealExecutionDisabledError

        bad_data = dict(config_version.data)
        bad_data["paper_execution"] = {**bad_data["paper_execution"], "real_execution_enabled": True}
        bad_cv = ConfigVersion(
            config_version_id="bad", config_hash="bad", loaded_at_utc=config_version.loaded_at_utc,
            source="FILE", data=bad_data,
        )
        with pytest.raises(RealExecutionDisabledError):
            HybridRuntime(config_version=bad_cv, database=db, provider=replay_provider)
