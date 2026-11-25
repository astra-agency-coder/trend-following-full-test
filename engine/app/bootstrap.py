"""HybridRuntime — the main orchestrator (spec 18 runtime workflow).

One analysis cycle:
  poll data -> manage open trades FIRST -> build snapshot -> evaluate all
  schools -> router decides -> risk approves -> paper trade opens ->
  shadow tracking -> persistence -> telegram notifications.

The same runtime is used live (MT5/Replay provider) and in backtests via
a VirtualClock — one code path, no divergent logic (spec 46.1).
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.clock import Clock, SystemClock
from app.core.config import ConfigLoader, ConfigVersion
from app.core.constants import APP_VERSION
from app.core.enums import (
    Actor,
    CircuitBreakerType,
    DataQuality,
    Direction,
    EvaluationStatus,
    RouterDecisionStatus,
)
from app.core.exceptions import ConfigurationError, RealExecutionDisabledError
from app.core.ids import new_uuid
from app.core.logging_setup import get_logger
from app.data.bar_service import BarService
from app.data.data_health import DataHealthMonitor
from app.data.engine import MarketDataEngine
from app.execution.fill_models import CostModel, PaperFillEngine, SlippageModel
from app.execution.paper_engine import PaperTradingEngine
from app.hybrid.router import HybridDecisionEngine, RouterDecision
from app.journal.audit import AuditJournal
from app.market_context.engine import MarketContextEngine
from app.market_context.sessions import SessionEngine
from app.market_context.structure import StructureEngine
from app.persistence.database import Database
from app.persistence.repositories.journal import JournalRepository
from app.reporting import metrics as perf
from app.risk.engine import OpenPositionRisk, RiskEngine
from app.shadow.tracker import ShadowRecord, ShadowTracker
from app.strategies.registry import StrategyRegistry
from app.telegram import messages as msg
from app.telegram.auth import TelegramAuthorizer
from app.telegram.bot import OutboxWorker, TelegramCommandHandler
from app.telegram.client import TelegramClient

logger = get_logger("runtime")


@dataclass
class CycleStats:
    cycles: int = 0
    evaluations: int = 0
    candidates: int = 0
    selections: int = 0
    trades_opened: int = 0
    rejections: int = 0
    shadow_registered: int = 0
    data_gap_cycles: int = 0


class HybridRuntime:
    """Wires every engine together and runs analysis cycles."""

    def __init__(
        self,
        *,
        config_version: ConfigVersion,
        database: Database,
        provider,
        clock: Clock | None = None,
        telegram_client=None,
        run_mode: str = "LIVE_PAPER",
    ):
        self.config_version = config_version
        self.config = config_version.data
        self.db = database
        self.provider = provider
        self.clock = clock or SystemClock()
        self.run_mode = run_mode
        cfg = self.config

        # ---- startup assertion: paper only, no real execution paths (spec 23)
        self._startup_safety_assertions(cfg)

        # ---- persistence
        self.repo = JournalRepository(database)
        self.audit = AuditJournal(database)
        self.config_version_id = self.repo.save_config_version(config_version)

        # ---- symbol
        from app.core.constants import CANONICAL_SYMBOL

        self.symbol = cfg.get("app", {}).get("canonical_symbol", CANONICAL_SYMBOL)
        self.spec = provider.get_symbol_info(self.symbol)
        self.repo.save_symbol_spec(self.spec)

        # ---- data engine
        md_cfg = cfg.get("market_data", {})
        # In replay/backtest the virtual clock jumps between data points;
        # tick age must allow for the replay step — but still detect real
        # disconnections (no tick for many times the step).
        if run_mode == "BACKTEST":
            step = float(cfg.get("backtesting", {}).get("step_seconds", 60))
            max_age = float(cfg.get("backtesting", {}).get("max_tick_age_seconds", step * 10))
        else:
            max_age = float(md_cfg.get("max_data_age_seconds", 10))
        self.bar_service = BarService(database=database, persist=True)
        self.health_monitor = DataHealthMonitor(
            max_data_age_seconds=max_age,
            on_event=self._on_data_health_event,
        )
        self.data_engine = MarketDataEngine(
            provider,
            symbol=self.symbol,
            spec=self.spec,
            timeframes=md_cfg.get("timeframes"),
            bar_service=self.bar_service,
            health_monitor=self.health_monitor,
        )

        # ---- context engine
        self.session_engine = SessionEngine.from_config(cfg.get("sessions", {}))
        self.context_engine = MarketContextEngine(
            symbol=self.symbol,
            source_symbol=self.spec.source_symbol,
            bar_service=self.bar_service,
            session_engine=self.session_engine,
            structure_engine=StructureEngine(),
            config=cfg,
            point=self.spec.point,
        )

        # ---- strategies & router
        self.registry = StrategyRegistry(cfg)
        self.router = HybridDecisionEngine(cfg)

        # ---- risk & paper execution
        self.risk_engine = RiskEngine(cfg, self.spec)
        pe_cfg = cfg.get("paper_execution", {})
        slippage = SlippageModel(pe_cfg, seed=cfg.get("backtesting", {}).get("random_seed"))
        costs = CostModel(pe_cfg)
        self.fill_engine = PaperFillEngine(slippage, costs, point=self.spec.point)
        self.paper = PaperTradingEngine(
            spec=self.spec,
            fill_engine=self.fill_engine,
            cost_model=costs,
            strategy_registry=self.registry,
            portfolio_id="",
            initial_balance=0.0,
        )
        portfolio_id, balance = self.repo.ensure_portfolio(
            name="HYBRID_MAIN", initial_balance=float(cfg.get("risk", {}).get("initial_balance", 10000))
        )
        self.paper.portfolio_id = portfolio_id
        self.paper.balance = balance

        # ---- shadow
        self.shadow = ShadowTracker(value_per_unit=self.paper.value_per_unit)
        self.shadow_links: dict[str, str] = {}  # shadow record id -> rejected_opportunity id

        # ---- telegram
        tg_cfg = cfg.get("telegram", {})
        self.telegram_enabled = bool(tg_cfg.get("enabled") and tg_cfg.get("bot_token"))
        client = telegram_client or (
            TelegramClient(str(tg_cfg.get("bot_token"))) if self.telegram_enabled else None
        )
        self.outbox = OutboxWorker(database, client, enabled=self.telegram_enabled)
        admin_chat = str((tg_cfg.get("allowed_chat_ids") or ["0"])[0])
        self.authorizer = TelegramAuthorizer(
            tg_cfg.get("allowed_user_ids") or [],
            tg_cfg.get("allowed_chat_ids") or [],
            rate_per_minute=int(tg_cfg.get("rate_limit_per_minute", 20)),
        )
        self.command_handler = TelegramCommandHandler(
            authorizer=self.authorizer,
            audit=self.audit,
            runtime=self,
            outbox=self.outbox,
            admin_chat_id=admin_chat,
        )

        # ---- state
        self.stats = CycleStats()
        self.decisions: list[RouterDecision] = []
        self.executed_opportunity_keys: set[str] = set()
        self.paused = False
        self.pause_reason: str | None = None
        self.daily_block_date = None
        self.last_snapshot = None
        self.last_market_changes: list[str] = []
        self.run_id: str | None = None
        self.component_status: dict[str, str] = {k: "HEALTHY" for k in (
            "market_data", "context_engine", "strategies", "router", "risk", "paper_execution",
            "shadow_tracker", "telegram", "database",
        )}

        # restore open trades + executed keys (restart recovery, spec 22.3)
        self._restore_state()

    # ------------------------------------------------------------------ startup
    def _startup_safety_assertions(self, cfg: dict) -> None:
        app = cfg.get("app", {})
        if app.get("paper_only") is not True:
            raise RealExecutionDisabledError("app.paper_only must be true; this build cannot run real execution")
        if cfg.get("paper_execution", {}).get("real_execution_enabled"):
            raise RealExecutionDisabledError("real execution is forbidden in this version")
        if app.get("enable_ai"):
            raise ConfigurationError("AI/ML is disabled by specification")

    def _restore_state(self) -> None:
        for trade in self.repo.load_open_trades(self.paper.portfolio_id):
            self.paper.restore_open_trade(trade)
            if trade.opportunity_key:
                self.executed_opportunity_keys.add(trade.opportunity_key)
        # historical executed keys to prevent duplicates across restarts
        for trade in self.repo.load_closed_trades(self.paper.portfolio_id):
            if trade.opportunity_key:
                self.executed_opportunity_keys.add(trade.opportunity_key)
        if self.paper.open_trades:
            logger.info(
                "restored open trades after restart",
                extra={"context": {"count": len(self.paper.open_trades)}},
            )

    def start(self) -> None:
        now = self.clock.now_utc()
        self.run_id = self.repo.start_system_run(
            app_version=APP_VERSION, host=socket.gethostname(), config_version_id=self.config_version_id
        )
        self.audit.append("SYSTEM_STARTED", payload={"run_id": self.run_id, "mode": self.run_mode})
        self.data_engine.bootstrap_history(now)
        self.notify(
            msg.system_started(
                version=APP_VERSION,
                started_at=now,
                mt5_connected=self.provider.health_check().connected,
                db_ok=self.db.healthy(),
                source_symbol=self.spec.source_symbol,
                strategies=self.registry.summary(),
            ),
            message_type="SYSTEM_STARTED",
            message_key=f"started:{self.run_id}",
        )

    def stop(self, reason: str) -> None:
        now = self.clock.now_utc()
        if self.run_id:
            self.repo.stop_system_run(self.run_id, reason=reason)
        self.audit.append("SYSTEM_STOPPED", payload={"reason": reason})
        self.notify(msg.system_stopped(stopped_at=now, reason=reason), message_type="SYSTEM_STOPPED")
        self.outbox.flush()

    # ------------------------------------------------------------------ cycle
    def run_cycle(self, *, now_utc: datetime | None = None) -> dict:
        """One full analysis cycle. Returns a small summary dict."""
        now = now_utc or self.clock.now_utc()
        self.stats.cycles += 1

        # 1. data poll + validation
        poll = self.data_engine.poll(now)
        data_ok = self.data_engine.data_ok_for_trading(poll.health)
        if not data_ok:
            self.stats.data_gap_cycles += 1

        # 2. manage open trades BEFORE new analysis (spec 18 note)
        if poll.tick is not None:
            snapshot_for_mgmt = self._build_snapshot(now, poll, persist=False)
            closed = self.paper.manage_trades(
                tick=poll.tick,
                snapshot=snapshot_for_mgmt,
                now_utc=now,
                config=self.config,
                on_event=self._trade_event,
            )
            for trade in closed:
                self.repo.save_trade(trade)
                self.notify(msg.trade_closed(trade=trade), message_type="TRADE_CLOSED", message_key=f"closed:{trade.id}")
                if trade.opportunity_key:
                    self.executed_opportunity_keys.add(trade.opportunity_key)
        # 3. shadow tracking (independent — spec 18 step p)
        if poll.tick is not None:
            resolved = self.shadow.update(poll.tick, now)
            for record in resolved:
                rejected_id = self.shadow_links.pop(record.id, None)
                if rejected_id:
                    self.repo.update_shadow_status(rejected_id, "COMPLETED" if not record.status.value == "CENSORED" else "CENSORED")
                    self.repo.save_shadow_outcome(rejected_id, record)

        # 4. snapshot + persist
        snapshot = self._build_snapshot(now, poll, persist=True)
        self.last_snapshot = snapshot
        self._check_market_change_alert(snapshot)

        # 5. evaluate every enabled school (isolated failures — spec 0.15)
        evaluations = []
        for strategy in self.registry.enabled_strategies():
            try:
                evaluation = strategy.evaluate(snapshot, self.bar_service, self.config)
            except Exception as exc:
                logger.exception(f"strategy {strategy.strategy_id} failed")
                self.repo.save_health_event(
                    component=f"strategy:{strategy.strategy_id}", severity="ERROR", status="ERROR",
                    message=str(exc)[:500], details=None,
                )
                evaluation = self._error_evaluation(strategy, snapshot, now)
            evaluations.append(evaluation)
            self.repo.save_evaluation(evaluation)
            self.stats.evaluations += 1
            if evaluation.status == EvaluationStatus.CANDIDATE:
                self.stats.candidates += 1

        # 6. hard blocks (spec 8.2-A — only documented blockers)
        hard_blocks: list[str] = []
        if not data_ok:
            hard_blocks.append("DATA_QUALITY_INVALID")
        spread_max = float(self.config.get("risk", {}).get("max_spread_points", 100))
        if snapshot.spread_points is not None and snapshot.spread_points > spread_max:
            hard_blocks.append("SPREAD_ABOVE_LIMIT")
        if self.paused:
            hard_blocks.append("SYSTEM_PAUSED")
        if self._daily_loss_block_active(now):
            hard_blocks.append("DAILY_LOSS_LIMIT")

        # 7. router decision
        decision = self.router.decide(
            snapshot=snapshot,
            evaluations=evaluations,
            now_utc=now,
            hard_blocks=hard_blocks,
            executed_opportunity_keys=self.executed_opportunity_keys,
        )
        self.decisions.append(decision)
        self.repo.save_decision(decision, self.config_version_id)
        self.audit.append(
            "ROUTER_DECISION",
            entity_type="router_decision",
            entity_id=decision.decision_id,
            payload={"status": decision.decision_status.value, "selected": decision.selected.strategy_id if decision.selected else None},
        )
        if decision.decision_status == RouterDecisionStatus.SELECTED and decision.selected:
            self.stats.selections += 1

        # 8. rejected opportunities -> shadow records
        selected_id = decision.selected.strategy_id if decision.selected else None
        for ranked in decision.ranked:
            ev = ranked.evaluation
            if ev.evaluation_id == (decision.selected.evaluation_id if decision.selected else None):
                continue
            stage = "ROUTER"
            reasons = ranked.rejection_codes or ["NOT_TOP_RANKED"]
            shadow_status = "TRACKING"
            rejected_id = self.repo.save_rejected_opportunity(
                evaluation=ev,
                decision=decision,
                stage=stage,
                reasons=reasons,
                selected_alternative=selected_id,
                shadow_status=shadow_status,
            )
            self.stats.rejections += 1
            if self.config.get("hybrid_router", {}).get("enable_shadow_tracking", True) and poll.tick is not None:
                record = self.shadow.register(
                    ev,
                    rejected_at_utc=now,
                    rejection_stage=stage,
                    rejection_reasons=reasons,
                    selected_alternative=selected_id,
                    tick=poll.tick,
                )
                self.shadow_links[record.id] = rejected_id
                self.stats.shadow_registered += 1
            else:
                self.repo.update_shadow_status(rejected_id, "DISABLED")

        # 9. risk approval + paper trade
        opened_trade = None
        if decision.selected and poll.tick is not None:
            risk_result = self.risk_engine.approve(
                decision.selected,
                equity=self.paper.equity(poll.tick),
                balance=self.paper.balance,
                open_positions=[
                    OpenPositionRisk(
                        trade_id=t.id,
                        side=t.side,
                        strategy_id=t.origin_strategy_id,
                        risk_amount=t.planned_risk_amount or 0.0,
                        entry_price=t.entry_price or 0.0,
                        stop_loss=t.stop_loss or 0.0,
                        volume_lots=t.volume_lots,
                        opportunity_key=t.opportunity_key,
                    )
                    for t in self.paper.open_trades.values()
                ],
                day_realized_pnl=self.paper.day_realized_pnl(self._day_start(now)),
                now_utc=now,
            )
            self._save_risk_decision(decision, decision.selected, risk_result)
            if risk_result.approved and risk_result.calculated_volume:
                opened_trade = self.paper.open_trade(
                    evaluation=decision.selected,
                    decision_id=decision.decision_id,
                    volume=risk_result.calculated_volume,
                    risk_amount=risk_result.post_rounding_risk or risk_result.risk_amount,
                    tick=poll.tick,
                    snapshot=snapshot,
                    now_utc=now,
                )
                self.repo.save_trade(opened_trade)
                self._trade_event(opened_trade.id, "TRADE_OPENED", {
                    "new_values": {"entry": opened_trade.entry_price, "sl": opened_trade.stop_loss, "volume": opened_trade.volume_lots}
                }, now_utc=now, snapshot_id=snapshot.snapshot_id)
                if opened_trade.opportunity_key:
                    self.executed_opportunity_keys.add(opened_trade.opportunity_key)
                self.stats.trades_opened += 1
                self.notify(
                    msg.trade_opened(trade=opened_trade, snapshot=snapshot, decision=decision),
                    message_type="TRADE_OPENED",
                    message_key=f"opened:{opened_trade.id}",
                )
            elif not risk_result.approved:
                self.stats.rejections += 1
                rejected_id = self.repo.save_rejected_opportunity(
                    evaluation=decision.selected,
                    decision=decision,
                    stage="RISK",
                    reasons=risk_result.rejection_codes,
                    selected_alternative=None,
                    shadow_status="PENDING",
                )
                if poll.tick is not None and self.config.get("hybrid_router", {}).get("enable_shadow_tracking", True):
                    record = self.shadow.register(
                        decision.selected,
                        rejected_at_utc=now,
                        rejection_stage="RISK",
                        rejection_reasons=risk_result.rejection_codes,
                        selected_alternative=None,
                        tick=poll.tick,
                    )
                    self.shadow_links[record.id] = rejected_id

        # 10. outbox flush
        self.outbox.flush()

        return {
            "cycle": self.stats.cycles,
            "data_ok": data_ok,
            "trend": snapshot.trend_state.value,
            "session": snapshot.session.session_name,
            "decision": decision.decision_status.value,
            "selected": selected_id,
            "opened": opened_trade.id if opened_trade else None,
            "open_trades": len(self.paper.open_trades),
        }

    # ------------------------------------------------------------------ helpers
    def _build_snapshot(self, now, poll, *, persist: bool):
        quality = DataQuality.VALID if self.data_engine.data_ok_for_trading(poll.health) else poll.health.quality
        snapshot = self.context_engine.build_snapshot(
            now_utc=now,
            tick=poll.tick,
            data_quality=quality,
            snapshot_id=new_uuid(),
        )
        if persist:
            self.repo.save_snapshot(snapshot, self.config_version_id)
        return snapshot

    def _error_evaluation(self, strategy, snapshot, now):
        from app.strategies.base import StrategyEvaluation
        from app.core.enums import EvaluationStatus

        return StrategyEvaluation(
            evaluation_id=new_uuid(),
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            config_version=self.config_version.config_hash[:12],
            evaluation_timestamp_utc=now,
            market_snapshot_id=snapshot.snapshot_id,
            status=EvaluationStatus.ERROR,
            rejection_reason_codes=["STRATEGY_EXCEPTION"],
            notes="strategy raised an exception; see health events",
        )

    def _trade_event(self, trade_id, event_type, payload, *, now_utc=None, snapshot_id=None, actor=Actor.SYSTEM.value):
        self.repo.save_trade_event(trade_id, event_type, payload, now_utc=now_utc, snapshot_id=snapshot_id, actor=actor)

    def _save_risk_decision(self, decision, evaluation, risk_result):
        import json as _json

        from app.core.ids import new_uuid
        from app.persistence.models import RiskDecisionRow

        with self.db.session() as session:
            session.merge(
                RiskDecisionRow(
                    id=new_uuid(),
                    router_decision_id=decision.decision_id,
                    evaluation_id=evaluation.evaluation_id,
                    approved="1" if risk_result.approved else "0",
                    risk_amount=risk_result.risk_amount,
                    risk_pct=risk_result.risk_pct,
                    calculated_volume=risk_result.calculated_volume,
                    post_rounding_risk=risk_result.post_rounding_risk,
                    checks_json=_json.dumps(risk_result.to_dict()["checks"]),
                    rejection_codes=_json.dumps(risk_result.rejection_codes),
                )
            )

    def _day_start(self, now: datetime) -> datetime:
        return (now - timedelta(hours=now.hour, minutes=now.minute, seconds=now.second, microseconds=now.microsecond))

    def _daily_loss_block_active(self, now: datetime) -> bool:
        rcfg = self.config.get("risk", {})
        if not rcfg.get("daily_loss_limit_enabled", True):
            return False
        if self.daily_block_date == now.date():
            return True
        limit = float(rcfg.get("daily_loss_limit_pct", 2.0))
        day_pnl = self.paper.day_realized_pnl(self._day_start(now))
        if day_pnl < -self.paper.balance * limit / 100.0:
            self.daily_block_date = now.date()
            self.audit.append("CIRCUIT_BREAKER", payload={"type": CircuitBreakerType.DAILY_LOSS_LIMIT.value, "day_pnl": day_pnl})
            self.notify(
                f"🛑 <b>تفعيل قاطع الخسارة اليومي</b>\n\nخسارة اليوم: <code>${day_pnl}</code>\nسيتم إيقاف فتح صفقات جديدة حتى بداية يوم التداول التالي.",
                message_type="DAILY_LOSS_LIMIT",
            )
            return True
        return False

    def _check_market_change_alert(self, snapshot) -> None:
        if self.last_snapshot is None:
            self.last_snapshot = snapshot
            return
        prev = self.last_snapshot
        changes = []
        if snapshot.trend_state != prev.trend_state:
            changes.append(f"trend: {prev.trend_state.value}→{snapshot.trend_state.value}")
        if snapshot.volatility_state != prev.volatility_state:
            changes.append(f"volatility: {prev.volatility_state.value}→{snapshot.volatility_state.value}")
        if snapshot.session.session_name != prev.session.session_name:
            changes.append(f"session: {prev.session.session_name}→{snapshot.session.session_name}")
        if changes:
            self.notify(
                msg.market_context_changed(snapshot=snapshot, changes=changes),
                message_type="MARKET_CONTEXT",
                message_key=f"ctx:{snapshot.timestamp_utc.strftime('%Y%m%d%H%M')}:{changes[0][:20]}",
            )
        self.last_snapshot = snapshot

    def _on_data_health_event(self, event: str, details: dict) -> None:
        self.repo.save_health_event(
            component="market_data", severity="WARNING" if "DEGRADED" in event else "INFO",
            status="DEGRADED" if "DEGRADED" in event else "HEALTHY",
            message=event, details=details,
        )
        self.notify(
            msg.data_feed_event(event=event, details=details, at=self.clock.now_utc()),
            message_type=event,
            message_key=f"{event}:{details.get('last_tick_time_utc')}",
        )

    def notify(self, text: str, *, message_type: str, message_key: str | None = None) -> None:
        if not self.telegram_enabled:
            return
        chat = str((self.config.get("telegram", {}).get("allowed_chat_ids") or ["0"])[0])
        self.outbox.enqueue(chat_id=chat, message_type=message_type, text=text, message_key=message_key)

    # ------------------------------------------------------------------ telegram facade
    def status_text(self) -> str:
        now = self.clock.now_utc()
        health = self.data_engine.health.last_status
        return (
            f"🤖 <b>حالة النظام</b>\n\n"
            f"الوقت: <code>{now.strftime('%Y-%m-%d %H:%M UTC')}</code>\n"
            f"الوضع: Paper Trading فقط\n"
            f"جودة البيانات: <code>{health.quality.value}</code>\n"
            f"الدورات: <code>{self.stats.cycles}</code>\n"
            f"الصفقات المفتوحة: <code>{len(self.paper.open_trades)}</code>\n"
            f"الرصيد: <code>${self.paper.balance}</code>\n"
            f"الحالة: {'⏸️ موقوف عن فتح صفقات' if self.paused else '▶️ يعمل'}"
        )

    def market_text(self) -> str:
        if self.last_snapshot is None:
            return "لا توجد لقطة سوق بعد."
        d = self.last_snapshot.to_dict()
        return (
            f"📊 <b>آخر حالة سوق</b>\n\n"
            f"الوقت: <code>{d['timestamp_utc']}</code>\n"
            f"الاتجاه: <code>{d['trend_state']}</code> | التقلب: <code>{d['volatility_state']}</code>\n"
            f"النشاط: <code>{d['activity_state']}</code> | السيولة Proxy: <code>{d['liquidity_proxy_state']}</code>\n"
            f"البنية: <code>{', '.join(d['market_structure'][:4])}</code>\n"
            f"الجلسة: <code>{d['session']} / {d['session_phase']}</code>\n"
            f"Bid/Ask: <code>{d['bid']} / {d['ask']}</code>"
        )

    def open_trades_text(self) -> str:
        if not self.paper.open_trades:
            return "📭 لا توجد صفقات افتراضية مفتوحة."
        lines = ["📂 <b>الصفقات المفتوحة</b>", ""]
        for t in self.paper.open_trades.values():
            lines.append(
                f"• <code>{t.id[:8]}</code> {t.side.value} @ <code>{t.entry_price}</code> | "
                f"SL <code>{t.stop_loss}</code> | {t.origin_strategy_id} | فتح {t.opened_at_utc.strftime('%m-%d %H:%M')}"
            )
        return "\n".join(lines)

    def today_text(self) -> str:
        now = self.clock.now_utc()
        report = perf.daily_report(
            self.paper.closed_trades, self.decisions,
            day_start_utc=self._day_start(now), day_end_utc=now,
        )
        return msg.daily_report_text(report=report)

    def week_text(self) -> str:
        now = self.clock.now_utc()
        report = perf.weekly_report(
            self.paper.closed_trades, self.decisions, self.shadow,
            start_utc=now - timedelta(days=7), end_utc=now,
        )
        return msg.weekly_report_text(report=report)

    def performance_text(self) -> str:
        m = perf.compute_metrics(self.paper.closed_trades).to_dict()
        lines = [
            "📈 <b>الأداء الكلي</b>", "",
            f"الصفقات: <code>{m['total_trades']}</code> ({m['sample_status']})",
        ]
        if m["total_trades"]:
            lines += [
                f"Win Rate: <code>{m['win_rate']}%</code> | PF: <code>{m['profit_factor']}</code>",
                f"الصافي: <code>${m['net_profit']}</code> | Expectancy: <code>${m['expectancy']}</code>",
                f"Max DD: <code>${m['max_drawdown']}</code>",
            ]
        return "\n".join(lines)

    def strategies_text(self) -> str:
        lines = ["🏫 <b>المدارس</b>", ""]
        for sid, state in self.registry.summary().items():
            icon = "🟢" if state == "ENABLED" else "⚪"
            lines.append(f"{icon} <code>{sid}</code>: {state}")
        if self.router.preferred_strategy:
            lines.append(f"\n🎯 المدرسة المفضلة حالياً: <code>{self.router.preferred_strategy}</code>")
        return "\n".join(lines)

    def last_decision_text(self) -> str:
        if not self.decisions:
            return "لا قرارات بعد."
        return msg.strategy_selection(decision=self.decisions[-1])

    def logs_text(self) -> str:
        return f"آخر الأحداث: دورات={self.stats.cycles}، تقييمات={self.stats.evaluations}، مرشحون={self.stats.candidates}، اختيارات={self.stats.selections}، مفتوحة={self.stats.trades_opened}، مرفوضة={self.stats.rejections}"

    def health_text(self) -> str:
        health = self.data_engine.health.last_status
        lines = [
            "❤️ <b>صحة المكونات</b>", "",
            f"Market Data: <code>{health.quality.value}</code>",
            f"Database: <code>{'OK' if self.db.healthy() else 'FAIL'}</code>",
            f"Outbox Pending: <code>{self.outbox.pending_count()}</code>",
            f"Strategies: <code>{len(self.registry.enabled_strategies())} enabled</code>",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ pause/resume
    def pause_opening(self, *, reason: str = "MANUAL") -> str:
        self.paused = True
        self.pause_reason = reason
        self.audit.append("MANUAL_PAUSE", actor=Actor.TELEGRAM_USER.value, payload={"reason": reason})
        return ""

    def resume_opening(self) -> tuple[bool, str]:
        health = self.data_engine.health.last_status
        if health.quality.value not in ("VALID",):
            return False, f"جودة البيانات {health.quality.value} غير صالحة"
        self.paused = False
        self.pause_reason = None
        self.audit.append("MANUAL_RESUME", actor=Actor.TELEGRAM_USER.value, payload={})
        return True, ""
