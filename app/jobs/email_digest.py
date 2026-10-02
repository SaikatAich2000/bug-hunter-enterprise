"""Daily email-digest job.

Run once a day (e.g. from host cron) with::

    python -m app.jobs.email_digest

When ``EMAIL_DIGEST_ENABLED`` is on, work-item emails are not sent immediately.
Instead each operation is recorded as a notification row (the same rows that
power the in-app bell). This job sweeps up every user's un-emailed operations
from the last ``EMAIL_DIGEST_LOOKBACK_HOURS`` and sends one grouped email per
user, then stamps ``notifications.emailed_at`` so the same row is never
re-sent.

A few design properties worth knowing:

* **Idempotent.** Rows are selected on ``emailed_at IS NULL`` and stamped after
  a successful send, so re-running (or running twice in a day) never
  double-sends. The actual claim uses a guarded UPDATE rather than SELECT FOR
  UPDATE, so it works correctly on SQLite too (see ``run_digest`` for details).
* **Bounded.** The ``created_at >= now - lookback`` window prevents the first
  run after enabling the feature from replaying the entire notification history.
  Older rows age out of the window instead of flooding inboxes.
* **Role-scoped.** Notification rows only exist for users already entitled to
  see the operation, so the digest inherits that scoping with no extra checks.
* **Security emails are unaffected.** Password-reset and other transactional
  emails never become notification rows and always send immediately.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import email_service
from app.config import get_settings
from app.database import SessionLocal
from app.models import Notification, User, _utcnow

logger = logging.getLogger("bug_hunter.digest")

# Display order and labels for the five notification kinds, matching the in-app bell.
_CATEGORY_ORDER: tuple[tuple[str, str], ...] = (
    ("assigned", "🎯 Assigned to you"),
    ("reported", "🆕 Reported by / with you"),
    ("updated", "✏️ Updates"),
    ("comment", "💬 Comments"),
    ("event", "📅 Events"),
)
_KNOWN_KINDS = frozenset(kind for kind, _ in _CATEGORY_ORDER)


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def _link(base: str, row: Notification) -> str:
    """Deep-link to the work item or event the operation refers to."""
    if row.bug_id:
        return f"{base}/#bug={row.bug_id}"
    if row.event_id:
        return f"{base}/#event={row.event_id}"
    return base


def _section_lines(label: str, rows: list[Notification], base: str) -> list[str]:
    """Render one category section: a header + one bullet per operation."""
    lines = [f"{label} ({len(rows)})"]
    for row in rows:
        lines.append(f"  • {row.title}")
        if row.body:
            lines.append(f"    {row.body}")
        lines.append(f"    {_link(base, row)}")
    lines.append("")
    return lines


def render_digest(user: User, rows: list[Notification]) -> tuple[str, str]:
    """Build the (subject, body) for one user's batched operations."""
    settings = get_settings()
    base = settings.APP_BASE_URL.rstrip("/")
    by_kind: dict[str, list[Notification]] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)

    total = len(rows)
    body_lines = [
        f"Hi {user.name or 'there'},",
        "",
        f"Here's your {settings.APP_NAME} activity digest — {total} update{_plural(total)} "
        "since the last one.",
        "",
    ]
    for kind, label in _CATEGORY_ORDER:
        section = by_kind.get(kind)
        if section:
            body_lines += _section_lines(label, section, base)

    # Surface any kind without a known category rather than dropping it.
    other = [r for r in rows if r.kind not in _KNOWN_KINDS]
    if other:
        body_lines += _section_lines("📌 Other", other, base)

    body_lines.append(f"— {settings.APP_NAME}")
    subject = f"[{settings.APP_NAME}] Your activity digest — {total} update{_plural(total)}"
    return subject, "\n".join(body_lines)


def _group_by_user(rows: list[Notification]) -> dict[int, list[Notification]]:
    grouped: dict[int, list[Notification]] = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row)
    return grouped


