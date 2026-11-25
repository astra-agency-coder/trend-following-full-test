"""Hybrid Decision Engine tests: eligibility, ranking, anti-churn, duplicates, states."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.enums import (
    DataQuality,
    Direction,
    EvaluationStatus,
    RouterDecisionStatus,
    SetupStage,
)
from app.core.ids import new_uuid
from app.hybrid.router import HybridDecisionEngine
from app.market_context.engine import MarketSnapshot
from app.strategies.base import ExitPlan, StrategyEvaluation


def make_snapshot(now: datetime) -> MarketSnapshot:
    from app.market_context.sessions import SessionInfo
    from app.core.enums import SessionPhase

    return MarketSnapshot(
        snapshot_id=new_uuid(), timestamp_utc=now, symbol="XAUUSD", source_symbol="XAUUSD",
        data_quality=DataQuality.VALID, trend_state="BULLISH", volatility_state="NORMAL",
        activity_state="NORMAL", liquidity_proxy_state="NORMAL_PROXY",
        market_structure=["BOS_CONFIRMED"],
        session=SessionInfo(
            session_name="LONDON", phase=SessionPhase.MID_SESSION, timezone_name="Europe/London",
            local_time="10:00", minutes_from_open=120, minutes_to_close=420, is_overlap=False,
            active_sessions=("LONDON",),
            session_start_utc=now - timedelta(hours=2), session_end_utc=now + timedelta(hours=7),
        ),
        spread_points=25.0, bid=2000.0, ask=2000.3,
    )


def make_evaluation(strategy_id="smc_ict", *, direction=Direction.BUY, entry=2000.0, sl=1995.0,
                    tp=2012.0, rr=2.4, stage=SetupStage.TRIGGERED, status=EvaluationStatus.CANDIDATE,
                    key="SMC:TEST", quality=80.0, fit=70.0, valid_minutes=60) -> StrategyEvaluation:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    return StrategyEvaluation(
        evaluation_id=new_uuid(), strategy_id=strategy_id, strategy_version="1.0.0",
        config_version="test", evaluation_timestamp_utc=now, market_snapshot_id="snap",
        status=status, direction=direction, setup_type="TEST_SETUP", setup_stage=stage,
        entry_price_proposal=entry, stop_loss=sl, take_profit=tp,
        exit_plan=ExitPlan(take_profit=tp, exit_policy="FIXED_TP"),
        risk_reward_estimate=rr, initial_risk_distance=abs(entry - sl),
        valid_until_utc=now + timedelta(minutes=valid_minutes),
        setup_quality_score=quality, market_fit_score=fit, opportunity_key=key,
    )


class TestEligibility:
    def test_valid_candidate_eligible(self):
        router = HybridDecisionEngine({})
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ranked = router.check_eligibility(make_evaluation(), make_snapshot(now), now_utc=now)
        assert ranked.eligible
        assert ranked.score_total > 0
        assert set(ranked.score_components) == {
            "setup_integrity", "market_context_fit", "timing_freshness", "execution_feasibility", "risk_plan_quality",
        }

    def test_sl_wrong_side_rejected(self):
        router = HybridDecisionEngine({})
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation(direction=Direction.BUY, sl=2005.0)  # SL above entry for BUY
        ranked = router.check_eligibility(ev, make_snapshot(now), now_utc=now)
        assert not ranked.eligible
        assert "ELIGIBILITY_SL_WRONG_SIDE" in ranked.rejection_codes

    def test_expired_setup_rejected(self):
        router = HybridDecisionEngine({})
        now = datetime(2026, 1, 5, 13, 0, tzinfo=UTC)
        ev = make_evaluation(valid_minutes=30)  # expired 30 min ago
        ranked = router.check_eligibility(ev, make_snapshot(now), now_utc=now)
        assert "ELIGIBILITY_SETUP_EXPIRED" in ranked.rejection_codes

    def test_not_triggered_rejected(self):
        router = HybridDecisionEngine({})
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation(stage=SetupStage.ARMED)
        ranked = router.check_eligibility(ev, make_snapshot(now), now_utc=now)
        assert "ELIGIBILITY_NOT_TRIGGERED" in ranked.rejection_codes

    def test_no_exit_plan_rejected(self):
        router = HybridDecisionEngine({})
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation()
        ev.take_profit = None
        ev.exit_plan = None
        ranked = router.check_eligibility(ev, make_snapshot(now), now_utc=now)
        assert "ELIGIBILITY_NO_EXECUTABLE_EXIT" in ranked.rejection_codes

    def test_trailing_policy_is_executable(self):
        router = HybridDecisionEngine({})
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ev = make_evaluation()
        ev.take_profit = None
        ev.risk_reward_estimate = None
        ev.exit_plan = ExitPlan(take_profit=None, exit_policy="TRAILING_ATR", trailing_atr_mult=2.0)
        ranked = router.check_eligibility(ev, make_snapshot(now), now_utc=now)
        assert ranked.eligible


class TestSelection:
    def _router(self, cfg=None):
        return HybridDecisionEngine(cfg or {})

    def test_single_candidate_selected(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[make_evaluation()],
            now_utc=now, hard_blocks=[], executed_opportunity_keys=set(),
        )
        assert decision.decision_status == RouterDecisionStatus.SELECTED
        assert decision.selected.strategy_id == "smc_ict"

    def test_best_score_wins(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        weak = make_evaluation(strategy_id="weak", quality=50.0, rr=1.1, key="W:1")
        strong = make_evaluation(strategy_id="strong", quality=95.0, rr=3.0, key="S:1")
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[weak, strong],
            now_utc=now, hard_blocks=[], executed_opportunity_keys=set(),
        )
        assert decision.selected.strategy_id == "strong"
        top = min(decision.ranked, key=lambda r: r.rank_position)
        assert top.evaluation.strategy_id == "strong" and top.rank_position == 1

    def test_duplicate_opportunity_not_re_entered(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[make_evaluation()],
            now_utc=now, hard_blocks=[], executed_opportunity_keys=set(),
        )
        assert decision.decision_status == RouterDecisionStatus.SELECTED
        again = router.decide(
            snapshot=make_snapshot(now), evaluations=[make_evaluation()],
            now_utc=now, hard_blocks=[], executed_opportunity_keys={"SMC:TEST"},
        )
        assert again.decision_status == RouterDecisionStatus.CANDIDATE_REJECTED_BY_ROUTER
        assert any("DUPLICATE" in c for r in again.ranked for c in r.rejection_codes)

    def test_no_candidates_state(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[], now_utc=now,
            hard_blocks=[], executed_opportunity_keys=set(),
        )
        assert decision.decision_status == RouterDecisionStatus.NO_CANDIDATE
        assert decision.explanation_ar  # human-readable reason present

    def test_hard_block_state(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[make_evaluation()],
            now_utc=now, hard_blocks=["DATA_QUALITY_INVALID"], executed_opportunity_keys=set(),
        )
        assert decision.decision_status == RouterDecisionStatus.BLOCKED_BY_DATA
        assert decision.selected is None

    def test_system_pause_block(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[make_evaluation()],
            now_utc=now, hard_blocks=["SYSTEM_PAUSED"], executed_opportunity_keys=set(),
        )
        assert decision.decision_status == RouterDecisionStatus.BLOCKED_BY_SYSTEM

    def test_ranking_deterministic(self):
        """Same inputs -> same order, every time (spec 8.2-D)."""
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        snapshot = make_snapshot(now)
        evs = [make_evaluation(strategy_id=s, quality=70.0, rr=2.0, key=f"K:{s}")
               for s in ("breakout", "smc_ict", "trend_following")]
        orders = []
        for _ in range(5):
            fresh = [make_evaluation(strategy_id=s, quality=70.0, rr=2.0, key=f"K:{s}")
                     for s in ("breakout", "smc_ict", "trend_following")]
            decision = router.decide(
                snapshot=snapshot, evaluations=fresh, now_utc=now,
                hard_blocks=[], executed_opportunity_keys=set(),
            )
            orders.append([r.evaluation.strategy_id for r in sorted(decision.ranked, key=lambda x: x.rank_position)])
        assert all(o == orders[0] for o in orders)

    def test_no_consensus_required(self):
        """One eligible candidate among failures is enough (spec 0.11)."""
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        failing = make_evaluation(strategy_id="fail", status=EvaluationStatus.NO_SETUP, key="F:1")
        winning = make_evaluation(strategy_id="win", key="W:1")
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[failing, winning],
            now_utc=now, hard_blocks=[], executed_opportunity_keys=set(),
        )
        assert decision.decision_status == RouterDecisionStatus.SELECTED
        assert decision.selected.strategy_id == "win"

    def test_decision_documented(self):
        router = self._router()
        now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        decision = router.decide(
            snapshot=make_snapshot(now), evaluations=[make_evaluation()],
            now_utc=now, hard_blocks=[], executed_opportunity_keys=set(),
        )
        payload = decision.to_dict()
        assert payload["explanation_ar"] and payload["explanation"]
        assert payload["ranked_candidates"][0]["score_components"]
        assert payload["decision_status"] == "SELECTED"
