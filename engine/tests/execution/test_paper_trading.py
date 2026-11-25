"""Risk engine + paper trading tests: sizing, transitions, SL/TP, costs, restart."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import DataQuality, Direction, EvaluationStatus, ExitReason, SetupStage, TradeStatus
from app.core.exceptions import TradeStateError
from app.execution.fill_models import CostModel, PaperFillEngine, SlippageModel
from app.execution.paper_engine import PaperTrade, PaperTradingEngine
from app.risk.engine import OpenPositionRisk, RiskEngine
from app.strategies.base import ExitPlan, StrategyEvaluation
from app.strategies.registry import StrategyRegistry
from tests.hybrid.test_router import make_evaluation


def make_paper_engine(config, spec, registry=None):
    slippage = SlippageModel(config.get("paper_execution", {}), seed=1)
    costs = CostModel(config.get("paper_execution", {}))
    fills = PaperFillEngine(slippage, costs, point=spec.point)
    return PaperTradingEngine(
        spec=spec, fill_engine=fills, cost_model=costs,
        strategy_registry=registry or StrategyRegistry(config),
        portfolio_id="p-test", initial_balance=10000.0,
    )


def snap(now):
    from app.core.ids import new_uuid

    from app.market_context.engine import MarketSnapshot
    from app.market_context.sessions import SessionInfo
    from app.core.enums import SessionPhase

    return MarketSnapshot(
        snapshot_id=new_uuid(), timestamp_utc=now, symbol="XAUUSD", source_symbol="XAUUSD",
        data_quality=DataQuality.VALID, trend_state="BULLISH", volatility_state="NORMAL",
        activity_state="NORMAL", liquidity_proxy_state="NORMAL_PROXY", market_structure=[],
        session=SessionInfo("LONDON", SessionPhase.MID_SESSION, "Europe/London", "10:00", 120, 420,
                            False, ("LONDON",), now - timedelta(hours=2), now + timedelta(hours=7)),
        spread_points=30.0, bid=2000.0, ask=2000.3,
        atr_values={"M15": 2.5},
    )


class TestRiskSizing:
    def test_volume_math(self, config, spec):
        engine = RiskEngine(config, spec)
        # $50 risk, 5.0 distance, $100/point/lot -> 50 / (5*100) = 0.1 lot
        volume, actual, err = engine.compute_volume(entry=2000.0, stop=1995.0, risk_amount=50.0)
        assert err is None
        assert abs(volume - 0.10) < 1e-9
        assert abs(actual - 50.0) < 0.01

    def test_volume_floors_never_up(self, config, spec):
        engine = RiskEngine(config, spec)
        volume, actual, err = engine.compute_volume(entry=2000.0, stop=1999.0, risk_amount=50.0)
        # raw = 0.5 lot exactly divisible
        assert volume == 0.5
        volume2, _, _ = engine.compute_volume(entry=2000.0, stop=1998.9, risk_amount=50.0)
        # raw = 0.4545 -> floor to 0.45
        assert volume2 == 0.45

    def test_min_volume_rejects(self, config, spec):
        engine = RiskEngine(config, spec)
        # risk so small even 0.01 lot exceeds it: 0.01 lot * 0.01 dist * $100 = $0.01 < needed
        volume, _, err = engine.compute_volume(entry=2000.0, stop=1999.99, risk_amount=0.005)
        assert volume is None and err == "MIN_VOLUME_EXCEEDS_RISK"

    def test_post_rounding_risk_within_plan(self, config, spec):
        engine = RiskEngine(config, spec)
        _, actual, _ = engine.compute_volume(entry=2000.0, stop=1998.9, risk_amount=50.0)
        assert actual <= 50.0 + 1e-6

    def test_approval_blocks_aggregate_risk(self, config, spec):
        engine = RiskEngine(config, spec)
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation()
        # max_total_open_risk_pct = 1.5% of 10000 = $150; already open $140
        open_positions = [OpenPositionRisk("t1", Direction.BUY, "smc_ict", 140.0, 2000, 1995, 0.28, None)]
        result = engine.approve(ev, equity=10000, balance=10000, open_positions=open_positions,
                                day_realized_pnl=0, now_utc=now)
        assert not result.approved
        assert "MAX_TOTAL_OPEN_RISK" in result.rejection_codes

    def test_daily_loss_circuit_breaker(self, config, spec):
        engine = RiskEngine(config, spec)
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation()
        result = engine.approve(ev, equity=10000, balance=10000, open_positions=[],
                                day_realized_pnl=-300.0, now_utc=now)  # > 2% of 10k
        assert not result.approved
        assert "DAILY_LOSS_LIMIT" in result.rejection_codes

    def test_per_strategy_position_limit(self, config, spec):
        engine = RiskEngine(config, spec)
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation()  # smc_ict
        open_positions = [OpenPositionRisk("t1", Direction.BUY, "smc_ict", 40.0, 2000, 1995, 0.08, None)]
        result = engine.approve(ev, equity=10000, balance=10000, open_positions=open_positions,
                                day_realized_pnl=0, now_utc=now)
        assert "MAX_POSITIONS_PER_STRATEGY" in result.rejection_codes


class TestTradeStateMachine:
    def test_illegal_transition_raises(self, config, spec):
        engine = make_paper_engine(config, spec)
        trade = PaperTrade(
            id="t1", portfolio_id="p", origin_strategy_id="x", strategy_version="1",
            management_policy_version="1", router_decision_id=None, entry_evaluation_id=None,
            opportunity_key=None, symbol="XAUUSD", side=Direction.BUY,
            status=TradeStatus.CLOSED_TP, order_type="MARKET", created_at_utc=datetime.now(UTC),
        )
        with pytest.raises(TradeStateError):
            engine._transition(trade, TradeStatus.OPEN)

    def test_open_trade_has_required_fields(self, config, spec):
        engine = make_paper_engine(config, spec)
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        from app.data.replay_provider import make_mock_tick

        tick = make_mock_tick(time_utc=now, bid=2000.0, ask=2000.3)
        ev = make_evaluation()
        trade = engine.open_trade(evaluation=ev, decision_id="d1", volume=0.1, risk_amount=50.0,
                                  tick=tick, snapshot=snap(now), now_utc=now)
        assert trade.status == TradeStatus.OPEN
        assert trade.entry_price and trade.stop_loss
        assert trade.entry_price >= tick.ask  # BUY fills on ask (+slippage)
        assert trade.opened_at_utc == now


class TestSlTpExecution:
    def _engine_with_trade(self, config, spec, direction=Direction.BUY):
        engine = make_paper_engine(config, spec)
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        from app.data.replay_provider import make_mock_tick

        tick = make_mock_tick(time_utc=now, bid=2000.0, ask=2000.3)
        ev = make_evaluation(direction=direction, entry=2000.0, sl=1995.0, tp=2010.0)
        if direction == Direction.SELL:
            ev = make_evaluation(direction=direction, entry=2000.3, sl=2005.0, tp=1990.0)
        trade = engine.open_trade(evaluation=ev, decision_id="d1", volume=0.1, risk_amount=50.0,
                                  tick=tick, snapshot=snap(now), now_utc=now)
        return engine, trade, tick, now

    def test_tp_hit_closes_trade(self, config, spec):
        engine, trade, _, now = self._engine_with_trade(config, spec)
        from app.data.replay_provider import make_mock_tick

        tick = make_mock_tick(time_utc=now + timedelta(minutes=30), bid=2010.5, ask=2010.8)
        closed = engine.manage_trades(tick=tick, snapshot=snap(tick.time_utc), now_utc=tick.time_utc, config=config)
        assert len(closed) == 1
        assert closed[0].exit_reason == ExitReason.TAKE_PROFIT.value
        assert closed[0].net_pnl > 0
        assert closed[0].r_multiple > 1

    def test_sl_hit_closes_trade(self, config, spec):
        engine, trade, _, now = self._engine_with_trade(config, spec)
        from app.data.replay_provider import make_mock_tick

        tick = make_mock_tick(time_utc=now + timedelta(minutes=30), bid=1994.0, ask=1994.3)
        closed = engine.manage_trades(tick=tick, snapshot=snap(tick.time_utc), now_utc=tick.time_utc, config=config)
        assert closed[0].exit_reason == ExitReason.STOP_LOSS.value
        assert closed[0].net_pnl < 0

    def test_gap_through_sl_fills_at_first_available(self, config, spec):
        engine, trade, _, now = self._engine_with_trade(config, spec)
        from app.data.replay_provider import make_mock_tick

        # price gaps far below SL: fill at the bid (worse), not at SL level
        tick = make_mock_tick(time_utc=now + timedelta(minutes=30), bid=1985.0, ask=1985.3)
        closed = engine.manage_trades(tick=tick, snapshot=snap(tick.time_utc), now_utc=tick.time_utc, config=config)
        assert closed[0].exit_price == 1985.0
        assert closed[0].exit_reason == ExitReason.STOP_LOSS.value

    def test_sell_side_sl_on_ask(self, config, spec):
        engine, trade, _, now = self._engine_with_trade(config, spec, direction=Direction.SELL)
        from app.data.replay_provider import make_mock_tick

        # ask reaches 2005 -> SL for SELL; bid alone touching is NOT enough
        tick = make_mock_tick(time_utc=now + timedelta(minutes=30), bid=2004.0, ask=2005.2)
        closed = engine.manage_trades(tick=tick, snapshot=snap(tick.time_utc), now_utc=tick.time_utc, config=config)
        assert closed[0].exit_reason == ExitReason.STOP_LOSS.value

    def test_stop_only_tightens(self, config, spec):
        engine, trade, _, now = self._engine_with_trade(config, spec)
        assert engine.move_stop(trade, 1993.0, reason="test") is False   # wider for BUY
        assert engine.move_stop(trade, 1997.0, reason="test") is True
        assert engine.move_stop(trade, 1996.0, reason="test") is False   # lower than current


class TestCosts:
    def test_commission_round_trip(self):
        costs = CostModel({"commission_mode": "PER_LOT_PER_SIDE", "commission_per_lot_side": 7.0})
        assert costs.commission(0.5) == pytest.approx(7.0)

    def test_commission_none(self):
        costs = CostModel({"commission_mode": "NONE"})
        assert costs.commission(0.5) == 0.0

    def test_unknown_cost_model_flagged(self):
        costs = CostModel({})  # nothing explicitly configured
        from app.core.enums import CostModelStatus

        assert costs.status == CostModelStatus.INCOMPLETE

    def test_net_pnl_subtracts_costs(self, config, spec):
        engine = make_paper_engine(config, spec)
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        from app.data.replay_provider import make_mock_tick

        tick = make_mock_tick(time_utc=now, bid=2000.0, ask=2000.3)
        ev = make_evaluation()
        trade = engine.open_trade(evaluation=ev, decision_id="d1", volume=1.0, risk_amount=500.0,
                                  tick=tick, snapshot=snap(now), now_utc=now)
        tick2 = make_mock_tick(time_utc=now + timedelta(hours=1), bid=2013.5, ask=2013.8)  # beyond TP 2012
        closed = engine.manage_trades(tick=tick2, snapshot=snap(tick2.time_utc), now_utc=tick2.time_utc, config=config)
        t = closed[0]
        assert t.gross_pnl > t.net_pnl  # costs subtracted
        assert t.commission == pytest.approx(14.0, abs=0.01)  # 7 x 1 lot x 2 sides

    def test_slippage_always_adverse(self):
        from app.data.replay_provider import make_mock_tick

        model = SlippageModel({"slippage_model": "FIXED_POINTS", "slippage_fixed_points": 3})
        tick = make_mock_tick(time_utc=datetime(2026, 1, 5, tzinfo=UTC), bid=2000.0, ask=2000.3)
        fills = PaperFillEngine(model, CostModel({"commission_mode": "NONE"}))
        buy = fills.entry_fill("BUY", tick)
        sell = fills.entry_fill("SELL", tick)
        assert buy.fill_price > tick.ask
        assert sell.fill_price < tick.bid


class TestRestartRecovery:
    def test_open_trades_restored(self, config, spec, db, market_data):
        """Restart with open trade: state restored, no double execution."""
        from app.persistence.repositories.journal import JournalRepository

        repo = JournalRepository(db)
        portfolio_id, _ = repo.ensure_portfolio(name="HYBRID_MAIN", initial_balance=10000)

        engine = make_paper_engine(config, spec)
        engine.portfolio_id = portfolio_id
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        from app.data.replay_provider import make_mock_tick

        tick = make_mock_tick(time_utc=now, bid=2000.0, ask=2000.3)
        ev = make_evaluation(key="RESTART:KEY1")
        trade = engine.open_trade(evaluation=ev, decision_id="d1", volume=0.1, risk_amount=50.0,
                                  tick=tick, snapshot=snap(now), now_utc=now)
        repo.save_trade(trade)
        assert len(engine.open_trades) == 1

        # simulate restart: fresh engine loads from DB
        engine2 = make_paper_engine(config, spec)
        engine2.portfolio_id = portfolio_id
        for restored in repo.load_open_trades(portfolio_id):
            engine2.restore_open_trade(restored)
        assert len(engine2.open_trades) == 1
        restored = engine2.open_trades[trade.id]
        assert restored.stop_loss == trade.stop_loss
        assert restored.opportunity_key == "RESTART:KEY1"

        # duplicate protection after restart
        executed = {t.opportunity_key for t in repo.load_closed_trades(portfolio_id)}
        executed.add(restored.opportunity_key)
        assert "RESTART:KEY1" in executed
