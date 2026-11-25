"""Market Context Engine (spec section 6 / 34).

Builds a multi-dimensional ``MarketSnapshot`` (trend, volatility, activity,
liquidity proxy, structure, session) — it *describes* the market and never
blocks trading by itself (spec 6.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.constants import CONTEXT_VERSION, FEATURE_SET_VERSION
from app.core.enums import (
    ActivityState,
    DataQuality,
    LiquidityProxyState,
    MarketStructureEvent,
    NewsContextState,
    SessionPhase,
    TrendState,
    VolatilityState,
)
from app.core.logging_setup import get_logger
from app.data.bar_service import BarService
from app.data.contracts import MarketTick
from app.market_context.indicators import adx, atr, efficiency_ratio, realized_volatility, rsi
from app.market_context.sessions import SessionEngine, SessionInfo
from app.market_context.structure import StructureEngine

logger = get_logger("context.engine")


@dataclass
class MarketSnapshot:
    snapshot_id: str
    timestamp_utc: datetime
    symbol: str
    source_symbol: str
    data_quality: DataQuality
    trend_state: TrendState
    volatility_state: VolatilityState
    activity_state: ActivityState
    liquidity_proxy_state: LiquidityProxyState
    market_structure: list[str]
    session: SessionInfo
    spread_points: float | None
    bid: float | None
    ask: float | None
    atr_values: dict[str, float] = field(default_factory=dict)
    feature_values: dict[str, Any] = field(default_factory=dict)
    news_context: NewsContextState = NewsContextState.UNAVAILABLE
    context_version: str = CONTEXT_VERSION
    feature_set_version: str = FEATURE_SET_VERSION
    timeframe_context: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "symbol": self.symbol,
            "source_symbol": self.source_symbol,
            "data_quality": self.data_quality.value,
            "trend_state": self.trend_state.value,
            "volatility_state": self.volatility_state.value,
            "activity_state": self.activity_state.value,
            "liquidity_proxy_state": self.liquidity_proxy_state.value,
            "market_structure": self.market_structure,
            "session": self.session.session_name,
            "session_phase": self.session.phase.value,
            "session_local_time": self.session.local_time,
            "is_session_overlap": self.session.is_overlap,
            "active_sessions": list(self.session.active_sessions),
            "spread_points": self.spread_points,
            "bid": self.bid,
            "ask": self.ask,
            "atr_values": self.atr_values,
            "feature_values": self.feature_values,
            "news_context": self.news_context.value,
            "context_version": self.context_version,
            "timeframe_context": self.timeframe_context,
        }


class MarketContextEngine:
    def __init__(
        self,
        *,
        symbol: str,
        source_symbol: str,
        bar_service: BarService,
        session_engine: SessionEngine,
        structure_engine: StructureEngine | None = None,
        config: dict | None = None,
        point: float = 0.01,
    ) -> None:
        self.symbol = symbol
        self.source_symbol = source_symbol
        self.bars = bar_service
        self.sessions = session_engine
        self.structure = structure_engine or StructureEngine()
        self.config = config or {}
        self.point = point
        ctx_cfg = self.config.get("market_context", {})
        self.trend_tf = ctx_cfg.get("trend_timeframe", "M15")
        self.high_tf = ctx_cfg.get("higher_timeframe", "H1")
        self.base_tf = ctx_cfg.get("base_timeframe", "M5")

    # -------------------------------------------------------------- dimensions
    def _trend_state(self, as_of: datetime) -> tuple[TrendState, dict]:
        bars = self.bars.closed_bars(self.trend_tf, as_of=as_of)
        features: dict[str, Any] = {}
        if len(bars) < 40:
            return TrendState.UNCLEAR, features

        adx_value = adx(bars, 14)
        er = efficiency_ratio(bars, 20)
        closes = [b.close for b in bars]
        fast = sum(closes[-20:]) / 20
        slow = sum(closes[-50:]) / 50
        slope = (closes[-1] - closes[-11]) / 10

        features["adx"] = round(adx_value, 2) if adx_value is not None else None
        features["efficiency_ratio"] = round(er, 3) if er is not None else None
        features["ma20"] = round(fast, 2)
        features["ma50"] = round(slow, 2)
        features["ma_slope_10"] = round(slope, 4)

        adx_threshold = float(self.config.get("market_context", {}).get("trend_adx_threshold", 22.0))
        er_threshold = float(self.config.get("market_context", {}).get("trend_er_threshold", 0.30))

        if adx_value is None or er is None:
            return TrendState.UNCLEAR, features

        price_above = closes[-1] > fast > slow
        price_below = closes[-1] < fast < slow
        trending = adx_value >= adx_threshold and er >= er_threshold

        if trending and price_above and slope > 0:
            return TrendState.BULLISH, features
        if trending and price_below and slope < 0:
            return TrendState.BEARISH, features
        if adx_value < adx_threshold * 0.7:
            return TrendState.RANGING, features
        if (price_above and slope < 0) or (price_below and slope > 0):
            return TrendState.TRANSITIONAL, features
        return TrendState.UNCLEAR, features

    def _volatility_state(self, as_of: datetime) -> tuple[VolatilityState, dict]:
        bars = self.bars.closed_bars(self.trend_tf, as_of=as_of)
        features: dict[str, Any] = {}
        if len(bars) < 30:
            return VolatilityState.NORMAL, features
        current_atr = atr(bars, 14)
        rv = realized_volatility(bars, 20)
        # ATR ratio vs its own 50-bar trailing mean (no future data)
        atr_series = [atr(bars[: i + 1], 14) for i in range(20, len(bars))]
        atr_series = [a for a in atr_series if a is not None]
        features["atr14"] = round(current_atr, 4) if current_atr is not None else None
        features["realized_vol_20"] = round(rv, 6) if rv is not None else None
        if current_atr is None or not atr_series:
            return VolatilityState.NORMAL, features
        baseline = sum(atr_series) / len(atr_series)
        ratio = current_atr / baseline if baseline > 0 else 1.0
        features["atr_ratio"] = round(ratio, 3)
        if ratio < 0.5:
            return VolatilityState.VERY_LOW, features
        if ratio < 0.8:
            return VolatilityState.LOW, features
        if ratio <= 1.25:
            return VolatilityState.NORMAL, features
        if ratio <= 2.0:
            return VolatilityState.HIGH, features
        return VolatilityState.EXTREME, features

    def _activity_state(self, as_of: datetime, tick: MarketTick | None) -> tuple[ActivityState, dict]:
        bars = self.bars.closed_bars(self.base_tf, as_of=as_of)
        features: dict[str, Any] = {"activity_source": "TICK_VOLUME_PROXY"}
        if len(bars) < 30:
            return ActivityState.NORMAL, features
        vols = [b.tick_volume or 0.0 for b in bars]
        current = sum(vols[-5:]) / 5
        baseline = sum(vols[-30:]) / 30
        features["tick_volume_avg_5"] = round(current, 1)
        features["tick_volume_avg_30"] = round(baseline, 1)
        if baseline <= 0:
            return ActivityState.NORMAL, features
        ratio = current / baseline
        features["activity_ratio"] = round(ratio, 3)
        if ratio < 0.5:
            return ActivityState.QUIET, features
        if ratio < 0.9:
            return ActivityState.NORMAL, features
        if ratio <= 1.8:
            return ActivityState.ACTIVE, features
        return ActivityState.VERY_ACTIVE, features

    def _liquidity_proxy(self, as_of: datetime, tick: MarketTick | None) -> tuple[LiquidityProxyState, dict]:
        features: dict[str, Any] = {"proxy_note": "broker feed proxy, NOT global liquidity"}
        if tick is None:
            return LiquidityProxyState.NORMAL_PROXY, features
        spread_points = tick.spread_points
        features["spread_points"] = round(spread_points, 1)
        bars = self.bars.closed_bars(self.base_tf, as_of=as_of)
        spreads = [b.spread for b in bars[-50:] if b.spread]
        if spreads:
            baseline = sum(spreads) / len(spreads)
            features["spread_baseline_points"] = round(baseline, 1)
            ratio = spread_points / baseline if baseline > 0 else 1.0
            features["spread_ratio"] = round(ratio, 2)
            if ratio > 3.0:
                return LiquidityProxyState.DISLOCATED, features
            if ratio > 1.5:
                return LiquidityProxyState.LOW_PROXY, features
            if ratio < 0.75:
                return LiquidityProxyState.HIGH_PROXY, features
        return LiquidityProxyState.NORMAL_PROXY, features

    # -------------------------------------------------------------- snapshot
    def build_snapshot(
        self,
        *,
        now_utc: datetime,
        tick: MarketTick | None,
        data_quality: DataQuality,
        snapshot_id: str,
    ) -> MarketSnapshot:
        trend_state, trend_features = self._trend_state(now_utc)
        volatility_state, vol_features = self._volatility_state(now_utc)
        activity_state, act_features = self._activity_state(now_utc, tick)
        liquidity_state, liq_features = self._liquidity_proxy(now_utc, tick)

        structure_state = self.structure.analyze(
            self.bars.closed_bars(self.trend_tf, as_of=now_utc), self.trend_tf
        )

        session_info = self.sessions.evaluate(now_utc)

        # reference levels (previous day / session) from closed bars only
        base_bars = self.bars.closed_bars(self.base_tf, as_of=now_utc)
        day_bars = [b for b in base_bars if b.open_time_utc <= now_utc]
        features: dict[str, Any] = {**trend_features, **vol_features, **act_features, **liq_features}
        if day_bars:
            today = now_utc.date()
            yesterday = [b for b in day_bars if b.open_time_utc.date() < today]
            if yesterday:
                features["prev_day_high"] = max(b.high for b in yesterday[-96:])
                features["prev_day_low"] = min(b.low for b in yesterday[-96:])
            session_bars = self.sessions.session_bars(day_bars, session_info)
            if session_bars:
                features["session_high"] = max(b.high for b in session_bars)
                features["session_low"] = min(b.low for b in session_bars)

        atr_values = {}
        for timeframe in (self.base_tf, self.trend_tf, self.high_tf):
            bars = self.bars.closed_bars(timeframe, as_of=now_utc)
            value = atr(bars, 14)
            if value is not None:
                atr_values[timeframe] = round(value, 4)

        return MarketSnapshot(
            snapshot_id=snapshot_id,
            timestamp_utc=now_utc,
            symbol=self.symbol,
            source_symbol=self.source_symbol,
            data_quality=data_quality,
            trend_state=trend_state,
            volatility_state=volatility_state,
            activity_state=activity_state,
            liquidity_proxy_state=liquidity_state,
            market_structure=structure_state.labels_for_snapshot() or [MarketStructureEvent.UNKNOWN.value],
            session=session_info,
            spread_points=round(tick.spread_points, 2) if tick else None,
            bid=tick.bid if tick else None,
            ask=tick.ask if tick else None,
            atr_values=atr_values,
            feature_values=features,
            news_context=NewsContextState.UNAVAILABLE,  # no calendar provider configured
            timeframe_context=[self.base_tf, self.trend_tf, self.high_tf],
        )
