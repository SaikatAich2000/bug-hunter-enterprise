"""Per-user in-app notifications, the counterpart to app/email_service.py.

Rows are written synchronously on the request's Session and committed with it,
so they're transactional with the change that caused them. Recipients match the
email layer (reporter + assignees minus actor, event managers), so a row is
only ever created for a user entitled to know.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Notification, _utcnow

if TYPE_CHECKING:  # avoid importing FastAPI just for the type hint
    from fastapi import BackgroundTasks


def notify(
    db: Session,
    user_ids: Iterable[int | None],
    *,
    kind: str,
    title: str,
    body: str = "",
    bug_id: int | None = None,
    event_id: int | None = None,
    actor_name: str = "",
    exclude: int | None = None,
    background: "BackgroundTasks | None" = None,
) -> list[int]:
    """Queue an in-app notification per distinct recipient; with a ``background``
    runner, also schedule an immediate web push to them.

    Rows are added but not committed (caller commits). None ids, ``exclude`` (the
    actor), and duplicates are skipped. Returns the queued recipient ids. Push
    fires regardless of the email-digest setting.
    """
    # Immediate-email mode stamps emailed_at now so a later switch to digest mode
    # never re-sends; digest mode leaves it NULL for the daily job to pick up once.
    emailed_at = None if get_settings().EMAIL_DIGEST_ENABLED else _utcnow()

    seen: set[int] = set()
    recipients: list[int] = []
    for uid in user_ids:
        if uid is None or uid == exclude or uid in seen:
            continue
        seen.add(uid)
        recipients.append(uid)
        db.add(Notification(
            user_id=uid,
            kind=kind,
            title=title,
            body=body,
            bug_id=bug_id,
            event_id=event_id,
            actor_name=actor_name,
            emailed_at=emailed_at,
        ))

    if background is not None and recipients:
        # Lazy import: keeps push/FCM deps out for callers that never push.
        from app import push_service
        push_service.schedule(
            background, recipients, title=title, body=body,
            bug_id=bug_id, event_id=event_id,
            channel=push_service.channel_for(kind, body),
        )
    return recipients
