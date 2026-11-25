"""Persistence repositories — glue between engine objects and DB rows.

Contains NO trading logic (spec 4.3). Every entity keeps its version ids
and lineage so any decision can be reconstructed from the database alone.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.constants import EXECUTION_MODEL_VERSION, ROUTER_VERSION, SHADOW_CALCULATION_VERSION
from app.core.enums import DataQuality, Direction, TradeStatus
from app.core.logging_setup import get_logger
from app.execution.paper_engine import PaperTrade
from app.hybrid.router import RouterDecision
from app.market_context.engine import MarketSnapshot
from app.persistence.database import Database, dumps, loads
from app.persistence.models import (
    ConfigVersionRow,
    MarketSnapshotRow,
    PaperPortfolioRow,
    PaperTradeRow,
    RejectedOpportunityRow,
    RouterCandidateRankingRow,
    RouterDecisionRow,
    ShadowOutcomeRow,
    StrategyEvaluationRow,
    SymbolSpec as SymbolSpecRow,
    SystemRun,
    SystemHealthEventRow,
    TradeEventRow,
)
from app.strategies.base import StrategyEvaluation

logger = get_logger("persistence.repos")


class JournalRepository:
    """Writes engine artifacts to their tables."""

    def __init__(self, database: Database):
        self.db = database

    # -- config & runs
    def save_config_version(self, config_version) -> str:
        with self.db.session() as session:
            existing = session.execute(
                select(ConfigVersionRow).where(ConfigVersionRow.config_hash == config_version.config_hash)
            ).scalar_one_or_none()
            if existing:
                return existing.id
            row = ConfigVersionRow(
                config_hash=config_version.config_hash,
                source=config_version.source,
                config_json=dumps(config_version.data),
                notes=f"loaded at {config_version.loaded_at_utc.isoformat()}",
            )
            session.add(row)
            session.flush()
            return row.id

    def start_system_run(self, *, app_version: str, host: str, config_version_id: str) -> str:
        from app.core.ids import new_uuid

        row = SystemRun(
            id=new_uuid(),
            app_version=app_version,
            host_name=host,
            run_status="RUNNING",
            config_version_id=config_version_id,
        )
        with self.db.session() as session:
            session.add(row)
        return row.id

    def stop_system_run(self, run_id: str, *, reason: str) -> None:
        with self.db.session() as session:
            row = session.get(SystemRun, run_id)
            if row:
                row.run_status = "STOPPED"
                row.stop_reason = reason
                row.stopped_at_utc = datetime.now(UTC)

    def save_symbol_spec(self, spec) -> str:
        from app.core.ids import new_uuid

        row = SymbolSpecRow(
            id=new_uuid(),
            broker_name=spec.broker_name,
            source_symbol=spec.source_symbol,
            canonical_symbol=spec.canonical_symbol,
            digits=spec.digits,
            point=spec.point,
            tick_size=spec.tick_size,
            tick_value=spec.tick_value,
            contract_size=spec.contract_size,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
            currency_base=spec.currency_base,
            currency_profit=spec.currency_profit,
            raw_spec_json=dumps(spec.raw or {}),
        )
        with self.db.session() as session:
            session.add(row)
        return row.id

    def latest_symbol_spec(self, canonical_symbol: str):
        with self.db.session() as session:
            rows = (
                session.execute(
                    select(SymbolSpecRow)
                    .where(SymbolSpecRow.canonical_symbol == canonical_symbol)
                    .order_by(SymbolSpecRow.captured_at_utc.desc())
                    .limit(1)
                )
                .scalars()
                .all()
            )
        return rows[0] if rows else None

    # -- snapshots
    def save_snapshot(self, snapshot: MarketSnapshot, config_version_id: str | None) -> None:
        row = MarketSnapshotRow(
            id=snapshot.snapshot_id,
            timestamp_utc=snapshot.timestamp_utc,
            symbol=snapshot.symbol,
            source_symbol=snapshot.source_symbol,
            data_quality=snapshot.data_quality.value,
            trend_state=snapshot.trend_state.value,
            volatility_state=snapshot.volatility_state.value,
            activity_state=snapshot.activity_state.value,
            liquidity_proxy_state=snapshot.liquidity_proxy_state.value,
            market_structure=dumps(snapshot.market_structure),
            session_name=snapshot.session.session_name,
            session_phase=snapshot.session.phase.value,
            session_local_time=snapshot.session.local_time,
            is_session_overlap="1" if snapshot.session.is_overlap else "0",
            spread_points=snapshot.spread_points,
            bid=snapshot.bid,
            ask=snapshot.ask,
            feature_values=dumps(snapshot.feature_values),
            news_context=snapshot.news_context.value,
            context_version=snapshot.context_version,
            config_version_id=config_version_id,
        )
        with self.db.session() as session:
            session.merge(row)

    # -- evaluations / decisions
    def save_evaluation(self, evaluation: StrategyEvaluation) -> None:
        row = StrategyEvaluationRow(
            id=evaluation.evaluation_id,
            timestamp_utc=evaluation.evaluation_timestamp_utc,
            market_snapshot_id=evaluation.market_snapshot_id,
            strategy_id=evaluation.strategy_id,
            strategy_version=evaluation.strategy_version,
            config_version_id=evaluation.config_version,
            status=evaluation.status.value,
            direction=evaluation.direction.value,
            setup_type=evaluation.setup_type,
            setup_stage=evaluation.setup_stage.value,
            market_fit_score=evaluation.market_fit_score,
            setup_quality_score=evaluation.setup_quality_score,
            proposed_entry=evaluation.entry_price_proposal,
            proposed_sl=evaluation.stop_loss,
            proposed_tp=evaluation.take_profit,
            risk_reward=evaluation.risk_reward_estimate,
            valid_until_utc=evaluation.valid_until_utc,
            timeframes=dumps(evaluation.timeframes_used),
            conditions_passed=dumps([c.to_dict() for c in evaluation.conditions_passed]),
            conditions_failed=dumps([c.to_dict() for c in evaluation.conditions_failed]),
            score_breakdown=dumps({"market_fit": evaluation.market_fit_score, "quality": evaluation.setup_quality_score}),
            rejection_reasons=dumps(evaluation.rejection_reason_codes),
            feature_snapshot=dumps(evaluation.feature_snapshot),
            opportunity_key=evaluation.opportunity_key,
            notes=evaluation.notes,
            raw_result=dumps(evaluation.to_dict()),
        )
        with self.db.session() as session:
            session.merge(row)

    def save_decision(self, decision: RouterDecision, config_version_id: str | None) -> None:
        row = RouterDecisionRow(
            id=decision.decision_id,
            timestamp_utc=decision.timestamp_utc,
            market_snapshot_id=decision.market_snapshot_id,
            router_version=ROUTER_VERSION,
            config_version_id=config_version_id,
            selected_strategy_id=decision.selected.strategy_id if decision.selected else None,
            selected_evaluation_id=decision.selected.evaluation_id if decision.selected else None,
            decision_status=decision.decision_status.value,
            ranked_candidates=dumps(decision.to_dict()["ranked_candidates"]),
            rejection_details=dumps(decision.no_trade_reasons),
            hard_blocks=dumps(decision.hard_blocks),
            explanation=decision.explanation,
            explanation_ar=decision.explanation_ar,
        )
        with self.db.session() as session:
            session.merge(row)
            for ranked in decision.ranked:
                session.merge(
                    RouterCandidateRankingRow(
                        router_decision_id=decision.decision_id,
                        evaluation_id=ranked.evaluation.evaluation_id,
                        strategy_id=ranked.evaluation.strategy_id,
                        rank_position=ranked.rank_position,
                        eligible="1" if ranked.eligible else "0",
                        score_total=ranked.score_total,
                        score_components=dumps(ranked.score_components),
                        rejection_codes=dumps(ranked.rejection_codes),
                    )
                )

    # -- trades
    def ensure_portfolio(self, *, name: str, initial_balance: float) -> tuple[str, float]:
        with self.db.session() as session:
            row = session.execute(
                select(PaperPortfolioRow).where(PaperPortfolioRow.name == name)
            ).scalar_one_or_none()
            if row is None:
                row = PaperPortfolioRow(
                    name=name, base_currency="USD",
                    initial_balance=initial_balance, current_balance=initial_balance,
                )
                session.add(row)
                session.flush()
            return row.id, float(row.current_balance)

    def save_trade(self, trade: PaperTrade) -> None:
        row = PaperTradeRow(
            id=trade.id,
            portfolio_id=trade.portfolio_id,
            origin_strategy_id=trade.origin_strategy_id,
            strategy_version=trade.strategy_version,
            management_policy_version=trade.management_policy_version,
            router_decision_id=trade.router_decision_id,
            entry_evaluation_id=trade.entry_evaluation_id,
            opportunity_key=trade.opportunity_key,
            symbol=trade.symbol,
            side=trade.side.value,
            status=trade.status.value,
            order_type=trade.order_type,
            created_at_utc=trade.created_at_utc,
            opened_at_utc=trade.opened_at_utc,
            closed_at_utc=trade.closed_at_utc,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            stop_loss=trade.stop_loss,
            take_profit=trade.take_profit,
            volume_lots=trade.volume_lots,
            planned_risk_amount=trade.planned_risk_amount,
            planned_risk_pct=None,
            gross_pnl=trade.gross_pnl,
            commission=trade.commission,
            swap=trade.swap,
            slippage_cost=trade.slippage_cost,
            net_pnl=trade.net_pnl,
            r_multiple=trade.r_multiple,
            exit_reason=trade.exit_reason,
            entry_spread_points=trade.entry_spread_points,
            execution_model_version=EXECUTION_MODEL_VERSION,
            management_policy_json=dumps(trade.management_policy),
            metadata_json=dumps(
                {
                    **trade.metadata,
                    "exit_resolution_method": trade.exit_resolution_method,
                    "highest_bid_since_entry": trade.highest_bid_since_entry,
                    "lowest_ask_since_entry": trade.lowest_ask_since_entry,
                    "initial_stop_loss": trade.initial_stop_loss,
                    "remaining_volume_lots": trade.remaining_volume_lots,
                    "entry_snapshot_id": trade.entry_snapshot_id,
                    "entry_session_name": trade.entry_session_name,
                    "exit_session_name": trade.exit_session_name,
                }
            ),
        )
        with self.db.session() as session:
            session.merge(row)

    def save_trade_event(
        self,
        trade_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        now_utc: datetime | None = None,
        snapshot_id: str | None = None,
        actor: str = "SYSTEM",
    ) -> None:
        from app.core.ids import new_uuid

        row = TradeEventRow(
            id=new_uuid(),
            trade_id=trade_id,
            timestamp_utc=now_utc or datetime.now(UTC),
            event_type=event_type,
            old_values=dumps(payload.get("old_values")) if payload and "old_values" in payload else None,
            new_values=dumps(payload.get("new_values")) if payload and "new_values" in payload else None,
            reason=(payload or {}).get("reason"),
            market_snapshot_id=snapshot_id,
            actor=actor,
        )
        with self.db.session() as session:
            session.add(row)

    def load_open_trades(self, portfolio_id: str) -> list[PaperTrade]:
        with self.db.session() as session:
            rows = (
                session.execute(
                    select(PaperTradeRow).where(
                        PaperTradeRow.portfolio_id == portfolio_id,
                        PaperTradeRow.status.in_(["OPEN", "PENDING", "PARTIALLY_CLOSED"]),
                    )
                )
                .scalars()
                .all()
            )
        trades = []
        for row in rows:
            meta = loads(row.metadata_json, {}) or {}
            policy = loads(row.management_policy_json, {}) or {}
            trades.append(
                PaperTrade(
                    id=row.id,
                    portfolio_id=row.portfolio_id,
                    origin_strategy_id=row.origin_strategy_id,
                    strategy_version=row.strategy_version,
                    management_policy_version=row.management_policy_version,
                    router_decision_id=row.router_decision_id,
                    entry_evaluation_id=row.entry_evaluation_id,
                    opportunity_key=row.opportunity_key,
                    symbol=row.symbol,
                    side=Direction(row.side),
                    status=TradeStatus(row.status),
                    order_type=row.order_type,
                    created_at_utc=row.created_at_utc,
                    opened_at_utc=row.opened_at_utc,
                    closed_at_utc=row.closed_at_utc,
                    entry_price=float(row.entry_price) if row.entry_price else None,
                    exit_price=float(row.exit_price) if row.exit_price else None,
                    stop_loss=float(row.stop_loss) if row.stop_loss else None,
                    take_profit=float(row.take_profit) if row.take_profit else None,
                    initial_stop_loss=meta.get("initial_stop_loss"),
                    volume_lots=float(row.volume_lots),
                    remaining_volume_lots=meta.get("remaining_volume_lots", float(row.volume_lots)),
                    planned_risk_amount=float(row.planned_risk_amount) if row.planned_risk_amount else None,
                    gross_pnl=float(row.gross_pnl) if row.gross_pnl else None,
                    commission=float(row.commission) if row.commission else None,
                    swap=float(row.swap) if row.swap else None,
                    slippage_cost=float(row.slippage_cost) if row.slippage_cost else None,
                    net_pnl=float(row.net_pnl) if row.net_pnl else None,
                    r_multiple=float(row.r_multiple) if row.r_multiple else None,
                    exit_reason=row.exit_reason,
                    entry_spread_points=float(row.entry_spread_points) if row.entry_spread_points else None,
                    exit_resolution_method=meta.get("exit_resolution_method"),
                    entry_session_name=meta.get("entry_session_name"),
                    exit_session_name=meta.get("exit_session_name"),
                    entry_snapshot_id=meta.get("entry_snapshot_id"),
                    highest_bid_since_entry=meta.get("highest_bid_since_entry"),
                    lowest_ask_since_entry=meta.get("lowest_ask_since_entry"),
                    management_policy=policy,
                    metadata={k: v for k, v in meta.items() if k not in (
                        "exit_resolution_method", "highest_bid_since_entry", "lowest_ask_since_entry",
                        "initial_stop_loss", "remaining_volume_lots", "entry_snapshot_id",
                        "entry_session_name", "exit_session_name",
                    )},
                )
            )
        return trades

    def load_closed_trades(self, portfolio_id: str, *, limit: int = 5000) -> list[PaperTrade]:
        with self.db.session() as session:
            rows = (
                session.execute(
                    select(PaperTradeRow)
                    .where(PaperTradeRow.portfolio_id == portfolio_id, PaperTradeRow.status.like("CLOSED%"))
                    .order_by(PaperTradeRow.closed_at_utc.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        out = []
        for row in rows:
            out.append(self._row_to_trade(row))
        return out

    def _row_to_trade(self, row) -> PaperTrade:
        meta = loads(row.metadata_json, {}) or {}
        return PaperTrade(
            id=row.id,
            portfolio_id=row.portfolio_id,
            origin_strategy_id=row.origin_strategy_id,
            strategy_version=row.strategy_version,
            management_policy_version=row.management_policy_version,
            router_decision_id=row.router_decision_id,
            entry_evaluation_id=row.entry_evaluation_id,
            opportunity_key=row.opportunity_key,
            symbol=row.symbol,
            side=Direction(row.side),
            status=TradeStatus(row.status),
            order_type=row.order_type,
            created_at_utc=row.created_at_utc,
            opened_at_utc=row.opened_at_utc,
            closed_at_utc=row.closed_at_utc,
            entry_price=float(row.entry_price) if row.entry_price else None,
            exit_price=float(row.exit_price) if row.exit_price else None,
            stop_loss=float(row.stop_loss) if row.stop_loss else None,
            take_profit=float(row.take_profit) if row.take_profit else None,
            volume_lots=float(row.volume_lots),
            remaining_volume_lots=meta.get("remaining_volume_lots", float(row.volume_lots)),
            planned_risk_amount=float(row.planned_risk_amount) if row.planned_risk_amount else None,
            gross_pnl=float(row.gross_pnl) if row.gross_pnl else None,
            commission=float(row.commission) if row.commission else None,
            swap=float(row.swap) if row.swap else None,
            slippage_cost=float(row.slippage_cost) if row.slippage_cost else None,
            net_pnl=float(row.net_pnl) if row.net_pnl else None,
            r_multiple=float(row.r_multiple) if row.r_multiple else None,
            exit_reason=row.exit_reason,
            entry_spread_points=float(row.entry_spread_points) if row.entry_spread_points else None,
            entry_session_name=meta.get("entry_session_name"),
            exit_session_name=meta.get("exit_session_name"),
            metadata=meta,
        )

    # -- rejected / shadow
    def save_rejected_opportunity(
        self,
        *,
        evaluation: StrategyEvaluation,
        decision: RouterDecision | None,
        stage: str,
        reasons: list[str],
        selected_alternative: str | None,
        shadow_status: str,
    ) -> str:
        from app.core.ids import new_uuid

        row = RejectedOpportunityRow(
            id=new_uuid(),
            strategy_evaluation_id=evaluation.evaluation_id,
            router_decision_id=decision.decision_id if decision else None,
            rejection_stage=stage,
            rejection_reason_codes=dumps(reasons),
            selected_alternative_strategy_id=selected_alternative,
            shadow_tracking_status=shadow_status,
        )
        with self.db.session() as session:
            session.add(row)
            session.flush()
        return row.id

    def update_shadow_status(self, rejected_id: str, status: str) -> None:
        with self.db.session() as session:
            row = session.get(RejectedOpportunityRow, rejected_id)
            if row:
                row.shadow_tracking_status = status

    def save_shadow_outcome(self, rejected_id: str, record) -> None:
        from app.core.ids import new_uuid

        row = ShadowOutcomeRow(
            id=new_uuid(),
            rejected_opportunity_id=rejected_id,
            tracked_at_utc=record.rejected_at_utc,
            simulated_entry_time_utc=record.rejected_at_utc,
            simulated_entry_price=record.entry_price,
            simulated_exit_time_utc=record.exit_time_utc,
            simulated_exit_price=record.exit_price,
            simulated_exit_reason=record.exit_reason,
            simulated_gross_pnl=record.gross_pnl_per_lot,
            simulated_net_pnl=record.gross_pnl_per_lot,  # costs tracked separately
            simulated_r_multiple=(
                round(record.gross_pnl_per_lot / 50.0, 3) if record.gross_pnl_per_lot is not None else None
            ),
            max_favorable_excursion=record.mfe,
            max_adverse_excursion=record.mae,
            calculation_version=SHADOW_CALCULATION_VERSION,
        )
        with self.db.session() as session:
            session.merge(row)

    # -- health
    def save_health_event(self, *, component: str, severity: str, status: str, message: str, details: dict | None) -> None:
        from app.core.ids import new_uuid

        with self.db.session() as session:
            session.add(
                SystemHealthEventRow(
                    id=new_uuid(),
                    component=component,
                    severity=severity,
                    status=status,
                    message=message,
                    details=dumps(details or {}),
                )
            )
