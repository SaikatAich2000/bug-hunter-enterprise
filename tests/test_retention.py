"""Retention job (app/jobs/retention.py): what it removes, what it keeps, and that it batches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from tests.conftest import default_org_id

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _count(db, model, *where):
    stmt = select(func.count()).select_from(model)
    for w in where:
        stmt = stmt.where(w)
    return db.scalar(stmt)


def test_retention_removes_only_what_has_expired(admin_client, monkeypatch):
    from app.database import SessionLocal
    from app.jobs import retention
    from app.models import (
        ChatConversation,
        ChatMessage,
        IdempotencyKey,
        Notification,
        PasswordResetToken,
        Project,
        User,
    )
    from app.models import Session as SessionRow

    monkeypatch.setattr(retention, "BATCH_SIZE", 2)  # several batches with a handful of rows
    old, recent = NOW - timedelta(days=200), NOW - timedelta(days=10)
    with SessionLocal() as db:
        uid = db.scalar(select(User.id))
        pid = db.scalar(select(Project.id)) or db.merge(Project(org_id=default_org_id(), name="Retention")).id
        db.flush()
        for i, when in enumerate([old] * 5 + [recent] * 2):
            db.add(Notification(user_id=uid, kind="updated", title=f"n{i}", body="", created_at=when))
        db.add_all([
            SessionRow(user_id=uid, jti="gone", expires_at=NOW - timedelta(seconds=1)),
            SessionRow(user_id=uid, jti="live", expires_at=NOW + timedelta(days=1)),
            PasswordResetToken(user_id=uid, token_hash="a" * 64, expires_at=NOW - timedelta(hours=1)),
            PasswordResetToken(user_id=uid, token_hash="b" * 64, expires_at=NOW + timedelta(hours=1), used_at=NOW),
            PasswordResetToken(user_id=uid, token_hash="c" * 64, expires_at=NOW + timedelta(hours=1)),
        ])
        stale = ChatConversation(user_id=uid, created_at=old, updated_at=old)
        fresh = ChatConversation(user_id=uid, created_at=recent, updated_at=recent)
        db.add_all([stale, fresh])
        db.flush()
        db.add_all([ChatMessage(conversation_id=stale.id, role="user", content="old"),
                    ChatMessage(conversation_id=fresh.id, role="user", content="new")])
        db.add_all([
            IdempotencyKey(user_id=uid, project_id=pid, operation_key="op", idempotency_key=key,
                           request_fingerprint="f", response_status=200, expires_at=when)
            for key, when in (("k1", NOW - timedelta(hours=1)), ("k2", NOW + timedelta(hours=1)))
        ])
        db.commit()

        stats = retention.run_retention(db, now=NOW)
        assert stats["notifications"] == 5 and stats["chat_conversations"] == 1
        assert stats["sessions"] >= 1 and stats["reset_tokens"] == 2 and stats["idempotency_keys"] == 1
        assert _count(db, Notification) == 2
        assert _count(db, SessionRow, SessionRow.jti == "live") == 1
        assert _count(db, SessionRow, SessionRow.jti == "gone") == 0
        assert [t.token_hash[0] for t in db.scalars(select(PasswordResetToken))] == ["c"]
        assert _count(db, ChatMessage) == 1 and _count(db, ChatConversation) == 1
        assert [k.idempotency_key for k in db.scalars(select(IdempotencyKey))] == ["k2"]


def test_zero_days_keeps_notifications_and_chat(admin_client, monkeypatch):
    from app.config import Settings, get_settings
    from app.database import SessionLocal
    from app.jobs import retention
    from app.models import Notification, User

    monkeypatch.setattr(Settings, "NOTIFICATION_RETENTION_DAYS", 0)
    monkeypatch.setattr(Settings, "CHAT_RETENTION_DAYS", 0)
    get_settings.cache_clear()
    with SessionLocal() as db:
        uid = db.scalar(select(User.id))
        db.add(Notification(user_id=uid, kind="updated", title="ancient", body="",
                            created_at=NOW - timedelta(days=3000)))
        db.commit()
        stats = retention.run_retention(db, now=NOW)
        assert stats["notifications"] == 0
        assert _count(db, Notification, Notification.title == "ancient") == 1
    get_settings.cache_clear()


def test_retention_applies_one_policy_to_every_organization_and_spares_fresh_rows(client):
    """Tenancy: expired rows of every org are purged by the same cutoff, and a fresh row of one
    org is never swept because another org's old rows are."""
    from app.database import SessionLocal
    from app.jobs import retention
    from app.models import Notification, User
    from tests.conftest import sign_up

    sign_up("Org One", "one@retention.test")
    sign_up("Org Two", "two@retention.test")
    old, recent = NOW - timedelta(days=200), NOW - timedelta(days=10)
    with SessionLocal() as db:
        owners = {u.email: u.id for u in db.scalars(select(User))}
        one, two = owners["one@retention.test"], owners["two@retention.test"]
        db.add_all([
            Notification(user_id=one, kind="updated", title="one-old", body="", created_at=old),
            Notification(user_id=one, kind="updated", title="one-new", body="", created_at=recent),
            Notification(user_id=two, kind="updated", title="two-old", body="", created_at=old),
            Notification(user_id=two, kind="updated", title="two-new", body="", created_at=recent),
        ])
        db.commit()
        assert retention.run_retention(db, now=NOW)["notifications"] == 2
        left = {n.title for n in db.scalars(select(Notification))}
        assert left == {"one-new", "two-new"}
