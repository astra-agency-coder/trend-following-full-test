"""Read-model queries for the Web API.

Everything the dashboard displays is read here from the database (source
of truth) or from the embedded runtime. Financial metrics reuse
``app.reporting.metrics`` so there is exactly one calculator in the system
(frontend spec §2.2 — the client never recomputes P/L or Profit Factor).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import APP_VERSION, CANONICAL_SYMBOL
from app.core.enums import DataQuality, Direction, TradeStatus
from app.persistence.database import Database, dumps, loads
from app.persistence.models import (
    AuditEventRow,
    MarketBarRow,
    MarketSnapshotRow,
    PaperPortfolioRow,
    PaperTradeRow,
    RejectedOpportunityRow,
    RouterCandidateRankingRow,
    RouterDecisionRow,
    ServiceComponentRow,
    ShadowOutcomeRow,
    StrategyEvaluationRow,
    SymbolSpec,
    SystemHealthEventRow,
    TradeEventRow,
)
from app.persistence.repositories.journal import JournalRepository
from app.reporting import metrics as perf

SCHEMA_VERSION = "1.0"


# ------------------------------------------------------------------ helpers
def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def fl(value) -> float | None:
    return None if value is None else float(value)


def jload(raw: str | None, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def get_portfolio(session: Session) -> PaperPortfolioRow | None:
    return session.execute(
        select(PaperPortfolioRow).where(PaperPortfolioRow.is_active == "1").order_by(PaperPortfolioRow.created_at_utc)
    ).scalars().first()


def latest_symbol_spec(session: Session, symbol: str = CANONICAL_SYMBOL) -> SymbolSpec | None:
    return session.execute(
        select(SymbolSpec).where(SymbolSpec.canonical_symbol == symbol).order_by(SymbolSpec.captured_at_utc.desc())
    ).scalars().first()


def value_per_unit(session: Session) -> float:
    spec = latest_symbol_spec(session)
    if spec is None:
        return 100.0
    tick_size = spec.tick_size or spec.point or 0.01
    if spec.tick_value and tick_size:
        return float(spec.tick_value) / float(tick_size)
    return float(spec.contract_size or 100.0)


def floating_pnl(row: PaperTradeRow, bid: float | None, ask: float | None, vpu: float) -> float | None:
    """Same formula as PaperTrade.unrealized_pnl (single source of truth)."""
    if bid is None or ask is None or row.entry_price is None or row.closed_at_utc is not None:
        return None
    meta = jload(row.metadata_json, {}) or {}
    remaining = float(meta.get("remaining_volume_lots", row.volume_lots) or row.volume_lots)
    close_side_price = bid if row.side == "BUY" else ask
    diff = (close_side_price - float(row.entry_price)) if row.side == "BUY" else (float(row.entry_price) - close_side_price)
    return round(remaining * diff * vpu, 2)


def strategy_names(config: dict) -> dict[str, str]:
    names = {}
    for sid, cfg in (config.get("strategies", {}) or {}).items():
        names[sid] = cfg.get("display_name") or sid.replace("_", " ").title()
    return names or {
        "smc_ict": "SMC / ICT",
        "wyckoff": "Wyckoff",
        "trend_following": "Trend Following",
        "mean_reversion": "Mean Reversion",
        "breakout": "Breakout",
        "order_flow": "Order Flow",
    }


def enabled_strategies(config: dict) -> list[str]:
    return [sid for sid, cfg in (config.get("strategies", {}) or {}).items() if cfg.get("enabled", True)]


# ------------------------------------------------------------------ snapshot
def snapshot_from_row(row: MarketSnapshotRow | None) -> dict | None:
    if row is None:
        return None
    features = jload(row.feature_values, {}) or {}
    atr = features.get("atr_m15") or features.get("atr") or features.get("atr_m5")
    # The engine computes regime through its trend descriptor (documented
    # derivation — frontend shows the source in the tooltip).
    regime = row.trend_state if row.trend_state in ("BULLISH", "BEARISH", "RANGING") else None
    return {
        "symbol": row.symbol,
        "broker_symbol": row.source_symbol or row.symbol,
        "bid": fl(row.bid),
        "ask": fl(row.ask),
        "mid": round((float(row.bid) + float(row.ask)) / 2, 5) if row.bid and row.ask else None,
        "spread_points": fl(row.spread_points),
        "quote_time": to_iso(row.timestamp_utc),
        "received_at": to_iso(row.timestamp_utc),
        "is_stale": row.data_quality == "STALE",
        "data_quality": row.data_quality.lower() if row.data_quality else "unavailable",
        "regime": regime,
        "regime_source": "trend_state" if regime else None,
        "trend_state": row.trend_state,
        "volatility_state": row.volatility_state,
        "atr": fl(atr),
        "activity_proxy": row.activity_state,
        "liquidity_proxy": row.liquidity_proxy_state,
        "session": row.session_name,
        "session_phase": row.session_phase,
        "active_sessions": [],
        "market_structure": jload(row.market_structure, []) or [],
        "context_version": row.context_version,
    }


def latest_snapshot(session: Session, symbol: str = CANONICAL_SYMBOL) -> dict | None:
    row = session.execute(
        select(MarketSnapshotRow).where(MarketSnapshotRow.symbol == symbol).order_by(MarketSnapshotRow.timestamp_utc.desc())
    ).scalars().first()
    return snapshot_from_row(row)


# ------------------------------------------------------------------ dashboard
def dashboard_summary(session: Session, state, period_from: datetime, period_to: datetime) -> dict:
    portfolio = get_portfolio(session)
    if portfolio is None:
        return {"portfolio": None, "kpis": None, "insufficient_data": True}

    repo = JournalRepository(state.database)
    all_closed = repo.load_closed_trades(portfolio.id, limit=10000)
    closed_in_period = [
        t for t in all_closed
        if t.closed_at_utc and period_from <= _aware(t.closed_at_utc) < period_to
    ]
    open_rows = session.execute(
        select(PaperTradeRow).where(
            PaperTradeRow.portfolio_id == portfolio.id,
            PaperTradeRow.status.in_(["OPEN", "PENDING", "PARTIALLY_CLOSED"]),
        )
    ).scalars().all()

    snap = latest_snapshot(session)
    vpu = value_per_unit(session)
    bid = snap.get("bid") if snap else None
    ask = snap.get("ask") if snap else None

    metrics = perf.compute_metrics(closed_in_period)
    open_unrealized = round(sum((floating_pnl(r, bid, ask, vpu) or 0.0) for r in open_rows), 2)
    open_risk = round(sum((fl(r.planned_risk_amount) or 0.0) for r in open_rows), 2)

    last_decision = session.execute(
        select(RouterDecisionRow).order_by(RouterDecisionRow.timestamp_utc.desc())
    ).scalars().first()

    kpis = {
        "initial_balance": fl(portfolio.initial_balance),
        "balance": fl(portfolio.current_balance),
        "equity": round(fl(portfolio.current_balance) + open_unrealized, 2),
        "unrealized_pnl": open_unrealized if open_rows else None,
        "net_realized_pnl": metrics.net_profit if metrics.total_trades else None,
        "win_rate": metrics.win_rate,
        "profit_factor": metrics.profit_factor if not isinstance(metrics.profit_factor, str) else None,
        "profit_factor_label": metrics.profit_factor if isinstance(metrics.profit_factor, str) else None,
        "expectancy": metrics.expectancy,
        "max_drawdown": metrics.max_drawdown if metrics.total_trades else None,
        "closed_trades": metrics.total_trades,
        "open_trades": len(open_rows),
        "wins": metrics.wins,
        "losses": metrics.losses,
        "breakeven": metrics.breakeven,
        "sample_status": metrics.sample_status,
        "currency": portfolio.base_currency,
    }
    control = control_state(state, session)
    return {
        "portfolio": {
            "id": portfolio.id,
            "name": portfolio.name,
            "currency": portfolio.base_currency,
            "initial_balance": fl(portfolio.initial_balance),
        },
        "kpis": kpis,
        "current_decision": {
            "decision_id": last_decision.id,
            "status": last_decision.decision_status,
            "selected_strategy_id": last_decision.selected_strategy_id,
            "timestamp": to_iso(last_decision.timestamp_utc),
            "explanation": last_decision.explanation,
            "explanation_ar": last_decision.explanation_ar,
        } if last_decision else None,
        "control": control,
        "period": {"from": to_iso(period_from), "to": to_iso(period_to)},
        "insufficient_data": metrics.total_trades == 0,
    }


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def equity_curve(session: Session, state, period_from: datetime, period_to: datetime) -> dict:
    """Closed-basis equity/balance curve — computed here (backend), never client-side."""
    portfolio = get_portfolio(session)
    if portfolio is None:
        return {"points": [], "basis": "closed_trades", "insufficient_data": True}
    repo = JournalRepository(state.database)
    all_closed = repo.load_closed_trades(portfolio.id, limit=10000)
    closed = sorted(
        (t for t in all_closed if t.closed_at_utc and t.net_pnl is not None),
        key=lambda t: _aware(t.closed_at_utc),
    )
    initial = fl(portfolio.initial_balance)
    running = initial
    points = []
    before = [t for t in closed if _aware(t.closed_at_utc) < period_from]
    running += sum(t.net_pnl or 0.0 for t in before)
    points.append({"ts": to_iso(period_from), "balance": round(running, 2), "equity": round(running, 2), "trade_id": None})
    for t in closed:
        closed_at = _aware(t.closed_at_utc)
        if period_from <= closed_at <= period_to:
            running += t.net_pnl or 0.0
            points.append(
                {
                    "ts": to_iso(closed_at),
                    "balance": round(running, 2),
                    "equity": round(running, 2),
                    "trade_id": t.id,
                    "net_pnl": t.net_pnl,
                }
            )
    if not any(p["trade_id"] for p in points):
        return {"points": points, "basis": "closed_trades", "insufficient_data": True}
    return {"points": points, "basis": "closed_trades", "insufficient_data": False}


def recent_activity(session: Session, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(AuditEventRow).order_by(AuditEventRow.timestamp_utc.desc(), AuditEventRow.id.desc()).limit(limit)
    ).scalars().all()
    return [audit_event_dto(r) for r in rows]


def trade_list_dto(row: PaperTradeRow, session: Session, current: dict | None) -> dict:
    snap = current or {}
    vpu = value_per_unit(session)
    floating = floating_pnl(row, snap.get("bid"), snap.get("ask"), vpu)
    meta = jload(row.metadata_json, {}) or {}
    opened = to_iso(row.opened_at_utc)
    closed = to_iso(row.closed_at_utc)
    duration_minutes = None
    if row.opened_at_utc and row.closed_at_utc:
        duration_minutes = round((row.closed_at_utc - row.opened_at_utc).total_seconds() / 60.0, 1)
    return {
        "trade_id": row.id,
        "symbol": row.symbol,
        "status": row.status,
        "side": row.side,
        "strategy_id": row.origin_strategy_id,
        "strategy_version": row.strategy_version,
        "timeframe": meta.get("timeframe"),
        "opened_at": opened,
        "closed_at": closed,
        "entry_price": fl(row.entry_price),
        "current_price": snap.get("bid") if row.closed_at_utc is None else fl(row.exit_price),
        "exit_price": fl(row.exit_price),
        "stop_loss": fl(row.stop_loss),
        "initial_stop_loss": meta.get("initial_stop_loss"),
        "take_profit": fl(row.take_profit),
        "volume": fl(row.volume_lots),
        "risk_amount": fl(row.planned_risk_amount),
        "realized_pnl": fl(row.net_pnl) if row.closed_at_utc else None,
        "unrealized_pnl": floating,
        "r_multiple": fl(row.r_multiple),
        "total_costs": round((fl(row.commission) or 0) + (fl(row.swap) or 0) + (fl(row.slippage_cost) or 0), 2),
        "duration_minutes": duration_minutes,
        "session_at_entry": meta.get("entry_session_name") or row.entry_session_name,
        "exit_session": row.exit_session_name,
        "exit_reason": row.exit_reason,
        "decision_id": row.router_decision_id,
        "opportunity_key": row.opportunity_key,
    }


def open_trades(session: Session, portfolio_id: str | None = None) -> list[PaperTradeRow]:
    stmt = select(PaperTradeRow).where(PaperTradeRow.status.in_(["OPEN", "PENDING", "PARTIALLY_CLOSED"]))
    if portfolio_id:
        stmt = stmt.where(PaperTradeRow.portfolio_id == portfolio_id)
    return list(session.execute(stmt.order_by(PaperTradeRow.opened_at_utc.desc())).scalars().all())


# ------------------------------------------------------------------ market data
def available_timeframes(session: Session, symbol: str = CANONICAL_SYMBOL) -> list[str]:
    rows = session.execute(
        select(MarketBarRow.timeframe).where(MarketBarRow.canonical_symbol == symbol).distinct()
    ).scalars().all()
    order = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
    present = set(rows)
    return [tf for tf in order if tf in present] or sorted(present)


def bars(session: Session, symbol: str, timeframe: str, *, limit: int = 400, tf_from: datetime | None = None) -> list[dict]:
    stmt = (
        select(MarketBarRow)
        .where(MarketBarRow.canonical_symbol == symbol, MarketBarRow.timeframe == timeframe)
        .order_by(MarketBarRow.open_time_utc.desc())
        .limit(limit)
    )
    rows = list(session.execute(stmt).scalars().all())[::-1]
    return [
        {
            "time": to_iso(r.open_time_utc),
            "open": fl(r.open),
            "high": fl(r.high),
            "low": fl(r.low),
            "close": fl(r.close),
            "tick_volume": fl(r.tick_volume),
            "spread": fl(r.spread),
            "is_complete": r.is_complete in (True, "1"),
        }
        for r in rows
    ]


def market_annotations(session: Session, symbol: str, tf_from: datetime | None, tf_to: datetime | None) -> list[dict]:
    """Annotations computed by the backend only (structure events + trades)."""
    out: list[dict] = []
    stmt = select(MarketSnapshotRow).order_by(MarketSnapshotRow.timestamp_utc.desc()).limit(400)
    if tf_from:
        stmt = stmt.where(MarketSnapshotRow.timestamp_utc >= tf_from)
    if tf_to:
        stmt = stmt.where(MarketSnapshotRow.timestamp_utc <= tf_to)
    for row in session.execute(stmt).scalars():
        structure = jload(row.market_structure, []) or []
        if structure:
            out.append(
                {
                    "type": "market_structure",
                    "time": to_iso(row.timestamp_utc),
                    "events": structure,
                    "snapshot_id": row.id,
                }
            )
    trade_stmt = select(PaperTradeRow).where(
        PaperTradeRow.symbol == symbol,
        PaperTradeRow.opened_at_utc.is_not(None),
    ).order_by(PaperTradeRow.opened_at_utc.desc()).limit(100)
    if tf_from:
        trade_stmt = trade_stmt.where(PaperTradeRow.opened_at_utc >= tf_from)
    for row in session.execute(trade_stmt).scalars():
        out.append(
            {
                "type": "trade_entry",
                "time": to_iso(row.opened_at_utc),
                "side": row.side,
                "price": fl(row.entry_price),
                "trade_id": row.id,
                "strategy_id": row.origin_strategy_id,
            }
        )
        if row.closed_at_utc and row.exit_price is not None:
            out.append(
                {
                    "type": "trade_exit",
                    "time": to_iso(row.closed_at_utc),
                    "side": row.side,
                    "price": fl(row.exit_price),
                    "trade_id": row.id,
                    "exit_reason": row.exit_reason,
                }
            )
    return out


# ------------------------------------------------------------------ hybrid
def evaluation_dto(row: StrategyEvaluationRow, names: dict[str, str]) -> dict:
    passed = jload(row.conditions_passed, []) or []
    failed = jload(row.conditions_failed, []) or []
    rejections = jload(row.rejection_reasons, []) or []
    status_map = {
        "CANDIDATE": "candidate",
        "NO_SETUP": "not_selected",
        "ERROR": "data_unavailable",
        "INVALID_DATA": "data_unavailable",
    }
    setup_quality = fl(row.setup_quality_score)
    market_fit = fl(row.market_fit_score)
    fit = None
    if setup_quality is not None and market_fit is not None:
        fit = round(0.6 * setup_quality + 0.4 * market_fit, 2)
    return {
        "evaluation_id": row.id,
        "strategy_id": row.strategy_id,
        "strategy_name": names.get(row.strategy_id, row.strategy_id),
        "strategy_version": row.strategy_version,
        "evaluated_at": to_iso(row.timestamp_utc),
        "status": status_map.get(row.status, row.status.lower()),
        "raw_status": row.status,
        "setup_stage": row.setup_stage,
        "fit_score": fit,
        "fit_score_definition": "weighted combination of market fit (40%) and setup quality (60%) reported by the school engine; a Fit/Ranking score, not a win probability",
        "direction": None if row.direction == "NONE" else row.direction,
        "timeframe": None,
        "candidate_id": row.id if row.status == "CANDIDATE" else None,
        "opportunity_key": row.opportunity_key,
        "setup_type": row.setup_type,
        "proposed_entry": fl(row.proposed_entry),
        "proposed_sl": fl(row.proposed_sl),
        "proposed_tp": fl(row.proposed_tp),
        "risk_reward": fl(row.risk_reward),
        "evidence": [
            {"code": c.get("code", ""), "label": c.get("label", c.get("code", "")), "passed": bool(c.get("passed", True)), "details": c.get("details")}
            for c in (passed + failed) if isinstance(c, dict)
        ],
        "rejection_reasons": [
            {"code": r, "message": r.replace("_", " ").title()} if isinstance(r, str) else r for r in rejections
        ],
        "notes": row.notes,
    }


def decision_list(session: Session, names: dict[str, str], *, limit: int, cursor: str | None) -> dict:
    stmt = select(RouterDecisionRow).order_by(RouterDecisionRow.timestamp_utc.desc())
    if cursor:
        dt = parse_dt(cursor)
        if dt:
            stmt = stmt.where(RouterDecisionRow.timestamp_utc < dt)
    rows = session.execute(stmt.limit(limit + 1)).scalars().all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = to_iso(rows[-1].timestamp_utc)
    return {
        "items": [decision_dto(r, session, names, light=True) for r in rows],
        "next_cursor": next_cursor,
    }


def decision_dto(row: RouterDecisionRow, session: Session, names: dict[str, str], *, light: bool = False) -> dict:
    rankings = session.execute(
        select(RouterCandidateRankingRow)
        .where(RouterCandidateRankingRow.router_decision_id == row.id)
        .order_by(RouterCandidateRankingRow.rank_position)
    ).scalars().all()
    evaluations = []
    if not light:
        eval_rows = session.execute(
            select(StrategyEvaluationRow)
            .where(StrategyEvaluationRow.timestamp_utc >= row.timestamp_utc - timedelta(seconds=120))
            .where(StrategyEvaluationRow.timestamp_utc <= row.timestamp_utc + timedelta(seconds=120))
            .order_by(StrategyEvaluationRow.timestamp_utc.desc())
        ).scalars().all()
        seen: set[str] = set()
        for ev in eval_rows:
            if ev.strategy_id in seen:
                continue
            seen.add(ev.strategy_id)
            evaluations.append(evaluation_dto(ev, names))
    snapshot = None
    if not light and row.market_snapshot_id:
        snap_row = session.get(MarketSnapshotRow, row.market_snapshot_id)
        snapshot = snapshot_from_row(snap_row)
    return {
        "decision_id": row.id,
        "created_at": to_iso(row.timestamp_utc),
        "status": row.decision_status,
        "outcome": {
            "SELECTED": "selected",
            "TRADE_OPENED": "selected",
            "SELECTED_PENDING_ENTRY": "selected",
            "NO_CANDIDATE": "no_trade",
            "CANDIDATE_REJECTED_BY_ROUTER": "no_trade",
            "NO_TRADE": "no_trade",
            "BLOCKED_BY_RISK": "blocked_by_risk",
            "BLOCKED_BY_DATA": "blocked_by_data",
            "BLOCKED_BY_SYSTEM": "blocked_by_risk",
        }.get(row.decision_status, "no_trade"),
        "selected_strategy_id": row.selected_strategy_id,
        "selected_strategy_name": names.get(row.selected_strategy_id, row.selected_strategy_id) if row.selected_strategy_id else None,
        "selected_evaluation_id": row.selected_evaluation_id,
        "ranking_score": fl(rankings[0].score_total) if rankings else None,
        "score_definition": "Router ranking score (setup integrity 30%, market fit 25%, timing 15%, execution feasibility 15%, risk plan 15%). Fit/Ranking score — not a win probability.",
        "primary_reason": row.explanation or row.explanation_ar or row.decision_status,
        "primary_reason_ar": row.explanation_ar,
        "hard_blocks": jload(row.hard_blocks, []) or [],
        "no_trade_reasons": jload(row.rejection_details, {}) or {},
        "ranked_candidates": [
            {
                "strategy_id": rk.strategy_id,
                "strategy_name": names.get(rk.strategy_id, rk.strategy_id),
                "evaluation_id": rk.evaluation_id,
                "rank_position": rk.rank_position,
                "eligible": rk.eligible in (True, "1"),
                "score_total": fl(rk.score_total),
                "score_components": jload(rk.score_components, {}) or {},
                "rejection_codes": jload(rk.rejection_codes, []) or [],
            }
            for rk in rankings
        ],
        "market_snapshot": snapshot,
        "evaluations": evaluations,
        "router_version": row.router_version,
    }


def strategy_switches(session: Session, names: dict[str, str], *, limit: int = 50) -> list[dict]:
    rows = session.execute(
        select(RouterDecisionRow).order_by(RouterDecisionRow.timestamp_utc.asc()).limit(2000)
    ).scalars().all()
    switches = []
    previous = None
    for row in rows:
        current = row.selected_strategy_id
        if current and previous and current != previous:
            switches.append(
                {
                    "at": to_iso(row.timestamp_utc),
                    "decision_id": row.id,
                    "from_strategy_id": previous,
                    "from_strategy_name": names.get(previous, previous),
                    "to_strategy_id": current,
                    "to_strategy_name": names.get(current, current),
                    "reason": row.explanation or row.explanation_ar,
                    "reason_ar": row.explanation_ar,
                }
            )
        if current:
            previous = current
        if len(switches) >= limit:
            break
    return list(reversed(switches))


# ------------------------------------------------------------------ trades
def trade_detail(session: Session, trade_id: str, current: dict | None, names: dict[str, str]) -> dict | None:
    row = session.get(PaperTradeRow, trade_id)
    if row is None:
        return None
    dto = trade_list_dto(row, session, current)
    meta = jload(row.metadata_json, {}) or {}
    events = session.execute(
        select(TradeEventRow).where(TradeEventRow.trade_id == trade_id).order_by(TradeEventRow.timestamp_utc.asc())
    ).scalars().all()
    timeline = [
        {
            "event_type": ev.event_type,
            "time": to_iso(ev.timestamp_utc),
            "price": fl(ev.price),
            "old_values": jload(ev.old_values),
            "new_values": jload(ev.new_values),
            "reason": ev.reason,
            "actor": ev.actor,
        }
        for ev in events
    ]
    context = None
    entry_snapshot_id = meta.get("entry_snapshot_id") or (row.router_decision_id and None)
    if row.router_decision_id:
        decision = session.get(RouterDecisionRow, row.router_decision_id)
        if decision and decision.market_snapshot_id:
            snap_row = session.get(MarketSnapshotRow, decision.market_snapshot_id)
            context = snapshot_from_row(snap_row)
    evaluation = None
    if row.entry_evaluation_id:
        ev_row = session.get(StrategyEvaluationRow, row.entry_evaluation_id)
        if ev_row:
            evaluation = evaluation_dto(ev_row, names)
    return {
        **dto,
        "metadata": meta,
        "management_policy": jload(row.management_policy_json, {}) or {},
        "gross_pnl": fl(row.gross_pnl),
        "commission": fl(row.commission),
        "swap": fl(row.swap),
        "slippage_cost": fl(row.slippage_cost),
        "entry_spread_points": fl(row.entry_spread_points),
        "timeline": timeline,
        "market_context": context,
        "strategy_evidence": evaluation,
        "mfe": meta.get("highest_bid_since_entry"),
        "mae": meta.get("lowest_ask_since_entry"),
        "audit_correlation_id": row.id,
    }


def trade_chart(session: Session, trade_id: str, timeframe: str = "M15", pad_bars: int = 60) -> dict | None:
    row = session.get(PaperTradeRow, trade_id)
    if row is None or row.opened_at_utc is None:
        return None
    tf_map = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}
    seconds = tf_map.get(timeframe, 900)
    start = _aware(row.opened_at_utc) - timedelta(seconds=seconds * pad_bars)
    end = (_aware(row.closed_at_utc) or datetime.now(UTC)) + timedelta(seconds=seconds * 10)
    rows = session.execute(
        select(MarketBarRow)
        .where(
            MarketBarRow.canonical_symbol == row.symbol,
            MarketBarRow.timeframe == timeframe,
            MarketBarRow.open_time_utc >= start,
            MarketBarRow.open_time_utc <= end,
        )
        .order_by(MarketBarRow.open_time_utc.asc())
    ).scalars().all()
    return {
        "timeframe": timeframe,
        "bars": [
            {"time": to_iso(r.open_time_utc), "open": fl(r.open), "high": fl(r.high), "low": fl(r.low), "close": fl(r.close), "tick_volume": fl(r.tick_volume)}
            for r in rows
        ],
        "trade": {
            "trade_id": row.id,
            "side": row.side,
            "entry_price": fl(row.entry_price),
            "exit_price": fl(row.exit_price),
            "stop_loss": fl(row.stop_loss),
            "take_profit": fl(row.take_profit),
            "opened_at": to_iso(row.opened_at_utc),
            "closed_at": to_iso(row.closed_at_utc),
        },
    }


# ------------------------------------------------------------------ analytics
def _closed_trades_in_period(state, portfolio_id: str, period_from: datetime, period_to: datetime):
    repo = JournalRepository(state.database)
    all_closed = repo.load_closed_trades(portfolio_id, limit=10000)
    return [
        t for t in all_closed
        if t.closed_at_utc and period_from <= _aware(t.closed_at_utc) <= period_to
    ]


def _metrics_dict(m: perf.Metrics) -> dict:
    d = m.to_dict()
    d["profit_factor"] = None if isinstance(m.profit_factor, str) else m.profit_factor
    d["profit_factor_label"] = m.profit_factor if isinstance(m.profit_factor, str) else None
    return d


def strategy_analytics(session: Session, state, period_from: datetime, period_to: datetime) -> dict:
    portfolio = get_portfolio(session)
    if portfolio is None:
        return {"strategies": [], "insufficient_data": True}
    names = strategy_names(state.config)
    trades = _closed_trades_in_period(state, portfolio.id, period_from, period_to)
    by_strategy = perf.performance_by_strategy(trades)

    from_dt, to_dt = to_iso(period_from), to_iso(period_to)
    eval_counts = dict(session.execute(
        select(StrategyEvaluationRow.strategy_id, func.count())
        .where(StrategyEvaluationRow.timestamp_utc >= period_from, StrategyEvaluationRow.timestamp_utc <= period_to)
        .group_by(StrategyEvaluationRow.strategy_id)
    ).all())
    candidate_counts = dict(session.execute(
        select(StrategyEvaluationRow.strategy_id, func.count())
        .where(
            StrategyEvaluationRow.status == "CANDIDATE",
            StrategyEvaluationRow.timestamp_utc >= period_from,
            StrategyEvaluationRow.timestamp_utc <= period_to,
        )
        .group_by(StrategyEvaluationRow.strategy_id)
    ).all())
    selection_counts = dict(session.execute(
        select(RouterDecisionRow.selected_strategy_id, func.count())
        .where(
            RouterDecisionRow.selected_strategy_id.is_not(None),
            RouterDecisionRow.timestamp_utc >= period_from,
            RouterDecisionRow.timestamp_utc <= period_to,
        )
        .group_by(RouterDecisionRow.selected_strategy_id)
    ).all())
    rejection_counts = dict(session.execute(
        select(StrategyEvaluationRow.strategy_id, func.count())
        .join(RejectedOpportunityRow, RejectedOpportunityRow.strategy_evaluation_id == StrategyEvaluationRow.id)
        .where(RejectedOpportunityRow.rejected_at_utc >= period_from, RejectedOpportunityRow.rejected_at_utc <= period_to)
        .group_by(StrategyEvaluationRow.strategy_id)
    ).all())
    shadow_counts = dict(session.execute(
        select(StrategyEvaluationRow.strategy_id, func.count())
        .join(RejectedOpportunityRow, RejectedOpportunityRow.strategy_evaluation_id == StrategyEvaluationRow.id)
        .join(ShadowOutcomeRow, ShadowOutcomeRow.rejected_opportunity_id == RejectedOpportunityRow.id)
        .group_by(StrategyEvaluationRow.strategy_id)
    ).all())

    versions = dict(session.execute(
        select(StrategyEvaluationRow.strategy_id, StrategyEvaluationRow.strategy_version)
        .order_by(StrategyEvaluationRow.timestamp_utc.desc())
        .limit(200)
    ).all())

    strategies = []
    for sid in strategy_names(state.config):
        seg = by_strategy.get(sid) or perf.compute_metrics([]).to_dict()
        strategies.append(
            {
                "strategy_id": sid,
                "strategy_name": names.get(sid, sid),
                "strategy_version": versions.get(sid),
                "evaluations_count": int(eval_counts.get(sid, 0)),
                "candidates_count": int(candidate_counts.get(sid, 0)),
                "selected_count": int(selection_counts.get(sid, 0)),
                "executed_trades_count": seg.get("total_trades", 0),
                "win_rate": seg.get("win_rate"),
                "net_pnl": seg.get("net_profit") if seg.get("total_trades") else None,
                "profit_factor": seg.get("profit_factor"),
                "expectancy": seg.get("expectancy"),
                "average_r": seg.get("expectancy_r"),
                "average_duration_minutes": seg.get("average_duration_minutes"),
                "rejections_count": int(rejection_counts.get(sid, 0)),
                "shadow_sample_count": int(shadow_counts.get(sid, 0)),
                "sample_status": seg.get("sample_status", "NO_DATA"),
            }
        )
    return {
        "strategies": strategies,
        "period": {"from": from_dt, "to": to_dt},
        "attribution_note": "Executed attribution covers trades the Hybrid Router assigned to each school inside the single hybrid portfolio. Unselected-school results are Candidate Diagnostics / Shadow and are NOT part of portfolio P/L.",
        "insufficient_data": not any(s["executed_trades_count"] for s in strategies),
    }


def strategy_detail(session: Session, state, strategy_id: str, period_from: datetime, period_to: datetime) -> dict | None:
    if strategy_id not in strategy_names(state.config):
        return None
    analytics = strategy_analytics(session, state, period_from, period_to)
    entry = next((s for s in analytics["strategies"] if s["strategy_id"] == strategy_id), None)
    if entry is None:
        return None
    portfolio = get_portfolio(session)
    trades = _closed_trades_in_period(state, portfolio.id, period_from, period_to) if portfolio else []
    mine = [t for t in trades if t.origin_strategy_id == strategy_id]
    seg = perf.performance_by_strategy(mine).get(strategy_id) or perf.compute_metrics([]).to_dict()
    session_seg = perf.performance_by_session(mine)
    regime_seg = performance_by_regime(session, state, mine)
    rejection_codes: dict[str, int] = {}
    rows = session.execute(
        select(StrategyEvaluationRow)
        .where(
            StrategyEvaluationRow.strategy_id == strategy_id,
            StrategyEvaluationRow.timestamp_utc >= period_from,
            StrategyEvaluationRow.timestamp_utc <= period_to,
        )
        .order_by(StrategyEvaluationRow.timestamp_utc.desc())
        .limit(500)
    ).scalars().all()
    for row in rows:
        for code in jload(row.rejection_reasons, []) or []:
            if isinstance(code, str):
                rejection_codes[code] = rejection_codes.get(code, 0) + 1
    versions_seen: dict[str, str | None] = {}
    for row in rows:
        versions_seen.setdefault(row.strategy_version, to_iso(row.timestamp_utc))
    cfg = (state.config.get("strategies", {}) or {}).get(strategy_id, {})
    return {
        **entry,
        "role_description": cfg.get("description"),
        "recent_trades": [trade_list_dto(r, session, None) for r in _trade_rows_for(state, portfolio.id, strategy_id, 20)],
        "performance_summary": _metrics_dict(perf.compute_metrics(mine)),
        "by_session": session_seg,
        "by_regime": regime_seg,
        "common_rejections": sorted(rejection_codes.items(), key=lambda kv: -kv[1])[:8],
        "versions": [{"version": v, "first_seen": ts} for v, ts in versions_seen.items()],
    }


def _trade_rows_for(state, portfolio_id: str, strategy_id: str, limit: int) -> list[PaperTradeRow]:
    with state.database.session() as session:
        return list(session.execute(
            select(PaperTradeRow)
            .where(PaperTradeRow.portfolio_id == portfolio_id, PaperTradeRow.origin_strategy_id == strategy_id)
            .order_by(PaperTradeRow.closed_at_utc.desc().nullslast())
            .limit(limit)
        ).scalars().all())


def performance_by_regime(session: Session, state, trades) -> dict:
    """Group closed trades by trend_state of their decision snapshot."""
    trend_by_decision: dict[str, str | None] = {}
    with state.database.session() as s2:
        rows = s2.execute(select(RouterDecisionRow.id, RouterDecisionRow.market_snapshot_id)).all()
        snapshot_ids = [r[1] for r in rows if r[1]]
        snap_rows = s2.execute(
            select(MarketSnapshotRow.id, MarketSnapshotRow.trend_state).where(MarketSnapshotRow.id.in_(snapshot_ids))
        ).all() if snapshot_ids else []
        trend_by_snapshot = {r[0]: r[1] for r in snap_rows}
        trend_by_decision = {r[0]: trend_by_snapshot.get(r[1]) for r in rows}
    buckets: dict[str, list] = {}
    for t in trades:
        trend = trend_by_decision.get(t.router_decision_id or "") if t.router_decision_id else None
        key = trend or "UNKNOWN"
        buckets.setdefault(key, []).append(t)
    return {k: _metrics_dict(perf.compute_metrics(v)) for k, v in buckets.items()}


def session_analytics_full(session: Session, state, period_from: datetime, period_to: datetime) -> dict:
    portfolio = get_portfolio(session)
    if portfolio is None:
        return {"by_session": {}, "by_weekday": {}, "by_hour": {}, "heatmap": [], "selected_distribution": {}, "insufficient_data": True}
    trades = _closed_trades_in_period(state, portfolio.id, period_from, period_to)
    heatmap = time_heatmap(trades)
    sel = session.execute(
        select(RouterDecisionRow.selected_strategy_id, func.count())
        .where(
            RouterDecisionRow.selected_strategy_id.is_not(None),
            RouterDecisionRow.timestamp_utc >= period_from,
            RouterDecisionRow.timestamp_utc <= period_to,
        )
        .group_by(RouterDecisionRow.selected_strategy_id)
    ).all()
    return {
        "by_session": {k: _metrics_dict(perf.compute_metrics(v)) for k, v in session_bucket(trades, lambda t: t.entry_session_name).items()},
        "by_weekday": {k: _metrics_dict(perf.compute_metrics(v)) for k, v in session_bucket(trades, lambda t: _aware(t.opened_at_utc).strftime("%A").upper() if t.opened_at_utc else None).items()},
        "by_hour": {k: _metrics_dict(perf.compute_metrics(v)) for k, v in session_bucket(trades, lambda t: f"{_aware(t.opened_at_utc).hour:02d}:00" if t.opened_at_utc else None).items()},
        "heatmap": heatmap,
        "selected_distribution": {k: int(v) for k, v in sel},
        "insufficient_data": not trades,
        "period": {"from": to_iso(period_from), "to": to_iso(period_to)},
    }


def session_bucket(trades, key_fn) -> dict[str, list]:
    buckets: dict[str, list] = {}
    for t in trades:
        key = key_fn(t)
        if key is None:
            continue
        buckets.setdefault(str(key), []).append(t)
    return buckets


WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def time_heatmap(trades) -> list[dict]:
    cells: dict[tuple[int, int], list] = {}
    for t in trades:
        if not t.opened_at_utc:
            continue
        aware = _aware(t.opened_at_utc)
        cells.setdefault((aware.weekday(), aware.hour), []).append(t)
    out = []
    for (weekday, hour), group in cells.items():
        m = perf.compute_metrics(group)
        out.append(
            {
                "weekday": WEEKDAYS[weekday],
                "hour": hour,
                "trades": m.total_trades,
                "net_pnl": m.net_profit if m.total_trades else None,
                "win_rate": m.win_rate,
                "expectancy": m.expectancy,
                "sample_status": m.sample_status,
            }
        )
    return out


# ------------------------------------------------------------------ opportunities
def rejected_opportunities(session: Session, state, period_from: datetime, period_to: datetime, *, limit: int, cursor: str | None, strategy_id: str | None = None, outcome: str | None = None) -> dict:
    stmt = (
        select(RejectedOpportunityRow, StrategyEvaluationRow, ShadowOutcomeRow)
        .join(StrategyEvaluationRow, StrategyEvaluationRow.id == RejectedOpportunityRow.strategy_evaluation_id)
        .outerjoin(ShadowOutcomeRow, ShadowOutcomeRow.rejected_opportunity_id == RejectedOpportunityRow.id)
        .where(RejectedOpportunityRow.rejected_at_utc >= period_from, RejectedOpportunityRow.rejected_at_utc <= period_to)
        .order_by(RejectedOpportunityRow.rejected_at_utc.desc())
    )
    if strategy_id:
        stmt = stmt.where(StrategyEvaluationRow.strategy_id == strategy_id)
    if cursor:
        dt = parse_dt(cursor)
        if dt:
            stmt = stmt.where(RejectedOpportunityRow.rejected_at_utc < dt)
    rows = session.execute(stmt.limit(limit + 1)).all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = to_iso(rows[-1][0].rejected_at_utc)
    names = strategy_names(state.config)
    items = []
    for rej, ev, shadow in rows:
        if outcome == "favorable" and (shadow is None or (shadow.simulated_net_pnl or 0) <= 0):
            continue
        if outcome == "unfavorable" and (shadow is None or (shadow.simulated_net_pnl or 0) > 0):
            continue
        if outcome == "incomplete" and shadow is not None:
            continue
        items.append(rejected_dto(rej, ev, shadow, names))
    return {"items": items, "next_cursor": next_cursor}


def rejected_dto(rej: RejectedOpportunityRow, ev: StrategyEvaluationRow | None, shadow: ShadowOutcomeRow | None, names: dict[str, str]) -> dict:
    if ev is None:  # evaluation rows are pruned in tests; still render the record
        ev_id = rej.strategy_evaluation_id
        ev = StrategyEvaluationRow(id=ev_id, strategy_id="unknown", strategy_version="-", status="NO_SETUP", direction="NONE", timestamp_utc=rej.rejected_at_utc)
    return {
        "opportunity_id": rej.id,
        "detected_at": to_iso(rej.rejected_at_utc),
        "strategy_id": ev.strategy_id,
        "strategy_name": names.get(ev.strategy_id, ev.strategy_id),
        "side": None if ev.direction == "NONE" else ev.direction,
        "rejection_stage": rej.rejection_stage,
        "rejection_reasons": jload(rej.rejection_reason_codes, []) or [],
        "selected_alternative_strategy_id": rej.selected_alternative_strategy_id,
        "selected_alternative_strategy_name": names.get(rej.selected_alternative_strategy_id, "") if rej.selected_alternative_strategy_id else None,
        "shadow_status": rej.shadow_tracking_status,
        "shadow_outcome": {
            "entry_price": fl(shadow.simulated_entry_price),
            "exit_price": fl(shadow.simulated_exit_price),
            "exit_time": to_iso(shadow.simulated_exit_time_utc),
            "exit_reason": shadow.simulated_exit_reason,
            "net_pnl": fl(shadow.simulated_net_pnl),
            "r_multiple": fl(shadow.simulated_r_multiple),
            "mfe": fl(shadow.max_favorable_excursion),
            "mae": fl(shadow.max_adverse_excursion),
            "hypothesis": "favorable" if (shadow.simulated_net_pnl or 0) > 0 else ("unfavorable" if shadow.simulated_net_pnl is not None else None),
            "calculation_version": shadow.calculation_version,
        } if shadow else None,
        "counterfactual": True,
    }


def opportunity_detail(session: Session, state, opportunity_id: str) -> dict | None:
    row = session.get(RejectedOpportunityRow, opportunity_id)
    if row is None:
        return None
    ev = session.get(StrategyEvaluationRow, row.strategy_evaluation_id)
    shadow = session.execute(
        select(ShadowOutcomeRow).where(ShadowOutcomeRow.rejected_opportunity_id == opportunity_id)
    ).scalars().first()
    names = strategy_names(state.config)
    dto = rejected_dto(row, ev, shadow, names)
    if ev is not None:
        dto["evaluation"] = evaluation_dto(ev, names)
        if ev.market_snapshot_id:
            dto["market_snapshot"] = snapshot_from_row(session.get(MarketSnapshotRow, ev.market_snapshot_id))
    decision = session.get(RouterDecisionRow, row.router_decision_id) if row.router_decision_id else None
    if decision:
        dto["router_decision"] = {
            "decision_id": decision.id,
            "status": decision.decision_status,
            "selected_strategy_id": decision.selected_strategy_id,
            "explanation": decision.explanation,
            "explanation_ar": decision.explanation_ar,
            "timestamp": to_iso(decision.timestamp_utc),
        }
    return dto


def shadow_analytics(session: Session, state, period_from: datetime, period_to: datetime) -> dict:
    names = strategy_names(state.config)
    rows = session.execute(
        select(RejectedOpportunityRow, StrategyEvaluationRow, ShadowOutcomeRow)
        .join(StrategyEvaluationRow, StrategyEvaluationRow.id == RejectedOpportunityRow.strategy_evaluation_id)
        .outerjoin(ShadowOutcomeRow, ShadowOutcomeRow.rejected_opportunity_id == RejectedOpportunityRow.id)
        .where(RejectedOpportunityRow.rejected_at_utc >= period_from, RejectedOpportunityRow.rejected_at_utc <= period_to)
        .order_by(RejectedOpportunityRow.rejected_at_utc.desc())
        .limit(2000)
    ).all()
    total = len(rows)
    resolved = [r for r in rows if r[2] is not None]
    favorable = [r for r in resolved if (r[2].simulated_net_pnl or 0) > 0]
    unfavorable = [r for r in resolved if (r[2].simulated_net_pnl or 0) <= 0]
    by_strategy: dict[str, dict] = {}
    by_reason: dict[str, int] = {}
    for rej, ev, shadow in rows:
        sid = ev.strategy_id if ev else "unknown"
        bucket = by_strategy.setdefault(sid, {"strategy_id": sid, "strategy_name": names.get(sid, sid), "count": 0, "resolved": 0, "favorable": 0, "unfavorable": 0, "net_shadow_pnl": 0.0})
        bucket["count"] += 1
        if shadow is not None:
            bucket["resolved"] += 1
            if (shadow.simulated_net_pnl or 0) > 0:
                bucket["favorable"] += 1
            else:
                bucket["unfavorable"] += 1
            bucket["net_shadow_pnl"] = round(bucket["net_shadow_pnl"] + (shadow.simulated_net_pnl or 0), 2)
        for code in jload(rej.rejection_reason_codes, []) or []:
            if isinstance(code, str):
                by_reason[code] = by_reason.get(code, 0) + 1
    return {
        "total": total,
        "tracking": sum(1 for r in rows if r[0].shadow_tracking_status in ("TRACKING", "PENDING")),
        "resolved": len(resolved),
        "favorable": len(favorable),
        "unfavorable": len(unfavorable),
        "by_strategy": list(by_strategy.values()),
        "rejection_reasons": sorted(by_reason.items(), key=lambda kv: -kv[1]),
        "counterfactual": True,
        "note": "Shadow outcomes are counterfactual simulations of unexecuted opportunities; they are NOT part of portfolio equity or P/L.",
        "insufficient_data": total == 0,
    }


# ------------------------------------------------------------------ audit
def audit_event_dto(row: AuditEventRow) -> dict:
    return {
        "event_id": str(row.id),
        "event_time": to_iso(row.timestamp_utc),
        "actor": row.actor,
        "event_type": row.event_type,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "correlation_id": row.correlation_id,
        "summary": row.event_type.replace("_", " ").title(),
        "payload": jload(row.payload),
        "previous_event_hash": row.previous_event_hash,
        "event_hash": row.event_hash,
        "immutable": True,
    }


def audit_events(session: Session, *, limit: int, cursor: str | None, event_type: str | None, severity: str | None, entity_id: str | None) -> dict:
    stmt = select(AuditEventRow).order_by(AuditEventRow.id.desc())
    if event_type:
        stmt = stmt.where(AuditEventRow.event_type == event_type)
    if entity_id:
        stmt = stmt.where(AuditEventRow.entity_id == entity_id)
    if cursor:
        try:
            stmt = stmt.where(AuditEventRow.id < int(cursor))
        except ValueError:
            pass
    rows = session.execute(stmt.limit(limit + 1)).scalars().all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = str(rows[-1].id)
    items = [audit_event_dto(r) for r in rows]
    if severity:
        # severity is derived from event_type keywords (audit table has no severity column)
        warning_types = ("DEGRADED", "WARNING", "BLOCK", "REJECT", "STOP", "CIRCUIT")
        error_types = ("ERROR", "FAIL", "EMERGENCY")
        def _sev(dto):
            et = dto["event_type"].upper()
            if any(w in et for w in error_types):
                return "error"
            if any(w in et for w in warning_types):
                return "warning"
            return "info"
        items = [d for d in items if _sev(d) == severity]
    return {"items": items, "next_cursor": next_cursor}


# ------------------------------------------------------------------ system
def control_state(state, session: Session) -> dict:
    if state.has_runtime:
        runtime = state.runtime
        system_state = "paused" if runtime.paused else "running"
        if state.run_mode == "BACKTEST":
            system_state = "starting"
        return {
            "system_state": system_state,
            "accepts_new_entries": not runtime.paused,
            "manages_open_trades": True,
            "changed_at": None,
            "changed_by": None,
            "reason": runtime.pause_reason,
            "version": "1.0",
            "runtime_attached": True,
        }
    # standalone: infer last control audit event
    last = session.execute(
        select(AuditEventRow)
        .where(AuditEventRow.event_type.in_(["WEB_PAUSE", "WEB_RESUME", "MANUAL_PAUSE", "MANUAL_RESUME"]))
        .order_by(AuditEventRow.id.desc())
    ).scalars().first()
    paused = last is not None and "PAUSE" in last.event_type and "RESUME" not in last.event_type
    return {
        "system_state": "paused" if paused else "unknown",
        "accepts_new_entries": not paused,
        "manages_open_trades": False,
        "changed_at": to_iso(last.timestamp_utc) if last else None,
        "changed_by": last.actor if last else None,
        "reason": None,
        "version": "1.0",
        "runtime_attached": False,
    }


def system_health(session: Session, state) -> dict:
    components = session.execute(select(ServiceComponentRow)).scalars().all()
    services = [
        {
            "service_id": c.component_id,
            "name": c.component_name,
            "status": c.status.lower(),
            "last_heartbeat_at": to_iso(c.last_heartbeat_utc),
            "last_error_summary": (c.last_error or "")[:200] if c.last_error else None,
            "enabled": c.enabled in (True, "1"),
        }
        for c in components
    ]
    # engine-provided component status when embedded
    if state.has_runtime:
        for component_id, status in (state.runtime.component_status or {}).items():
            services.append(
                {
                    "service_id": component_id,
                    "name": component_id.replace("_", " ").title(),
                    "status": status.lower(),
                    "last_heartbeat_at": to_iso(state.runtime.clock.now_utc()),
                    "last_error_summary": None,
                    "enabled": True,
                }
            )
    db_ok = state.database.healthy()
    services.append(
        {
            "service_id": "database",
            "name": "Database",
            "status": "healthy" if db_ok else "down",
            "last_heartbeat_at": to_iso(datetime.now(UTC)) if db_ok else None,
            "last_error_summary": None if db_ok else "database health check failed",
            "enabled": True,
        }
    )
    services.append(
        {
            "service_id": "web_api",
            "name": "Web API",
            "status": "healthy",
            "last_heartbeat_at": to_iso(datetime.now(UTC)),
            "last_error_summary": None,
            "enabled": True,
        }
    )
    events = session.execute(
        select(SystemHealthEventRow).order_by(SystemHealthEventRow.timestamp_utc.desc()).limit(10)
    ).scalars().all()
    return {
        "overall": _overall_status(services, state),
        "services": services,
        "recent_events": [
            {
                "time": to_iso(e.timestamp_utc),
                "component": e.component,
                "severity": e.severity,
                "status": e.status,
                "message": e.message,
            }
            for e in events
        ],
        "runtime_attached": state.has_runtime,
    }


def _overall_status(services: list[dict], state) -> str:
    statuses = {s["status"] for s in services}
    if "down" in statuses:
        return "degraded"
    if "degraded" in statuses:
        return "degraded"
    if state.run_mode == "EMBEDDED_ENGINE":
        return "healthy"
    return "starting"


def server_metrics(state) -> dict:
    import os
    import shutil

    metrics: dict = {}
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        metrics["load_average"] = {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2), "cpu_count": cpu_count, "usage_pct": round(min(load1 / cpu_count * 100, 100), 1)}
    except (AttributeError, OSError):
        pass
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    metrics["memory"] = {"rss_mb": round(rss_kb / 1024, 1)}
                    break
    except OSError:
        pass
    usage = shutil.disk_usage(os.getcwd())
    metrics["disk"] = {
        "total_gb": round(usage.total / 1e9, 1),
        "used_gb": round(usage.used / 1e9, 1),
        "free_gb": round(usage.free / 1e9, 1),
        "used_pct": round(usage.used / usage.total * 100, 1),
    }
    if state.has_runtime:
        rt = state.runtime
        metrics["engine"] = {
            "cycles": rt.stats.cycles,
            "evaluations": rt.stats.evaluations,
            "candidates": rt.stats.candidates,
            "selections": rt.stats.selections,
            "trades_opened": rt.stats.trades_opened,
            "run_mode": rt.run_mode,
            "started_at": to_iso(rt.clock.now_utc() if not rt.run_id else rt.clock.now_utc()),
        }
    return metrics


def incidents(session: Session, *, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(SystemHealthEventRow)
        .where(SystemHealthEventRow.severity.in_(["ERROR", "CRITICAL", "WARNING"]))
        .order_by(SystemHealthEventRow.timestamp_utc.desc())
        .limit(limit)
    ).scalars().all()
    return [
        {
            "time": to_iso(r.timestamp_utc),
            "component": r.component,
            "severity": r.severity,
            "status": r.status,
            "message": (r.message or "")[:300],
        }
        for r in rows
    ]


def version_info(state) -> dict:
    return {
        "app_name": "GOLD_HYBRID_ENGINE",
        "app_version": APP_VERSION,
        "api_schema_version": SCHEMA_VERSION,
        "run_mode": state.run_mode,
        "demo_data": state.run_mode in ("DEMO", "BACKTEST") or bool(state.config.get("market_data", {}).get("provider") in ("replay", "mock")),
        "paper_only": True,
        "canonical_symbol": CANONICAL_SYMBOL,
    }


def reports_list(session: Session, *, limit: int = 30) -> list[dict]:
    from app.persistence.models import ReportRunRow

    rows = session.execute(select(ReportRunRow).order_by(ReportRunRow.generated_at_utc.desc()).limit(limit)).scalars().all()
    return [
        {
            "report_id": r.id,
            "report_type": r.report_type,
            "period_start": to_iso(r.period_start_utc),
            "period_end": to_iso(r.period_end_utc),
            "generated_at": to_iso(r.generated_at_utc),
            "status": "ready" if r.metrics_json else "failed",
            "has_export": bool(r.export_path),
        }
        for r in rows
    ]