def run_digest(
    db: Session,
    *,
    now: datetime | None = None,
    lookback_hours: int | None = None,
) -> dict[str, int]:
    """Send one digest email per user for their un-emailed operations.

    Selects ``emailed_at IS NULL`` rows created within the lookback window,
    groups them by user, emails each deliverable user once, and stamps the
    sent rows. When the backend reports a failed send, that user's rows are
    released (emailed_at cleared) so the next scheduled run retries them.
    Returns ``{"users", "emails_sent", "operations", "failed"}`` for logging
    and tests; ``emails_sent`` counts emails handed to the backend and
    ``failed`` counts those the backend reported as undelivered.
    """
    settings = get_settings()
    now = now or _utcnow()
    if lookback_hours is None:
        lookback_hours = settings.EMAIL_DIGEST_LOOKBACK_HOURS
    cutoff = now - timedelta(hours=lookback_hours)

    rows = list(db.scalars(
        select(Notification)
        .where(Notification.emailed_at.is_(None), Notification.created_at >= cutoff)
        .order_by(Notification.user_id, Notification.created_at)
    ).all())
    if not rows:
        return {"users": 0, "emails_sent": 0, "operations": 0, "failed": 0}

    by_user = _group_by_user(rows)
    # Claim rows with a guarded UPDATE (emailed_at IS NULL) rather than SELECT
    # FOR UPDATE, which SQLite silently ignores. This way two concurrent runs
    # can't both claim and send the same rows — the loser sees rowcount 0 and
    # skips. We commit the claims before the SMTP loop so a slow mail server
    # can't hold a DB connection for the entire batch.
    outbox: list[tuple[str, str, str, list[int]]] = []  # (email, subject, body, row ids)
    operations = 0
    for user_id, user_rows in by_user.items():
        user = db.get(User, user_id)
        if user is None or not user.is_active or not user.email:
            # No deliverable address; leave rows un-stamped so they stay
            # visible in-app and age out of the window naturally.
            continue
        ids = [r.id for r in user_rows]
        claimed = (
            db.query(Notification)
            .filter(Notification.id.in_(ids), Notification.emailed_at.is_(None))
            .update({Notification.emailed_at: now}, synchronize_session=False)
        )
        if not claimed:
            # Another runner already claimed these rows.
            continue
        subject, body = render_digest(user, user_rows)
        outbox.append((user.email, subject, body, ids))
        operations += claimed

    db.commit()  # claimed rows are now stamped (at-most-once)

    # Deliver outside the transaction (no DB locks across network I/O). When
    # the backend reports a failed send (deliver() returns False — e.g. SMTP
    # down or rejecting at exactly this moment), release that user's rows by
    # clearing emailed_at so the next scheduled run retries them instead of
    # silently losing the day's digest. At-most-once still holds: rows are only
    # released when nothing was sent. A disabled backend also returns False,
    # but that is an operator choice, not a failure — those rows stay stamped
    # (same pattern as notify_password_reset). A crash mid-loop still drops the
    # remaining batch (already stamped), which beats double-sending.
    failed = 0
    failed_ids: list[int] = []
    for email, subject, body, ids in outbox:
        if email_service.deliver(subject, [email], body):
            continue
        if settings.EMAIL_BACKEND == "disabled":
            continue
        failed += 1
        failed_ids.extend(ids)
    if failed_ids:
        db.query(Notification).filter(Notification.id.in_(failed_ids)).update(
            {Notification.emailed_at: None}, synchronize_session=False
        )
        db.commit()
        logger.error(
            "Digest delivery FAILED for %d user(s) covering %d operation(s); "
            "their rows were released and will be retried on the next "
            "scheduled run (ensure EMAIL_DIGEST_LOOKBACK_HOURS covers the gap).",
            failed, len(failed_ids),
        )

    return {
        "users": len(by_user),
        "emails_sent": len(outbox),
        "operations": operations,
        "failed": failed,
    }


def main() -> int:
    """Entry point for ``python -m app.jobs.email_digest``."""
    settings = get_settings()
    if not settings.EMAIL_DIGEST_ENABLED:
        logger.info(
            "EMAIL_DIGEST_ENABLED is off — digest job is a no-op "
            "(work-item emails are sending immediately)."
        )
        return 0
    db = SessionLocal()
    try:
        stats = run_digest(db)
        logger.info(
            "Email digest complete: %s email(s) sent covering %s operation(s) "
            "across %s user(s).",
            stats["emails_sent"], stats["operations"], stats["users"],
        )
        return 0
    except Exception:
        # Broad catch so any failure becomes a non-zero exit code that a cron
        # wrapper can detect, rather than an uncaught traceback.
        logger.exception("Email digest job failed.")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(main())
