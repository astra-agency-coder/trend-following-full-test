"""Web API tests — envelope shape, auth, data contracts, control safety.

Seeds an in-memory SQLite database directly through the ORM and verifies
that every dashboard read-model endpoint answers with the unified envelope
and that control endpoints enforce OWNER + CSRF (frontend spec §17/§22).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.persistence.database import dumps
from app.persistence.models import (
    AuditEventRow,
    MarketBarRow,
    MarketSnapshotRow,
    PaperPortfolioRow,
    PaperTradeRow,
    RejectedOpportunityRow,
    RouterCandidateRankingRow,
    RouterDecisionRow,
    ShadowOutcomeRow,
    StrategyEvaluationRow,
    SymbolSpec,
    TradeEventRow,
)
from app.webapi import create_app
from app.webapi.dependencies import set_state


@pytest.fixture()
def db(tmp_path):
    from app.persistence.database import Database

    database = Database(f"sqlite:///{tmp_path}/webapi_test.db")
    database.create_all()
    return database


@pytest.fixture()
def seeded_db(db):
    now = datetime.now(UTC)
    with db.session() as s:
        s.add(
            PaperPortfolioRow(
                id="pf1", name="HYBRID_MAIN", base_currency="USD", initial_balance=10000, current_balance=10042.5
            )
        )
        s.add(
            SymbolSpec(
                id="spec1", broker_name="TEST", source_symbol="XAUUSD", canonical_symbol="XAUUSD",
                digits=2, point=0.01, tick_size=0.01, tick_value=1.0, contract_size=100,
                volume_min=0.01, volume_max=10, volume_step=0.01,
                currency_base="XAU", currency_profit="USD",
            )
        )
        snap = MarketSnapshotRow(
            id="snap1", timestamp_utc=now - timedelta(minutes=5), symbol="XAUUSD", source_symbol="XAUUSD",
            data_quality="VALID", trend_state="BULLISH", volatility_state="NORMAL", activity_state="NORMAL",
            liquidity_proxy_state="NORMAL_PROXY", market_structure=dumps(["BOS_CONFIRMED"]),
            session_name="LONDON", session_phase="MID_SESSION", is_session_overlap="0",
            spread_points=18.0, bid=3331.24, ask=3331.42, feature_values=dumps({"atr_m15": 4.2}),
            context_version="1.0.0",
        )
        s.add(snap)
        for tf, days in (("M15", 2),):
            for i in range(10):
                s.add(
                    MarketBarRow(
                        canonical_symbol="XAUUSD", timeframe=tf,
                        open_time_utc=now - timedelta(minutes=15 * (i + 1)),
                        close_time_utc=now - timedelta(minutes=15 * i),
                        open=3330 + i, high=3331 + i, low=3329 + i, close=3330.5 + i,
                        tick_volume=120 + i, spread=18, source_symbol="XAUUSD",
                        is_complete=True, source_type="BROKER_BAR",
                    )
                )
        ev_selected = StrategyEvaluationRow(
            id="eval1", timestamp_utc=now - timedelta(minutes=4), market_snapshot_id="snap1",
            strategy_id="trend_following", strategy_version="1.0.0", status="CANDIDATE", direction="BUY",
            setup_stage="TRIGGERED", market_fit_score=0.72, setup_quality_score=0.81,
            proposed_entry=3330.5, proposed_sl=3325.0, proposed_tp=3341.0, risk_reward=1.9,
            conditions_passed=dumps([{"code": "EMA_ALIGN", "label": "EMA alignment", "passed": True}]),
            conditions_failed=dumps([]), rejection_reasons=dumps([]),
            opportunity_key="opk1", notes=None,
        )
        ev_other = StrategyEvaluationRow(
            id="eval2", timestamp_utc=now - timedelta(minutes=4), market_snapshot_id="snap1",
            strategy_id="breakout", strategy_version="1.0.0", status="NO_SETUP", direction="NONE",
            setup_stage="NONE", market_fit_score=0.3, setup_quality_score=None,
            conditions_passed=dumps([]), conditions_failed=dumps([{"code": "NO_COMPRESSION", "label": "no compression", "passed": False}]),
            rejection_reasons=dumps(["NO_TRIGGERED_SETUP"]), opportunity_key=None,
        )
        s.add_all([ev_selected, ev_other])
        s.flush()  # flush dependency groups explicitly (unit-of-work ordering)
        decision = RouterDecisionRow(
            id="dec1", timestamp_utc=now - timedelta(minutes=4), market_snapshot_id="snap1",
            router_version="1.0.0", selected_strategy_id="trend_following", selected_evaluation_id="eval1",
            decision_status="SELECTED", ranked_candidates=dumps({}),
            rejection_details=dumps({"breakout": ["NO_TRIGGERED_SETUP"]}), hard_blocks=dumps([]),
            explanation="Trend setup with TRIGGERED stage selected by rank",
            explanation_ar="اختيار مدرسة الاتجاه بمرحلة مفعلة",
        )
        s.add(decision)
        s.flush()
        s.add(
            RouterCandidateRankingRow(
                id="rk1", router_decision_id="dec1", evaluation_id="eval1", strategy_id="trend_following",
                rank_position=1, eligible="1", score_total=0.77, score_components=dumps({"market": 0.7}), rejection_codes=dumps([]),
            )
        )
        s.add(
            PaperTradeRow(
                id="t1", portfolio_id="pf1", origin_strategy_id="trend_following", strategy_version="1.0.0",
                management_policy_version="1.0.0", router_decision_id="dec1", entry_evaluation_id="eval1",
                opportunity_key="opk1", symbol="XAUUSD", side="BUY", status="OPEN", order_type="MARKET",
                created_at_utc=now - timedelta(minutes=4), opened_at_utc=now - timedelta(minutes=4),
                entry_price=3330.5, stop_loss=3325.0, take_profit=3341.0, volume_lots=0.05,
                planned_risk_amount=27.5, entry_session_name="LONDON",
                metadata_json=dumps({"entry_snapshot_id": "snap1", "remaining_volume_lots": 0.05}),
            )
        )
        closed = PaperTradeRow(
            id="t2", portfolio_id="pf1", origin_strategy_id="trend_following", strategy_version="1.0.0",
            management_policy_version="1.0.0", router_decision_id="dec1", entry_evaluation_id="eval1",
            symbol="XAUUSD", side="SELL", status="CLOSED_TP", order_type="MARKET",
            created_at_utc=now - timedelta(hours=6), opened_at_utc=now - timedelta(hours=6),
            closed_at_utc=now - timedelta(hours=2), entry_price=3332.0, exit_price=3325.0,
            stop_loss=3338.0, take_profit=3325.0, volume_lots=0.04, planned_risk_amount=28.0,
            gross_pnl=28.0, commission=0.4, swap=0.0, slippage_cost=0.2, net_pnl=27.4, r_multiple=0.98,
            exit_reason="TAKE_PROFIT", entry_session_name="LONDON",
            metadata_json=dumps({"entry_session_name": "LONDON"}),
        )
        s.add(closed)
        # fix portfolio_id for trades (portfolio created above with generated id)
        portfolio = s.query(PaperPortfolioRow).first()
        closed.portfolio_id = portfolio.id
        t1 = s.query(PaperTradeRow).filter_by(id="t1").first()
        t1.portfolio_id = portfolio.id
        s.add(
            TradeEventRow(id="te1", trade_id="t1", timestamp_utc=now - timedelta(minutes=4),
                          event_type="TRADE_OPENED", price=3330.5, new_values=dumps({"entry": 3330.5}), actor="SYSTEM")
        )
        rej = RejectedOpportunityRow(
            id="rej1", strategy_evaluation_id="eval2", router_decision_id="dec1",
            rejected_at_utc=now - timedelta(minutes=4), rejection_stage="ROUTER",
            rejection_reason_codes=dumps(["NOT_TOP_RANKED"]), selected_alternative_strategy_id="trend_following",
            shadow_tracking_status="COMPLETED",
        )
        s.add(rej)
        s.add(
            ShadowOutcomeRow(
                id="sh1", rejected_opportunity_id="rej1", tracked_at_utc=now - timedelta(minutes=1),
                simulated_entry_time_utc=now - timedelta(minutes=4), simulated_entry_price=3331.0,
                simulated_exit_time_utc=now - timedelta(minutes=1), simulated_exit_price=3335.0,
                simulated_exit_reason="SHADOW_TP", simulated_gross_pnl=40.0, simulated_net_pnl=39.6,
                simulated_r_multiple=0.79, max_favorable_excursion=5.0, max_adverse_excursion=1.0,
                calculation_version="1.0.0",
            )
        )
        s.add(
            AuditEventRow(timestamp_utc=now, actor="SYSTEM", event_type="SYSTEM_STARTED",
                          entity_type="system", entity_id=None, payload=dumps({"mode": "PAPER"}))
        )
        s.add(
            AuditEventRow(timestamp_utc=now, actor="SYSTEM", event_type="ROUTER_DECISION",
                          entity_type="router_decision", entity_id="dec1", payload=dumps({"status": "SELECTED"}))
        )
    return db


@pytest.fixture()
def client(seeded_db):
    app = create_app(database=seeded_db)
    return TestClient(app)


def _login(client, username="admin", password="admin123"):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    csrf = client.cookies.get("gold_hybrid_csrf")
    return csrf


# ------------------------------------------------------------------ auth
def test_health_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["paper_only"] is True


def test_login_failure_envelope(client):
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 401
    body = response.json()
    assert body["data"] is None
    assert body["error"]["code"] == "INVALID_CREDENTIALS"
    assert body["meta"]["request_id"].startswith("req_")
    assert "scrypt" not in json.dumps(body)


def test_login_success_and_me(client):
    csrf = _login(client)
    assert csrf
    me = client.get("/api/v1/auth/me").json()
    assert me["data"]["role"] == "OWNER"
    assert me["data"]["permissions"]["control"] is True


def test_viewer_cannot_control(client, seeded_db):
    _login(client, "viewer", "viewer123")
    me = client.get("/api/v1/auth/me").json()["data"]
    assert me["role"] == "VIEWER"
    # VIEWER blocked even without CSRF check order (auth first)
    response = client.post(
        "/api/v1/control/pause",
        json={"reason": "test"},
        headers={"X-CSRF-Token": client.cookies.get("gold_hybrid_csrf") or ""},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


# ------------------------------------------------------------------ envelope
def test_unauthenticated_returns_envelope(client):
    response = client.get("/api/v1/dashboard/summary")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["meta"]["schema_version"] == "1.0"


# ------------------------------------------------------------------ dashboard
def test_dashboard_summary(client):
    _login(client)
    response = client.get("/api/v1/dashboard/summary?period=7d")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["kpis"]["initial_balance"] == 10000
    assert data["kpis"]["balance"] == 10042.5
    assert data["kpis"]["open_trades"] == 1
    assert data["kpis"]["closed_trades"] == 1
    assert data["current_decision"]["decision_id"] == "dec1"
    # unrealized: BUY 0.05 lots @3330.5, bid 3331.24 → (3331.24-3330.5)*100*0.05 = 3.70
    assert data["kpis"]["unrealized_pnl"] == 3.7


def test_equity_curve(client):
    _login(client)
    response = client.get("/api/v1/dashboard/equity-curve?period=7d")
    data = response.json()["data"]
    assert data["basis"] == "closed_trades"
    assert len(data["points"]) >= 2
    assert data["points"][-1]["balance"] == 10027.4  # 10000 + 27.4


def test_recent_activity(client):
    _login(client)
    items = client.get("/api/v1/dashboard/recent-activity").json()["data"]["items"]
    assert {i["event_type"] for i in items} >= {"SYSTEM_STARTED", "ROUTER_DECISION"}


def test_open_trades_preview(client):
    _login(client)
    items = client.get("/api/v1/dashboard/open-trades-preview").json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["trade_id"] == "t1"
    assert items[0]["unrealized_pnl"] == 3.7


# ------------------------------------------------------------------ market
def test_market_snapshot(client):
    _login(client)
    data = client.get("/api/v1/market/snapshot").json()["data"]
    assert data["snapshot"]["bid"] == 3331.24
    assert data["snapshot"]["session"] == "LONDON"
    assert data["snapshot"]["regime"] == "BULLISH"
    assert data["snapshot"]["data_quality"] == "valid"
    assert "M15" in data["available_timeframes"]


def test_market_bars(client):
    _login(client)
    data = client.get("/api/v1/market/bars?timeframe=M15").json()["data"]
    assert len(data["bars"]) == 10
    assert data["bars"][0]["time"] < data["bars"][-1]["time"]
    unknown = client.get("/api/v1/market/bars?timeframe=H4").json()["data"]
    assert unknown["bars"] == []
    assert unknown["insufficient_data"] is True


# ------------------------------------------------------------------ hybrid
def test_hybrid_current(client):
    _login(client)
    data = client.get("/api/v1/hybrid/current").json()["data"]
    decision = data["decision"]
    assert decision["decision_id"] == "dec1"
    assert decision["outcome"] == "selected"
    assert decision["selected_strategy_id"] == "trend_following"
    assert decision["market_snapshot"]["trend_state"] == "BULLISH"
    assert len(decision["evaluations"]) == 2
    assert any(ev["strategy_id"] == "breakout" and ev["rejection_reasons"] for ev in decision["evaluations"])
    assert "not a win probability" in decision["score_definition"]


def test_hybrid_decisions_list(client):
    _login(client)
    data = client.get("/api/v1/hybrid/decisions").json()["data"]
    assert data["items"][0]["decision_id"] == "dec1"


def test_strategy_switches_empty(client):
    _login(client)
    items = client.get("/api/v1/hybrid/strategy-switches").json()["data"]["items"]
    assert items == []


# ------------------------------------------------------------------ trades
def test_trades_list_and_filters(client):
    _login(client)
    data = client.get("/api/v1/trades?status=all").json()["data"]
    assert data["total_count"] == 2
    open_only = client.get("/api/v1/trades?status=open").json()["data"]
    assert [i["trade_id"] for i in open_only["items"]] == ["t1"]
    winning = client.get("/api/v1/trades?result=winning").json()["data"]
    assert [i["trade_id"] for i in winning["items"]] == ["t2"]


def test_trade_detail(client):
    _login(client)
    data = client.get("/api/v1/trades/t1").json()["data"]
    assert data["status"] == "OPEN"
    assert data["timeline"][0]["event_type"] == "TRADE_OPENED"
    assert data["market_context"]["trend_state"] == "BULLISH"
    assert data["decision_id"] == "dec1"
    assert data["strategy_evidence"]["strategy_id"] == "trend_following"


def test_trade_detail_not_found(client):
    _login(client)
    response = client.get("/api/v1/trades/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_no_trade_creation_endpoint(client):
    _login(client)
    paths = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert not any(p.endswith("/trades") and "POST" in getattr(client.app.routes, "methods", set()) for p in paths)


# ------------------------------------------------------------------ analytics
def test_strategy_analytics(client):
    _login(client)
    data = client.get("/api/v1/analytics/strategies?period=7d").json()["data"]
    trend = next(s for s in data["strategies"] if s["strategy_id"] == "trend_following")
    assert trend["executed_trades_count"] == 1  # only closed trades count as executed attribution
    assert trend["win_rate"] == 100.0
    assert trend["net_pnl"] == 27.4
    assert trend["candidates_count"] == 1
    assert "NOT part of portfolio" in data["attribution_note"]


def test_session_analytics(client):
    _login(client)
    data = client.get("/api/v1/analytics/sessions?period=7d").json()["data"]
    assert data["insufficient_data"] is False
    assert data["by_session"]["LONDON"]["total_trades"] == 1


# ------------------------------------------------------------------ opportunities
def test_rejected_opportunities_counterfactual(client):
    _login(client)
    data = client.get("/api/v1/opportunities/rejected").json()["data"]
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["counterfactual"] is True
    assert item["shadow_outcome"]["hypothesis"] == "favorable"
    assert item["selected_alternative_strategy_id"] == "trend_following"


def test_shadow_analytics(client):
    _login(client)
    stats = client.get("/api/v1/opportunities/rejected").json()["data"]
    assert stats["items"][0]["shadow_outcome"]["net_pnl"] == 39.6


# ------------------------------------------------------------------ audit
def test_audit_readonly(client):
    _login(client)
    data = client.get("/api/v1/audit/events").json()["data"]
    assert len(data["items"]) == 2
    assert all(i["immutable"] for i in data["items"])
    methods_by_path = {r.path: getattr(r, "methods", set()) for r in client.app.routes if hasattr(r, "path")}
    assert all("POST" not in m for p, m in methods_by_path.items() if "/audit/events" in p and "export" not in p)
    assert all("DELETE" not in m for m in methods_by_path.values())


def test_audit_export(client):
    _login(client)
    response = client.get("/api/v1/audit/export?format=json")
    assert response.status_code == 200
    body = json.loads(response.content)
    assert len(body["audit_events"]) == 2


# ------------------------------------------------------------------ control
@pytest.fixture()
def runtime_state(seeded_db):
    class FakeClock:
        def now_utc(self):
            return datetime.now(UTC)

    class FakeRuntime:
        paused = False
        pause_reason = None
        run_id = "run1"
        component_status = {"router": "HEALTHY", "market_data": "HEALTHY"}
        clock = FakeClock()

        def pause_opening(self, *, reason="MANUAL"):
            self.paused = True
            self.pause_reason = reason
            return ""

        def resume_opening(self):
            self.paused = False
            self.pause_reason = None
            return True, ""

    from app.webapi.state import build_state

    state = build_state(seeded_db, {"strategies": {}, "sessions": {}}, run_mode="EMBEDDED_ENGINE", runtime=FakeRuntime())
    return state


@pytest.fixture()
def runtime_client(seeded_db, runtime_state):
    app = create_app(database=seeded_db, config={"strategies": {}, "sessions": {}}, runtime=runtime_state.runtime, run_mode="EMBEDDED_ENGINE")
    set_state(runtime_state)  # ensure same state object (create_app rebuilds its own)
    return TestClient(app)


def test_pause_requires_csrf(runtime_client):
    _login(runtime_client)
    response = runtime_client.post("/api/v1/control/pause", json={"reason": "test"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_FAILED"


def test_pause_and_resume_with_owner(runtime_client, runtime_state):
    csrf = _login(runtime_client)
    response = runtime_client.post(
        "/api/v1/control/pause",
        json={"reason": "maintenance"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "key-1"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["confirmed"] is True
    assert data["system_state"] == "paused"
    assert runtime_state.runtime.paused is True
    # replay blocked
    replay = runtime_client.post(
        "/api/v1/control/pause",
        json={"reason": "maintenance"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "key-1"},
    )
    assert replay.status_code == 409

    resume = runtime_client.post("/api/v1/control/resume", json={}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "key-2"})
    assert resume.status_code == 200
    assert resume.json()["data"]["system_state"] == "running"
    assert runtime_state.runtime.paused is False


def test_control_status(client):
    _login(client)
    data = client.get("/api/v1/control/status").json()["data"]
    assert "system_state" in data
    assert data["runtime_attached"] is False


# ------------------------------------------------------------------ reports
def test_report_create_and_download(client):
    csrf = _login(client)
    created = client.post(
        "/api/v1/reports",
        json={"report_type": "weekly"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 200
    report_id = created.json()["data"]["report_id"]
    detail = client.get(f"/api/v1/reports/{report_id}").json()["data"]
    assert detail["status"] == "ready"
    assert detail["content"]["metrics"]["total_trades"] == 1
    download = client.get(f"/api/v1/reports/{report_id}/download?format=json")
    assert download.status_code == 200
    assert b"metrics" in download.content


# ------------------------------------------------------------------ system
def test_system_version_and_health(client):
    _login(client)
    version = client.get("/api/v1/system/version").json()["data"]
    assert version["paper_only"] is True
    health = client.get("/api/v1/system/health").json()["data"]
    assert any(s["service_id"] == "database" for s in health["services"])
    metrics = client.get("/api/v1/system/metrics").json()["data"]
    assert "disk" in metrics


def test_engine_config_readonly_summary(client):
    _login(client)
    data = client.get("/api/v1/system/engine-config").json()["data"]
    assert data["editable_in_ui"] is False
