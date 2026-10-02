"""Work-item hierarchy rules (Jira Software model).

    Epic                         parent_id = NULL, epic_id = NULL
    Story / Task / Bug / Req.    parent_id = NULL, epic_id = NULL or an Epic in the same project
    Sub-task                     parent_id = a standard issue in the same project,
                                 epic_id and sprint_id follow that parent

Routes call validate_hierarchy_assignment()/assign_hierarchy() to turn a bad
request into a 422 with a clear message. The derived values (a Sub-task's
epic and sprint, and the cascade when a parent moves) are applied at flush
time by app.agile.integrity, so they hold on every write path.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.agile.itemtypes import STANDARD_TYPES, is_epic, is_standard, is_subtask
from app.models import Bug, EpicDetail

LEVEL2_TYPES = set(STANDARD_TYPES)


class HierarchyError(ValueError):
    """Raised for any invariant violation; routes translate this to 422."""


def _get_same_project(db: Session, item: Bug, other_id: int | None, label: str) -> Bug | None:
    if other_id is None:
        return None
    other = db.get(Bug, other_id)
    if other is None:
        raise HierarchyError(f"{label} #{other_id} not found")
    if other.project_id != item.project_id:
        raise HierarchyError(f"{label} must belong to the same project")
    return other


def _validate_epic_link(db: Session, item: Bug, epic_id: int | None) -> int | None:
    epic = _get_same_project(db, item, epic_id, "Epic")
    if epic is None:
        return None
    if not is_epic(epic.item_type):
        raise HierarchyError(f"#{epic_id} is a {epic.item_type}, not an Epic")
    detail = db.get(EpicDetail, epic.id)
    if detail is not None and detail.archived:
        raise HierarchyError("Archived epics cannot take new issues")
    return epic.id


def _validate_subtask_parent(db: Session, item: Bug, parent_id: int | None) -> int:
    if parent_id is None:
        raise HierarchyError("A Sub-task needs a parent issue (a Story, Task, Bug or Requirement)")
    if item.id is not None and parent_id == item.id:
        raise HierarchyError("An issue cannot be its own parent")
    parent = _get_same_project(db, item, parent_id, "Parent")
    if not is_standard(parent.item_type):
        raise HierarchyError(
            f"A Sub-task's parent must be a Story, Task, Bug or Requirement, not a {parent.item_type}"
        )
    return parent.id


def validate_hierarchy_assignment(
    db: Session, item: Bug, parent_id: int | None, epic_id: int | None,
) -> tuple[int | None, int | None]:
    """Validate a proposed (parent_id, epic_id) for ``item`` and return the pair to
    store. A Sub-task's epic is not accepted from the caller (it is its
    parent's); the returned epic_id for a Sub-task is the parent's current one.
    Raises HierarchyError on any violation."""
    item_type = item.item_type or "Bug"
    if is_epic(item_type):
        if parent_id is not None or epic_id is not None:
            raise HierarchyError("An Epic cannot have a parent or belong to another Epic")
        return None, None
    if is_standard(item_type):
        if parent_id is not None:
            raise HierarchyError(
                f"A {item_type} cannot have a parent issue; only Sub-tasks do. Use the Epic link instead."
            )
        return None, _validate_epic_link(db, item, epic_id)
    if is_subtask(item_type):
        resolved_parent = _validate_subtask_parent(db, item, parent_id)
        return resolved_parent, db.get(Bug, resolved_parent).epic_id
    raise HierarchyError(f"{item_type} items do not take part in the hierarchy")


def assign_hierarchy(
    db: Session, item: Bug, parent_id: int | None, epic_id: int | None,
) -> Bug:
    """Validate then store parent_id/epic_id on ``item`` (flush, no commit).
    Sub-tasks under a re-linked standard issue follow it at flush time."""
    resolved_parent_id, resolved_epic_id = validate_hierarchy_assignment(
        db, item, parent_id, epic_id,
    )
    item.parent_id = resolved_parent_id
    item.epic_id = resolved_epic_id
    db.flush()
    return item
