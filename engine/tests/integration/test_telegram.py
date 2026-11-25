"""Telegram tests with Mock client: outbox, auth, commands, retry, dedup."""
from __future__ import annotations

from datetime import UTC, datetime

from app.journal.audit import AuditJournal
from app.telegram.auth import TelegramAuthorizer
from app.telegram.bot import OutboxWorker, TelegramCommandHandler
from app.telegram.client import MockTelegramClient, TelegramUpdate


class TestOutbox:
    def test_sends_and_dedups(self, db):
        mock = MockTelegramClient()
        worker = OutboxWorker(db, mock, enabled=True)
        worker.enqueue(chat_id="100", message_type="TEST", text="first", message_key="mk1")
        worker.enqueue(chat_id="100", message_type="TEST", text="first", message_key="mk1")  # dup
        result = worker.flush()
        assert result["sent"] == 1
        assert len(mock.sent) == 1

    def test_retry_with_backoff(self, db):
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from app.persistence.models import TelegramOutboxRow

        mock = MockTelegramClient()
        mock.fail_next = True
        worker = OutboxWorker(db, mock, enabled=True)
        worker.enqueue(chat_id="100", message_type="TEST", text="will fail once")
        result = worker.flush()
        assert result["deferred"] == 1  # not sent, not dead
        # backoff pushed next_attempt into the future — make it due now
        with db.session() as session:
            row = session.execute(select(TelegramOutboxRow)).scalar_one()
            row.next_attempt_at_utc = datetime.now(UTC) - timedelta(seconds=1)
        result2 = worker.flush()
        assert result2["sent"] == 1

    def test_gives_up_after_max_attempts(self, db):
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from app.persistence.models import TelegramOutboxRow

        mock = AlwaysFailClient()
        worker = OutboxWorker(db, mock, enabled=True)
        worker.enqueue(chat_id="100", message_type="TEST", text="x")
        for _ in range(5):
            worker.flush()
            with db.session() as session:
                for row in session.execute(select(TelegramOutboxRow)).scalars():
                    row.next_attempt_at_utc = datetime.now(UTC) - timedelta(seconds=1)
        with db.session() as session:
            rows = session.execute(select(TelegramOutboxRow)).scalars().all()
        assert rows[0].status == "FAILED"
        assert rows[0].attempts == 5

    def test_disabled_telegram_sends_nothing(self, db):
        mock = MockTelegramClient()
        worker = OutboxWorker(db, mock, enabled=False)
        worker.enqueue(chat_id="100", message_type="TEST", text="x")
        assert worker.flush() == {"sent": 0, "failed": 0, "deferred": 0}
        assert mock.sent == []


class AlwaysFailClient(MockTelegramClient):
    def send_message(self, chat_id, text):
        from app.core.exceptions import TelegramError

        raise TelegramError("always failing")


class TestAuth:
    def test_user_whitelist(self):
        auth = TelegramAuthorizer(["42"], ["100"])
        assert auth.authorize("42", "100") == (True, "OK")
        ok, reason = auth.authorize("43", "100")
        assert not ok and reason == "UNAUTHORIZED_USER"

    def test_missing_user_id_rejected(self):
        auth = TelegramAuthorizer(["42"], ["100"])
        ok, reason = auth.authorize(None, "100")
        assert not ok and reason == "UNAUTHORIZED_NO_USER_ID"

    def test_rate_limit(self):
        auth = TelegramAuthorizer(["42"], ["100"], rate_per_minute=3)
        assert auth.authorize("42", "100")[0]
        assert auth.authorize("42", "100")[0]
        assert auth.authorize("42", "100")[0]
        ok, reason = auth.authorize("42", "100")
        assert not ok and reason == "RATE_LIMITED"


class _FakeRuntime:
    """Minimal facade the command handler needs."""

    def __init__(self):
        self.paused = False

    def status_text(self):
        return "STATUS OK"

    def market_text(self):
        return "MARKET OK"

    def open_trades_text(self):
        return "NO TRADES"

    def today_text(self):
        return "TODAY OK"

    def week_text(self):
        return "WEEK OK"

    def performance_text(self):
        return "PERF OK"

    def strategies_text(self):
        return "STRATS OK"

    def last_decision_text(self):
        return "DECISION OK"

    def logs_text(self):
        return "LOGS OK"

    def health_text(self):
        return "HEALTH OK"

    def pause_opening(self, *, reason=""):
        self.paused = True
        return ""

    def resume_opening(self):
        if self.paused:
            self.paused = False
            return True, ""
        return False, "already running"


class TestCommands:
    def _handler(self, db, users=("42",)):
        mock = MockTelegramClient()
        worker = OutboxWorker(db, mock, enabled=True)
        auth = TelegramAuthorizer(list(users), ["100"])
        runtime = _FakeRuntime()
        handler = TelegramCommandHandler(
            authorizer=auth, audit=AuditJournal(db), runtime=runtime,
            outbox=worker, admin_chat_id="100",
        )
        return handler, mock, worker, runtime

    def _update(self, text, user="42"):
        return TelegramUpdate(update_id=1, user_id=user, chat_id="100", text=text)

    def test_status_command_replies(self, db):
        handler, mock, worker, _ = self._handler(db)
        handler.handle_updates([self._update("/status")])
        worker.flush()
        assert any("STATUS OK" in text for _, text in mock.sent)

    def test_unknown_command_hint(self, db):
        handler, mock, worker, _ = self._handler(db)
        handler.handle_updates([self._update("/fly")])
        worker.flush()
        assert any("/help" in text for _, text in mock.sent)

    def test_unauthorized_user_ignored(self, db):
        handler, mock, worker, _ = self._handler(db, users=("42",))
        handler.handle_updates([self._update("/status", user="999")])
        worker.flush()
        assert mock.sent == []

    def test_pause_resume_flow(self, db):
        handler, mock, worker, runtime = self._handler(db)
        handler.handle_updates([self._update("/pause")])
        worker.flush()
        assert runtime.paused is True
        handler.handle_updates([self._update("/resume")])
        worker.flush()
        assert runtime.paused is False

    def test_commands_are_audited(self, db):
        handler, *_ = self._handler(db)
        handler.handle_updates([self._update("/status")])
        from sqlalchemy import select

        from app.persistence.models import AuditEventRow

        with db.session() as session:
            events = session.execute(
                select(AuditEventRow).where(AuditEventRow.event_type == "TELEGRAM_COMMAND")
            ).scalars().all()
        assert len(events) == 1
        assert events[0].entity_id == "/status"

    def test_all_documented_commands_work(self, db):
        handler, mock, worker, _ = self._handler(db)
        for command in ("/start", "/help", "/status", "/market", "/open", "/today", "/week",
                        "/performance", "/strategies", "/decision", "/logs", "/health"):
            handler.handle_updates([self._update(command)])
            worker.flush()
        replies = [text for _, text in mock.sent]
        assert len(replies) == 12
        assert all(r.strip() for r in replies)
