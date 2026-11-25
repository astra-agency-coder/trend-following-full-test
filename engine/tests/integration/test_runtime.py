"""Integration tests: full runtime cycles, data disconnection, no-lookahead,
duplicate signals, restart recovery, backtest reproducibility."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.bootstrap import HybridRuntime
from app.core.clock import VirtualClock
from app.core.enums import DataQuality, EvaluationStatus, RouterDecisionStatus, TradeStatus
from app.data.replay_provider import ReplayProvider, make_mock_tick


def make_runtime(config_version, db, market_data, spec, *, start_offset_hours=50):
    provider = ReplayProvider(
        spec,
        bars=market_data["m5"] + market_data["m15"] + market_data["h1"],
        ticks=market_data["ticks"],
    )
    start = market_data["start"] + timedelta(hours=start_offset_hours)
    provider.set_virtual_now(start)
    clock = VirtualClock(start)
    runtime = HybridRuntime(
        config_version=config_version, database=db, provider=provider,
        clock=clock, run_mode="BACKTEST",
    )
    runtime.start()
    return runtime, provider, clock


def run_cycles(runtime, provider, clock, count=30, step=900):
    summaries = []
    for _ in range(count):
        now = clock.now_utc()
        provider.set_virtual_now(now)
        summaries.append(runtime.run_cycle(now_utc=now))
        clock.advance_seconds(step)
    return summaries


class TestFullRuntime:
    def test_cycles_execute_and_persist(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        summaries = run_cycles(runtime, provider, clock, count=25)
        assert len(summaries) == 25
        assert runtime.stats.cycles == 25
        assert runtime.stats.evaluations == 25 * 5  # five enabled schools

        # artifacts really persisted
        from sqlalchemy import func, select

        from app.persistence.models import (
            MarketSnapshotRow,
            RouterDecisionRow,
            StrategyEvaluationRow,
        )

        with db.session() as session:
            assert session.execute(select(func.count(MarketSnapshotRow.id))).scalar() >= 25
            assert session.execute(select(func.count(RouterDecisionRow.id))).scalar() == 25
            assert session.execute(select(func.count(StrategyEvaluationRow.id))).scalar() == 125
        assert runtime.audit.verify_chain() is True

    def test_trade_lifecycle_persisted(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        run_cycles(runtime, provider, clock, count=60)
        from sqlalchemy import select

        from app.persistence.models import PaperTradeRow, TradeEventRow

        with db.session() as session:
            trades = session.execute(select(PaperTradeRow)).scalars().all()
            events = session.execute(select(TradeEventRow)).scalars().all()
        # every opened trade has TRADE_OPENED event; every closed one has TRADE_CLOSED
        opened_ids = {e.trade_id for e in events if e.event_type == "TRADE_OPENED"}
        closed_ids = {e.trade_id for e in events if e.event_type == "TRADE_CLOSED"}
        for trade in trades:
            if trade.opened_at_utc:
                assert trade.id in opened_ids
            if trade.status.startswith("CLOSED"):
                assert trade.id in closed_ids
                assert trade.net_pnl is not None
                assert trade.exit_price is not None

    def test_open_trade_bound_to_origin_school(self, config_version, db, market_data, spec):
        """Trades stay owned by the school that opened them (spec 0.6)."""
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        run_cycles(runtime, provider, clock, count=40)
        for trade in runtime.paper.open_trades.values():
            assert trade.origin_strategy_id in (
                "smc_ict", "wyckoff", "trend_following", "mean_reversion", "breakout",
            )
            assert trade.strategy_version
            assert trade.entry_evaluation_id

    def test_school_error_does_not_crash_system(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        # sabotage one strategy to raise on every evaluate
        strategy = runtime.registry.strategies["wyckoff"]

        def boom(snapshot, bar_service, config):
            raise RuntimeError("wyckoff exploded")

        strategy.evaluate = boom  # type: ignore[method-assign]
        summaries = run_cycles(runtime, provider, clock, count=8)
        assert runtime.stats.cycles == 8
        assert all(s is not None for s in summaries)
        # wyckoff evaluated as ERROR but others kept working
        with db.session() as session:
            from sqlalchemy import select

            from app.persistence.models import StrategyEvaluationRow

            errors = session.execute(
                select(StrategyEvaluationRow).where(
                    StrategyEvaluationRow.strategy_id == "wyckoff",
                    StrategyEvaluationRow.status == "ERROR",
                )
            ).scalars().all()
        assert len(errors) == 8


class TestDataDisconnection:
    def test_stale_data_blocks_trading(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        # run one healthy cycle
        now = clock.now_utc()
        provider.set_virtual_now(now)
        runtime.run_cycle(now_utc=now)

        # jump virtual time far beyond data -> stale feed
        clock.advance_seconds(3600 * 30)
        now = clock.now_utc()
        provider.set_virtual_now(now)
        summary = runtime.run_cycle(now_utc=now)
        assert summary["data_ok"] is False
        assert summary["decision"] == "BLOCKED_BY_DATA"

        # health events recorded for the degradation
        from sqlalchemy import select

        from app.persistence.models import SystemHealthEventRow

        with db.session() as session:
            rows = session.execute(
                select(SystemHealthEventRow).where(SystemHealthEventRow.component == "market_data")
            ).scalars().all()
        assert any(r.message == "DATA_FEED_DEGRADED" for r in rows)

    def test_provider_disconnect_blocks_trading(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        provider.disconnect()
        now = clock.now_utc()
        provider.set_virtual_now(now)
        summary = runtime.run_cycle(now_utc=now)
        assert summary["data_ok"] is False
        assert summary["decision"] in ("BLOCKED_BY_DATA", "NO_CANDIDATE")


class TestNoLookahead:
    def test_indicators_only_use_closed_bars(self, config_version, db, market_data, spec):
        """Context at time T must not change when future bars are appended."""
        from app.data.bar_service import BarService
        from app.market_context.engine import MarketContextEngine
        from app.market_context.sessions import SessionEngine
        from tests.fixtures.conftest_fixtures import SESSIONS_CFG

        bars = market_data["m15"][:150]
        now = bars[-1].close_time_utc + timedelta(seconds=1)

        service_a = BarService(persist=False)
        service_a.ingest_bars(bars)
        service_b = BarService(persist=False)
        service_b.ingest_bars(bars + market_data["m15"][150:200])  # 50 future bars

        sessions = SessionEngine.from_config(SESSIONS_CFG)
        engine_a = MarketContextEngine(symbol="XAUUSD", source_symbol="XAUUSD", bar_service=service_a, session_engine=sessions)
        engine_b = MarketContextEngine(symbol="XAUUSD", source_symbol="XAUUSD", bar_service=service_b, session_engine=sessions)
        snap_a = engine_a.build_snapshot(now_utc=now, tick=make_mock_tick(time_utc=now, bid=2000, ask=2000.3),
                                         data_quality=DataQuality.VALID, snapshot_id="a")
        snap_b = engine_b.build_snapshot(now_utc=now, tick=make_mock_tick(time_utc=now, bid=2000, ask=2000.3),
                                         data_quality=DataQuality.VALID, snapshot_id="b")
        assert snap_a.atr_values == snap_b.atr_values
        assert snap_a.trend_state == snap_b.trend_state
        assert snap_a.feature_values.get("adx") == snap_b.feature_values.get("adx")

    def test_replay_tick_no_future_leak(self, market_data, spec):
        provider = ReplayProvider(spec, bars=[], ticks=market_data["ticks"])
        ticks = market_data["ticks"]
        mid = ticks[len(ticks) // 2]
        provider.set_virtual_now(mid.time_utc)
        latest = provider.get_latest_tick("XAUUSD")
        assert latest.time_utc <= mid.time_utc

    def test_strategy_evaluations_invariant_to_future(self, config_version, market_data):
        """Evaluating at T gives identical results with and without future bars."""
        from app.data.bar_service import BarService
        from app.market_context.engine import MarketContextEngine
        from app.market_context.sessions import SessionEngine
        from app.strategies.registry import StrategyRegistry
        from tests.fixtures.conftest_fixtures import SESSIONS_CFG

        bars = market_data["m5"][:300] + market_data["m15"][:120]
        now = market_data["m15"][119].close_time_utc + timedelta(seconds=1)
        tick = make_mock_tick(time_utc=now, bid=2000, ask=2000.3)

        service_a = BarService(persist=False)
        service_a.ingest_bars(bars)
        service_b = BarService(persist=False)
        service_b.ingest_bars(bars + market_data["m15"][120:160])

        sessions = SessionEngine.from_config(SESSIONS_CFG)
        ctx_a = MarketContextEngine(symbol="XAUUSD", source_symbol="XAUUSD", bar_service=service_a, session_engine=sessions)
        ctx_b = MarketContextEngine(symbol="XAUUSD", source_symbol="XAUUSD", bar_service=service_b, session_engine=sessions)
        snap_a = ctx_a.build_snapshot(now_utc=now, tick=tick, data_quality=DataQuality.VALID, snapshot_id="a")
        snap_b = ctx_b.build_snapshot(now_utc=now, tick=tick, data_quality=DataQuality.VALID, snapshot_id="b")

        registry = StrategyRegistry(config_version.data)
        for strategy in registry.enabled_strategies():
            ev_a = strategy.evaluate(snap_a, service_a, config_version.data)
            ev_b = strategy.evaluate(snap_b, service_b, config_version.data)
            assert ev_a.status == ev_b.status
            assert ev_a.direction == ev_b.direction
            assert ev_a.stop_loss == ev_b.stop_loss


class TestDuplicateSignals:
    def test_same_opportunity_not_executed_twice(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        run_cycles(runtime, provider, clock, count=50)
        # collect opened opportunity keys from DB
        from sqlalchemy import select

        from app.persistence.models import PaperTradeRow

        with db.session() as session:
            trades = session.execute(select(PaperTradeRow)).scalars().all()
        opened_keys = [t.opportunity_key for t in trades if t.opened_at_utc and t.opportunity_key]
        assert len(opened_keys) == len(set(opened_keys)), "duplicate opportunity executed twice"

    def test_router_duplicate_protection(self, config_version, db, market_data, spec):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        # run one cycle, capture its decision, replay same evaluations immediately
        now = clock.now_utc()
        provider.set_virtual_now(now)
        runtime.run_cycle(now_utc=now)
        if runtime.decisions:
            first = runtime.decisions[-1]
            if first.selected:
                key = first.selected.opportunity_key
                clock.advance_seconds(900)
                now = clock.now_utc()
                provider.set_virtual_now(now)
                runtime.run_cycle(now_utc=now)
                # the same key can never open a second trade
                from sqlalchemy import select

                from app.persistence.models import PaperTradeRow

                with db.session() as session:
                    same_key = session.execute(
                        select(PaperTradeRow).where(PaperTradeRow.opportunity_key == key)
                    ).scalars().all()
                assert len([t for t in same_key if t.opened_at_utc]) <= 1


class TestRestartRecovery:
    def test_runtime_restart_preserves_open_trades_and_blocks_duplicates(
        self, config_version, db, market_data, spec
    ):
        runtime, provider, clock = make_runtime(config_version, db, market_data, spec)
        run_cycles(runtime, provider, clock, count=60)
        opened_before = {t.id: t for t in runtime.paper.open_trades.values()}
        keys_before = set(runtime.executed_opportunity_keys)
        runtime.stop("RESTART_TEST")

        # fresh runtime over the SAME database
        provider2 = ReplayProvider(
            spec,
            bars=market_data["m5"] + market_data["m15"] + market_data["h1"],
            ticks=market_data["ticks"],
        )
        provider2.set_virtual_now(clock.now_utc())
        runtime2 = HybridRuntime(
            config_version=config_version, database=db, provider=provider2,
            clock=VirtualClock(clock.now_utc()), run_mode="BACKTEST",
        )
        runtime2.start()
        # open trades restored
        assert set(runtime2.paper.open_trades.keys()) == set(opened_before.keys())
        for tid, trade in runtime2.paper.open_trades.items():
            assert trade.stop_loss == opened_before[tid].stop_loss
            assert trade.origin_strategy_id == opened_before[tid].origin_strategy_id
        # executed keys preserved -> no double execution
        assert keys_before <= runtime2.executed_opportunity_keys
        # and cycles continue fine
        summaries = run_cycles(runtime2, provider2, VirtualClock(clock.now_utc()), count=5)
        assert len(summaries) == 5
