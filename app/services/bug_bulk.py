"""Bulk actions on work items."""

from __future__ import annotations

from typing import Optional

from fastapi import (
    BackgroundTasks,
    HTTPException,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agile.workflow import record_status_change
from app.auth import (
    can_delete_bug,
    can_edit_bug,
)
from app.models import (
    Activity,
    Bug,
    User,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    BulkActionIn,
    normalize_choice,
    statuses_for_type,
)
from app.services.audit import audit
from app.services.bug_notifications import (
    bulk_notify_update,
    notify_item_stakeholders,
)
from app.services.work_items import (
    log_activity,
    work_item_type,
)


# Bulk actions. Each item goes through the same permission, audit, and
# notification path as its single-item endpoint. Items the caller can't touch
# are skipped so a mixed selection partially succeeds.
def norm_or_400(value: Optional[str], allowed: list[str], label: str) -> str:
    try:
        return normalize_choice(value or "", allowed, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def bulk_resolve_value(payload: BulkActionIn):
    """Validate and canonicalize the action's value once, so a bad value gives
    a single 400 for the whole request. Returns None for delete."""
    action = payload.action
    if action == "set_status":
        return norm_or_400(payload.value, ALLOWED_STATUSES, "status")
    if action == "set_priority":
        return norm_or_400(payload.value, ALLOWED_PRIORITIES, "priority")
    if action == "set_environment":
        return norm_or_400(payload.value, ALLOWED_ENVIRONMENTS, "environment")
    return None  # delete


def bulk_set_status(db: Session, bug: Bug, actor: User, value: str,
                     background: BackgroundTasks) -> str:
    # A status valid for Bug may be invalid for Task; skip if so.
    if value not in statuses_for_type(work_item_type(bug)):
        return "skipped"
    if bug.status == value:
        return "skipped"
    old = bug.status
    bug.status = value
    record_status_change(db, bug, actor, old)
    # Bump the version so a later expected_version PUT can detect this change.
    bug.version = (bug.version or 1) + 1
    log_activity(db, bug.id, actor, "status_changed",
         f"#{bug.id} '{bug.title}' — status: '{old}' → '{value}' (bulk)")
    bulk_notify_update(db, bug, actor, "status", background)
    return "updated"


def bulk_set_field(db: Session, bug: Bug, actor: User, field_name: str,
                    value: str, background: BackgroundTasks) -> str:
    old = getattr(bug, field_name)
    if old == value:
        return "skipped"
    setattr(bug, field_name, value)
    bug.version = (bug.version or 1) + 1
    log_activity(db, bug.id, actor, f"{field_name}_changed",
         f"#{bug.id} '{bug.title}' — {field_name}: '{old}' → '{value}' (bulk)")
    bulk_notify_update(db, bug, actor, field_name, background)
    return "updated"


def delete_item(db: Session, bug: Bug, actor: User, note: str = "") -> int:
    """Delete ``bug`` and record it in the audit trail.

    Its Sub-tasks go with it (a Sub-task cannot exist without its parent; the
    FK alone would leave them parentless), and an Epic's issues are unlinked
    through the ORM so the change log records it. Audit history is detached
    first so the trail survives: entity_id and detail still carry the id and
    title. Returns how many Sub-tasks were deleted with it.
    """
    bug_id = bug.id
    subtasks = list(db.scalars(
        select(Bug).where(Bug.parent_id == bug_id, Bug.item_type == "Sub-task")
    ).all())
    if bug.item_type == "Epic":
        for issue in db.scalars(select(Bug).where(Bug.epic_id == bug_id, Bug.item_type != "Sub-task")).all():
            issue.epic_id = None
    doomed = [*subtasks, bug]
    db.execute(
        update(Activity)
        .where(Activity.bug_id.in_([d.id for d in doomed]))
        .values(bug_id=None)
    )
    db.flush()
    for item in doomed:
        db.delete(item)
        audit(db, actor, "bug", item.id, "bug_deleted", (
            f"Deleted {work_item_type(item).lower()} #{item.id}: {item.title}"
            + (f" (sub-task of #{bug_id})" if item is not bug else "")
            + note
        ))
    return len(subtasks)


def bulk_delete(db: Session, bug: Bug, actor: User, background: BackgroundTasks) -> None:
    # Same pattern as delete_bug: notify first (link_bug=False, since the FK
    # would cascade the notification away), detach audit history, delete, then
    # record the delete in the trail.
    title, itype, bug_id = bug.title, work_item_type(bug), bug.id
    if bug in db.deleted or sa_inspect(bug).was_deleted:
        # Already removed with its parent earlier in this selection.
        return
    notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background, link_bug=False,
        title=f"{itype} #{bug_id} deleted",
        body=f"{actor.name} deleted “{title}”.",
    )
    delete_item(db, bug, actor, note=" (bulk)")


def bulk_version_conflict(bug: Bug, payload: BulkActionIn) -> bool:
    """Return True if the caller sent an expected version for this bug and it
    no longer matches, meaning a concurrent edit landed in between."""
    if payload.expected_versions is None:
        return False
    expected = payload.expected_versions.get(bug.id)
    return expected is not None and bug.version != expected


def apply_bulk_to_bug(db: Session, bug: Bug, actor: User, payload: BulkActionIn,
                       resolved, background: BackgroundTasks) -> str:
    """Apply one bulk action to one bug. Returns 'updated', 'skipped', or
    'conflict'. Permission failures become 'skipped' so a mixed selection
    partially succeeds."""
    if bulk_version_conflict(bug, payload):
        return "conflict"
    action = payload.action
    if action == "delete":
        if not can_delete_bug(actor):
            return "skipped"
        bulk_delete(db, bug, actor, background)
        return "updated"
    if not can_edit_bug(actor, item_type=work_item_type(bug)):
        return "skipped"
    if action == "set_status":
        return bulk_set_status(db, bug, actor, resolved, background)
    if action == "set_priority":
        return bulk_set_field(db, bug, actor, "priority", resolved, background)
    return bulk_set_field(db, bug, actor, "environment", resolved, background)
