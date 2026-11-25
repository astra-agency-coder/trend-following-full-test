"""Performance analytics (spec 15 / 45).

All metric equations follow spec 45 exactly (win rate, profit factor,
expectancy, R multiple, drawdown on closed-trade equity...). Segments with
too-small samples are flagged ``LOW_SAMPLE`` / ``NO_DATA`` instead of being
interpreted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.execution.paper_engine import PaperTrade
from app.hybrid.router import RouterDecision
from app.shadow.tracker import ShadowTracker


@dataclass
class Metrics:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float | None = None
    loss_rate: float | None = None
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    profit_factor: float | None = None
    expectancy: float | None = None
    expectancy_r: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    median_win: float | None = None
    median_loss: float | None = None
    max_drawdown: float = 0.0
    average_duration_minutes: float | None = None
    total_costs: float = 0.0
    long_trades: int = 0
    short_trades: int = 0
    sample_status: str = "NO_DATA"

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "win_rate": self.win_rate,
            "loss_rate": self.loss_rate,
            "gross_profit": self.gross_profit,
            "gross_loss": self.gross_loss,
            "net_profit": self.net_profit,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "expectancy_r": self.expectancy_r,
            "average_win": self.average_win,
            "average_loss": self.average_loss,
            "median_win": self.median_win,
            "median_loss": self.median_loss,
            "max_drawdown": self.max_drawdown,
            "average_duration_minutes": self.average_duration_minutes,
            "total_costs": self.total_costs,
            "long_trades": self.long_trades,
            "short_trades": self.short_trades,
            "sample_status": self.sample_status,
        }


MIN_SAMPLE = 20


def compute_metrics(trades: list[PaperTrade], *, min_sample: int = MIN_SAMPLE) -> Metrics:
    closed = [t for t in trades if t.net_pnl is not None and t.closed_at_utc is not None]
    m = Metrics()
    m.total_trades = len(closed)
    if not closed:
        m.sample_status = "NO_DATA"
        return m

    pnls = [t.net_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]

    m.wins, m.losses, m.breakeven = len(wins), len(losses), len(breakeven)
    m.win_rate = round(len(wins) / len(closed) * 100, 2)
    m.loss_rate = round(len(losses) / len(closed) * 100, 2)
    m.gross_profit = round(sum(wins), 2)
    m.gross_loss = round(abs(sum(losses)), 2)
    m.net_profit = round(sum(pnls), 2)
    m.profit_factor = (
        "N/A_INFINITE_NO_LOSSES" if m.gross_loss == 0 and m.wins > 0 else
        (round(m.gross_profit / m.gross_loss, 3) if m.gross_loss > 0 else None)
    )
    m.expectancy = round(sum(pnls) / len(pnls), 2)
    rs = [t.r_multiple for t in closed if t.r_multiple is not None]
    m.expectancy_r = round(sum(rs) / len(rs), 3) if rs else None
    m.average_win = round(sum(wins) / len(wins), 2) if wins else None
    m.average_loss = round(sum(losses) / len(losses), 2) if losses else None
    m.median_win = round(sorted(wins)[len(wins) // 2], 2) if wins else None
    m.median_loss = round(sorted(losses)[len(losses) // 2], 2) if losses else None

    # closed-trade equity drawdown (spec 45.4, closed basis)
    equity_curve = []
    running = 0.0
    for p in pnls:
        running += p
        equity_curve.append(running)
    peak = float("-inf")
    for value in equity_curve:
        peak = max(peak, value)
        m.max_drawdown = max(m.max_drawdown, peak - value)
    m.max_drawdown = round(m.max_drawdown, 2)

    durations = [
        (t.closed_at_utc - t.opened_at_utc).total_seconds() / 60.0
        for t in closed
        if t.closed_at_utc and t.opened_at_utc
    ]
    m.average_duration_minutes = round(sum(durations) / len(durations), 1) if durations else None
    m.total_costs = round(sum((t.commission or 0) + (t.swap or 0) for t in closed), 2)
    m.long_trades = sum(1 for t in closed if t.side.value == "BUY")
    m.short_trades = sum(1 for t in closed if t.side.value == "SELL")
    m.sample_status = "SUFFICIENT" if len(closed) >= min_sample else "LOW_SAMPLE"
    return m


def segment(trades: list[PaperTrade], key_fn, *, min_sample: int = MIN_SAMPLE) -> dict[str, dict]:
    """Group closed trades by a key (session, strategy, weekday...)."""
    buckets: dict[str, list[PaperTrade]] = {}
    for trade in trades:
        if trade.net_pnl is None or trade.closed_at_utc is None:
            continue
        key = key_fn(trade)
        if key is None:
            continue
        buckets.setdefault(str(key), []).append(trade)
    out: dict[str, dict] = {}
    for key, group in buckets.items():
        metrics = compute_metrics(group, min_sample=min_sample)
        out[key] = metrics.to_dict()
    return out


def performance_by_strategy(trades: list[PaperTrade]) -> dict[str, dict]:
    return segment(trades, lambda t: t.origin_strategy_id)


def performance_by_session(trades: list[PaperTrade]) -> dict[str, dict]:
    return segment(trades, lambda t: t.entry_session_name)


def performance_by_hour(trades: list[PaperTrade]) -> dict[str, dict]:
    return segment(trades, lambda t: t.opened_at_utc.strftime("%H:00") if t.opened_at_utc else None)


def performance_by_weekday(trades: list[PaperTrade]) -> dict[str, dict]:
    names = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    return segment(trades, lambda t: names[t.opened_at_utc.weekday()] if t.opened_at_utc else None)


def performance_by_direction(trades: list[PaperTrade]) -> dict[str, dict]:
    return segment(trades, lambda t: t.side.value)


# ------------------------------------------------------------------ router quality
def router_quality(decisions: list[RouterDecision], shadow: ShadowTracker) -> dict:
    total = len(decisions)
    if total == 0:
        return {"decisions": 0, "sample_status": "NO_DATA"}
    by_status: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    for decision in decisions:
        by_status[decision.decision_status.value] = by_status.get(decision.decision_status.value, 0) + 1
        if decision.selected:
            by_strategy[decision.selected.strategy_id] = by_strategy.get(decision.selected.strategy_id, 0) + 1
    no_trade_ratio = round(
        (by_status.get("NO_CANDIDATE", 0) + by_status.get("CANDIDATE_REJECTED_BY_ROUTER", 0)) / total, 3
    )
    return {
        "decisions": total,
        "by_status": by_status,
        "selections_by_strategy": by_strategy,
        "no_trade_ratio": no_trade_ratio,
        "router_switches": sum(
            1
            for prev, cur in zip(decisions, decisions[1:])
            if prev.new_preferred and cur.new_preferred and prev.new_preferred != cur.new_preferred
        ),
        "shadow": shadow.summary(),
        "shadow_by_strategy": shadow.by_strategy(),
        "sample_status": "SUFFICIENT" if total >= 50 else "LOW_SAMPLE",
    }


# ------------------------------------------------------------------ reports
def daily_report(trades: list[PaperTrade], decisions: list[RouterDecision], *, day_start_utc: datetime, day_end_utc: datetime) -> dict:
    day_trades = [t for t in trades if t.closed_at_utc and day_start_utc <= t.closed_at_utc < day_end_utc]
    opened = [t for t in trades if t.opened_at_utc and day_start_utc <= t.opened_at_utc < day_end_utc]
    day_decisions = [d for d in decisions if day_start_utc <= d.timestamp_utc < day_end_utc]
    return {
        "period": {"start": day_start_utc.isoformat(), "end": day_end_utc.isoformat()},
        "analysis_cycles": len(day_decisions),
        "trades_opened": len(opened),
        "trades_closed": len(day_trades),
        "metrics": compute_metrics(day_trades).to_dict(),
        "no_trade_reasons_top": _top_no_trade_reasons(day_decisions),
    }


def weekly_report(trades: list[PaperTrade], decisions: list[RouterDecision], shadow: ShadowTracker, *, start_utc: datetime, end_utc: datetime) -> dict:
    week_trades = [t for t in trades if t.closed_at_utc and start_utc <= t.closed_at_utc < end_utc]
    week_decisions = [d for d in decisions if start_utc <= d.timestamp_utc < end_utc]
    return {
        "period": {"start": start_utc.isoformat(), "end": end_utc.isoformat()},
        "hybrid_portfolio": compute_metrics(week_trades).to_dict(),
        "by_strategy": performance_by_strategy(week_trades),
        "by_session": performance_by_session(week_trades),
        "by_hour": performance_by_hour(week_trades),
        "by_weekday": performance_by_weekday(week_trades),
        "by_direction": performance_by_direction(week_trades),
        "router_quality": router_quality(week_decisions, shadow),
        "rejected_opportunity_performance": shadow.summary(),
    }


def _top_no_trade_reasons(decisions: list[RouterDecision], limit: int = 5) -> list[dict]:
    counter: dict[str, int] = {}
    for decision in decisions:
        for reasons in decision.no_trade_reasons.values():
            for reason in reasons:
                counter[reason] = counter.get(reason, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"reason": r, "count": c} for r, c in ranked]
