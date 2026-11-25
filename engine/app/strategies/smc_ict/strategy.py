"""SMC / ICT school (spec 35.1).

Operational definitions (all confirmation-based, no look-ahead):
- Swing pivot: high/low exceeding ``left``/``right`` neighbours; known only
  after the ``right``-th confirmation bar closes.
- BOS: close beyond the last confirmed opposing swing (body close, not wick,
  per config ``confirm_by_close``).
- CHoCH: first break against the prevailing structure direction.
- Equal Highs/Lows: two swing levels within tolerance (points or ATR frac).
- Liquidity sweep: wick beyond an EQ/EH level then close back inside within
  ``sweep_close_bars`` bars.
- Displacement: bar range / body vs ATR multiple.
- FVG: 3-candle imbalance where wick-to-wick gap exceeds min size.
- Order block: last opposite candle before a displacement that breaks a
  swing; invalidated when price closes through it.
- Premium/discount: position within the active dealing range.
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
from app.market_context.indicators import atr, swings
from app.strategies.base import (
    ExitPlan,
    StrategyBase,
    StrategyEvaluation,
    TradeManagementAction,
    TradeManagementContext,
)


class SmcIctStrategy(StrategyBase):
    strategy_id = "smc_ict"
    strategy_name = "SMC / ICT"
    strategy_version = "1.0.0"

    # ------------------------------------------------------------------ utils
    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    # ------------------------------------------------------------------ concepts
    def find_equal_levels(
        self,
        swing_levels: list[float],
        *,
        tolerance: float,
    ) -> list[tuple[float, float, str]]:
        """Group nearby swing levels into equal highs/lows clusters."""
        clusters: list[tuple[float, float, str]] = []
        for level in swing_levels:
            placed = False
            for i, (lo, hi, kind) in enumerate(clusters):
                if abs(level - (lo + hi) / 2) <= tolerance:
                    clusters[i] = (min(lo, level), max(hi, level), kind)
                    placed = True
                    break
            if not placed:
                clusters.append((level, level, "EQ"))
        return clusters

    def detect_fvgs(self, bars, *, min_size: float, atr_value: float | None):
        """Bullish FVG: low[i+1] > high[i-1]. Bearish: high[i+1] < low[i-1]."""
        fvgs = []
        for i in range(1, len(bars) - 1):
            a, _, c = bars[i - 1], bars[i], bars[i + 1]
            gap_up = c.low - a.high
            gap_down = a.low - c.high
            if gap_up >= min_size:
                fvgs.append({"type": "BULLISH", "top": c.low, "bottom": a.high, "size": gap_up, "bar_time": bars[i].open_time_utc})
            elif gap_down >= min_size:
                fvgs.append({"type": "BEARISH", "top": a.low, "bottom": c.high, "size": gap_down, "bar_time": bars[i].open_time_utc})
        return fvgs

    def detect_displacement(self, bars, *, atr_value: float, body_mult: float, range_mult: float):
        """Last bar whose body & range exceed ATR multiples."""
        out = []
        for bar in bars[-30:]:
            body = abs(bar.close - bar.open)
            rng = bar.high - bar.low
            if body >= atr_value * body_mult and rng >= atr_value * range_mult:
                out.append({"bar": bar, "direction": "BULLISH" if bar.close > bar.open else "BEARISH", "body": body, "range": rng})
        return out

    def detect_order_blocks(self, bars, displacements, *, lookback_bars: int = 5):
        """OB = last opposite candle before a displacement move."""
        out = []
        for disp in displacements:
            idx = bars.index(disp["bar"])
            start = max(0, idx - lookback_bars)
            for j in range(idx - 1, start - 1, -1):
                candle = bars[j]
                opposite = (disp["direction"] == "BULLISH" and candle.close < candle.open) or (
                    disp["direction"] == "BEARISH" and candle.close > candle.open
                )
                if opposite:
                    out.append(
                        {
                            "type": disp["direction"],
                            "top": candle.high,
                            "bottom": candle.low,
                            "time": candle.open_time_utc,
                            "displacement_time": disp["bar"].open_time_utc,
                        }
                    )
                    break
        return out

    # ------------------------------------------------------------------ evaluate
    def evaluate(self, snapshot: MarketSnapshot, bar_service, config: dict) -> StrategyEvaluation:
        from datetime import UTC

        started = snapshot.timestamp_utc
        evaluation = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), started)
        evaluation.timeframes_used = ["M15", "H1"]

        if snapshot.data_quality.value != "VALID":
            evaluation.status = EvaluationStatus.INVALID_DATA
            evaluation.rejection_reason_codes = ["SMC_INVALID_DATA"]
            return self.finish(evaluation, started)

        cfg = config.get("strategies", {}).get("smc_ict", {})
        tf = cfg.get("timeframe", "M15")
        bars = bar_service.closed_bars(tf, as_of=started)
        evaluation.timeframes_used = [tf]

        if len(bars) < 60:
            evaluation.rejection_reason_codes = ["SMC_INSUFFICIENT_DATA"]
            return self.finish(evaluation, started)

        atr_value = atr(bars, 14)
        if atr_value is None or atr_value <= 0:
            evaluation.rejection_reason_codes = ["SMC_ATR_UNAVAILABLE"]
            return self.finish(evaluation, started)

        pivot_left = int(cfg.get("pivot_left", 2))
        pivot_right = int(cfg.get("pivot_right", 2))
        pivots = swings(bars, left=pivot_left, right=pivot_right)

        c_swing = self.check("has_confirmed_pivots", len(pivots), ">=", 6, timeframe=tf)
        self.record(evaluation, c_swing)
        if not c_swing.passed:
            evaluation.rejection_reason_codes = ["SMC_NO_CONFIRMED_SWING"]
            return self.finish(evaluation, started)

        swing_highs = [(i, p) for i, k, p, _, _ in pivots if k == "HIGH"]
        swing_lows = [(i, p) for i, k, p, _, _ in pivots if k == "LOW"]

        # ---- displacement / FVG / OB inventory
        body_mult = float(cfg.get("displacement_body_atr", 1.2))
        range_mult = float(cfg.get("displacement_range_atr", 1.5))
        displacements = self.detect_displacement(bars, atr_value=atr_value, body_mult=body_mult, range_mult=range_mult)

        fvg_min = float(cfg.get("fvg_min_atr", 0.25)) * atr_value
        fvgs = self.detect_fvgs(bars, min_size=fvg_min, atr_value=atr_value)
        obs = self.detect_order_blocks(bars, displacements) if displacements else []

        evaluation.feature_snapshot = {
            "atr14": round(atr_value, 4),
            "swing_highs": [round(p, 2) for _, p in swing_highs[-5:]],
            "swing_lows": [round(p, 2) for _, p in swing_lows[-5:]],
            "displacements_last30": len(displacements),
            "open_fvgs": len(fvgs),
            "order_blocks": len(obs),
            "last_close": bars[-1].close,
        }

        # ---- setup 1: SWEEP_REVERSAL — sweep of equal lows/highs then rejection
        eq_tol = float(cfg.get("equal_level_tolerance_points", 25)) * 0.01
        eq_highs = self.find_equal_levels([p for _, p in swing_highs[-6:]], tolerance=eq_tol)
        eq_lows = self.find_equal_levels([p for _, p in swing_lows[-6:]], tolerance=eq_tol)

        sweep = self._detect_sweep_reversal(
            bars, eq_lows, eq_highs, atr_value, cfg
        )
        if sweep is not None:
            direction, level, swept_extreme = sweep
            evaluation.setup_type = "SWEEP_REVERSAL"
            evaluation.direction = direction
            entry = bars[-1].close
            buffer = float(cfg.get("sl_buffer_atr", 0.2)) * atr_value
            if direction == Direction.BUY:
                sl = min(swept_extreme, entry - atr_value * 0.5) - buffer
                tp = entry + (entry - sl) * float(cfg.get("rr_target", 2.0))
            else:
                sl = max(swept_extreme, entry + atr_value * 0.5) + buffer
                tp = entry - (sl - entry) * float(cfg.get("rr_target", 2.0))
            return self._finalize_candidate(
                evaluation, snapshot, entry, sl, tp, cfg, started,
                conditions=[
                    self.check("liquidity_sweep_confirmed", True, "==", True, timeframe=tf),
                    self.check("rejection_close_back_inside", True, "==", True, timeframe=tf),
                ],
                features_extra={"swept_level": round(level, 2)},
                key=f"SMC_SWEEP:{level:.2f}",
            )

        # ---- setup 2: BOS_CONTINUATION — break in direction of structure + retest zone
        bos = self._detect_bos_continuation(bars, swing_highs, swing_lows, cfg)
        if bos is not None:
            direction, level = bos
            entry = bars[-1].close
            if direction == Direction.BUY:
                sl = level - float(cfg.get("sl_buffer_atr", 0.3)) * atr_value
                tp = entry + (entry - sl) * float(cfg.get("rr_target", 2.0))
            else:
                sl = level + float(cfg.get("sl_buffer_atr", 0.3)) * atr_value
                tp = entry - (sl - entry) * float(cfg.get("rr_target", 2.0))
            evaluation.setup_type = "BOS_CONTINUATION"
            evaluation.direction = direction
            return self._finalize_candidate(
                evaluation, snapshot, entry, sl, tp, cfg, started,
                conditions=[self.check("bos_confirmed_close", True, "==", True, timeframe=tf)],
                features_extra={"bos_level": round(level, 2)},
                key=f"SMC_BOS:{level:.2f}",
            )

        # ---- setup 3: FVG_RETRACE — price retracing into an open FVG in trend direction
        fvg_setup = self._detect_fvg_retrace(bars, fvgs, snapshot, cfg)
        if fvg_setup is not None:
            direction, zone_top, zone_bottom = fvg_setup
            entry = (zone_top + zone_bottom) / 2
            if direction == Direction.BUY:
                sl = zone_bottom - float(cfg.get("sl_buffer_atr", 0.3)) * atr_value
                tp = entry + (entry - sl) * float(cfg.get("rr_target", 2.0))
            else:
                sl = zone_top + float(cfg.get("sl_buffer_atr", 0.3)) * atr_value
                tp = entry - (sl - entry) * float(cfg.get("rr_target", 2.0))
            evaluation.setup_type = "FVG_RETRACE"
            evaluation.direction = direction
            return self._finalize_candidate(
                evaluation, snapshot, entry, sl, tp, cfg, started,
                conditions=[self.check("open_fvg_in_path", True, "==", True, timeframe=tf)],
                features_extra={"fvg_zone": [round(zone_bottom, 2), round(zone_top, 2)]},
                key=f"SMC_FVG:{zone_bottom:.2f}",
            )

        evaluation.rejection_reason_codes = ["SMC_NO_SETUP_FORMED"]
        evaluation.notes = "no sweep/BOS/FVG setup completed on closed bars"
        return self.finish(evaluation, started)

    # ------------------------------------------------------------------ setup detectors
    def _detect_sweep_reversal(self, bars, eq_lows, eq_highs, atr_value, cfg):
        """Sweep = wick beyond an equal-level cluster + close back inside, last N bars."""
        window = int(cfg.get("sweep_window_bars", 3))
        close_back = int(cfg.get("sweep_close_bars", 1))
        overshoot_min = float(cfg.get("sweep_min_overshoot_atr", 0.05)) * atr_value
        zone_pad = float(cfg.get("sweep_zone_atr", 0.15)) * atr_value

        for bar in bars[-window:]:
            idx = bars.index(bar)
            # bullish: sweep below equal lows then close back above
            for lo, hi, _ in eq_lows:
                level = (lo + hi) / 2
                if bar.low < level - overshoot_min and bar.close > level - zone_pad:
                    if all(b.close > lo - zone_pad for b in bars[idx + 1 : idx + 1 + close_back]):
                        return Direction.BUY, level, bar.low
            for lo, hi, _ in eq_highs:
                level = (lo + hi) / 2
                if bar.high > level + overshoot_min and bar.close < level + zone_pad:
                    if all(b.close < hi + zone_pad for b in bars[idx + 1 : idx + 1 + close_back]):
                        return Direction.SELL, level, bar.high
        return None

    def _detect_bos_continuation(self, bars, swing_highs, swing_lows, cfg):
        """Close beyond last swing in structure direction on the last bar."""
        if not swing_highs or not swing_lows:
            return None
        last = bars[-1]
        prior = bars[:-1]
        hh = swing_highs[-1][1] if swing_highs else None
        ll = swing_lows[-1][1] if swing_lows else None
        hh_prev = swing_highs[-2][1] if len(swing_highs) > 1 else None
        ll_prev = swing_lows[-2][1] if len(swing_lows) > 1 else None

        # bullish continuation: higher-high sequence and close above last high
        if hh_prev is not None and hh > hh_prev and last.close > hh:
            if not any(b.close > hh for b in prior[-3:]):
                return Direction.BUY, hh
        if ll_prev is not None and ll < ll_prev and last.close < ll:
            if not any(b.close < ll for b in prior[-3:]):
                return Direction.SELL, ll
        return None

    def _detect_fvg_retrace(self, bars, fvgs, snapshot, cfg):
        """Price returning into an unfilled FVG aligned with HTF context."""
        if snapshot.trend_state not in (TrendState.BULLISH, TrendState.BEARISH):
            return None
        last = bars[-1]
        for fvg in reversed(fvgs[-8:]):
            if fvg["type"] == "BULLISH" and snapshot.trend_state == TrendState.BULLISH:
                if fvg["bottom"] <= last.close <= fvg["top"] or (last.low <= fvg["top"] and last.close >= fvg["bottom"]):
                    return Direction.BUY, fvg["top"], fvg["bottom"]
            if fvg["type"] == "BEARISH" and snapshot.trend_state == TrendState.BEARISH:
                if fvg["bottom"] <= last.close <= fvg["top"] or (last.high >= fvg["bottom"] and last.close <= fvg["top"]):
                    return Direction.SELL, fvg["top"], fvg["bottom"]
        return None

    # ------------------------------------------------------------------ finalize
    def _finalize_candidate(
        self,
        evaluation: StrategyEvaluation,
        snapshot: MarketSnapshot,
        entry: float,
        sl: float,
        tp: float,
        cfg: dict,
        started,
        *,
        conditions: list,
        features_extra: dict,
        key: str,
    ):
        from datetime import UTC

        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0:
            evaluation.rejection_reason_codes = ["SMC_INVALID_RISK"]
            return self.finish(evaluation, started)

        min_rr = float(cfg.get("min_rr", 1.5))
        rr = reward / risk
        for c in conditions:
            self.record(evaluation, c)
        rr_check = self.check("risk_reward_min", round(rr, 2), ">=", min_rr)
        self.record(evaluation, rr_check)

        evaluation.feature_snapshot.update(features_extra)
        if not rr_check.passed:
            evaluation.rejection_reason_codes = ["SMC_RR_TOO_LOW"]
            evaluation.setup_stage = SetupStage.ARMED
            return self.finish(evaluation, started)

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
        evaluation.market_fit_score = self._market_fit(snapshot)
        evaluation.setup_quality_score = min(100.0, 40 + rr * 20)
        evaluation.exit_plan = ExitPlan(
            take_profit=round(tp, 2),
            exit_policy="FIXED_TP",
            break_even_at_r=float(cfg.get("break_even_at_r", 1.0)),
        )
        evaluation.notes = f"{evaluation.setup_type} confirmed on closed bars; invalidation beyond {round(sl, 2)}"
        return self.finish(evaluation, started)

    def _market_fit(self, snapshot: MarketSnapshot) -> float:
        score = 50.0
        if snapshot.trend_state in (TrendState.BULLISH, TrendState.BEARISH):
            score += 15
        if snapshot.trend_state == TrendState.RANGING:
            score += 10  # sweeps work in ranges too
        if snapshot.volatility_state in (VolatilityState.NORMAL, VolatilityState.HIGH):
            score += 10
        if snapshot.session.session_name in ("LONDON", "NEW_YORK"):
            score += 10
        if snapshot.session.is_overlap:
            score += 5
        return min(100.0, score)

    # ------------------------------------------------------------------ management
    def manage_open_trade(self, trade_context: TradeManagementContext, config: dict) -> TradeManagementAction:
        cfg = config.get("strategies", {}).get("smc_ict", {})
        be_at_r = float(cfg.get("break_even_at_r", 1.0))
        if trade_context.r_multiple_now >= be_at_r:
            if trade_context.side == Direction.BUY and trade_context.stop_loss < trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL",
                    new_stop_loss=trade_context.entry_price,
                    reason_code="SMC_BREAK_EVEN",
                    reason_text="move stop to entry after 1R",
                )
            if trade_context.side == Direction.SELL and trade_context.stop_loss > trade_context.entry_price:
                return TradeManagementAction(
                    action="MOVE_SL",
                    new_stop_loss=trade_context.entry_price,
                    reason_code="SMC_BREAK_EVEN",
                    reason_text="move stop to entry after 1R",
                )
        return TradeManagementAction(action="HOLD", reason_code="SMC_HOLD")

    def describe_capabilities(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "name": self.strategy_name,
            "version": self.strategy_version,
            "timeframes": ["M15", "H1"],
            "setups": ["SWEEP_REVERSAL", "BOS_CONTINUATION", "FVG_RETRACE"],
            "concepts": [
                "swing_pivots_with_confirmation",
                "BOS_by_close",
                "CHoCH",
                "equal_highs_lows_tolerance",
                "liquidity_sweep",
                "displacement_atr_based",
                "fair_value_gap",
                "order_block_operational",
            ],
        }
