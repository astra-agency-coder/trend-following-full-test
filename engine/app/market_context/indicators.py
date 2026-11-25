"""Pure technical indicator functions (spec 34 / 51.4).

Every function is side-effect free and computed from *closed* bars only;
the caller is responsible for slicing the bar list at the decision time
(see ``available_at`` slicing in the market context engine).
"""
from __future__ import annotations

import math
from typing import Sequence

from app.data.contracts import MarketBar


def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> list[float] | None:
    if period <= 0 or len(values) < period:
        return None
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for value in values[period:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def true_range(prev_close: float, bar: MarketBar) -> float:
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr(bars: Sequence[MarketBar], period: int = 14) -> float | None:
    """Wilder's ATR over closed bars."""
    if len(bars) < period + 1:
        return None
    trs = [true_range(bars[i - 1].close, bars[i]) for i in range(1, len(bars))]
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def realized_volatility(bars: Sequence[MarketBar], period: int = 20) -> float | None:
    """Std-dev of close-to-close returns over the last ``period`` bars."""
    if len(bars) < period + 1:
        return None
    returns = [
        (bars[i].close - bars[i - 1].close) / bars[i - 1].close
        for i in range(len(bars) - period, len(bars))
    ]
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var)


def rsi(bars: Sequence[MarketBar], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        change = bars[i].close - bars[i - 1].close
        gains += max(change, 0)
        losses += max(-change, 0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(bars)):
        change = bars[i].close - bars[i - 1].close
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def adx(bars: Sequence[MarketBar], period: int = 14) -> float | None:
    """Wilder's ADX — measures trend strength regardless of direction."""
    if len(bars) < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(true_range(bars[i - 1].close, bars[i]))
    atr_ = sum(trs[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])
    dxs: list[float] = []
    for i in range(period, len(trs)):
        atr_ = atr_ - atr_ / period + trs[i]
        plus = plus - plus / period + plus_dm[i]
        minus = minus - minus / period + minus_dm[i]
        if atr_ <= 0:
            continue
        pdi, mdi = 100 * plus / atr_, 100 * minus / atr_
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    if len(dxs) < period:
        return None
    return sum(dxs[-period:]) / period


def bollinger_bands(bars: Sequence[MarketBar], period: int = 20, mult: float = 2.0) -> tuple[float, float, float] | None:
    """(middle, upper, lower) using population std-dev."""
    if len(bars) < period:
        return None
    closes = [b.close for b in bars[-period:]]
    middle = sum(closes) / period
    var = sum((c - middle) ** 2 for c in closes) / period
    sd = math.sqrt(var)
    return middle, middle + mult * sd, middle - mult * sd


def donchian_channel(bars: Sequence[MarketBar], period: int = 20) -> tuple[float, float, float] | None:
    """(upper, lower, mid) of the last ``period`` closed bars (excl. current)."""
    if len(bars) < period:
        return None
    window = bars[-period:]
    upper = max(b.high for b in window)
    lower = min(b.low for b in window)
    return upper, lower, (upper + lower) / 2


def z_score(bars: Sequence[MarketBar], period: int = 20) -> float | None:
    if len(bars) < period:
        return None
    closes = [b.close for b in bars[-period:]]
    mean = sum(closes) / period
    sd = math.sqrt(sum((c - mean) ** 2 for c in closes) / period)
    if sd == 0:
        return 0.0
    return (closes[-1] - mean) / sd


def rolling_mean(values: Sequence[float], period: int) -> float | None:
    return sma(values, period)


def vwap_session(bars: Sequence[MarketBar]) -> float | None:
    """Session VWAP proxy using tick_volume as weight (Activity Proxy — spec 5.5).

    ``bars`` must be the current session's closed bars only.
    """
    num, den = 0.0, 0.0
    for bar in bars:
        typical = (bar.high + bar.low + bar.close) / 3
        weight = bar.tick_volume if bar.tick_volume and bar.tick_volume > 0 else 1.0
        num += typical * weight
        den += weight
    return num / den if den > 0 else None


def efficiency_ratio(bars: Sequence[MarketBar], period: int = 20) -> float | None:
    """Kaufman efficiency: net move / summed path (0 = chop, 1 = clean trend)."""
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1) :]
    net = abs(window[-1].close - window[0].close)
    path = sum(abs(window[i].close - window[i - 1].close) for i in range(1, len(window)))
    return net / path if path > 0 else 0.0


def swings(
    bars: Sequence[MarketBar],
    *,
    left: int = 2,
    right: int = 2,
) -> list[tuple[int, str, float, "MarketBar", "MarketBar"]]:
    """Confirmed swing highs/lows (pivot definition, spec 35.1).

    A pivot needs ``left`` bars before and ``right`` bars after it — it only
    becomes *known* after the ``right``-th following bar closes, so the
    returned confirmation bar enforces no look-ahead.

    Returns tuples of (index, kind, price, pivot_bar, confirmation_bar).
    """
    out: list[tuple[int, str, float, MarketBar, MarketBar]] = []
    n = len(bars)
    for i in range(left, n - right):
        window = bars[i - left : i + right + 1]
        pivot = bars[i]
        if pivot.high == max(b.high for b in window) and sum(1 for b in window if b.high == pivot.high) == 1:
            out.append((i, "HIGH", pivot.high, pivot, bars[i + right]))
        elif pivot.low == min(b.low for b in window) and sum(1 for b in window if b.low == pivot.low) == 1:
            out.append((i, "LOW", pivot.low, pivot, bars[i + right]))
    return out
