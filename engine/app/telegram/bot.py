"""Telegram outbox worker + command handler (spec 14.5 / 44).

- Messages are queued in DB (outbox pattern) and sent by a background
  worker with retry/backoff; unique message_key prevents duplicates.
- Commands are operational only (status/pause/resume/reports) — no
  Buy/Sell/Close, no raw SQL. Every admin command is audited.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.enums import Actor, OutboxStatus
from app.core.logging_setup import get_logger
from app.journal.audit import AuditJournal
from app.persistence.database import Database, dumps
from app.persistence.models import TelegramOutboxRow
from app.telegram.auth import TelegramAuthorizer
from app.telegram.client import TelegramClient
from app.telegram import messages as msg

logger = get_logger("telegram.outbox")

MAX_ATTEMPTS = 5
BACKOFF_MINUTES = [1, 5, 15, 60, 240]


class OutboxWorker:
    def __init__(self, database: Database, client: TelegramClient, *, enabled: bool):
        self.db = database
        self.client = client
        self.enabled = enabled

    def enqueue(self, *, chat_id: str, message_type: str, text: str, message_key: str | None = None) -> str | None:
        """Queue a message; returns outbox id or None if duplicate key."""
        with self.db.session() as session:
            if message_key:
                existing = session.execute(
                    select(TelegramOutboxRow).where(TelegramOutboxRow.message_key == message_key)
                ).scalar_one_or_none()
                if existing is not None:
                    return None  # already queued/sent — no duplicate notifications
            row = TelegramOutboxRow(
                chat_id=chat_id,
                message_type=message_type,
                message_key=message_key,
                message_text=text,
                payload=dumps({"message_type": message_type}),
                status=OutboxStatus.PENDING.value,
            )
            session.add(row)
            session.flush()
            return row.id

    def flush(self, *, limit: int = 30) -> dict:
        """Send due messages; returns {sent, failed, deferred}."""
        if not self.enabled:
            return {"sent": 0, "failed": 0, "deferred": 0}
        now = datetime.now(UTC)
        sent = failed = deferred = 0
        with self.db.session() as session:
            rows = (
                session.execute(
                    select(TelegramOutboxRow)
                    .where(
                        TelegramOutboxRow.status == OutboxStatus.PENDING.value,
                        TelegramOutboxRow.next_attempt_at_utc <= now,
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            for row in rows:
                try:
                    self.client.send_message(row.chat_id, row.message_text or "")
                except Exception as exc:
                    row.attempts += 1
                    row.last_error = str(exc)[:500]
                    if row.attempts >= MAX_ATTEMPTS:
                        row.status = OutboxStatus.FAILED.value
                        failed += 1
                    else:
                        backoff = BACKOFF_MINUTES[min(row.attempts - 1, len(BACKOFF_MINUTES) - 1)]
                        row.next_attempt_at_utc = now + timedelta(minutes=backoff)
                        deferred += 1
                    continue
                row.status = OutboxStatus.SENT.value
                row.sent_at_utc = now
                row.attempts += 1
                sent += 1
        return {"sent": sent, "failed": failed, "deferred": deferred}

    def pending_count(self) -> int:
        with self.db.session() as session:
            rows = session.execute(
                select(TelegramOutboxRow).where(TelegramOutboxRow.status == OutboxStatus.PENDING.value)
            ).scalars().all()
        return len(rows)


class TelegramCommandHandler:
    """Parses authorized commands and answers with operational data only."""

    def __init__(
        self,
        *,
        authorizer: TelegramAuthorizer,
        audit: AuditJournal,
        runtime,  # HybridRuntime facade (status/pause/resume/queries)
        outbox: OutboxWorker,
        admin_chat_id: str,
    ):
        self.authorizer = authorizer
        self.audit = audit
        self.runtime = runtime
        self.outbox = outbox
        self.admin_chat_id = admin_chat_id

    def handle_updates(self, updates) -> None:
        for update in updates:
            if not update.text:
                continue
            ok, reason = self.authorizer.authorize(update.user_id, update.chat_id)
            if not ok:
                logger.info("telegram command rejected", extra={"context": {"reason": reason}})
                continue
            self._dispatch(update)

    def _reply(self, text: str) -> None:
        self.outbox.enqueue(
            chat_id=self.admin_chat_id, message_type="COMMAND_REPLY", text=text
        )

    def _dispatch(self, update) -> None:
        command = update.text.strip().split()[0].lower()
        args = update.text.strip().split()[1:]
        self.audit.append(
            "TELEGRAM_COMMAND",
            actor=Actor.TELEGRAM_USER.value,
            entity_type="command",
            entity_id=command,
            payload={"user_id": update.user_id, "args": args[:3]},
        )
        handlers = {
            "/start": self._cmd_start,
            "/help": self._cmd_help,
            "/status": self._cmd_status,
            "/market": self._cmd_market,
            "/open": self._cmd_open,
            "/today": self._cmd_today,
            "/week": self._cmd_week,
            "/performance": self._cmd_performance,
            "/strategies": self._cmd_strategies,
            "/schools": self._cmd_strategies,
            "/decision": self._cmd_decision,
            "/logs": self._cmd_logs,
            "/health": self._cmd_health,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
        }
        handler = handlers.get(command)
        if handler is None:
            self._reply(f"أمر غير معروف: {command}\nاستخدم /help لعرض الأوامر.")
            return
        handler(args)

    # ------------------------------------------------------------------ commands
    def _cmd_start(self, args):
        self._reply(
            "🤖 <b>GOLD HYBRID ENGINE</b>\n"
            "نظام تداول هجين افتراضي للذهب XAUUSD.\n"
            "Paper Trading فقط — لا أوامر حقيقية.\n\n"
            "اكتب /help لعرض الأوامر المتاحة."
        )

    def _cmd_help(self, args):
        self._reply(
            "📖 <b>الأوامر المتاحة:</b>\n"
            "/status — حالة النظام\n"
            "/market — آخر حالة سوق\n"
            "/open — الصفقات المفتوحة\n"
            "/today — ملخص اليوم\n"
            "/week — التقرير الأسبوعي\n"
            "/performance — الإحصائيات\n"
            "/strategies — حالة المدارس\n"
            "/decision — آخر قرار Router\n"
            "/health — صحة المكونات\n"
            "/logs — آخر الأحداث\n"
            "/pause — إيقاف فتح صفقات جديدة\n"
            "/resume — استئناف فتح الصفقات"
        )

    def _cmd_status(self, args):
        self._reply(self.runtime.status_text())

    def _cmd_market(self, args):
        self._reply(self.runtime.market_text())

    def _cmd_open(self, args):
        self._reply(self.runtime.open_trades_text())

    def _cmd_today(self, args):
        self._reply(self.runtime.today_text())

    def _cmd_week(self, args):
        self._reply(self.runtime.week_text())

    def _cmd_performance(self, args):
        self._reply(self.runtime.performance_text())

    def _cmd_strategies(self, args):
        self._reply(self.runtime.strategies_text())

    def _cmd_decision(self, args):
        self._reply(self.runtime.last_decision_text())

    def _cmd_logs(self, args):
        self._reply(self.runtime.logs_text())

    def _cmd_health(self, args):
        self._reply(self.runtime.health_text())

    def _cmd_pause(self, args):
        result = self.runtime.pause_opening(reason="TELEGRAM_PAUSE")
        self._reply(
            "⏸️ تم إيقاف فتح صفقات جديدة.\n"
            "الصفقات المفتوحة تستمر تحت إدارة مدارسها.\n"
            f"{result}"
            if result
            else "⏸️ تم إيقاف فتح صفقات جديدة. الصفقات المفتوحة تستمر تحت إدارة مدارسها."
        )

    def _cmd_resume(self, args):
        ok, reason = self.runtime.resume_opening()
        if ok:
            self._reply("▶️ تم استئناف فتح الصفقات الجديدة.")
        else:
            self._reply(f"❌ لا يمكن الاستئناف الآن — مانع نشط: {reason}")
