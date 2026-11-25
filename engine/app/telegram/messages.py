"""Arabic message templates (spec 14 / 44.3-44.4).

Rich, structured notifications — never a bare BUY/SELL. Every message is
tied to searchable ids (trade id, decision id).
"""
from __future__ import annotations

from app.core.enums import DataQuality
from app.execution.paper_engine import PaperTrade
from app.hybrid.router import RouterDecision
from app.market_context.engine import MarketSnapshot


def fmt_ts(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def system_started(
    *,
    version: str,
    started_at,
    mt5_connected: bool,
    db_ok: bool,
    source_symbol: str,
    strategies: dict[str, str],
) -> str:
    lines = [
        "✅ <b>تشغيل GOLD HYBRID ENGINE</b>",
        "",
        f"الوقت: <code>{fmt_ts(started_at)}</code>",
        f"الإصدار: <code>{version}</code>",
        f"اتصال MT5: {'متصل ✅' if mt5_connected else 'غير متصل ❌'}",
        f"قاعدة البيانات: {'تعمل ✅' if db_ok else 'خطأ ❌'}",
        f"الرمز المصدر: <code>{source_symbol}</code>",
        "الوضع: <b>Paper Trading فقط — لا تُرسل أي أوامر حقيقية</b>",
        "",
        "<b>حالة المدارس:</b>",
    ]
    for sid, state in strategies.items():
        icon = "🟢" if state == "ENABLED" else ("⚪" if "DATA" in state else "🔴")
        lines.append(f"{icon} <code>{sid}</code>: {state}")
    return "\n".join(lines)


def system_stopped(*, stopped_at, reason: str) -> str:
    return (
        f"⛔ <b>توقف النظام</b>\n\nالوقت: <code>{fmt_ts(stopped_at)}</code>\nالسبب: {reason}"
    )


def data_feed_event(*, event: str, details: dict, at) -> str:
    if event == "DATA_FEED_DEGRADED":
        icon, title = "⚠️", "انقطاع/تدهور في تدفق البيانات"
    else:
        icon, title = "🔄", "استعادة تدفق البيانات"
    quality = details.get("quality", "?")
    return (
        f"{icon} <b>{title}</b>\n\n"
        f"الوقت: <code>{fmt_ts(at)}</code>\n"
        f"جودة البيانات: <code>{quality}</code>\n"
        f"الأسباب: <code>{details.get('degraded_reasons') or []}</code>\n"
        f"عمر آخر Tick: <code>{details.get('last_tick_age_seconds')}</code> ثانية"
    )


def market_context_changed(*, snapshot: MarketSnapshot, changes: list[str]) -> str:
    d = snapshot.to_dict()
    return (
        "📊 <b>تغير حالة السوق</b>\n\n"
        f"الوقت: <code>{fmt_ts(snapshot.timestamp_utc)}</code>\n"
        f"الاتجاه: <code>{d['trend_state']}</code> | التقلب: <code>{d['volatility_state']}</code>\n"
        f"النشاط: <code>{d['activity_state']}</code> | السيولة (Proxy): <code>{d['liquidity_proxy_state']}</code>\n"
        f"البنية: <code>{', '.join(d['market_structure'][:3])}</code>\n"
        f"الجلسة: <code>{d['session']} / {d['session_phase']}</code>\n"
        f"المتغير: <code>{', '.join(changes[:4])}</code>"
    )


def strategy_selection(*, decision: RouterDecision) -> str:
    lines = [
        "🎯 <b>اختيار مدرسة جديدة</b>",
        "",
        f"الوقت: <code>{fmt_ts(decision.timestamp_utc)}</code>",
        f"القرار: <code>{decision.decision_id[:8]}</code>",
        "",
        decision.explanation_ar,
        "",
        "<b>المرشحون:</b>",
    ]
    for ranked in decision.ranked:
        ev = ranked.evaluation
        state = "مؤهل" if ranked.eligible else f"مرفوض ({','.join(ranked.rejection_codes)})"
        lines.append(
            f"• <code>{ev.strategy_id}</code> [{ev.setup_type}] — نتيجة {ranked.score_total:.2f} — {state}"
        )
    return "\n".join(lines)


def trade_opened(*, trade: PaperTrade, snapshot: MarketSnapshot, decision: RouterDecision) -> str:
    setup = trade.metadata.get("setup_type", "?")
    rr = trade.metadata.get("rr")
    lines = [
        "🟢 <b>فتح صفقة افتراضية — PAPER TRADE OPENED</b>",
        "",
        f"Trade ID: <code>{trade.id[:8]}</code>",
        f"الرمز: <code>{trade.symbol}</code> | الاتجاه: <b>{trade.side.value}</b>",
        f"المدرسة: <code>{trade.origin_strategy_id}</code> ({trade.strategy_version})",
        f"النمط: <code>{setup}</code>",
        "",
        f"الدخول: <code>{trade.entry_price}</code>",
        f"وقف الخسارة: <code>{trade.stop_loss}</code>",
        f"الهدف: <code>{trade.take_profit if trade.take_profit else 'خروج متحرك (Trailing)'}</code>",
        f"الحجم: <code>{trade.volume_lots}</code> لوت",
        f"المخاطرة: <code>${trade.planned_risk_amount}</code>",
        f"السبريد وقت الدخول: <code>{trade.entry_spread_points}</code> نقطة",
        "",
        "<b>سياق السوق:</b>",
        f"الاتجاه: <code>{snapshot.trend_state.value}</code> | التقلب: <code>{snapshot.volatility_state.value}</code>",
        f"الجلسة: <code>{snapshot.session.session_name} / {snapshot.session.phase.value}</code>",
        "",
        f"<b>سبب الاختيار:</b> {decision.explanation_ar}",
        "",
        "⚙️ محاكاة فقط — لم يُرسل أي أمر حقيقي.",
    ]
    return "\n".join(lines)


def trade_closed(*, trade: PaperTrade) -> str:
    duration = ""
    if trade.opened_at_utc and trade.closed_at_utc:
        minutes = (trade.closed_at_utc - trade.opened_at_utc).total_seconds() / 60
        duration = f"{minutes:.0f} دقيقة" if minutes < 1440 else f"{minutes / 1440:.1f} يوم"
    reason_ar = {
        "TAKE_PROFIT": "تحقيق الهدف",
        "STOP_LOSS": "ضرب وقف الخسارة",
        "TRAILING_STOP": "وقف متحرك",
        "STRATEGY_EXIT": "خروج استراتيجي",
        "TIME_EXIT": "خروج زمني",
    }.get(trade.exit_reason or "", trade.exit_reason)
    lines = [
        "🔴 <b>إغلاق صفقة افتراضية — PAPER TRADE CLOSED</b>",
        "",
        f"Trade ID: <code>{trade.id[:8]}</code>",
        f"المدرسة: <code>{trade.origin_strategy_id}</code> | الاتجاه: <b>{trade.side.value}</b>",
        f"الدخول: <code>{trade.entry_price}</code> ← الخروج: <code>{trade.exit_price}</code>",
        f"سبب الإغلاق: <b>{reason_ar}</b>",
        f"المدة: <code>{duration}</code>",
        "",
        f"الربح الإجمالي: <code>${trade.gross_pnl}</code>",
        f"العمولة: <code>${trade.commission}</code> | السواب: <code>${trade.swap}</code>",
        f"<b>الصافي: ${trade.net_pnl}</b> | R Multiple: <code>{trade.r_multiple}</code>",
        "",
        f"جلسة الدخول: <code>{trade.entry_session_name}</code> | جلسة الإغلاق: <code>{trade.exit_session_name}</code>",
        "⚙️ محاكاة فقط.",
    ]
    return "\n".join(lines)


def daily_report_text(*, report: dict) -> str:
    metrics = report["metrics"]
    lines = [
        "📅 <b>التقرير اليومي</b>",
        "",
        f"دورات التحليل: <code>{report['analysis_cycles']}</code>",
        f"صفقات مفتوحة اليوم: <code>{report['trades_opened']}</code> | مغلقة: <code>{report['trades_closed']}</code>",
    ]
    if metrics["total_trades"] > 0:
        lines += [
            "",
            f"نسبة النجاح: <code>{metrics['win_rate']}%</code> ({metrics['wins']}W/{metrics['losses']}L)",
            f"الصافي: <code>${metrics['net_profit']}</code> | Expectancy: <code>${metrics['expectancy']}</code>",
        ]
    else:
        lines.append("\nلا صفقات مغلقة اليوم.")
    if report.get("no_trade_reasons_top"):
        lines.append("")
        lines.append("<b>أكثر أسباب عدم التداول:</b>")
        for item in report["no_trade_reasons_top"][:3]:
            lines.append(f"• <code>{item['reason']}</code> × {item['count']}")
    return "\n".join(lines)


def weekly_report_text(*, report: dict) -> str:
    hp = report["hybrid_portfolio"]
    lines = [
        "📈 <b>التقرير الأسبوعي — النظام الهجين</b>",
        "",
        f"عدد الصفقات: <code>{hp['total_trades']}</code> (حالة العينة: {hp['sample_status']})",
    ]
    if hp["total_trades"] > 0:
        lines += [
            f"نسبة النجاح: <code>{hp['win_rate']}%</code>",
            f"الصافي: <code>${hp['net_profit']}</code> | PF: <code>{hp['profit_factor']}</code>",
            f"Expectancy: <code>${hp['expectancy']}</code> | R: <code>{hp['expectancy_r']}</code>",
            f"أقصى تراجع: <code>${hp['max_drawdown']}</code>",
            f"Long/Short: <code>{hp['long_trades']}/{hp['short_trades']}</code>",
        ]
    rq = report.get("router_quality", {})
    if rq:
        lines += [
            "",
            "<b>جودة المحرك الهجين (Router):</b>",
            f"القرارات: <code>{rq.get('decisions')}</code> | نسبة NO_TRADE: <code>{rq.get('no_trade_ratio')}</code>",
        ]
        by_strategy = rq.get("selections_by_strategy") or {}
        for sid, count in sorted(by_strategy.items(), key=lambda kv: -kv[1]):
            lines.append(f"• <code>{sid}</code>: اختيرت {count} مرة")
    shadow = report.get("rejected_opportunity_performance") or {}
    if shadow.get("count"):
        lines += [
            "",
            "<b>الفرص غير المختارة (Shadow — ليست صفقات منفذة):</b>",
            f"العدد: <code>{shadow['count']}</code> | وصلت للهدف: {shadow.get('reached_tp')} | وصلت للوقف: {shadow.get('reached_sl')}",
        ]
    return "\n".join(lines)


def error_alert(*, component: str, message: str, at) -> str:
    return (
        f"🚨 <b>خطأ في النظام</b>\n\n"
        f"المكوّن: <code>{component}</code>\n"
        f"الرسالة: {message[:300]}\n"
        f"الوقت: <code>{fmt_ts(at)}</code>"
    )


def no_trade_digest(*, totals: dict, at) -> str:
    return (
        "🤝 <b>ملخص الفرص المرفوضة</b>\n\n"
        f"عدد التقييمات: <code>{totals.get('evaluations', 0)}</code>\n"
        f"المرشحون: <code>{totals.get('candidates', 0)}</code>\n"
        f"المرفوضون: <code>{totals.get('rejected', 0)}</code>\n"
        f"المحفوظون للـ Shadow: <code>{totals.get('shadow_registered', 0)}</code>\n"
        f"الوقت: <code>{fmt_ts(at)}</code>"
    )
