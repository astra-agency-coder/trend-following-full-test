"""Hybrid Decision Engine (spec sections 8 / 30 / 36).

The ONLY component allowed to pick which school trades. Pipeline:

  evaluations -> eligibility -> ranking -> selection -> risk -> paper trade

Key policies implemented here:
- Decision states distinguish NO_CANDIDATE / CANDIDATE_REJECTED_BY_ROUTER /
  BLOCKED_BY_RISK / BLOCKED_BY_DATA / BLOCKED_BY_SYSTEM (spec 36.5).
- No consensus requirement; a single eligible candidate trades.
- Deterministic ranking + tie-breaking (never random).
- Anti-churn (hysteresis) affects *preferred strategy* reporting only, never
  delays a valid entry (spec 36.8).
- Only TRIGGERED candidates are entry-capable; ARMED is monitoring-only.
- Duplicate opportunity keys are rejected (idempotency, spec 19).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import (
    DataQuality,
    Direction,
    EvaluationStatus,
    RouterDecisionStatus,
    SetupStage,
)
from app.core.ids import new_uuid
from app.core.logging_setup import get_logger
from app.market_context.engine import MarketSnapshot
from app.strategies.base import StrategyEvaluation

logger = get_logger("hybrid.router")


@dataclass
class RankedCandidate:
    evaluation: StrategyEvaluation
    score_total: float
    score_components: dict[str, float]
    eligible: bool
    rejection_codes: list[str] = field(default_factory=list)
    rank_position: int | None = None


@dataclass
class RouterDecision:
    decision_id: str
    timestamp_utc: datetime
    market_snapshot_id: str | None
    decision_status: RouterDecisionStatus
    selected: StrategyEvaluation | None
    ranked: list[RankedCandidate]
    hard_blocks: list[str]
    explanation: str
    explanation_ar: str
    previous_preferred: str | None
    new_preferred: str | None
    no_trade_reasons: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "market_snapshot_id": self.market_snapshot_id,
            "decision_status": self.decision_status.value,
            "selected_strategy_id": self.selected.strategy_id if self.selected else None,
            "selected_evaluation_id": self.selected.evaluation_id if self.selected else None,
            "ranked_candidates": [
                {
                    "strategy_id": r.evaluation.strategy_id,
                    "evaluation_id": r.evaluation.evaluation_id,
                    "score_total": round(r.score_total, 2),
                    "score_components": {k: round(v, 2) for k, v in r.score_components.items()},
                    "eligible": r.eligible,
                    "rank_position": r.rank_position,
                    "rejection_codes": r.rejection_codes,
                    "setup_type": r.evaluation.setup_type,
                    "direction": r.evaluation.direction.value,
                }
                for r in self.ranked
            ],
            "hard_blocks": self.hard_blocks,
            "explanation": self.explanation,
            "explanation_ar": self.explanation_ar,
            "previous_preferred": self.previous_preferred,
            "new_preferred": self.new_preferred,
            "no_trade_reasons": self.no_trade_reasons,
        }


class HybridDecisionEngine:
    """Rank & select one school per entry opportunity (router_version 1.0.0)."""

    router_version = "1.0.0"

    def __init__(self, config: dict, *, router_cfg: dict | None = None):
        self.config = config
        rcfg = router_cfg or config.get("hybrid_router", {})
        self.weights = {
            "setup_integrity": float(rcfg.get("weight_setup_integrity", 0.30)),
            "market_context_fit": float(rcfg.get("weight_market_fit", 0.25)),
            "timing_freshness": float(rcfg.get("weight_timing", 0.15)),
            "execution_feasibility": float(rcfg.get("weight_execution", 0.15)),
            "risk_plan_quality": float(rcfg.get("weight_risk_plan", 0.15)),
        }
        self.min_score = float(rcfg.get("min_candidate_score", 0.0))  # default: no arbitrary bar
        self.preferred_strategy: str | None = None
        self.preference_since: datetime | None = None
        self.anti_churn_margin = float(rcfg.get("anti_churn_score_margin", 0.0))
        self.min_preference_minutes = float(rcfg.get("min_preference_duration_minutes", 0.0))

    # ------------------------------------------------------------- eligibility
    def check_eligibility(self, evaluation: StrategyEvaluation, snapshot: MarketSnapshot, *, now_utc: datetime) -> RankedCandidate:
        """Phase B checks (spec 8.2-B). Returns a ranked candidate with verdict."""
        codes: list[str] = []

        if evaluation.status == EvaluationStatus.ERROR:
            codes.append("ELIGIBILITY_STRATEGY_ERROR")
        if evaluation.status == EvaluationStatus.INVALID_DATA:
            codes.append("ELIGIBILITY_INVALID_DATA")
        if evaluation.status == EvaluationStatus.CANDIDATE:
            if evaluation.direction == Direction.NONE:
                codes.append("ELIGIBILITY_NO_DIRECTION")
            if evaluation.stop_loss is None:
                codes.append("ELIGIBILITY_NO_STOP_LOSS")
            if evaluation.entry_price_proposal is None:
                codes.append("ELIGIBILITY_NO_ENTRY")
            # stop on the wrong side of entry
            if evaluation.stop_loss is not None and evaluation.entry_price_proposal is not None:
                if evaluation.direction == Direction.BUY and evaluation.stop_loss >= evaluation.entry_price_proposal:
                    codes.append("ELIGIBILITY_SL_WRONG_SIDE")
                if evaluation.direction == Direction.SELL and evaluation.stop_loss <= evaluation.entry_price_proposal:
                    codes.append("ELIGIBILITY_SL_WRONG_SIDE")
            # expired?
            if evaluation.valid_until_utc is not None and now_utc > evaluation.valid_until_utc:
                codes.append("ELIGIBILITY_SETUP_EXPIRED")
            # only TRIGGERED stage may enter (spec 36.6)
            if evaluation.setup_stage not in (SetupStage.TRIGGERED,):
                codes.append("ELIGIBILITY_NOT_TRIGGERED")
            # exit plan must be executable (TP or trailing policy)
            if evaluation.take_profit is None and (
                evaluation.exit_plan is None or evaluation.exit_plan.exit_policy not in ("TRAILING_ATR", "TRAILING_STRUCTURE")
            ):
                codes.append("ELIGIBILITY_NO_EXECUTABLE_EXIT")

        eligible = evaluation.status == EvaluationStatus.CANDIDATE and not codes
        if eligible:
            score, components = self._score(evaluation, snapshot)
            if score < self.min_score:
                eligible = False
                codes.append("ELIGIBILITY_SCORE_BELOW_MIN")
        else:
            score, components = 0.0, {}

        return RankedCandidate(
            evaluation=evaluation,
            score_total=score if eligible else 0.0,
            score_components=components,
            eligible=eligible,
            rejection_codes=codes,
        )

    # ------------------------------------------------------------- scoring
    def _score(self, evaluation: StrategyEvaluation, snapshot: MarketSnapshot) -> tuple[float, dict[str, float]]:
        """Deterministic 0..1 components — a *ranking* score, not a win probability."""
        w = self.weights

        # setup integrity: quality score normalized + RR contribution
        quality = (evaluation.setup_quality_score or 50.0) / 100.0
        rr = evaluation.risk_reward_estimate
        rr_component = min(1.0, (rr / 3.0)) if rr is not None else 0.55  # trailing setups get neutral value
        setup_integrity = 0.6 * quality + 0.4 * rr_component

        # market context fit: school's own fit score
        market_fit = evaluation.market_fit_score / 100.0

        # timing freshness: was the setup triggered on this snapshot?
        timing = 1.0 if evaluation.setup_stage == SetupStage.TRIGGERED else 0.3

        # execution feasibility: spread sanity + valid risk distance
        feasibility = 1.0
        if snapshot.spread_points is not None and snapshot.spread_points > 50:
            feasibility -= 0.3
        if evaluation.initial_risk_distance is not None and evaluation.initial_risk_distance <= 0:
            feasibility = 0.0

        # risk plan quality: SL distance sane vs ATR, RR valid
        risk_plan = 0.5
        if rr is not None and rr >= 1.5:
            risk_plan = 1.0
        elif rr is not None and rr >= 1.0:
            risk_plan = 0.7
        if evaluation.exit_plan is not None and evaluation.exit_plan.break_even_at_r:
            risk_plan = min(1.0, risk_plan + 0.1)

        components = {
            "setup_integrity": setup_integrity,
            "market_context_fit": market_fit,
            "timing_freshness": timing,
            "execution_feasibility": feasibility,
            "risk_plan_quality": risk_plan,
        }
        total = sum(w[k] * v for k, v in components.items())
        return total, components

    # ------------------------------------------------------------- main cycle
    def decide(
        self,
        *,
        snapshot: MarketSnapshot,
        evaluations: list[StrategyEvaluation],
        now_utc: datetime,
        hard_blocks: list[str],
        executed_opportunity_keys: set[str],
    ) -> RouterDecision:
        ranked = [
            self.check_eligibility(ev, snapshot, now_utc=now_utc)
            for ev in evaluations
            if ev.status == EvaluationStatus.CANDIDATE
        ]

        # duplicate protection (spec 19)
        for candidate in ranked:
            key = candidate.evaluation.opportunity_key
            if key and key in executed_opportunity_keys:
                candidate.eligible = False
                candidate.rejection_codes.append("ELIGIBILITY_DUPLICATE_OPPORTUNITY")

        eligible = [c for c in ranked if c.eligible]
        eligible.sort(key=self._sort_key)

        previous_preferred = self.preferred_strategy
        no_trade_reasons = {
            ev.strategy_id: ev.rejection_reason_codes or [ev.status.value]
            for ev in evaluations
            if ev.status != EvaluationStatus.CANDIDATE
        }

        if hard_blocks:
            decision_status = RouterDecisionStatus.BLOCKED_BY_SYSTEM if "SYSTEM_PAUSED" in hard_blocks else RouterDecisionStatus.BLOCKED_BY_DATA
            explanation = f"hard blocks active: {', '.join(hard_blocks)}"
            explanation_ar = f"مانع صريح يمنع الدخول: {', '.join(hard_blocks)}"
            self._update_preferred(None, now_utc)
            return RouterDecision(
                decision_id=new_uuid(),
                timestamp_utc=now_utc,
                market_snapshot_id=snapshot.snapshot_id,
                decision_status=decision_status,
                selected=None,
                ranked=self._assign_ranks(ranked),
                hard_blocks=hard_blocks,
                explanation=explanation,
                explanation_ar=explanation_ar,
                previous_preferred=previous_preferred,
                new_preferred=self.preferred_strategy,
                no_trade_reasons=no_trade_reasons,
            )

        if not eligible:
            status = (
                RouterDecisionStatus.NO_CANDIDATE
                if not ranked
                else RouterDecisionStatus.CANDIDATE_REJECTED_BY_ROUTER
            )
            explanation = (
                "no school produced a candidate" if not ranked else
                f"{len(ranked)} candidate(s) failed eligibility: "
                + "; ".join(f"{c.evaluation.strategy_id}:{','.join(c.rejection_codes)}" for c in ranked)
            )
            explanation_ar = (
                "لم تُكوّن أي مدرسة فرصة مؤهلة في هذه الدورة" if not ranked else
                "فرص موجودة لكنها غير مؤهلة: "
                + "؛ ".join(f"{c.evaluation.strategy_id}: {','.join(c.rejection_codes)}" for c in ranked)
            )
            return RouterDecision(
                decision_id=new_uuid(),
                timestamp_utc=now_utc,
                market_snapshot_id=snapshot.snapshot_id,
                decision_status=status,
                selected=None,
                ranked=self._assign_ranks(ranked),
                hard_blocks=[],
                explanation=explanation,
                explanation_ar=explanation_ar,
                previous_preferred=previous_preferred,
                new_preferred=self.preferred_strategy,
                no_trade_reasons=no_trade_reasons,
            )

        # ---- selection with deterministic tie-break (spec 8.2-D)
        selected = eligible[0]
        if len(eligible) > 1:
            tie_note = ""
            best, second = eligible[0], eligible[1]
            if abs(best.score_total - second.score_total) < 1e-9:
                tie_note = " (tie broken by setup quality, then R:R, then configured strategy order)"
        self._update_preferred(selected.evaluation.strategy_id, now_utc)

        explanation = (
            f"selected {selected.evaluation.strategy_id} "
            f"(score {selected.score_total:.3f}, setup {selected.evaluation.setup_type}); "
            f"other eligible: {[c.evaluation.strategy_id for c in eligible[1:]]}"
        )
        explanation_ar = (
            f"تم اختيار مدرسة {selected.evaluation.strategy_id} "
            f"(نتيجة الترتيب {selected.score_total:.2f}، النمط {selected.evaluation.setup_type})؛ "
            f"المدارس المؤهلة الأخرى: {[c.evaluation.strategy_id for c in eligible[1:]] or 'لا يوجد'}"
        )

        return RouterDecision(
            decision_id=new_uuid(),
            timestamp_utc=now_utc,
            market_snapshot_id=snapshot.snapshot_id,
            decision_status=RouterDecisionStatus.SELECTED,
            selected=selected.evaluation,
            ranked=self._assign_ranks(ranked),
            hard_blocks=[],
            explanation=explanation,
            explanation_ar=explanation_ar,
            previous_preferred=previous_preferred,
            new_preferred=self.preferred_strategy,
            no_trade_reasons=no_trade_reasons,
        )

    def _sort_key(self, candidate: RankedCandidate):
        """Deterministic: score desc, setup quality desc, RR desc, strategy order."""
        order = ["smc_ict", "wyckoff", "trend_following", "mean_reversion", "breakout", "order_flow"]
        ev = candidate.evaluation
        return (
            -candidate.score_total,
            -(ev.setup_quality_score or 0),
            -(ev.risk_reward_estimate or 0),
            order.index(ev.strategy_id) if ev.strategy_id in order else 99,
        )

    def _assign_ranks(self, ranked: list[RankedCandidate]) -> list[RankedCandidate]:
        eligible_sorted = sorted([c for c in ranked if c.eligible], key=self._sort_key)
        for i, candidate in enumerate(eligible_sorted, start=1):
            candidate.rank_position = i
        # ineligible candidates get ordered positions after the eligible ones
        ineligible = [c for c in ranked if not c.eligible]
        ineligible.sort(key=self._sort_key)
        offset = len(eligible_sorted)
        for j, candidate in enumerate(ineligible, start=1):
            candidate.rank_position = offset + j
        return ranked

    def _update_preferred(self, strategy_id: str | None, now_utc: datetime) -> None:
        """Anti-churn: only switch preferred strategy after margin/duration."""
        if strategy_id is None:
            return
        if self.preferred_strategy == strategy_id:
            return
        if (
            self.preferred_strategy is not None
            and self.min_preference_minutes > 0
            and self.preference_since is not None
            and (now_utc - self.preference_since).total_seconds() < self.min_preference_minutes * 60
        ):
            return  # keep current preferred; does NOT block the entry itself
        self.preferred_strategy = strategy_id
        self.preference_since = now_utc
