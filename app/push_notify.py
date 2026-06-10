"""Push-notification sibling of `email_service.py`.

Mirrors every email notifier with a `push_*` function so a route handler
can fire both with parallel `background.add_task(...)` lines.

Design notes:
  - Each push function takes a SQLAlchemy `Session`. The session must be
    valid for the duration of the call. Since these run as FastAPI
    background tasks, the route is responsible for passing a session
    that outlives the request (or for letting FastAPI open one via
    Depends). We follow the existing pattern in webhooks_delivery.py.
  - Recipient resolution uses the same snapshot dataclasses as the email
    side. UserSnapshot already carries `id`, so we don't need to query
    extra rows here.
  - Channels mirror the email-side intent:
        notify_assignment        → 'assignments'  (HIGH on Android)
        notify_comment (mention) → 'mentions'     (HIGH)
        notify_comment (normal)  → 'activity'     (LOW)
        notify_bug_*             → 'activity'
        notify_event_*           → 'activity'
  - Deep links use `app://bughunter/<entity>/<id>` so the Android
    NavHost picks them up via the existing `app://bughunter` scheme.

Skipped on purpose (same rationale as the plan):
  - password reset / email-change code / invitation — security or
    inviter-not-yet-installed flows; email-only is the right channel.
"""
from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Iterable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.email_service import BugSnapshot, EventSnapshot, UserSnapshot
from app.push_service import (
    CHANNEL_ACTIVITY,
    CHANNEL_ASSIGNMENTS,
    CHANNEL_MENTIONS,
    send_to_user,
    send_to_users,
)

logger = logging.getLogger("bug_hunter.push.notify")


@contextmanager
def _session():
    """Open a fresh session for the duration of a background-task call.

    FastAPI's `BackgroundTasks` fires its callables AFTER the response is
    sent, by which point the request's `Depends(get_db)` session has
    already been closed. Each push notifier owns its own session — same
    pattern the webhook deliverer uses (`app/webhooks_delivery.py`).
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Inline @mention regex. Mirrors what the SPA renderer supports:
# @ followed by 1-30 chars from a conservative alphanumeric/._- set.
# We never trust this list against User.name directly; the
# notification-targeting code resolves mentions by user_id elsewhere.
# This regex only powers the "any @ in the body?" channel-upgrade
# heuristic — pessimistic by design, so a body with "@anything" pushes
# to the mentions channel rather than the activity channel.
_MENTION_RE = re.compile(r"@[A-Za-z0-9._-]{1,30}")


def _item_noun(bug: BugSnapshot) -> str:
    return (getattr(bug, "item_type", None) or "Bug").lower()


def _bug_deep_link(bug_id: int) -> str:
    return f"app://bughunter/bug/{bug_id}"


def _event_deep_link(event_id: int) -> str:
    return f"app://bughunter/event/{event_id}"


def _bug_tag(bug_id: int) -> str:
    # Same `tag` for every push about the same bug so the latest replaces
    # older ones in the system tray — five status changes don't pile up.
    return f"bug:{bug_id}"


def _event_tag(event_id: int) -> str:
    return f"event:{event_id}"


def _recipient_ids(
    bug: BugSnapshot, exclude_user_id: int | None,
) -> list[int]:
    """Reporter + assignees, deduped, minus the actor. Mirrors the email
    side's recipient logic but returns user_ids instead of emails."""
    seen: set[int] = set()
    ordered: list[int] = []
    candidates: list[UserSnapshot] = []
    if bug.reporter:
        candidates.append(bug.reporter)
    candidates.extend(bug.assignees)
    for u in candidates:
        if exclude_user_id is not None and u.id == exclude_user_id:
            continue
        if u.id in seen:
            continue
        seen.add(u.id)
        ordered.append(u.id)
    return ordered


def _event_recipient_ids(
    ev: EventSnapshot, exclude_user_id: int | None,
) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for m in ev.managers:
        if exclude_user_id is not None and m.id == exclude_user_id:
            continue
        if m.id in seen:
            continue
        seen.add(m.id)
        ordered.append(m.id)
    return ordered


def _safe(fn, *args, **kwargs) -> None:
    """Push notifiers must never raise into the FastAPI background-task
    runner — a failure should be logged and dropped, not surfaced as a
    500-after-200. Wraps every public call in a broad except."""
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — explicit broad swallow
        logger.exception("push_notify call failed")


# ---------------------------------------------------------------------------
# Bug events
# ---------------------------------------------------------------------------
def push_bug_created(bug: BugSnapshot, actor_user_id: int | None) -> None:
    recipients = _recipient_ids(bug, exclude_user_id=actor_user_id)
    if not recipients:
        return
    noun = _item_noun(bug)
    with _session() as db:
        _safe(
            send_to_users,
            db, recipients,
            title=f"New {noun}: {bug.title}",
            body=f"Project: {bug.project_name} · Priority: {bug.priority}",
            channel=CHANNEL_ACTIVITY,
            deep_link=_bug_deep_link(bug.id),
            tag=_bug_tag(bug.id),
        )


