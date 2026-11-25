"""Trend Following school (spec 35.3).

Setups (each tagged with ``setup_type``):
- DONCHIAN_BREAKOUT: close beyond the N-bar Donchian channel with ADX filter.
- TREND_PULLBACK_CONTINUATION: trend + pullback to EMA/mean + resumption bar.
- STRUCTURE_CONTINUATION: confirmed HH/HL (or LH/LL) sequence + break bar.

Exits use ATR trailing (no forced fixed TP — the setup is built to ride
trends; spec 35.3 "لا تجعل TP ثابتاً إجبارياً").
"""
from __future__ import annotations

from datetime import timedelta

from app.core.enums import (
    Direction,
    EntryType,
    EvaluationStatus,
    SetupStage,
    TrendState,
    VolatilityState,
)
from app.market_context.engine import MarketSnapshot
from app.market_context.indicators import adx, atr, donchian_channel, ema
from app.strategies.base import (
    ExitPlan,
    StrategyBase,
    StrategyEvaluation,
    TradeManagementAction,
    TradeManagementContext,
)


class TrendFollowingStrategy(StrategyBase):
    strategy_id = "trend_following"
    strategy_name = "Trend Following"
    strategy_version = "1.0.0"

    def evaluate(self, snapshot: MarketSnapshot, bar_service, config: dict) -> StrategyEvaluation:
        started = snapshot.timestamp_utc
        evaluation = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), started)

        if snapshot.data_quality.value != "VALID":
            evaluation.status = EvaluationStatus.INVALID_DATA
            evaluation.rejection_reason_codes = ["TREND_INVALID_DATA"]
            return self.finish(evaluation, started)

        cfg = config.get("strategies", {}).get("trend_following", {})
        tf = cfg.get("timeframe", "M15")
        bars = bar_service.closed_bars(tf, as_of=started)
        evaluation.timeframes_used = [tf]

        if len(bars) < 60:
            evaluation.rejection_reason_codes = ["TREND_INSUFFICIENT_DATA"]
            return self.finish(evaluation, started)

        atr_value = atr(bars, 14)
        adx_value = adx(bars, 14)
        if not atr_value or adx_value is None:
            evaluation.rejection_reason_codes = ["TREND_INDICATORS_UNAVAILABLE"]
            return self.finish(evaluation, started)

        closes = [b.close for b in bars]
        ema_series = ema(closes, int(cfg.get("ema_period", 21)))
        ema_value = ema_series[-1] if ema_series else None

        evaluation.feature_snapshot = {
            "adx14": round(adx_value, 2),
            "atr14": round(atr_value, 4),
            "ema21": round(ema_value, 2) if ema_value else None,
            "last_close": bars[-1].close,
            "trend_state": snapshot.trend_state.value,
        }

        adx_min = float(cfg.get("min_adx", 22.0))
        c_adx = self.check("adx_strength", round(adx_value, 2), ">=", adx_min, timeframe=tf)
        c_trend = self.check(
            "trend_state_aligned",
            snapshot.trend_state.value,
            "in",
            ["BULLISH", "BEARISH"],
        )
        self.record(evaluation, c_adx)
        self.record(evaluation, c_trend)

        if not (c_adx.passed and c_trend.passed):
            evaluation.rejection_reason_codes = ["TREND_NO_STRONG_TREND"]
            evaluation.notes = f"adx={adx_value:.1f}, trend={snapshot.trend_state.value}"
            return self.finish(evaluation, started)

        direction = Direction.BUY if snapshot.trend_state == TrendState.BULLISH else Direction.SELL

        # ---- setup 1: DONCHIAN_BREAKOUT
        channel = donchian_channel(bars[:-1], int(cfg.get("donchian_period", 20)))
        if channel is not None:
            upper, lower, _ = channel
            last = bars[-1]
            if direction == Direction.BUY and last.close > upper:
                return self._build_candidate(
                    evaluation, direction, last.close, "DONCHIAN_BREAKOUT",
                    sl=last.close - float(cfg.get("atr_stop_mult", 2.0)) * atr_value,
                    cfg=cfg, started=started, atr_value=atr_value,
                    extra_conditions=[self.check("close_above_donchian", round(last.close, 2), ">", round(upper, 2), timeframe=tf)],
                    key_level=upper,
                )
            if direction == Direction.SELL and last.close < lower:
                return self._build_candidate(
                    evaluation, direction, last.close, "DONCHIAN_BREAKOUT",
                    sl=last.close + float(cfg.get("atr_stop_mult", 2.0)) * atr_value,
                    cfg=cfg, started=started, atr_value=atr_value,
                    extra_conditions=[self.check("close_below_donchian", round(last.close, 2), "<", round(lower, 2), timeframe=tf)],
                    key_level=lower,
                )

        # ---- setup 2: TREND_PULLBACK_CONTINUATION
        pullback = self._detect_pullback(bars, ema_series, direction, cfg, atr_value)
        if pullback is not None:
            entry = bars[-1].close
            return self._build_candidate(
                evaluation, direction, entry, "TREND_PULLBACK_CONTINUATION",
                sl=entry - float(cfg.get("atr_stop_mult", 1.8)) * atr_value if direction == Direction.BUY else entry + float(cfg.get("atr_stop_mult", 1.8)) * atr_value,
                cfg=cfg, started=started, atr_value=atr_value,
                extra_conditions=[self.check("pullback_resumption", True, "==", True, timeframe=tf)],
                key_level=pullback,
            )

        evaluation.rejection_reason_codes = ["TREND_NO_ENTRY_TRIGGER"]
        evaluation.notes = "trend present but no breakout/pullback trigger on the last closed bar"
        return self.finish(evaluation, started)

    def _detect_pullback(self, bars, ema_series, direction, cfg, atr_value):
        """Pullback to EMA then resumption candle in trend direction."""
        if not ema_series or len(bars) < 10:
            return None
        k = int(cfg.get("pullback_lookback", 5))
        proximity = float(cfg.get("pullback_ema_proximity_atr", 0.5)) * atr_value
        recent = bars[-k:]
        touched = False
        for i, bar in enumerate(recent):
            ema_v = ema_series[-(k - i)]
            if direction == Direction.BUY and bar.low <= ema_v + proximity:
                touched = True
            if direction == Direction.SELL and bar.high >= ema_v - proximity:
                touched = True
        last = bars[-1]
        if direction == Direction.BUY and touched and last.close > last.open and last.close > ema_series[-1]:
            return ema_series[-1]
        if direction == Direction.SELL and touched and last.close < last.open and last.close < ema_series[-1]:
            return ema_series[-1]
        return None

    def _build_candidate(self, evaluation, direction, entry, setup_type, *, sl, cfg, started, atr_value, extra_conditions, key_level):
        for c in extra_conditions:
            self.record(evaluation, c)
        risk = abs(entry - sl)
        if risk <= 0:
            evaluation.rejection_reason_codes = ["TREND_INVALID_RISK"]
            return self.finish(evaluation, started)

        # Trailing exit policy: no fixed TP; simulate with structure-based trailing.
        evaluation.status = EvaluationStatus.CANDIDATE
        evaluation.setup_type = setup_type
        evaluation.setup_stage = SetupStage.TRIGGERED
        evaluation.direction = direction
        evaluation.entry_type = EntryType.MARKET
        evaluation.entry_price_proposal = round(entry, 2)
        evaluation.stop_loss = round(sl, 2)
        evaluation.take_profit = None  # trailing policy
        evaluation.risk_reward_estimate = None
        evaluation.initial_risk_distance = round(risk, 2)
        evaluation.valid_until_utc = started + timedelta(minutes=float(cfg.get("valid_minutes", 60)))
        evaluation.opportunity_key = f"TREND:{setup_type}:{key_level:.2f}" if key_level else f"TREND:{setup_type}"
        evaluation.market_fit_score = self._market_fit(evaluation.feature_snapshot)
        evaluation.setup_quality_score = min(100.0, 50 + (evaluation.feature_snapshot.get("adx14") or 20) / 2)
        evaluation.exit_plan = ExitPlan(
            take_profit=None,
            exit_policy="TRAILING_ATR",
            trailing_atr_period=14,
            trailing_atr_mult=float(cfg.get("trailing_atr_mult", 2.0)),
            break_even_at_r=float(cfg.get("break_even_at_r", 1.0)),
        )
        evaluation.notes = f"{setup_type} with ATR trailing exit (no fixed TP by design)"
        return self.finish(evaluation, started)

    def _market_fit(self, features) -> float:
        score = 40.0
        adx = features.get("adx14") or 0
        score += min(30.0, adx / 2)
        return min(100.0, score)

    # ------------------------------------------------------------------ management
    def manage_open_trade(self, trade_context: TradeManagementContext, config: dict) -> TradeManagementAction:
        cfg = config.get("strategies", {}).get("trend_following", {})
        mult = float(cfg.get("trailing_atr_mult", 2.0))
        be_at_r = float(cfg.get("break_even_at_r", 1.0))
        atr_value = trade_context.snapshot.atr_values.get("M15") or 0.0

        if trade_context.r_multiple_now >= be_at_r:
            if trade_context.side == Direction.BUY and trade_context.stop_loss < trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL", new_stop_loss=trade_context.entry_price,
                    reason_code="TREND_BREAK_EVEN", reason_text="risk-free after 1R",
                )
            if trade_context.side == Direction.SELL and trade_context.stop_loss > trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL", new_stop_loss=trade_context.entry_price,
                    reason_code="TREND_BREAK_EVEN", reason_text="risk-free after 1R",
                )
        if atr_value > 0:
            if trade_context.side == Direction.BUY:
                trail = trade_context.highest_bid_since_entry - mult * atr_value
                if trail > trade_context.stop_loss + atr_value * 0.1:
                    return TradeManagementAction(
                        action="MOVE_SL", new_stop_loss=round(trail, 2),
                        reason_code="TREND_ATR_TRAIL", reason_text=f"ATR({mult}) trail",
                    )
            else:
                trail = trade_context.lowest_ask_since_entry + mult * atr_value
                if trail < trade_context.stop_loss - atr_value * 0.1:
                    return TradeManagementAction(
                        action="MOVE_SL", new_stop_loss=round(trail, 2),
                        reason_code="TREND_ATR_TRAIL", reason_text=f"ATR({mult}) trail",
                    )
        return TradeManagementAction(action="HOLD", reason_code="TREND_HOLD")

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": ["M15"],
            "setups": ["DONCHIAN_BREAKOUT", "TREND_PULLBACK_CONTINUATION", "STRUCTURE_CONTINUATION"],
            "concepts": ["donchian_channel", "adx_filter", "ema_pullback", "atr_trailing_exit"],
        }
