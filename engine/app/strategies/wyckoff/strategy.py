"""Wyckoff school (spec 35.2).

Operational definitions:
- Trading Range: N bars where price stays within [range_low, range_high]
  built from confirmed swing pivots; quality = touches + width vs ATR.
- Spring/Upthrust: pierce of range boundary then close back inside within
  M bars, ideally on elevated activity.
- Sign of Strength/Weakness: displacement close outside the range held
  for K bars.
- Phase (A-E): *estimated* from the ordered event sequence — recorded as
  ``estimated_phase`` with confidence, never as fact.
Entry needs an independent trigger (spring/upthrust or SOS/SOW retest).
"""
from __future__ import annotations

from datetime import timedelta

from app.core.enums import (
    ActivityState,
    Direction,
    EntryType,
    EvaluationStatus,
    SetupStage,
)
from app.market_context.engine import MarketSnapshot
from app.market_context.indicators import atr, swings
from app.strategies.base import (
    ExitPlan,
    StrategyBase,
    StrategyEvaluation,
    TradeManagementAction,
    TradeManagementContext,
)


class WyckoffStrategy(StrategyBase):
    strategy_id = "wyckoff"
    strategy_name = "Wyckoff"
    strategy_version = "1.0.0"

    def evaluate(self, snapshot: MarketSnapshot, bar_service, config: dict) -> StrategyEvaluation:
        started = snapshot.timestamp_utc
        evaluation = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), started)

        if snapshot.data_quality.value != "VALID":
            evaluation.status = EvaluationStatus.INVALID_DATA
            evaluation.rejection_reason_codes = ["WYC_INVALID_DATA"]
            return self.finish(evaluation, started)

        cfg = config.get("strategies", {}).get("wyckoff", {})
        tf = cfg.get("timeframe", "M15")
        bars = bar_service.closed_bars(tf, as_of=started)
        evaluation.timeframes_used = [tf]

        if len(bars) < 80:
            evaluation.rejection_reason_codes = ["WYC_INSUFFICIENT_DATA"]
            return self.finish(evaluation, started)

        atr_value = atr(bars, 14)
        if not atr_value:
            evaluation.rejection_reason_codes = ["WYC_ATR_UNAVAILABLE"]
            return self.finish(evaluation, started)

        # ---- trading range detection
        rng = self._detect_range(bars, cfg)
        c_range = self.check("trading_range_defined", rng is not None, "==", True, timeframe=tf)
        self.record(evaluation, c_range)
        if rng is None:
            evaluation.rejection_reason_codes = ["WYC_NO_TRADING_RANGE"]
            evaluation.notes = "no qualified trading range on closed bars"
            return self.finish(evaluation, started)

        range_low, range_high, range_bars = rng["low"], rng["high"], rng["bars"]
        touches = rng["touches"]
        width_ratio = (range_high - range_low) / atr_value

        evaluation.feature_snapshot = {
            "range_low": round(range_low, 2),
            "range_high": round(range_high, 2),
            "range_bars": len(range_bars),
            "range_touches": touches,
            "range_width_atr": round(width_ratio, 2),
            "last_close": bars[-1].close,
        }

        c_width = self.check("range_width_min_atr", round(width_ratio, 2), ">=", float(cfg.get("range_min_width_atr", 4.0)), timeframe=tf)
        c_touch = self.check("range_boundary_touches", touches, ">=", int(cfg.get("range_min_touches", 4)), timeframe=tf)
        self.record(evaluation, c_width)
        self.record(evaluation, c_touch)
        if not (c_width.passed and c_touch.passed):
            evaluation.rejection_reason_codes = ["WYC_RANGE_QUALITY_LOW"]
            return self.finish(evaluation, started)

        # ---- events inside/after the range
        phase, events = self._estimate_phase(range_bars, bars, range_low, range_high, cfg, atr_value)
        evaluation.feature_snapshot["estimated_phase"] = phase
        evaluation.feature_snapshot["events"] = events

        spring = self._detect_spring_upthrust(range_bars, range_low, range_high, cfg, atr_value)
        if spring is not None:
            direction, pierce_level, extreme = spring
            entry = bars[-1].close
            buffer = float(cfg.get("sl_buffer_atr", 0.25)) * atr_value
            if direction == Direction.BUY:
                sl = extreme - buffer
                tp = range_high - float(cfg.get("tp_inset_atr", 0.3)) * atr_value
            else:
                sl = extreme + buffer
                tp = range_low + float(cfg.get("tp_inset_atr", 0.3)) * atr_value
            evaluation.setup_type = "WYCKOFF_SPRING" if direction == Direction.BUY else "WYCKOFF_UPTHRUST"
            evaluation.direction = direction
            rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
            c_rr = self.check("risk_reward_min", round(rr, 2), ">=", float(cfg.get("min_rr", 1.5)))
            self.record(evaluation, c_rr)
            if c_rr.passed:
                return self._finalize(evaluation, entry, sl, tp, cfg, started, key=f"WYC:{pierce_level:.2f}")
            evaluation.rejection_reason_codes = ["WYC_RR_TOO_LOW"]
            evaluation.setup_stage = SetupStage.ARMED
            return self.finish(evaluation, started)

        sos = self._detect_sos_sow(range_bars, range_low, range_high, cfg, atr_value)
        if sos is not None:
            direction, break_level = sos
            entry = bars[-1].close
            buffer = float(cfg.get("sl_buffer_atr", 0.3)) * atr_value
            if direction == Direction.BUY:
                sl = break_level - buffer
                tp = entry + (entry - sl) * float(cfg.get("rr_target", 2.0))
            else:
                sl = break_level + buffer
                tp = entry - (sl - entry) * float(cfg.get("rr_target", 2.0))
            evaluation.setup_type = "WYCKOFF_SOS" if direction == Direction.BUY else "WYCKOFF_SOW"
            evaluation.direction = direction
            rr = abs(tp - entry) / max(abs(entry - sl), 1e-9)
            c_rr = self.check("risk_reward_min", round(rr, 2), ">=", float(cfg.get("min_rr", 1.5)))
            self.record(evaluation, c_rr)
            if c_rr.passed:
                return self._finalize(evaluation, entry, sl, tp, cfg, started, key=f"WYC_SOS:{break_level:.2f}")
            evaluation.rejection_reason_codes = ["WYC_RR_TOO_LOW"]
            return self.finish(evaluation, started)

        evaluation.rejection_reason_codes = ["WYC_NO_TRIGGER_EVENT"]
        evaluation.notes = f"range ok, phase={phase}, no spring/upthrust/SOS trigger"
        return self.finish(evaluation, started)

    # ------------------------------------------------------------------ helpers
    def _detect_range(self, bars, cfg):
        """Latest confirmed swing bracket containing the recent price action."""
        pivots = swings(bars, left=int(cfg.get("pivot_left", 3)), right=int(cfg.get("pivot_right", 3)))
        if len(pivots) < 4:
            return None
        window = bars[-int(cfg.get("range_max_bars", 120)) :]
        highs = [p for i, k, p, _, _ in pivots if k == "HIGH"]
        lows = [p for i, k, p, _, _ in pivots if k == "LOW"]
        if not highs or not lows:
            return None
        range_high = max(highs[-4:])
        range_low = min(lows[-4:])
        inside = [b for b in window if range_low <= b.low and b.high <= range_high]
        if len(inside) < int(cfg.get("range_min_bars", 20)):
            return None
        touches = sum(1 for b in inside if b.high >= range_high - (range_high - range_low) * 0.05) + sum(
            1 for b in inside if b.low <= range_low + (range_high - range_low) * 0.05
        )
        return {"low": range_low, "high": range_high, "bars": inside, "touches": touches}

    def _detect_spring_upthrust(self, range_bars, range_low, range_high, cfg, atr_value):
        """Pierce below range_low / above range_high then close back inside."""
        window = range_bars[-int(cfg.get("spring_window_bars", 4)) :]
        overshoot = float(cfg.get("spring_min_overshoot_atr", 0.1)) * atr_value
        for bar in window:
            if bar.low < range_low - overshoot and range_low <= bar.close <= range_high:
                return Direction.BUY, range_low, bar.low
            if bar.high > range_high + overshoot and range_low <= bar.close <= range_high:
                return Direction.SELL, range_high, bar.high
        return None

    def _detect_sos_sow(self, range_bars, range_low, range_high, cfg, atr_value):
        """Sign of Strength/Weakness: close outside range and holds K bars."""
        hold = int(cfg.get("sos_hold_bars", 3))
        bars_after = range_bars
        for i in range(len(bars_after) - hold - 1, len(bars_after) - 1):
            if i < 0:
                continue
            bar = bars_after[i]
            if bar.close > range_high and all(b.close > range_high for b in bars_after[i : i + hold]):
                return Direction.BUY, range_high
            if bar.close < range_low and all(b.close < range_low for b in bars_after[i : i + hold]):
                return Direction.SELL, range_low
        return None

    def _estimate_phase(self, range_bars, all_bars, range_low, range_high, cfg, atr_value):
        """Rough phase estimate from event ordering (explicitly an estimate)."""
        events: list[str] = []
        vol_avg = sum(b.tick_volume or 1 for b in range_bars) / max(len(range_bars), 1)
        for bar in range_bars:
            vol = bar.tick_volume or 1
            if bar.low < range_low and bar.close > range_low and vol > vol_avg * 1.3:
                events.append("SPRING_CANDIDATE")
            if bar.high > range_high and bar.close < range_high and vol > vol_avg * 1.3:
                events.append("UPTHRUST_CANDIDATE")
            if bar.close > range_high:
                events.append("SOS_CANDIDATE")
            if bar.close < range_low:
                events.append("SOW_CANDIDATE")
        tail = events[-6:]
        if not tail:
            phase = "PHASE_B_ESTIMATED"
        elif tail[-1] in ("SOS_CANDIDATE",) :
            phase = "PHASE_D_ESTIMATED"
        elif "SPRING_CANDIDATE" in tail:
            phase = "PHASE_C_ESTIMATED"
        elif "UPTHRUST_CANDIDATE" in tail:
            phase = "PHASE_C_ESTIMATED"
        else:
            phase = "PHASE_B_ESTIMATED"
        return phase, tail

    def _finalize(self, evaluation, entry, sl, tp, cfg, started, *, key):
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0
        evaluation.status = EvaluationStatus.CANDIDATE
        evaluation.setup_stage = SetupStage.TRIGGERED
        evaluation.entry_type = EntryType.MARKET
        evaluation.entry_price_proposal = round(entry, 2)
        evaluation.stop_loss = round(sl, 2)
        evaluation.take_profit = round(tp, 2)
        evaluation.risk_reward_estimate = round(rr, 2)
        evaluation.initial_risk_distance = round(risk, 2)
        evaluation.valid_until_utc = started + timedelta(minutes=float(cfg.get("valid_minutes", 90)))
        evaluation.opportunity_key = key
        evaluation.market_fit_score = self._market_fit(evaluation)
        evaluation.setup_quality_score = min(100.0, 40 + rr * 20)
        evaluation.exit_plan = ExitPlan(take_profit=round(tp, 2), exit_policy="FIXED_TP", break_even_at_r=1.0)
        evaluation.notes = f"{evaluation.setup_type} inside documented trading range"
        return self.finish(evaluation, started)

    def _market_fit(self, evaluation) -> float:
        # filled after snapshot available via feature snapshot — simple default
        score = 50.0
        return score

    def manage_open_trade(self, trade_context: TradeManagementContext, config: dict) -> TradeManagementAction:
        return TradeManagementAction(action="HOLD", reason_code="WYC_HOLD")

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": ["M15"],
            "setups": ["WYCKOFF_SPRING", "WYCKOFF_UPTHRUST", "WYCKOFF_SOS", "WYCKOFF_SOW"],
            "concepts": [
                "trading_range_quality",
                "spring_upthrust_pierce_and_return",
                "sign_of_strength_weakness_hold",
                "estimated_phase_a_e",
            ],
        }
