"""In-app and email notifications for work-item changes."""

from __future__ import annotations

from fastapi import (
    BackgroundTasks,
)
from sqlalchemy.orm import Session

from app import notification_service
from app.email_service import (
    BugSnapshot,
    UserSnapshot,
    notify_assignment,
    notify_bug_updated,
)
from app.models import (
    Bug,
    User,
)
from app.services.work_items import work_item_type


def stage_update_notifications(
    db: Session, bug: Bug,
    changes: list[tuple[str, str, str]],
    newly_assigned: list[User], newly_removed: list[User],
    actor: User, background: BackgroundTasks,
) -> None:
    """Stage in-app notification rows on the current session (no commit).
    They commit atomically with the change in _persist_update, so a crash
    between two commits cannot persist the change without its notifications."""
    new_ids = {u.id for u in newly_assigned}
    itype = work_item_type(bug).lower()
    # _apply_reporter_change and _apply_assignee_diff always append to `changes`
    # whenever they return a non-empty diff, so given the caller's guard (`if
    # changes or newly_assigned or newly_removed`), changes is never empty here.
    if changes:  # pragma: no branch
        recipients = [
            uid for uid in [bug.reporter_id, *[a.id for a in bug.assignees]]
            if uid not in new_ids
        ]
        notification_service.notify(
            db, recipients, kind="updated", background=background,
            title=f"{itype.capitalize()} #{bug.id} updated",
            body=f"{actor.name} changed " + ", ".join(f for f, _, _ in changes) + ".",
            bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
        )
    if newly_assigned:
        # No exclude: a self-assignment still notifies the actor.
        notification_service.notify(
            db, list(new_ids), kind="assigned", background=background,
            title=f"Assigned to {itype} #{bug.id}",
            body=f"{actor.name} assigned you to “{bug.title}”.",
            bug_id=bug.id, actor_name=actor.name,
        )
    if newly_removed:
        notification_service.notify(
            db, [u.id for u in newly_removed], kind="updated", background=background,
            title=f"Unassigned from {itype} #{bug.id}",
            body=f"{actor.name} removed you from “{bug.title}”.",
            bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
        )


def schedule_update_notifications(background: BackgroundTasks, snap: BugSnapshot,
                                   changes: list[tuple[str, str, str]],
                                   newly_assigned: list[User], actor: User) -> None:
    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor.name, actor.id,
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor.name,
        )


# Shared stakeholder notification for delete, attachments, comment edits, and
# links. The create/update paths build their own tailored notifications; these
# secondary operations all go through one helper so every mutation reaches the
# reporter + assignees and feeds the email digest.
def notify_item_stakeholders(
    db: Session, bug: Bug, actor: User, *, kind: str, title: str, body: str,
    background: "BackgroundTasks | None" = None, link_bug: bool = True,
) -> None:
    """Notify a work item's reporter and assignees, excluding the actor.

    Rows are written on the caller's session; the caller commits.

    Pass link_bug=False when deleting: the bug_id FK cascades on delete, so a
    notification that deep-links to the bug would vanish before the digest
    could send it.
    """
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind=kind, background=background, title=title, body=body,
        bug_id=bug.id if link_bug else None,
        actor_name=actor.name, exclude=actor.id,
    )


def link_stakeholders(bug: Bug) -> list[int | None]:
    """Reporter + assignee ids for one end of a link."""
    return [bug.reporter_id, *[a.id for a in bug.assignees]]


def notify_link_endpoints(
    db: Session, recipients: list[int | None], primary_id: int, actor: User, *,
    background: "BackgroundTasks | None", title: str, body: str,
) -> None:
    """Notify the reporters + assignees of both link endpoints. notify()
    deduplicates ids and drops the actor, so a user on both ends gets one
    notification. Deep-links to primary_id (the item the request was on)."""
    notification_service.notify(
        db, recipients, kind="updated", background=background,
        title=title, body=body, bug_id=primary_id,
        actor_name=actor.name, exclude=actor.id,
    )


def bulk_notify_update(db: Session, bug: Bug, actor: User, what: str,
                        background: BackgroundTasks) -> None:
    _itype = work_item_type(bug).lower()
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind="updated", background=background,
        title=f"{_itype.capitalize()} #{bug.id} updated",
        body=f"{actor.name} changed {what} (bulk).",
        bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
    )
