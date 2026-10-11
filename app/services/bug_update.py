"""Work-item update rules: validation, concurrency, change tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    HTTPException,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agile import sprints as sprints_svc
from app.agile.itemtypes import is_epic, is_subtask
from app.agile.permissions import MANAGE_BACKLOG, require_permission
from app.auth import (
    can_edit_bug,
)
from app.models import (
    Bug,
    Sprint,
    User,
    WorkItemBranch,
)
from app.people import reject_inactive, require_project_members, resolve_user, resolve_users
from app.schemas import (
    display_id_for,
    statuses_for_type,
)
from app.services.work_items import (
    assert_event_accessible,
    assert_project_accessible,
    log_activity,
    work_item_type,
)

# Item-type conversion errors (Phase 7 contract): stable codes carried in the
# X-Error-Code header and message fragments kept human-readable.
CONVERSION_NOT_SUPPORTED_CODE = "item_conversion_not_supported"

CONVERSION_BLOCKED_CODE = "item_conversion_blocked_by_active_branch"

VERSION_CONFLICT_CODE = "version_conflict"

# Same-row conversions supported through PUT /api/bugs/{id}. Story is included
# because a Story created through /api/agile/work-items lives in the same bugs
# table and must be able to move to/from the legacy types handled here.
CONVERTIBLE_ITEM_TYPES = ("Bug", "Requirement", "Task", "Story")

UPDATE_TRACKED_FIELDS = [
    "item_type", "status", "priority", "environment", "project_id",
    "due_date", "title", "description", "event_id", "sprint_id",
]


def conversion_refused(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


def validate_update_authorization(bug: Bug, actor: User) -> None:
    if not can_edit_bug(actor, item_type=work_item_type(bug)):
        # Regular users cannot edit Tasks or Requirements.
        noun = work_item_type(bug).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to edit this {noun}.",
        )


def authorize_item_type_change(fields: dict, bug: Bug, actor: User) -> None:
    """Guard against over-posting an item_type change. The earlier auth check
    only saw the current type, so without this a regular user could PUT
    {"item_type": "Task"} to convert a Bug into a type they can't edit.
    Same-table conversions among Bug/Requirement/Task/Story keep the row id and
    common fields, validate the status against the target type, and never delete
    a remote branch. A Story with an Active feature branch cannot convert away
    from Story (Deleted history does not block); converting a non-Story into a
    Story never touches existing branch rows.
    """
    new_type = fields.get("item_type")
    if new_type is None or new_type == work_item_type(bug):
        return
    if new_type not in CONVERTIBLE_ITEM_TYPES or work_item_type(bug) not in CONVERTIBLE_ITEM_TYPES:
        raise conversion_refused(
            422,
            CONVERSION_NOT_SUPPORTED_CODE,
            f"Cannot convert {work_item_type(bug)} to {new_type} via /api/bugs; use /api/agile/work-items",
        )
    if not can_edit_bug(actor, item_type=new_type):
        raise HTTPException(
            status_code=403,
            detail=(
                "You don't have permission to convert this item to a "
                f"{new_type.lower()}."
            ),
        )


def assert_no_active_branch_for_conversion(db: Session, bug: Bug) -> None:
    """An Active feature branch blocks moving a Story to another type.

    Deleted branch history never blocks. Nothing is deleted remotely: the
    operator removes the branch through the branch endpoint first.
    """
    row = db.scalar(
        select(WorkItemBranch.id)
        .where(
            WorkItemBranch.work_item_type == work_item_type(bug),
            WorkItemBranch.work_item_id == bug.id,
            WorkItemBranch.status == "Active",
        )
        .limit(1)
    )
    if row is not None:
        raise conversion_refused(
            409,
            CONVERSION_BLOCKED_CODE,
            "This User Story has an Active feature branch and cannot be converted "
            "to another type. Delete the branch first.",
        )


def conversion_change(fields: dict, bug: Bug) -> tuple[str, str] | None:
    """(from_type, to_type) when this payload reclassifies the item, else None."""
    new_type = fields.get("item_type")
    if new_type is None or new_type == work_item_type(bug):
        return None
    return work_item_type(bug), new_type


def audit_conversion(db: Session, bug: Bug, actor: User, conversion: tuple[str, str], action: str, detail: str = "") -> None:
    """Append one conversion audit row on the caller's session (caller commits)."""
    old_type, new_type = conversion
    suffix = f" — {detail}" if detail else ""
    log_activity(
        db, bug.id, actor, action,
        f"#{bug.id} '{bug.title}' — item_type: '{old_type}' → '{new_type}'{suffix}",
    )


