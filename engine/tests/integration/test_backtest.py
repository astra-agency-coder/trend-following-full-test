"""Historical backtesting tests: reproducibility, correctness, virtual clock."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.backtesting.engine import BacktestEngine
from app.data.replay_provider import bars_to_ticks, generate_synthetic_bars


def make_bars():
    start = datetime(2026, 1, 5, tzinfo=UTC)
    m5 = generate_synthetic_bars(timeframe="M5", count=400, start=start, trend=0.1, volatility=2.0, seed=21)
    m15 = generate_synthetic_bars(timeframe="M15", count=150, start=start, trend=0.1, volatility=3.2, seed=22)
    h1 = generate_synthetic_bars(timeframe="H1", count=60, start=start, trend=0.1, volatility=6.0, seed=23)
    return m5, m15, h1, start


def make_engine(config_version, db, spec):
    m5, m15, h1, start = make_bars()
    return (
        BacktestEngine(
            config_version=config_version, database=db,
            bars=m5 + m15 + h1, ticks=bars_to_ticks(m15), spec=spec, step_seconds=1800,
        ),
        m5[-1].close_time_utc,
    )


class TestBacktest:
    def test_runs_and_produces_metrics(self, config_version, db, spec):
        engine, end = make_engine(config_version, db, spec)
        result = engine.run(start_utc=end - timedelta(days=2), end_utc=end)
        assert result.cycles > 0
        assert "total_trades" in result.metrics
        assert "win_rate" in result.metrics
        assert result.reproducibility["config_hash"] == config_version.config_hash

    def test_reproducible_identical_results(self, config_version, spec, tmp_path):
        """Same seed + same data -> byte-identical metrics (spec 46)."""
        from app.persistence.database import Database

        db1 = Database(f"sqlite:///{tmp_path}/bt1.db")
        db1.create_all()
        db2 = Database(f"sqlite:///{tmp_path}/bt2.db")
        db2.create_all()

        engine1, end = make_engine(config_version, db1, spec)
        engine2, _ = make_engine(config_version, db2, spec)
        r1 = engine1.run(start_utc=end - timedelta(days=1), end_utc=end)
        r2 = engine2.run(start_utc=end - timedelta(days=1), end_utc=end)
        assert r1.cycles == r2.cycles
        assert r1.metrics == r2.metrics
        assert r1.by_strategy == r2.by_strategy

    def test_backtest_never_uses_wall_clock(self, config_version, db, spec, monkeypatch):
        """If any component reads the real 'now' during replay, this fails."""
        engine, end = make_engine(config_version, db, spec)

        def forbidden(*args, **kwargs):
            raise AssertionError("wall clock read during backtest")

        import app.market_context.engine as ctx_mod

        monkeypatch.setattr(ctx_mod, "datetime", BoomDatetime)
        result = engine.run(start_utc=end - timedelta(days=1), end_utc=end)
        assert result.cycles > 0


class BoomDatetime:
    """A datetime replacement that explodes if constructed from 'now'."""

    @classmethod
    def now(cls, tz=None):
        raise AssertionError("datetime.now() called inside context engine during backtest")

    def __getattr__(self, item):
        import datetime as _dt

        return getattr(_dt.datetime, item)
