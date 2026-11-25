"""Embedded engine runner — runs HybridRuntime cycles in a background thread
and publishes SSE events (frontend spec §20.2/§20.3).

The thread owns no trading logic of its own: it calls ``runtime.run_cycle()``
and diffs the runtime state to emit spec-shaped events. It also feeds the
event bus with quote updates so the dashboard can refresh without polling.
"""
from __future__ import annotations

import threading
import time
import traceback

from app.core.logging_setup import get_logger
from app.webapi.event_bus import make_event

logger = get_logger("webapi.engine")


class EngineRunner(threading.Thread):
    def __init__(self, state, *, poll_seconds: float = 5.0):
        super().__init__(name="webapi-engine", daemon=True)
        self.state = state
        self.poll_seconds = poll_seconds
        self._last_decision_id: str | None = None
        self._known_open_trade_ids: set[str] = set()
        self._known_closed_trade_ids: set[str] = set()

    # ------------------------------------------------------------------ utils
    def _publish(self, event: dict) -> None:
        try:
            self.state.event_bus.publish(event)
        except Exception:  # transport must never break the engine
            logger.exception("event publish failed")

    def _diff_state(self) -> None:
        runtime = self.state.runtime
        if runtime is None:
            return
        # decisions
        if runtime.decisions:
            decision = runtime.decisions[-1]
            if decision.decision_id != self._last_decision_id:
                self._last_decision_id = decision.decision_id
                self._publish(
                    make_event(
                        "hybrid.decision.created",
                        {
                            "decision_id": decision.decision_id,
                            "status": decision.decision_status.value,
                            "selected_strategy_id": decision.selected.strategy_id if decision.selected else None,
                            "explanation": decision.explanation,
                            "explanation_ar": decision.explanation_ar,
                        },
                    )
                )
                if decision.selected is not None:
                    self._publish(
                        make_event(
                            "hybrid.strategy.selected",
                            {
                                "decision_id": decision.decision_id,
                                "strategy_id": decision.selected.strategy_id,
                                "strategy_name": decision.selected.strategy_id.replace("_", " ").title(),
                                "direction": decision.selected.direction.value,
                            },
                        )
                    )
        # trades
        for trade_id, trade in runtime.paper.open_trades.items():
            if trade_id not in self._known_open_trade_ids:
                self._known_open_trade_ids.add(trade_id)
                self._publish(
                    make_event(
                        "trade.opened",
                        {
                            "trade_id": trade_id,
                            "side": trade.side.value,
                            "strategy_id": trade.origin_strategy_id,
                            "entry_price": trade.entry_price,
                            "stop_loss": trade.stop_loss,
                            "take_profit": trade.take_profit,
                            "volume": trade.volume_lots,
                        },
                    )
                )
        for trade in runtime.paper.closed_trades:
            if trade.id not in self._known_closed_trade_ids:
                self._known_closed_trade_ids.add(trade.id)
                self._known_open_trade_ids.discard(trade.id)
                self._publish(
                    make_event(
                        "trade.closed",
                        {
                            "trade_id": trade.id,
                            "side": trade.side.value,
                            "strategy_id": trade.origin_strategy_id,
                            "exit_price": trade.exit_price,
                            "net_pnl": trade.net_pnl,
                            "exit_reason": trade.exit_reason,
                        },
                    )
                )
        # context
        snapshot = runtime.last_snapshot
        if snapshot is not None:
            self._publish(
                make_event(
                    "market.context.updated",
                    {
                        "trend_state": snapshot.trend_state.value,
                        "volatility_state": snapshot.volatility_state.value,
                        "session": snapshot.session.session_name,
                        "session_phase": snapshot.session.phase.value,
                        "data_quality": snapshot.data_quality.value,
                    },
                )
            )

    # ------------------------------------------------------------------ thread
    def run(self) -> None:
        state = self.state
        try:
            runtime = state.runtime
            logger.info("embedded engine starting (mode=%s)", state.run_mode)
            runtime.start()
            self._publish(
                make_event("system.health.updated", {"status": "running", "mode": state.run_mode})
            )
            while not state.engine_stop.is_set():
                try:
                    runtime.run_cycle()
                    self._diff_state()
                    # quote event from the last snapshot bid/ask
                    snapshot = runtime.last_snapshot
                    if snapshot is not None and snapshot.bid is not None:
                        self._publish(
                            make_event(
                                "market.quote.updated",
                                {
                                    "symbol": snapshot.symbol,
                                    "bid": snapshot.bid,
                                    "ask": snapshot.ask,
                                    "spread_points": snapshot.spread_points,
                                    "quote_time": snapshot.timestamp_utc.isoformat(),
                                    "data_quality": snapshot.data_quality.value,
                                },
                            )
                        )
                except Exception:
                    state.engine_error = traceback.format_exc(limit=5)
                    logger.exception("engine cycle failed")
                    self._publish(make_event("system.health.updated", {"status": "degraded", "error": "engine cycle failed"}))
                state.engine_stop.wait(self.poll_seconds)
            runtime.stop("WEB_API_SHUTDOWN")
            logger.info("embedded engine stopped")
        except Exception:
            state.engine_error = traceback.format_exc(limit=10)
            logger.exception("engine thread crashed")
            self._publish(make_event("system.health.updated", {"status": "down", "error": "engine crashed"}))

    def shutdown(self) -> None:
        self.state.engine_stop.set()