def audit_conversion_failure(db: Session, bug: Bug, actor: User, conversion: tuple[str, str], exc: Exception) -> None:
    """Record a refused/failed conversion on a fresh transaction.

    The request's own work has been rolled back, so this is committed
    separately; a failure to audit must never mask the original error. Only the
    human-readable refusal message is stored — never a credential or payload.
    """
    detail = str(getattr(exc, "detail", "") or exc)[:300]
    try:
        db.rollback()
        audit_conversion(db, bug, actor, conversion, "conversion_failed", detail)
        db.commit()
    except Exception:  # pragma: no cover - audit must never break the response
        db.rollback()


def preserve_display_id_on_type_change(fields: dict, bug: Bug) -> None:
    """Freeze the current user-facing id before a reclassification.

    The display id is assigned once. Converting a Story into a Task keeps
    "USRSTR-<id>", so existing references and the branch rows keyed to the
    original type + numeric id stay valid.
    """
    new_type = fields.get("item_type")
    if new_type is None or new_type == work_item_type(bug):
        return
    if not (getattr(bug, "display_id", None) or "").strip():
        bug.display_id = display_id_for(work_item_type(bug), bug.id)


def normalize_update_event_id(fields: dict, db: Session, actor: User, accessible) -> None:
    if "event_id" in fields and fields["event_id"]:
        assert_event_accessible(db, actor, accessible, fields["event_id"])
    if "event_id" in fields and fields["event_id"] == 0:
        # Treat 0 as a "clear event" signal for clients that can't send null.
        fields["event_id"] = None


def validate_update_status(fields: dict, bug: Bug) -> None:
    """Per-type status validation. Pydantic only checks the union of all
    allowed statuses; this enforces the per-type subset.

    When item_type changes, the effective status is re-validated against the
    new type, so converting a Resolved Bug into a Task can't leave it stuck on
    a Bug-only status. An unchanged status is tolerated only when the type
    isn't also changing."""
    new_type = fields.get("item_type")
    type_changing = new_type is not None and new_type != work_item_type(bug)
    has_new_status = "status" in fields and fields["status"] is not None
    if not has_new_status and not type_changing:
        return
    effective_type = new_type or work_item_type(bug)
    effective_status = fields["status"] if has_new_status else bug.status
    allowed_for_type = statuses_for_type(effective_type)
    if effective_status in allowed_for_type:
        return
    # Tolerate an unchanged status only when the type isn't changing; a type
    # conversion must land on a valid status for the target type.
    if not type_changing and has_new_status and effective_status == bug.status:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Status '{effective_status}' is not valid for "
            f"{effective_type}. Allowed: {', '.join(allowed_for_type)}"
        ),
    )


def validate_update_payload(
    fields: dict, bug: Bug, db: Session, actor: User, accessible,
) -> None:
    if "project_id" in fields and fields["project_id"] is not None:
        assert_project_accessible(db, accessible, fields["project_id"])
    normalize_update_event_id(fields, db, actor, accessible)
    validate_update_status(fields, bug)
    if "sprint_id" in fields and fields["sprint_id"] is not None:
        # Validate against the project the item will end up in.
        target_project_id = fields.get("project_id") or bug.project_id
        sprint = db.get(Sprint, fields["sprint_id"])
        if sprint is None or sprint.project_id != target_project_id or sprint.state in ("closed", "cancelled"):
            raise HTTPException(status_code=422, detail="sprint_id must reference an open Sprint in this project")


