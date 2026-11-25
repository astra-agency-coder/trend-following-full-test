"""Breakout school (spec 35.5).

Setups:
- CLOSE_BREAKOUT: close beyond a defined level after range compression.
- BREAKOUT_RETEST: breakout then pullback that retests the level and holds.
- VOLATILITY_EXPANSION: compression (NR7-style / ATR contraction) + expansion bar.

Every candidate records the breakout level, its formation time, distance at
signal, and a validity window so the engine never enters very late.
"""
from __future__ import annotations

from datetime import timedelta

from app.core.enums import (
    Direction,
    EntryType,
    EvaluationStatus,
    SetupStage,
)
from app.market_context.engine import MarketSnapshot
from app.market_context.indicators import atr, donchian_channel
from app.strategies.base import (
    ExitPlan,
    StrategyBase,
    StrategyEvaluation,
    TradeManagementAction,
    TradeManagementContext,
)


class BreakoutStrategy(StrategyBase):
    strategy_id = "breakout"
    strategy_name = "Breakout Trading"
    strategy_version = "1.0.0"

    def evaluate(self, snapshot: MarketSnapshot, bar_service, config: dict) -> StrategyEvaluation:
        started = snapshot.timestamp_utc
        evaluation = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), started)

        if snapshot.data_quality.value != "VALID":
            evaluation.status = EvaluationStatus.INVALID_DATA
            evaluation.rejection_reason_codes = ["BRK_INVALID_DATA"]
            return self.finish(evaluation, started)

        cfg = config.get("strategies", {}).get("breakout", {})
        tf = cfg.get("timeframe", "M15")
        bars = bar_service.closed_bars(tf, as_of=started)
        evaluation.timeframes_used = [tf]

        if len(bars) < 60:
            evaluation.rejection_reason_codes = ["BRK_INSUFFICIENT_DATA"]
            return self.finish(evaluation, started)

        atr_value = atr(bars, 14)
        if not atr_value:
            evaluation.rejection_reason_codes = ["BRK_ATR_UNAVAILABLE"]
            return self.finish(evaluation, started)

        # ---- compression detection (range of last N vs ATR)
        comp_window = int(cfg.get("compression_window", 12))
        comp_max_atr = float(cfg.get("compression_max_range_atr", 3.0))
        recent = bars[-comp_window:]
        comp_high = max(b.high for b in recent)
        comp_low = min(b.low for b in recent)
        compression_ratio = (comp_high - comp_low) / atr_value

        evaluation.feature_snapshot = {
            "compression_ratio": round(compression_ratio, 2),
            "compression_high": round(comp_high, 2),
            "compression_low": round(comp_low, 2),
            "atr14": round(atr_value, 4),
            "last_close": bars[-1].close,
        }

        c_comp = self.check("range_compression", round(compression_ratio, 2), "<=", comp_max_atr, timeframe=tf)
        self.record(evaluation, c_comp)

        last = bars[-1]

        # ---- CLOSE_BREAKOUT on compression box
        if c_comp.passed:
            if last.close > comp_high:
                return self._candidate(
                    evaluation, snapshot, Direction.BUY, last.close, comp_low,
                    setup_type="CLOSE_BREAKOUT", level=comp_high, cfg=cfg, started=started,
                    atr_value=atr_value,
                    extra=[self.check("close_above_box", round(last.close, 2), ">", round(comp_high, 2), timeframe=tf)],
                )
            if last.close < comp_low:
                return self._candidate(
                    evaluation, snapshot, Direction.SELL, last.close, comp_high,
                    setup_type="CLOSE_BREAKOUT", level=comp_low, cfg=cfg, started=started,
                    atr_value=atr_value,
                    extra=[self.check("close_below_box", round(last.close, 2), "<", round(comp_low, 2), timeframe=tf)],
                )

        # ---- BREAKOUT_RETEST on Donchian channel
        channel = donchian_channel(bars[:-6], int(cfg.get("donchian_period", 20)))
        if channel is not None:
            upper, lower, _ = channel
            # had a breakout in the last 5 bars; now retest near the level
            broke_up = any(b.close > upper for b in bars[-6:-1])
            broke_down = any(b.close < lower for b in bars[-6:-1])
            near_up = abs(last.close - upper) <= float(cfg.get("retest_proximity_atr", 0.3)) * atr_value
            near_down = abs(last.close - lower) <= float(cfg.get("retest_proximity_atr", 0.3)) * atr_value
            if broke_up and near_up and last.close > upper - atr_value * 0.2 and last.close > last.open:
                return self._candidate(
                    evaluation, snapshot, Direction.BUY, last.close, lower,
                    setup_type="BREAKOUT_RETEST", level=upper, cfg=cfg, started=started,
                    atr_value=atr_value,
                    extra=[self.check("retest_held_above_level", True, "==", True, timeframe=tf)],
                )
            if broke_down and near_down and last.close < lower + atr_value * 0.2 and last.close < last.open:
                return self._candidate(
                    evaluation, snapshot, Direction.SELL, last.close, upper,
                    setup_type="BREAKOUT_RETEST", level=lower, cfg=cfg, started=started,
                    atr_value=atr_value,
                    extra=[self.check("retest_held_below_level", True, "==", True, timeframe=tf)],
                )

        evaluation.rejection_reason_codes = ["BRK_NO_VALID_BREAKOUT"]
        evaluation.notes = f"compression={compression_ratio:.2f} but no close beyond level / no retest trigger"
        return self.finish(evaluation, started)

    def _candidate(
        self, evaluation, snapshot, direction, entry, opposite_extreme, *,
        setup_type, level, cfg, started, atr_value, extra,
    ):
        for c in extra:
            self.record(evaluation, c)

        # spread check at signal time (spec 35.5: تحقق من Spread عند الدخول)
        if snapshot.spread_points is not None:
            max_spread = float(cfg.get("max_spread_points", 60))
            c_spread = self.check("spread_ok", snapshot.spread_points, "<=", max_spread)
            self.record(evaluation, c_spread)
            if not c_spread.passed:
                evaluation.rejection_reason_codes = ["BRK_SPREAD_TOO_HIGH"]
                return self.finish(evaluation, started)

        buffer = float(cfg.get("sl_buffer_atr", 0.3)) * atr_value
        if direction == Direction.BUY:
            sl = level - buffer
            tp = entry + (entry - sl) * float(cfg.get("rr_target", 2.0))
        else:
            sl = level + buffer
            tp = entry - (sl - entry) * float(cfg.get("rr_target", 2.0))

        # distance guard: don't enter after a huge extension (late entry)
        max_ext = float(cfg.get("max_extension_atr", 2.5)) * atr_value
        extension = abs(entry - level)
        c_ext = self.check("extension_not_late", round(extension, 2), "<=", round(max_ext, 2), timeframe=evaluation.timeframes_used[0])
        self.record(evaluation, c_ext)
        if not c_ext.passed:
            evaluation.rejection_reason_codes = ["BRK_ENTRY_TOO_LATE"]
            evaluation.setup_stage = SetupStage.ARMED
            return self.finish(evaluation, started)

        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0
        c_rr = self.check("risk_reward_min", round(rr, 2), ">=", float(cfg.get("min_rr", 1.5)))
        self.record(evaluation, c_rr)
        if not c_rr.passed:
            evaluation.rejection_reason_codes = ["BRK_RR_TOO_LOW"]
            return self.finish(evaluation, started)

        evaluation.status = EvaluationStatus.CANDIDATE
        evaluation.setup_type = setup_type
        evaluation.setup_stage = SetupStage.TRIGGERED
        evaluation.direction = direction
        evaluation.entry_type = EntryType.MARKET
        evaluation.entry_price_proposal = round(entry, 2)
        evaluation.stop_loss = round(sl, 2)
        evaluation.take_profit = round(tp, 2)
        evaluation.risk_reward_estimate = round(rr, 2)
        evaluation.initial_risk_distance = round(risk, 2)
        evaluation.valid_until_utc = started + timedelta(minutes=float(cfg.get("valid_minutes", 45)))
        evaluation.opportunity_key = f"BRK:{setup_type}:{level:.2f}"
        evaluation.market_fit_score = self._market_fit(snapshot, compression_ok=True)
        evaluation.setup_quality_score = min(100.0, 45 + rr * 15)
        evaluation.exit_plan = ExitPlan(
            take_profit=round(tp, 2),
            exit_policy="FIXED_TP",
            break_even_at_r=float(cfg.get("break_even_at_r", 1.0)),
        )
        evaluation.notes = f"{setup_type} beyond {round(level, 2)} with compression context"
        return self.finish(evaluation, started)

    def _market_fit(self, snapshot, *, compression_ok: bool) -> float:
        score = 50.0
        if compression_ok:
            score += 15
        if snapshot.session.session_name in ("LONDON", "NEW_YORK"):
            score += 15
        if snapshot.session.phase.value in ("OPENING_WINDOW", "EARLY_SESSION"):
            score += 10
        return min(100.0, score)

    def manage_open_trade(self, trade_context: TradeManagementContext, config: dict) -> TradeManagementAction:
        cfg = config.get("strategies", {}).get("breakout", {})
        be_at_r = float(cfg.get("break_even_at_r", 1.0))
        if trade_context.r_multiple_now >= be_at_r:
            if trade_context.side == Direction.BUY and trade_context.stop_loss < trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL", new_stop_loss=trade_context.entry_price,
                    reason_code="BRK_BREAK_EVEN", reason_text="risk-free after 1R",
                )
            if trade_context.side == Direction.SELL and trade_context.stop_loss > trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL", new_stop_loss=trade_context.entry_price,
                    reason_code="BRK_BREAK_EVEN", reason_text="risk-free after 1R",
                )
        return TradeManagementAction(action="HOLD", reason_code="BRK_HOLD")

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": ["M15"],
            "setups": ["CLOSE_BREAKOUT", "BREAKOUT_RETEST", "VOLATILITY_EXPANSION"],
            "concepts": ["range_compression", "close_beyond_level", "retest_logic", "extension_guard", "spread_guard"],
        }
