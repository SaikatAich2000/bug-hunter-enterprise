"""Epics, Releases/Versions, Components and Labels: normalized-name uniqueness,
archive (soft-delete) and epic progress roll-ups.
"""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agile.hierarchy import LEVEL2_TYPES
from app.models import (
    Bug,
    Component,
    EpicDetail,
    Label,
    Version,
    WorkflowStatus,
    WorkItemComponent,
    WorkItemLabel,
)


class TaxonomyError(ValueError):
    pass


def normalize_name(name: str) -> str:
    """Trim, Unicode-normalize (NFKC), collapse whitespace, lowercase."""
    s = unicodedata.normalize("NFKC", (name or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s.lower()


# --- Epics ---

def get_or_create_epic_detail(db: Session, epic: Bug) -> EpicDetail:
    detail = db.get(EpicDetail, epic.id)
    if detail is None:
        detail = EpicDetail(epic_id=epic.id)
        db.add(detail)
        db.flush()
    return detail


def status_categories(db: Session) -> dict[tuple[str, str], str]:
    """(item type, status value) -> category (todo/in_progress/testing/done)."""
    rows = db.execute(
        select(WorkflowStatus.work_item_type, WorkflowStatus.persisted_status_value, WorkflowStatus.category)
        .where(WorkflowStatus.scope_key == "global")
    ).all()
    return {(t, v): c for t, v, c in rows}


def category_of(categories: dict[tuple[str, str], str], item: Bug) -> str:
    return categories.get((item.item_type or "Bug", item.status), "todo")


def is_done_status(db: Session, item: Bug) -> bool:
    return category_of(status_categories(db), item) == "done"


def _empty_progress() -> dict:
    return {
        "child_count": 0, "completed_child_count": 0, "in_progress_child_count": 0,
        "total_estimate": 0.0, "completed_estimate": 0.0, "unestimated_count": 0,
    }


def epic_progress_many(db: Session, epic_ids: list[int]) -> dict[int, dict]:
    """Progress of each epic from its standard issues, by status category
    (Jira's epic progress). Explicit counts and estimates, never an average
    of child percentages. Sub-tasks are part of their parent, not counted."""
    out = {eid: _empty_progress() for eid in epic_ids}
    categories = status_categories(db) if epic_ids else {}
    children = db.scalars(
        select(Bug).where(Bug.epic_id.in_(epic_ids), Bug.item_type.in_(LEVEL2_TYPES))
    ).all() if epic_ids else []
    for child in children:
        entry = out[child.epic_id]
        entry["child_count"] += 1
        points = float(child.story_points) if child.story_points is not None else None
        if points is None:
            entry["unestimated_count"] += 1
        else:
            entry["total_estimate"] += points
        category = category_of(categories, child)
        if category == "done":
            entry["completed_child_count"] += 1
            if points is not None:
                entry["completed_estimate"] += points
        elif category in ("in_progress", "testing"):
            entry["in_progress_child_count"] += 1
    return out


def epic_progress(db: Session, epic: Bug) -> dict:
    return epic_progress_many(db, [epic.id])[epic.id]


# --- Versions / Releases ---

def create_version(db: Session, project_id: int, **fields) -> Version:
    v = Version(project_id=project_id, name_normalized=normalize_name(fields["name"]), **fields)
    db.add(v)
    db.flush()
    return v


# --- Components ---

def create_component(db: Session, project_id: int, **fields) -> Component:
    c = Component(project_id=project_id, name_normalized=normalize_name(fields["name"]), **fields)
    db.add(c)
    db.flush()
    return c


def _require_in_project(db: Session, model, ids, project_id: int, what: str, *, allow_archived=()) -> None:
    """Every id must name a ``model`` row of the item's own project; another
    project's components and labels are neither attachable nor revealed."""
    wanted = set(ids)
    if not wanted:
        return
    rows = db.scalars(select(model).where(model.id.in_(wanted), model.project_id == project_id)).all()
    found = {r.id for r in rows}
    missing = wanted - found
    if missing:
        raise TaxonomyError(f"Unknown {what} for this project: {sorted(missing)}")
    archived = [r.id for r in rows if getattr(r, "archived", False) and r.id not in allow_archived]
    if archived:
        raise TaxonomyError(f"Archived {what} cannot be added: {sorted(archived)}")


def set_work_item_components(db: Session, item: Bug, component_ids: list[int]) -> None:
    current = set(db.scalars(
        select(WorkItemComponent.component_id).where(WorkItemComponent.work_item_id == item.id)
    ).all())
    # An already attached component may stay even if it was archived since.
    _require_in_project(db, Component, component_ids, item.project_id, "components", allow_archived=current)
    db.query(WorkItemComponent).filter(WorkItemComponent.work_item_id == item.id).delete()
    for cid in dict.fromkeys(component_ids):
        db.add(WorkItemComponent(work_item_id=item.id, component_id=cid))
    db.flush()


# --- Labels ---

def get_or_create_label(db: Session, project_id: int, display_name: str, color: str = "") -> Label:
    norm = normalize_name(display_name)
    existing = db.scalar(
        select(Label).where(Label.project_id == project_id, Label.normalized_name == norm)
    )
    if existing is not None:
        return existing
    label = Label(project_id=project_id, normalized_name=norm, display_name=display_name.strip(), color=color)
    db.add(label)
    db.flush()
    return label


def set_work_item_labels(db: Session, item: Bug, label_ids: list[int]) -> None:
    current = {
        r.label_id for r in
        db.scalars(select(WorkItemLabel).where(WorkItemLabel.work_item_id == item.id)).all()
    }
    new_ids = set(dict.fromkeys(label_ids))
    _require_in_project(db, Label, new_ids - current, item.project_id, "labels")
    for removed in current - new_ids:
        db.query(WorkItemLabel).filter(
            WorkItemLabel.work_item_id == item.id, WorkItemLabel.label_id == removed,
        ).delete()
        label = db.get(Label, removed)
        if label is not None and label.usage_count > 0:
            label.usage_count -= 1
    for added in new_ids - current:
        db.add(WorkItemLabel(work_item_id=item.id, label_id=added))
        label = db.get(Label, added)
        if label is not None:
            label.usage_count += 1
    db.flush()


def detach_project_taxonomy(db: Session, item_ids: list[int]) -> None:
    """Remove the items' components and labels (they belong to the project the
    items are leaving), keeping each label's usage count right."""
    if not item_ids:
        return
    db.query(WorkItemComponent).filter(WorkItemComponent.work_item_id.in_(item_ids)).delete(
        synchronize_session=False,
    )
    links = db.scalars(select(WorkItemLabel).where(WorkItemLabel.work_item_id.in_(item_ids))).all()
    for link in links:
        label = db.get(Label, link.label_id)
        if label is not None and label.usage_count > 0:
            label.usage_count -= 1
        db.delete(link)
    db.flush()


def delete_label_if_unused(db: Session, label: Label) -> None:
    if label.usage_count > 0:
        raise TaxonomyError("Label is in use; merge instead of deleting")
    db.delete(label)
    db.flush()