def is_project_move(fields: dict, bug: Bug) -> bool:
    new_pid = fields.get("project_id")
    return new_pid is not None and new_pid != bug.project_id


def detach_for_project_move(db: Session, bug: Bug, fields: dict, actor: User) -> None:
    """Sprints, backlog ranks, epic links, components and labels are
    project-scoped, so an issue moving to another project leaves them behind;
    its Sub-tasks move with it
    (app/agile/integrity.py). An Epic that still has issues, or a Sub-task
    (which belongs to its parent's project), cannot move on its own."""
    if not is_project_move(fields, bug):
        return
    if is_subtask(bug.item_type):
        raise HTTPException(
            status_code=409,
            detail="A Sub-task stays in its parent's project; move the parent issue instead.",
        )
    if is_epic(bug.item_type) and db.scalar(select(Bug.id).where(Bug.epic_id == bug.id).limit(1)):
        raise HTTPException(
            status_code=409,
            detail="This Epic still has issues; move or unlink them before changing its project.",
        )
    if bug.sprint_id is not None:
        sprint = db.get(Sprint, bug.sprint_id)
        if sprint is not None and sprint.state in ("future", "active"):
            # Taking an issue out of a planned or running sprint is backlog
            # management, whichever field the request changes.
            require_permission(actor, MANAGE_BACKLOG)
            sprints_svc.remove_item_from_sprint(db, sprint, bug.id, actor)
        bug.sprint_id = None
    bug.parent_id = bug.epic_id = None
    # Components and labels are per project: they stay behind too.
    from app.agile.taxonomy import detach_project_taxonomy

    subtask_ids = list(db.scalars(select(Bug.id).where(Bug.parent_id == bug.id)).all())
    detach_project_taxonomy(db, [bug.id, *subtask_ids])


def apply_sprint_change(db: Session, bug: Bug, new_sprint_id, actor: User) -> None:
    """Move the issue between sprints through the sprint service, so its rank
    follows it into the right list and an active sprint records the change.
    Sub-tasks follow their parent and Epics are never planned: both refused."""
    if new_sprint_id == bug.sprint_id:
        return
    target = db.get(Sprint, new_sprint_id) if new_sprint_id is not None else None
    try:
        sprints_svc.move_items(db, bug.project_id, [bug.id], target, actor)
    except sprints_svc.SprintLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def stale_edit_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=("This item was changed by someone else since you opened it. "
                "Reload to see the latest, then reapply your change."),
        headers={"X-Error-Code": VERSION_CONFLICT_CODE},
    )


def reject_if_updated_at_drifted(
    expected_updated_at: str, current_updated: datetime,
) -> None:
    """Timestamp path of the optimistic-concurrency check. Unparseable input
    returns 400 so a client that opts in gets a clear signal. current_updated
    comes from a NOT NULL column, so it's always present."""
    try:
        seen = datetime.fromisoformat(str(expected_updated_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="expected_updated_at is not a valid ISO-8601 timestamp.",
        ) from exc
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    current = current_updated
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    # updated_at is whole-second resolution, so compare at second granularity.
    if int(seen.timestamp()) != int(current.timestamp()):
        raise stale_edit_conflict()


def enforce_optimistic_concurrency(
    db: Session, bug_id: int,
    expected_version: Optional[int],
    expected_updated_at: Optional[str],
) -> None:
    """Opt-in optimistic concurrency check.

    Locks the row (FOR UPDATE) so concurrent writers serialize, then rejects
    with 409 when the client's last-seen version (preferred, sub-second safe)
    or updated_at (whole-second) no longer matches. Does nothing when neither
    field is sent, preserving last-write-wins behavior.
    """
    if expected_version is None and not expected_updated_at:
        return
    locked = db.execute(
        select(Bug.version, Bug.updated_at)
        .where(Bug.id == bug_id)
        .with_for_update()
    ).first()
    if locked is None:
        # Row vanished under a concurrent delete; the caller's load will 404.
        return
    current_version, current_updated = locked
    if expected_version is not None:
        if int(expected_version) != int(current_version):
            raise stale_edit_conflict()
        return
    reject_if_updated_at_drifted(expected_updated_at, current_updated)


def compute_tracked_changes(bug: Bug, fields: dict) -> list[tuple[str, str, str]]:
    """Return (field, old, new) for each tracked field that changed."""
    changes: list[tuple[str, str, str]] = []
    for f in UPDATE_TRACKED_FIELDS:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))
    return changes


