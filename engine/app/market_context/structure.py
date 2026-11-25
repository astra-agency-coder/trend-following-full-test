"""Market structure analysis: swings, BOS/CHoCH, compression/expansion (spec 34.7 / 35.1).

All detections are confirmation-based: nothing is reported before the bar
that makes it knowable has closed. Structure results feed both the Market
Context Engine and the SMC/ICT strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.data.contracts import MarketBar
from app.market_context.indicators import atr, swings


@dataclass(frozen=True)
class StructureEvent:
    kind: str          # BOS_CONFIRMED | CHOCH_CONFIRMED | COMPRESSION | EXPANSION | ...
    level: float
    timeframe: str
    condition_formed_at: datetime
    confirmed_at: datetime
    invalidation_level: float
    direction: str = "NONE"  # BULLISH | BEARISH | NONE


@dataclass
class StructureState:
    events: list[StructureEvent] = field(default_factory=list)
    swing_highs: list[tuple[datetime, float]] = field(default_factory=list)
    swing_lows: list[tuple[datetime, float]] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def labels_for_snapshot(self) -> list[str]:
        return list(dict.fromkeys(self.labels))


class StructureEngine:
    """Detects market structure per timeframe (deterministic, versioned)."""

    def __init__(self, *, pivot_left: int = 2, pivot_right: int = 2, compression_atr_mult: float = 0.6):
        self.pivot_left = pivot_left
        self.pivot_right = pivot_right
        self.compression_atr_mult = compression_atr_mult

    def analyze(self, bars: list[MarketBar], timeframe: str) -> StructureState:
        state = StructureState()
        if len(bars) < self.pivot_left + self.pivot_right + 5:
            state.labels = ["UNKNOWN"]
            return state

        pivots = swings(bars, left=self.pivot_left, right=self.pivot_right)
        for idx, kind, price, pivot_bar, confirm_bar in pivots:
            entry = (confirm_bar.close_time_utc, price)
            if kind == "HIGH":
                state.swing_highs.append(entry)
            else:
                state.swing_lows.append(entry)

        self._detect_bos_choch(bars, pivots, timeframe, state)
        self._detect_compression_expansion(bars, timeframe, state)
        self._detect_sequence(pivots, state)
        return state

    def _detect_bos_choch(
        self,
        bars: list[MarketBar],
        pivots: list[tuple[int, str, float, MarketBar, MarketBar]],
        timeframe: str,
        state: StructureState,
    ) -> None:
        """BOS = close beyond last same-side swing with the trend; CHoCH = against it."""
        last_high: tuple[datetime, float] | None = None
        last_low: tuple[datetime, float] | None = None
        last_break: str = "NONE"  # last structure break direction
        pivot_iter = iter(pivots)
        pivot = next(pivot_iter, None)

        for i, bar in enumerate(bars):
            # release pivots confirmed by this bar
            while pivot is not None and pivot[4].open_time_utc <= bar.open_time_utc:
                _, kind, price, _pb, confirm = pivot
                if kind == "HIGH":
                    last_high = (confirm.close_time_utc, price)
                else:
                    last_low = (confirm.close_time_utc, price)
                pivot = next(pivot_iter, None)

            if last_high is not None and bar.close > last_high[1]:
                kind = "BOS_CONFIRMED" if last_break != "BEARISH" else "CHOCH_CONFIRMED"
                state.events.append(
                    StructureEvent(
                        kind=kind,
                        level=last_high[1],
                        timeframe=timeframe,
                        condition_formed_at=last_high[0],
                        confirmed_at=bar.close_time_utc,
                        invalidation_level=bar.low,
                        direction="BULLISH",
                    )
                )
                state.labels.append(kind)
                last_break = "BULLISH"
                last_high = None  # level consumed
            elif last_low is not None and bar.close < last_low[1]:
                kind = "BOS_CONFIRMED" if last_break != "BULLISH" else "CHOCH_CONFIRMED"
                state.events.append(
                    StructureEvent(
                        kind=kind,
                        level=last_low[1],
                        timeframe=timeframe,
                        condition_formed_at=last_low[0],
                        confirmed_at=bar.close_time_utc,
                        invalidation_level=bar.high,
                        direction="BEARISH",
                    )
                )
                state.labels.append(kind)
                last_break = "BEARISH"
                last_low = None

    def _detect_compression_expansion(self, bars: list[MarketBar], timeframe: str, state: StructureState) -> None:
        atr_value = atr(bars, 14)
        if atr_value is None or len(bars) < 20:
            return
        recent = bars[-10:]
        avg_range = sum(b.high - b.low for b in recent) / len(recent)
        if avg_range < atr_value * self.compression_atr_mult:
            state.events.append(
                StructureEvent(
                    kind="COMPRESSION",
                    level=recent[-1].close,
                    timeframe=timeframe,
                    condition_formed_at=recent[0].open_time_utc,
                    confirmed_at=recent[-1].close_time_utc,
                    invalidation_level=max(b.high for b in recent),
                    direction="NONE",
                )
            )
            state.labels.append("COMPRESSION")
        elif avg_range > atr_value * 1.6:
            state.events.append(
                StructureEvent(
                    kind="EXPANSION",
                    level=recent[-1].close,
                    timeframe=timeframe,
                    condition_formed_at=recent[0].open_time_utc,
                    confirmed_at=recent[-1].close_time_utc,
                    invalidation_level=min(b.low for b in recent),
                    direction="BULLISH" if recent[-1].close > recent[0].open else "BEARISH",
                )
            )
            state.labels.append("EXPANSION")

    def _detect_sequence(
        self,
        pivots: list[tuple[int, str, float, MarketBar, MarketBar]],
        state: StructureState,
    ) -> None:
        """HH/HL vs LH/LL sequence from the last 4 confirmed pivots."""
        tail = pivots[-4:]
        highs = [p for p in tail if p[1] == "HIGH"]
        lows = [p for p in tail if p[1] == "LOW"]
        if len(highs) >= 2 and len(lows) >= 2:
            hh = highs[-1][2] > highs[-2][2]
            hl = lows[-1][2] > lows[-2][2]
            if hh and hl:
                state.labels.append("HH_HL_SEQUENCE")
            elif (not hh) and (not hl):
                state.labels.append("LH_LL_SEQUENCE")
            else:
                state.labels.append("RANGE_STRUCTURE")
