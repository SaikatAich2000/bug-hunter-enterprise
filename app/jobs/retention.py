"""Retention: delete what has outlived its purpose, in small batches.

Removed on every run:
* notifications older than NOTIFICATION_RETENTION_DAYS (0 keeps them),
* Sleuth conversations idle for longer than CHAT_RETENTION_DAYS (0 keeps them),
* expired sessions, used or expired password-reset tokens, and expired idempotency keys,
* invitations that expired more than INVITATION_GRACE_DAYS ago, and email-change requests that
  expired or were used.

Deletes go in batches of ids so a large backlog never holds one long lock, and the job is
safe to run on several replicas at once (a row deleted twice is simply gone). The app runs it
daily; ``python -m app.jobs.retention`` runs it once from an external scheduler.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ChatConversation,
    EmailChangeRequest,
    IdempotencyKey,
    Invitation,
    Notification,
    PasswordResetToken,
)
from app.models import Session as SessionRow

logger = logging.getLogger("bug_hunter.retention")

# Expired invitations stay visible (as "expired") in the manager for this long.
INVITATION_GRACE_DAYS = 30
BATCH_SIZE = 1000


def _purge(db: Session, model, condition) -> int:
    """Delete matching rows in id batches, committing each; returns the count."""
    removed = 0
    while True:
        ids = list(db.scalars(select(model.id).where(condition).limit(BATCH_SIZE)).all())
        if not ids:
            return removed
        db.execute(delete(model).where(model.id.in_(ids)))
        db.commit()
        removed += len(ids)


def run_retention(db: Session, now: datetime | None = None) -> dict[str, int]:
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    stats = {
        "sessions": _purge(db, SessionRow, SessionRow.expires_at < now),
        "reset_tokens": _purge(db, PasswordResetToken, or_(
            PasswordResetToken.expires_at < now, PasswordResetToken.used_at.is_not(None),
        )),
        "idempotency_keys": _purge(db, IdempotencyKey, IdempotencyKey.expires_at < now),
        "invitations": _purge(
            db, Invitation, Invitation.expires_at < now - timedelta(days=INVITATION_GRACE_DAYS),
        ),
        "email_change_requests": _purge(db, EmailChangeRequest, or_(
            EmailChangeRequest.expires_at < now, EmailChangeRequest.used_at.is_not(None),
        )),
        "notifications": 0,
        "chat_conversations": 0,
    }
    if settings.NOTIFICATION_RETENTION_DAYS:
        cutoff = now - timedelta(days=settings.NOTIFICATION_RETENTION_DAYS)
        stats["notifications"] = _purge(db, Notification, Notification.created_at < cutoff)
    if settings.CHAT_RETENTION_DAYS:
        cutoff = now - timedelta(days=settings.CHAT_RETENTION_DAYS)
        # Messages go with their conversation (ON DELETE CASCADE).
        stats["chat_conversations"] = _purge(db, ChatConversation, ChatConversation.updated_at < cutoff)
    if any(stats.values()):
        logger.info("Retention removed %s", ", ".join(f"{n} {k}" for k, n in stats.items() if n))
    return stats


def run_once() -> dict[str, int]:
    """Open a session, run retention, close. Blocking; the scheduler calls it in a thread."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return run_retention(db)
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover - operator entry point
    logging.basicConfig(level=logging.INFO)
    print(run_once())
