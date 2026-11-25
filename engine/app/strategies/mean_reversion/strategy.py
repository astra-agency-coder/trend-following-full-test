"""Mean Reversion school (spec 35.4).

Candidate conditions (all measured, RSI is never the sole trigger):
1. Regime filter: no strong opposing trend (ADX below threshold or ranging).
2. Deviation: |z-score| of close vs rolling mean exceeds threshold.
3. Confirmation: rejection candle back toward the mean on the last closed bar.
4. Invalidation: SL beyond the deviation extreme (hypothesis invalidation).
Target: rolling mean / opposite band (never a fixed guess).
"""
from __future__ import annotations

from datetime import timedelta

from app.core.enums import (
    ActivityState,
    Direction,
    EntryType,
    EvaluationStatus,
    SetupStage,
    TrendState,
    VolatilityState,
)
from app.market_context.engine import MarketSnapshot
from app.market_context.indicators import adx, atr, bollinger_bands, rsi, z_score
from app.strategies.base import (
    ExitPlan,
    StrategyBase,
    StrategyEvaluation,
    TradeManagementAction,
    TradeManagementContext,
)


class MeanReversionStrategy(StrategyBase):
    strategy_id = "mean_reversion"
    strategy_name = "Mean Reversion"
    strategy_version = "1.0.0"

    def evaluate(self, snapshot: MarketSnapshot, bar_service, config: dict) -> StrategyEvaluation:
        started = snapshot.timestamp_utc
        evaluation = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), started)

        if snapshot.data_quality.value != "VALID":
            evaluation.status = EvaluationStatus.INVALID_DATA
            evaluation.rejection_reason_codes = ["MRV_INVALID_DATA"]
            return self.finish(evaluation, started)

        cfg = config.get("strategies", {}).get("mean_reversion", {})
        tf = cfg.get("timeframe", "M5")
        bars = bar_service.closed_bars(tf, as_of=started)
        evaluation.timeframes_used = [tf]

        if len(bars) < 60:
            evaluation.rejection_reason_codes = ["MRV_INSUFFICIENT_DATA"]
            return self.finish(evaluation, started)

        atr_value = atr(bars, 14)
        adx_value = adx(bars, 14)
        z = z_score(bars, int(cfg.get("zscore_period", 20)))
        bands = bollinger_bands(bars, int(cfg.get("bb_period", 20)), float(cfg.get("bb_mult", 2.0)))
        rsi_value = rsi(bars, int(cfg.get("rsi_period", 14)))
        if z is None or bands is None or atr_value is None:
            evaluation.rejection_reason_codes = ["MRV_INDICATORS_UNAVAILABLE"]
            return self.finish(evaluation, started)

        middle, upper, lower = bands
        closes = [b.close for b in bars]
        mean = sum(closes[-int(cfg.get("zscore_period", 20)) :]) / int(cfg.get("zscore_period", 20))

        evaluation.feature_snapshot = {
            "z_score": round(z, 3),
            "bb_middle": round(middle, 2),
            "bb_upper": round(upper, 2),
            "bb_lower": round(lower, 2),
            "rsi14": round(rsi_value, 1) if rsi_value is not None else None,
            "adx14": round(adx_value, 2) if adx_value is not None else None,
            "atr14": round(atr_value, 4),
            "last_close": bars[-1].close,
        }

        # ---- condition 1: regime filter (documented, not overly strict)
        adx_max = float(cfg.get("max_adx_for_reversion", 30.0))
        if adx_value is not None:
            c_regime = self.check("adx_below_threshold", round(adx_value, 2), "<=", adx_max, timeframe=tf)
            self.record(evaluation, c_regime)
            if not c_regime.passed:
                evaluation.rejection_reason_codes = ["MRV_STRONG_TREND_RISK"]
                evaluation.notes = "trend continuation risk too high for reversion"
                return self.finish(evaluation, started)

        # ---- condition 2: deviation beyond threshold
        z_min = float(cfg.get("min_zscore", 2.0))
        direction = Direction.BUY if z < -z_min else (Direction.SELL if z > z_min else Direction.NONE)
        c_dev = self.check("zscore_beyond", round(z, 2), ">=" if direction == Direction.SELL else "<=", -z_min if direction == Direction.BUY else z_min, timeframe=tf)
        if direction == Direction.NONE:
            self.record(
                evaluation,
                self.check(
                    "zscore_beyond",
                    f"|{z:.2f}| < {z_min}",
                    "==",
                    "not_beyond",
                    timeframe=tf,
                    note="no side selected",
                ),
            )
            evaluation.rejection_reason_codes = ["MRV_NO_SIGNIFICANT_DEVIATION"]
            return self.finish(evaluation, started)
        self.record(evaluation, c_dev)

        # ---- condition 3: rejection/confirmation candle back toward mean
        last = bars[-1]
        prev = bars[-2]
        if direction == Direction.BUY:
            confirmed = last.close > last.open and last.close > prev.close
            extreme = min(b.low for b in bars[-int(cfg.get("deviation_window", 10)) :])
            sl = extreme - float(cfg.get("sl_buffer_atr", 0.25)) * atr_value
            target = mean
        else:
            confirmed = last.close < last.open and last.close < prev.close
            extreme = max(b.high for b in bars[-int(cfg.get("deviation_window", 10)) :])
            sl = extreme + float(cfg.get("sl_buffer_atr", 0.25)) * atr_value
            target = mean

        c_confirm = self.check("rejection_candle", confirmed, "==", True, timeframe=tf)
        self.record(evaluation, c_confirm)
        if not c_confirm:
            evaluation.rejection_reason_codes = ["MRV_NO_REVERSAL_CONFIRMATION"]
            evaluation.setup_stage = SetupStage.ARMED
            return self.finish(evaluation, started)

        entry = last.close
        risk = abs(entry - sl)
        reward = abs(target - entry)
        rr = reward / risk if risk > 0 else 0
        c_rr = self.check("risk_reward_min", round(rr, 2), ">=", float(cfg.get("min_rr", 1.0)))
        self.record(evaluation, c_rr)
        if not c_rr.passed:
            evaluation.rejection_reason_codes = ["MRV_RR_TOO_LOW"]
            return self.finish(evaluation, started)

        evaluation.status = EvaluationStatus.CANDIDATE
        evaluation.setup_type = "ZSCORE_REVERSION"
        evaluation.setup_stage = SetupStage.TRIGGERED
        evaluation.direction = direction
        evaluation.entry_type = EntryType.MARKET
        evaluation.entry_price_proposal = round(entry, 2)
        evaluation.stop_loss = round(sl, 2)
        evaluation.take_profit = round(target, 2)
        evaluation.risk_reward_estimate = round(rr, 2)
        evaluation.initial_risk_distance = round(risk, 2)
        evaluation.valid_until_utc = started + timedelta(minutes=float(cfg.get("valid_minutes", 45)))
        evaluation.opportunity_key = f"MRV:{round(extreme, 2)}"
        evaluation.market_fit_score = self._market_fit(snapshot)
        evaluation.setup_quality_score = min(100.0, 40 + abs(z) * 15)
        evaluation.exit_plan = ExitPlan(
            take_profit=round(target, 2),
            exit_policy="MEAN_TARGET",
            break_even_at_r=float(cfg.get("break_even_at_r", 1.0)),
        )
        evaluation.notes = f"z={z:.2f} deviation with rejection candle; target rolling mean {round(target, 2)}"
        return self.finish(evaluation, started)

    def _market_fit(self, snapshot: MarketSnapshot) -> float:
        score = 50.0
        if snapshot.trend_state == TrendState.RANGING:
            score += 25
        elif snapshot.trend_state == TrendState.UNCLEAR:
            score += 10
        if snapshot.volatility_state in (VolatilityState.NORMAL, VolatilityState.HIGH):
            score += 10
        return min(100.0, score)

    def manage_open_trade(self, trade_context: TradeManagementContext, config: dict) -> TradeManagementAction:
        cfg = config.get("strategies", {}).get("mean_reversion", {})
        be_at_r = float(cfg.get("break_even_at_r", 1.0))
        if trade_context.r_multiple_now >= be_at_r:
            if trade_context.side == Direction.BUY and trade_context.stop_loss < trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL", new_stop_loss=trade_context.entry_price,
                    reason_code="MRV_BREAK_EVEN", reason_text="risk-free after 1R",
                )
            if trade_context.side == Direction.SELL and trade_context.stop_loss > trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL", new_stop_loss=trade_context.entry_price,
                    reason_code="MRV_BREAK_EVEN", reason_text="risk-free after 1R",
                )
        return TradeManagementAction(action="HOLD", reason_code="MRV_HOLD")

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": ["M5"],
            "setups": ["ZSCORE_REVERSION"],
            "concepts": ["zscore_deviation", "bollinger_reference", "rsi_auxiliary_only", "regime_filter_adx", "mean_target"],
        }
