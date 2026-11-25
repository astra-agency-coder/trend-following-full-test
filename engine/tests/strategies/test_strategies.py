"""Strategy tests: each school produces documented behavior on crafted data."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.core.enums import DataQuality, Direction, EvaluationStatus, SetupStage
from app.core.ids import new_uuid
from app.data.bar_service import BarService
from app.data.contracts import MarketBar
from app.market_context.engine import MarketContextEngine
from app.market_context.sessions import SessionEngine
from app.strategies.breakout.strategy import BreakoutStrategy
from app.strategies.mean_reversion.strategy import MeanReversionStrategy
from app.strategies.order_flow.strategy import OrderFlowStrategy
from app.strategies.registry import StrategyRegistry
from app.strategies.smc_ict.strategy import SmcIctStrategy
from app.strategies.trend_following.strategy import TrendFollowingStrategy
from app.strategies.wyckoff.strategy import WyckoffStrategy
from tests.fixtures.conftest_fixtures import SESSIONS_CFG


def build_context(bars, tick, now):
    service = BarService(persist=False)
    service.ingest_bars(bars)
    sessions = SessionEngine.from_config(SESSIONS_CFG)
    engine = MarketContextEngine(symbol="XAUUSD", source_symbol="XAUUSD", bar_service=service, session_engine=sessions)
    return service, engine.build_snapshot(
        now_utc=now, tick=tick, data_quality=DataQuality.VALID, snapshot_id=new_uuid()
    )


def flat_bars(count=120, price=2000.0, timeframe="M15", start=None):
    """Perfectly flat market — no valid setup should trigger."""
    start = start or datetime(2026, 1, 5, tzinfo=UTC)
    out = []
    for i in range(count):
        o = start + timedelta(minutes=15 * i)
        out.append(MarketBar(
            canonical_symbol="XAUUSD", source_symbol="XAUUSD", timeframe=timeframe,
            open_time_utc=o, close_time_utc=o + timedelta(minutes=15),
            open=price, high=price + 0.5, low=price - 0.5, close=price, tick_volume=100.0,
        ))
    return out


class TestAllStrategies:
    def test_all_evaluate_without_error(self, market_data, config):
        from app.data.replay_provider import make_mock_tick

        bars = market_data["m5"] + market_data["m15"] + market_data["h1"]
        now = market_data["m15"][-1].close_time_utc + timedelta(seconds=1)
        tick = market_data["ticks"][-1]
        service, snapshot = build_context(bars, tick, now)
        registry = StrategyRegistry(config)
        for strategy in registry.enabled_strategies():
            ev = strategy.evaluate(snapshot, service, config)
            assert ev.status in (EvaluationStatus.NO_SETUP, EvaluationStatus.CANDIDATE)
            assert ev.evaluation_started_at_utc is not None
            # NO_SETUP always carries machine-readable reasons
            if ev.status == EvaluationStatus.NO_SETUP:
                assert ev.rejection_reason_codes, f"{strategy.strategy_id} returned NO_SETUP without reasons"

    def test_invalid_data_blocks_evaluation(self, market_data, config):
        from app.data.replay_provider import make_mock_tick

        bars = market_data["m15"]
        now = bars[-1].close_time_utc + timedelta(seconds=1)
        service = BarService(persist=False)
        service.ingest_bars(bars)
        sessions = SessionEngine.from_config(SESSIONS_CFG)
        engine = MarketContextEngine(symbol="XAUUSD", source_symbol="XAUUSD", bar_service=service, session_engine=sessions)
        snapshot = engine.build_snapshot(
            now_utc=now, tick=make_mock_tick(time_utc=now, bid=2000, ask=2000.3),
            data_quality=DataQuality.STALE, snapshot_id=new_uuid(),
        )
        for strategy in (SmcIctStrategy(config={}), WyckoffStrategy(config={}), TrendFollowingStrategy(config={})):
            ev = strategy.evaluate(snapshot, service, config)
            assert ev.status == EvaluationStatus.INVALID_DATA

    def test_flat_market_no_candidates(self, config):
        from app.data.replay_provider import make_mock_tick

        bars = flat_bars()
        now = bars[-1].close_time_utc + timedelta(seconds=1)
        service, snapshot = build_context(bars, make_mock_tick(time_utc=now, bid=2000, ask=2000.3), now)
        for strategy in (SmcIctStrategy(config={}), WyckoffStrategy(config={}), BreakoutStrategy(config={}),
                          MeanReversionStrategy(config={})):
            ev = strategy.evaluate(snapshot, service, config)
            assert ev.status != EvaluationStatus.CANDIDATE, f"{strategy.strategy_id} traded a flat market"

    def test_candidate_has_complete_contract(self, market_data, config):
        """Every CANDIDATE must carry entry/SL/TP-or-policy/direction/RR fields (spec 32.5)."""
        registry = StrategyRegistry(config)
        bars = market_data["m5"] + market_data["m15"] + market_data["h1"]
        found = 0
        # scan the last 20 M15 closes — at least one candidate must appear
        for bar in market_data["m15"][-20:]:
            now = bar.close_time_utc + timedelta(seconds=1)
            service, snapshot = build_context(bars, market_data["ticks"][-1], now)
            for strategy in registry.enabled_strategies():
                ev = strategy.evaluate(snapshot, service, config)
                if ev.status == EvaluationStatus.CANDIDATE:
                    found += 1
                    assert ev.direction in (Direction.BUY, Direction.SELL)
                    assert ev.stop_loss is not None and ev.entry_price_proposal is not None
                    assert ev.setup_stage == SetupStage.TRIGGERED
                    assert ev.opportunity_key
                    assert ev.exit_plan is not None
                    if ev.take_profit is None:
                        assert ev.exit_plan.exit_policy in ("TRAILING_ATR", "TRAILING_STRUCTURE")
            if found:
                break
        assert found >= 1  # the seeded random-walk data reliably yields at least one


class TestSmcIct:
    def test_fvg_detection(self):
        strategy = SmcIctStrategy(config={})
        start = datetime(2026, 1, 5, tzinfo=UTC)
        bars = flat_bars(20, start=start)
        # craft a bullish FVG: gap up between bar i-1 high and bar i+1 low
        i = 10
        bars[i - 1] = replace(bars[i - 1], high=2000.0)
        bars[i] = replace(bars[i], open=2002.0, close=2004.0, high=2004.5, low=2001.5)
        bars[i + 1] = replace(bars[i + 1], low=2002.5)
        fvgs = strategy.detect_fvgs(bars, min_size=0.5, atr_value=2.0)
        bullish = [f for f in fvgs if f["type"] == "BULLISH"]
        assert any(abs(f["bottom"] - 2000.0) < 0.01 and abs(f["top"] - 2002.5) < 0.01 for f in bullish)

    def test_displacement_detection(self):
        strategy = SmcIctStrategy(config={})
        start = datetime(2026, 1, 5, tzinfo=UTC)
        bars = flat_bars(20, start=start)
        bars[10] = replace(bars[10], open=2000.0, close=2006.0, high=2006.2, low=1999.8)
        disp = strategy.detect_displacement(bars, atr_value=2.0, body_mult=1.2, range_mult=1.5)
        assert any(d["direction"] == "BULLISH" for d in disp)

    def test_equal_level_clustering(self):
        strategy = SmcIctStrategy(config={})
        clusters = strategy.find_equal_levels([2000.0, 2000.2, 2005.0], tolerance=0.5)
        assert len(clusters) == 2  # 2000-ish cluster + 2005


class TestOrderFlow:
    def test_disabled_with_reason(self, market_data, config):
        from app.data.replay_provider import make_mock_tick

        bars = market_data["m15"]
        now = bars[-1].close_time_utc + timedelta(seconds=1)
        service, snapshot = build_context(bars, make_mock_tick(time_utc=now, bid=2000, ask=2000.3), now)
        strategy = OrderFlowStrategy(config={})
        ev = strategy.evaluate(snapshot, service, config)
        assert ev.status == EvaluationStatus.NO_SETUP
        assert "ORDER_FLOW_DISABLED" in " ".join(ev.rejection_reason_codes)
        assert strategy.health_status() == "DISABLED_DATA_UNAVAILABLE"
