"""Work-item helpers shared by routes and services."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import can_access_project, event_visible
from app.models import Bug, Event, Project, User
from app.schemas import (
    display_id_for,
)
from app.services.audit import audit

DETAIL_BUG_NOT_FOUND = "Bug not found"

DETAIL_ATTACHMENT_NOT_FOUND = "Attachment not found"


def work_item_type(bug: Bug) -> str:
    """Return the work-item type, defaulting to 'Bug' for rows with no item_type."""
    return getattr(bug, "item_type", None) or "Bug"


def work_item_display_id(bug: Bug) -> str:
    """The stable user-facing reference for a work item ("USRSTR-12").

    A stored value always wins: once an item has been created the id people
    reference must not drift, even when the item is reclassified from Story to
    Task. Rows that predate the column fall back to ``<PREFIX>-<id>``.
    """
    stored = (getattr(bug, "display_id", None) or "").strip()
    return stored or display_id_for(work_item_type(bug), bug.id)


def log_activity(
    db: Session, bug_id: int | None, actor: User | None, action: str, detail: str,
    entity_type: str = "bug", entity_id: int | None = None,
) -> None:
    audit(db, actor, entity_type, entity_id if entity_id is not None else bug_id,
          action, detail, bug_id=bug_id)


# Project-scoped access. Managers and regular users can only act on items in
# their projects, admins on those of their own organization (``accessible`` is the
# actor's tenant-scoped project set). Items outside scope are surfaced as 404 so the
# response never leaks whether a bug or project exists.
def assert_bug_accessible(accessible, bug: Bug) -> None:
    """Raise 404 when the bug is outside the actor's project scope."""
    if not can_access_project(accessible, bug.project_id):
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)


def assert_project_accessible(db: Session, accessible, project_id: int) -> None:
    """Raise 400 when the project is missing or outside the actor's scope.

    A project the caller can't see is treated as nonexistent.
    """
    if db.get(Project, project_id) is None or not can_access_project(accessible, project_id):
        raise HTTPException(status_code=400, detail="Project does not exist")


def assert_event_accessible(db: Session, actor: User, accessible, event_id: int) -> None:
    """Raise 400 when the event is missing or out of the actor's scope."""
    ev = db.get(Event, event_id)
    if ev is None or not event_visible(actor, accessible, ev):
        raise HTTPException(status_code=400, detail="Event does not exist")


def load_accessible_bug(db: Session, bug_id: int, accessible) -> Bug:
    """Load a bug and raise 404 if it's missing or out of scope."""
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    assert_bug_accessible(accessible, bug)
    return bug


def assert_attachment_in_scope(db: Session, accessible, bug_id: int) -> None:
    """Raise 404 when the owning bug is outside the actor's project scope."""
    proj_id = db.scalar(select(Bug.project_id).where(Bug.id == bug_id))
    if proj_id is None or proj_id not in accessible:
        raise HTTPException(status_code=404, detail=DETAIL_ATTACHMENT_NOT_FOUND)