def apply_reporter_change(bug: Bug, db: Session, org_id: int, new_reporter_id: Optional[int],
                           changes: list[tuple[str, str, str]]) -> None:
    """Swap the reporter and record the change.
    Only called when the reporter id actually changed (see
    reporter_actually_changes), so this must always append to `changes` — even
    when two different users share a display name — or persist_update treats
    an empty `changes` list as a no-op and rolls back the FK write along with it.
    Permission is checked by the caller.
    """
    old_reporter = bug.reporter
    old_reporter_label = old_reporter.name if old_reporter else "—"
    new_reporter = None
    if new_reporter_id is None:
        bug.reporter_id = None
        new_reporter_label = "—"
    else:
        new_reporter = resolve_user(db, new_reporter_id, org_id)
        reject_inactive([new_reporter], "reporter")
        bug.reporter_id = new_reporter.id
        new_reporter_label = new_reporter.name if new_reporter else "—"
    if old_reporter_label == new_reporter_label and old_reporter_label != "—":
        # Same display name, different person: disambiguate with email so the
        # audit entry still shows a real change.
        old_reporter_label += f" ({old_reporter.email})" if old_reporter else ""
        new_reporter_label += f" ({new_reporter.email})" if new_reporter else ""
    changes.append(("reporter", old_reporter_label, new_reporter_label))


def apply_assignee_diff(bug: Bug, db: Session, org_id: int, assignee_ids: Optional[list[int]],
                        changes: list[tuple[str, str, str]]) -> tuple[list[User], list[User]]:
    """Diff and re-bind assignees. Returns (added, removed) so the caller
    can notify both sides (removal is as worth notifying as a new assignment)."""
    if assignee_ids is None:
        return [], []
    new_users = resolve_users(db, assignee_ids, org_id)
    old_users = list(bug.assignees)
    old_ids = {a.id for a in old_users}
    new_ids = {u.id for u in new_users}
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    if not (added_ids or removed_ids):
        return [], []
    added = [u for u in new_users if u.id in added_ids]
    removed = [u for u in old_users if u.id in removed_ids]
    reject_inactive(added)
    old_names = sorted(a.name for a in old_users)
    new_names = sorted(u.name for u in new_users)
    changes.append((
        "assignees",
        ", ".join(old_names) or "(none)",
        ", ".join(new_names) or "(none)",
    ))
    bug.assignees = new_users
    return added, removed


def require_people_on_project(db: Session, bug: Bug, project_moved: bool, added: list[User],
                               reporter_changed: bool) -> None:
    """Everyone newly attached to the item must be able to see its project; after a project
    move that means everyone still attached."""
    assignees = list(bug.assignees) if project_moved else added
    require_project_members(db, assignees, bug.project_id)
    if project_moved or reporter_changed:
        require_project_members(db, [bug.reporter], bug.project_id, noun="reporter")


def persist_update(db: Session, bug: Bug, actor: User,
                    changes: list[tuple[str, str, str]]) -> None:
    """Commit if there are tracked changes; roll back otherwise so a no-op
    PUT doesn't bump updated_at."""
    if not changes:
        db.rollback()
        return
    prefix = f"#{bug.id} '{bug.title}' — "
    for field, old, new in changes:
        log_activity(
            db, bug.id, actor, f"{field}_changed",
            f"{prefix}{field}: '{old}' → '{new}'",
        )
    # Bump the version so a concurrent writer with a stale copy gets a 409 at
    # sub-second resolution. version is NOT NULL (server_default 1).
    bug.version = bug.version + 1
    db.commit()