def push_bug_updated(
    bug: BugSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if not changes:
        return
    recipients = _recipient_ids(bug, exclude_user_id=actor_user_id)
    if not recipients:
        return
    noun = _item_noun(bug)
    # Summarise the diff in one line. The full diff is in the email; the
    # push is meant to nudge people back into the app.
    fields = ", ".join(field for field, _old, _new in changes[:3])
    if len(changes) > 3:
        fields += f", +{len(changes) - 3} more"
    with _session() as db:
        _safe(
            send_to_users,
            db, recipients,
            title=f"{noun.capitalize()} #{bug.id} updated",
            body=f"{actor_name} changed {fields}.",
            channel=CHANNEL_ACTIVITY,
            deep_link=_bug_deep_link(bug.id),
            tag=_bug_tag(bug.id),
        )


def push_assignment(
    bug: BugSnapshot,
    newly_assigned: Iterable[UserSnapshot],
    actor_name: str,
) -> None:
    """Assignments push on the high-importance assignments channel."""
    noun = _item_noun(bug)
    with _session() as db:
        for user in newly_assigned:
            _safe(
                send_to_user,
                db, user.id,
                title=f"Assigned to you: {bug.title}",
                body=f"{actor_name} assigned you a {noun}. Project: {bug.project_name}",
                channel=CHANNEL_ASSIGNMENTS,
                deep_link=_bug_deep_link(bug.id),
                tag=_bug_tag(bug.id),
            )


def push_comment_added(
    bug: BugSnapshot,
    comment_author_name: str,
    comment_author_id: int | None,
    comment_body: str,
) -> None:
    recipients = _recipient_ids(bug, exclude_user_id=comment_author_id)
    if not recipients:
        return
    noun = _item_noun(bug)
    # Heuristic channel pick: any @ in the body → bump to mentions for
    # everybody (we don't have a per-recipient mention resolver in the
    # snapshot). Without an @ we stay on activity. A future targeted
    # @mention resolver can replace this with a per-user channel pick.
    is_mention = bool(_MENTION_RE.search(comment_body or ""))
    channel = CHANNEL_MENTIONS if is_mention else CHANNEL_ACTIVITY
    # Trim to one-line preview. Notifications get truncated by Android
    # anyway; doing it here keeps the wire payload small and avoids
    # leaking obviously-truncated HTML markers if a user pastes rich
    # text. Strip leading whitespace + newlines.
    preview = (comment_body or "").strip().splitlines()[0] if comment_body else ""
    if len(preview) > 140:
        preview = preview[:139] + "…"
    with _session() as db:
        _safe(
            send_to_users,
            db, recipients,
            title=f"{comment_author_name} commented on {noun} #{bug.id}",
            body=preview or f"View the comment on {bug.title}",
            channel=channel,
            deep_link=_bug_deep_link(bug.id),
            tag=_bug_tag(bug.id),
        )


# ---------------------------------------------------------------------------
# Event events
# ---------------------------------------------------------------------------
def push_event_created(
    ev: EventSnapshot, actor_name: str, actor_user_id: int | None,
) -> None:
    recipients = _event_recipient_ids(ev, exclude_user_id=actor_user_id)
    if not recipients:
        return
    with _session() as db:
        _safe(
            send_to_users,
            db, recipients,
            title=f"New event: {ev.name}",
            body=f"{actor_name} created an event.",
            channel=CHANNEL_ACTIVITY,
            deep_link=_event_deep_link(ev.id),
            tag=_event_tag(ev.id),
        )


def push_event_updated(
    ev: EventSnapshot,
    changes: list[tuple[str, str, str]],
    actor_name: str,
    actor_user_id: int | None,
) -> None:
    if not changes:
        return
    recipients = _event_recipient_ids(ev, exclude_user_id=actor_user_id)
    if not recipients:
        return
    fields = ", ".join(field for field, _o, _n in changes[:3])
    if len(changes) > 3:
        fields += f", +{len(changes) - 3} more"
    with _session() as db:
        _safe(
            send_to_users,
            db, recipients,
            title=f"Event updated: {ev.name}",
            body=f"{actor_name} changed {fields}.",
            channel=CHANNEL_ACTIVITY,
            deep_link=_event_deep_link(ev.id),
            tag=_event_tag(ev.id),
        )


def push_event_deleted(
    ev: EventSnapshot, actor_name: str, actor_user_id: int | None,
) -> None:
    recipients = _event_recipient_ids(ev, exclude_user_id=actor_user_id)
    if not recipients:
        return
    with _session() as db:
        _safe(
            send_to_users,
            db, recipients,
            title=f"Event deleted: {ev.name}",
            body=f"{actor_name} deleted this event. Items remain.",
            channel=CHANNEL_ACTIVITY,
            # No deep link — the event no longer exists, would land on a
            # 404 screen. The user can find the items via the Bugs list.
            deep_link=None,
            tag=_event_tag(ev.id),
        )
