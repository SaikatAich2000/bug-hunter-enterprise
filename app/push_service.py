"""Web push orchestration: FCM token storage, recipient selection, and pruning
dead tokens. Fans notifications to all of a user's devices immediately (never
digested). The FCM network call lives in ``app.fcm_transport`` (mocked in tests).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import fcm_transport
from app.config import get_settings
from app.database import SessionLocal
from app.models import NotificationPreference, PushSubscription, _utcnow

# Push channels a user can switch off (NotificationPreference columns of the same name).
CHANNEL_MENTIONS = "mentions"
CHANNEL_ASSIGNMENTS = "assignments"
CHANNEL_ACTIVITY = "activity"


def channel_for(kind: str, body: str = "") -> str:
    """The push channel of a notification kind: assignments for "assigned", mentions for a
    comment that @-mentions someone, everything else is activity."""
    if kind == "assigned":
        return CHANNEL_ASSIGNMENTS
    if kind == "comment" and "@" in body:
        return CHANNEL_MENTIONS
    return CHANNEL_ACTIVITY

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

logger = logging.getLogger("bug_hunter.push")


class PushTokenConflict(Exception):
    """Subscribe presented a token already bound to a different user. Re-homing
    it would let a leaked FCM token hijack a victim's device push. Route → 409."""


def _deep_link(bug_id: int | None, event_id: int | None) -> str:
    """The in-app URL a push should open (matches the SPA's hash routes)."""
    if bug_id:
        return f"/#bug={bug_id}"
    if event_id:
        return f"/#event={event_id}"
    return "/"


def schedule(
    background: "BackgroundTasks",
    user_ids: Iterable[int | None],
    *,
    title: str,
    body: str,
    bug_id: int | None = None,
    event_id: int | None = None,
    channel: str = CHANNEL_ACTIVITY,
) -> None:
    """Schedule an immediate background push to ``user_ids``; no-op when web push
    is disabled."""
    if not get_settings().WEB_PUSH_ENABLED:
        logger.info("Push skipped: WEB_PUSH_ENABLED is off.")
        return
    ids = [uid for uid in user_ids if uid is not None]
    if not ids:
        return
    logger.info("Queuing push to user_ids=%s.", ids)
    background.add_task(
        push_to_users, ids, title=title, body=body,
        url=_deep_link(bug_id, event_id), channel=channel,
    )


def register(
    db: Session,
    *,
    user_id: int,
    token: str,
    platform: str = "web",
    user_agent: str = "",
) -> PushSubscription:
    """Upsert a device's FCM token for ``user_id``: refresh in place if already
    this user's, else raise ``PushTokenConflict``."""
    def _refresh(sub: PushSubscription) -> PushSubscription:
        sub.platform = platform or "web"
        sub.user_agent = (user_agent or "")[:400]
        sub.last_seen_at = _utcnow()
        return sub

    def _claim_or_conflict(sub: PushSubscription) -> PushSubscription:
        if sub.user_id != user_id:
            logger.warning(
                "Refused cross-user push-token rehome: token bound to user %s, "
                "subscribe attempted by user %s", sub.user_id, user_id,
            )
            raise PushTokenConflict()
        return _refresh(sub)

    existing = db.scalar(
        select(PushSubscription).where(PushSubscription.token == token)
    )
    if existing is not None:
        return _claim_or_conflict(existing)
    sub = PushSubscription(
        user_id=user_id,
        token=token,
        platform=platform or "web",
        user_agent=(user_agent or "")[:400],
    )
    db.add(sub)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent subscribe won the insert race; re-read and resolve.
        db.rollback()
        other = db.scalar(
            select(PushSubscription).where(PushSubscription.token == token)
        )
        if other is None:
            raise
        return _claim_or_conflict(other)
    return sub


def remove(db: Session, *, token: str, user_id: int | None = None) -> bool:
    """Delete a subscription by token (optionally user-scoped); True if removed."""
    query = select(PushSubscription).where(PushSubscription.token == token)
    if user_id is not None:
        query = query.where(PushSubscription.user_id == user_id)
    sub = db.scalar(query)
    if sub is None:
        return False
    db.delete(sub)
    return True


def _subs_for_users(db: Session, user_ids: Iterable[int]) -> list[PushSubscription]:
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return []
    return list(db.scalars(
        select(PushSubscription).where(PushSubscription.user_id.in_(ids))
    ).all())


def _opted_out(db: Session, user_ids: set[int], channel: str) -> set[int]:
    """Users who switched ``channel`` off (a user without a preference row has it on)."""
    column = getattr(NotificationPreference, channel, None)
    if column is None:
        return set()
    return set(db.scalars(
        select(NotificationPreference.user_id).where(
            NotificationPreference.user_id.in_(user_ids), column.is_(False),
        )
    ).all())


def push_to_users(
    user_ids: Iterable[int | None],
    *,
    title: str,
    body: str,
    url: str = "",
    channel: str = CHANNEL_ACTIVITY,
) -> int:
    """Push to every device of the given users who have ``channel`` on; returns tokens
    delivered to. Native Android devices get a data-only message, browsers a notification.

    Runs in a BackgroundTask after the request session closed, so it opens its
    own session. No-op (0) unless web push is enabled. Dead tokens are pruned.
    """
    if not get_settings().WEB_PUSH_ENABLED:
        return 0
    ids = [uid for uid in user_ids if uid is not None]
    if not ids:
        return 0

    db = SessionLocal()
    try:
        wanted = set(ids) - _opted_out(db, set(ids), channel)
        subs = _subs_for_users(db, wanted)
        if not subs:
            logger.info("Push skipped: no registered device for user_ids=%s.", ids)
            return 0
        tokens = [s.token for s in subs]
        native = [s.token for s in subs if s.platform == "android"]
        browser = [t for t in tokens if t not in set(native)]
        dead: set[str] = set()
        if browser:
            dead |= set(fcm_transport.send(browser, title=title, body=body, url=url, channel=channel))
        if native:
            dead |= set(fcm_transport.send(
                native, title=title, body=body, url=url, channel=channel, data_only=True,
            ))
        if dead:
            for sub in subs:
                if sub.token in dead:
                    db.delete(sub)
            db.commit()
        # Count by membership: FCM may report a dead token we don't have, which
        # len(tokens) - len(dead) would overstate.
        return sum(1 for t in tokens if t not in dead)
    except Exception:  # noqa: BLE001 — push must never break the request flow
        logger.exception("push_to_users failed")
        db.rollback()
        return 0
    finally:
        db.close()
