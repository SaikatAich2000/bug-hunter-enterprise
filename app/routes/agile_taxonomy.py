"""Agile API: Epics, Releases/Versions, Components, Labels and the roadmap.
Namespaced under /api/agile.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.agile import releases as releases_svc
from app.agile import taxonomy as taxonomy_svc
from app.agile.permissions import (
    MANAGE_COMPONENTS,
    MANAGE_EPICS,
    MANAGE_LABELS,
    MANAGE_RELEASES,
    require_permission,
)
from app.api_docs import (
    NOT_FOUND_404,
    NOT_FOUND_CONFLICT_409,
    NOT_FOUND_CONFLICT_VALIDATION_422,
)
from app.auth import get_current_user
from app.database import get_db
from app.models import (
    Activity,
    Bug,
    Component,
    EpicDetail,
    Label,
    Project,
    User,
    Version,
)
from app.schemas import (
    ComponentCreateIn,
    ComponentOut,
    ComponentUpdateIn,
    EpicOut,
    EpicUpdateIn,
    LabelCreateIn,
    LabelOut,
    LabelUpdateIn,
    Release360Out,
    ReleaseActionIn,
    RoadmapOut,
    VersionCreateIn,
    VersionOut,
    VersionUpdateIn,
    display_id_for,
)

router = APIRouter(prefix="/api/agile", tags=["agile-taxonomy"])

_DETAIL_PROJECT_NOT_FOUND = "Project not found"
# Agile-off is reported honestly while staying 404, so an inaccessible project
# remains indistinguishable from a disabled one (see app/routes/agile.py).
_DETAIL_AGILE_NOT_ENABLED = "Agile is not enabled for this project"
_DETAIL_NOT_FOUND = "Not found"
_DETAIL_VERSION_CONFLICT = "This was modified by someone else; reload and retry"


def _audit(db: Session, actor: User, entity_type: str, entity_id: int, action: str, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type=entity_type, entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name, action=action, detail=detail,
    ))


# Nullable epic fields a PUT may clear with an explicit null (a removed date or
# owner). Every other field treats null as "leave unchanged".
_CLEARABLE_EPIC_FIELDS = frozenset({"start_date", "target_date", "owner_id"})


def _apply_fields(entity, fields: dict, clearable: frozenset[str]) -> None:
    for key, value in fields.items():
        if value is not None or key in clearable:
            setattr(entity, key, value)


def _get_project_or_404(db: Session, project_id: int, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None or not can_access_project(accessible_project_ids(db, user), project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)
    if not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_AGILE_NOT_ENABLED)
    return project


# --- Epics ---

def _epic_out(epic: Bug, detail: EpicDetail | None, progress: dict, categories: dict) -> dict:
    return {
        "id": epic.id,
        "display_id": (epic.display_id or "").strip() or display_id_for("Epic", epic.id),
        "title": epic.title, "description": epic.description or "",
        "status": epic.status, "project_id": epic.project_id,
        "status_category": taxonomy_svc.category_of(categories, epic),
        "color": detail.color if detail else "",
        "owner_id": detail.owner_id if detail else None,
        "start_date": detail.start_date if detail else None,
        "target_date": detail.target_date if detail else None,
        "health": detail.health if detail else "unknown",
        "summary_note": detail.summary_note if detail else "",
        "archived": detail.archived if detail else False,
        "version": detail.version if detail else 1,
        "progress": progress,
    }


def _epics_out(db: Session, epics: list[Bug]) -> list[dict]:
    ids = [e.id for e in epics]
    details = {d.epic_id: d for d in db.scalars(select(EpicDetail).where(EpicDetail.epic_id.in_(ids))).all()} if ids else {}
    progress = taxonomy_svc.epic_progress_many(db, ids)
    categories = taxonomy_svc.status_categories(db)
    return [_epic_out(e, details.get(e.id), progress[e.id], categories) for e in epics]


def _get_epic_or_404(db: Session, epic_id: int, user: User) -> Bug:
    epic = db.get(Bug, epic_id)
    if epic is None or epic.item_type != "Epic" or not can_access_project(
        accessible_project_ids(db, user), epic.project_id
    ):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    return epic


@router.get("/epics", response_model=list[EpicOut], responses=NOT_FOUND_404)
def list_epics(
    project_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[dict]:
    _get_project_or_404(db, project_id, user)
    epics = list(db.scalars(
        select(Bug).where(Bug.project_id == project_id, Bug.item_type == "Epic").order_by(Bug.rank.is_(None), Bug.rank, Bug.id)
    ).all())
    return _epics_out(db, epics)


@router.get("/epics/{epic_id}", response_model=EpicOut, responses=NOT_FOUND_404)
def get_epic(
    epic_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    return _epics_out(db, [_get_epic_or_404(db, epic_id, user)])[0]


@router.put("/epics/{epic_id}", response_model=EpicOut, responses=NOT_FOUND_CONFLICT_VALIDATION_422)
def update_epic(
    epic_id: int, payload: EpicUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    epic = _get_epic_or_404(db, epic_id, actor)
    require_permission(actor, MANAGE_EPICS)
    detail = taxonomy_svc.get_or_create_epic_detail(db, epic)
    if detail.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    fields = payload.model_dump(exclude_unset=True, exclude={"version"})
    # title and description live on the Epic's work-item row, not the detail.
    for key in ("title", "description"):
        if key in fields:
            value = fields.pop(key)
            if value is not None:
                setattr(epic, key, value)
    start = fields.get("start_date", detail.start_date)
    target = fields.get("target_date", detail.target_date)
    if start and target and target < start:
        raise HTTPException(status_code=422, detail="The target date cannot be before the start date")
    _apply_fields(detail, fields, _CLEARABLE_EPIC_FIELDS)
    detail.version += 1
    _audit(db, actor, "bug", epic.id, "epic_updated", f"Updated epic #{epic.id} '{epic.title}'")
    db.commit()
    return _epics_out(db, [epic])[0]


@router.post("/epics/{epic_id}/archive", response_model=EpicOut, responses=NOT_FOUND_404)
def archive_epic(
    epic_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    epic = _get_epic_or_404(db, epic_id, actor)
    require_permission(actor, MANAGE_EPICS)
    detail = taxonomy_svc.get_or_create_epic_detail(db, epic)
    detail.archived = True
    detail.version += 1
    _audit(db, actor, "bug", epic.id, "epic_archived", f"Archived epic #{epic.id} '{epic.title}'")
    db.commit()
    return _epics_out(db, [epic])[0]


# --- Versions / Releases (core: CRUD/archive/item-association only) ---

@router.get("/releases", response_model=list[VersionOut], responses=NOT_FOUND_404)
def list_versions(
    project_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[Version]:
    _get_project_or_404(db, project_id, user)
    return list(db.scalars(select(Version).where(Version.project_id == project_id)).all())


@router.post("/releases", response_model=VersionOut, status_code=status.HTTP_201_CREATED, responses=NOT_FOUND_CONFLICT_409)
def create_version(
    payload: VersionCreateIn, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Version:
    _get_project_or_404(db, payload.project_id, actor)
    require_permission(actor, MANAGE_RELEASES)
    fields = payload.model_dump(exclude={"project_id"})
    try:
        v = taxonomy_svc.create_version(db, payload.project_id, **fields)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A version with this name already exists") from exc
    _audit(db, actor, "release", v.id, "release_created", f"Created release '{v.name}'")
    db.commit()
    db.refresh(v)
    return v


@router.get("/releases/{version_id}", response_model=VersionOut, responses=NOT_FOUND_404)
def get_version(
    version_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> Version:
    v = db.get(Version, version_id)
    if v is None or not can_access_project(accessible_project_ids(db, user), v.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    return v


@router.put("/releases/{version_id}", response_model=VersionOut, responses=NOT_FOUND_CONFLICT_409)
def update_version(
    version_id: int, payload: VersionUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Version:
    v = db.get(Version, version_id)
    if v is None or not can_access_project(accessible_project_ids(db, actor), v.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_RELEASES)
    if v.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    fields = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in fields.items():
        if key == "name" and value is not None:
            v.name_normalized = taxonomy_svc.normalize_name(value)
        if value is not None:
            setattr(v, key, value)
    v.version += 1
    _audit(db, actor, "release", v.id, "release_updated", f"Updated release '{v.name}'")
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A version with this name already exists") from exc
    db.refresh(v)
    return v


@router.post("/releases/{version_id}/archive", response_model=VersionOut, responses=NOT_FOUND_404)
def archive_version(
    version_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Version:
    v = db.get(Version, version_id)
    if v is None or not can_access_project(accessible_project_ids(db, actor), v.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_RELEASES)
    v.archived = True
    v.status = "archived"
    v.version += 1
    _audit(db, actor, "release", v.id, "release_archived", f"Archived release '{v.name}'")
    db.commit()
    db.refresh(v)
    return v


@router.get("/releases/{version_id}/360", response_model=Release360Out, responses=NOT_FOUND_404)
def get_release_360(
    version_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    v = db.get(Version, version_id)
    if v is None or not can_access_project(accessible_project_ids(db, user), v.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    return releases_svc.release_360(db, v)


@router.post("/releases/{version_id}/release", response_model=Release360Out, responses=NOT_FOUND_CONFLICT_409)
def release_version(
    version_id: int, payload: ReleaseActionIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    v = db.get(Version, version_id)
    if v is None or not can_access_project(accessible_project_ids(db, actor), v.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_RELEASES)
    if v.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    try:
        releases_svc.mark_released(db, v, actor)
    except releases_svc.ReleaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(db, actor, "release", v.id, "release_released", f"Released '{v.name}'")
    db.commit()
    return releases_svc.release_360(db, v)


@router.get("/roadmap", response_model=RoadmapOut, responses=NOT_FOUND_404)
def get_roadmap(
    project_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    _get_project_or_404(db, project_id, user)
    return releases_svc.roadmap_data(db, project_id)


# --- Components ---

@router.get("/components", response_model=list[ComponentOut], responses=NOT_FOUND_404)
def list_components(
    project_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[Component]:
    _get_project_or_404(db, project_id, user)
    return list(db.scalars(select(Component).where(Component.project_id == project_id)).all())


@router.post("/components", response_model=ComponentOut, status_code=status.HTTP_201_CREATED, responses=NOT_FOUND_CONFLICT_409)
def create_component(
    payload: ComponentCreateIn, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Component:
    _get_project_or_404(db, payload.project_id, actor)
    require_permission(actor, MANAGE_COMPONENTS)
    fields = payload.model_dump(exclude={"project_id"})
    try:
        c = taxonomy_svc.create_component(db, payload.project_id, **fields)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A component with this name already exists") from exc
    _audit(db, actor, "component", c.id, "component_created", f"Created component '{c.name}'")
    db.commit()
    db.refresh(c)
    return c


@router.get("/components/{component_id}", response_model=ComponentOut, responses=NOT_FOUND_404)
def get_component(
    component_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> Component:
    c = db.get(Component, component_id)
    if c is None or not can_access_project(accessible_project_ids(db, user), c.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    return c


@router.put("/components/{component_id}", response_model=ComponentOut, responses=NOT_FOUND_CONFLICT_409)
def update_component(
    component_id: int, payload: ComponentUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Component:
    c = db.get(Component, component_id)
    if c is None or not can_access_project(accessible_project_ids(db, actor), c.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_COMPONENTS)
    if c.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    fields = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in fields.items():
        if key == "name" and value is not None:
            c.name_normalized = taxonomy_svc.normalize_name(value)
        if value is not None:
            setattr(c, key, value)
    c.version += 1
    _audit(db, actor, "component", c.id, "component_updated", f"Updated component '{c.name}'")
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A component with this name already exists") from exc
    db.refresh(c)
    return c


@router.post("/components/{component_id}/archive", response_model=ComponentOut, responses=NOT_FOUND_404)
def archive_component(
    component_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Component:
    c = db.get(Component, component_id)
    if c is None or not can_access_project(accessible_project_ids(db, actor), c.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_COMPONENTS)
    c.archived = True
    c.version += 1
    _audit(db, actor, "component", c.id, "component_archived", f"Archived component '{c.name}'")
    db.commit()
    db.refresh(c)
    return c


# --- Labels ---

@router.get("/labels", response_model=list[LabelOut], responses=NOT_FOUND_404)
def list_labels(
    project_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[Label]:
    _get_project_or_404(db, project_id, user)
    return list(db.scalars(select(Label).where(Label.project_id == project_id)).all())


@router.post("/labels", response_model=LabelOut, status_code=status.HTTP_201_CREATED, responses=NOT_FOUND_404)
def create_label(
    payload: LabelCreateIn, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Label:
    _get_project_or_404(db, payload.project_id, actor)
    require_permission(actor, MANAGE_LABELS)
    label = taxonomy_svc.get_or_create_label(db, payload.project_id, payload.display_name, payload.color)
    _audit(db, actor, "label", label.id, "label_created", f"Created label '{label.display_name}'")
    db.commit()
    db.refresh(label)
    return label


@router.get("/labels/{label_id}", response_model=LabelOut, responses=NOT_FOUND_404)
def get_label(
    label_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> Label:
    label = db.get(Label, label_id)
    if label is None or not can_access_project(accessible_project_ids(db, user), label.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    return label


@router.put("/labels/{label_id}", response_model=LabelOut, responses=NOT_FOUND_CONFLICT_409)
def update_label(
    label_id: int, payload: LabelUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> Label:
    label = db.get(Label, label_id)
    if label is None or not can_access_project(accessible_project_ids(db, actor), label.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_LABELS)
    if label.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    if payload.display_name is not None:
        label.display_name = payload.display_name
        label.normalized_name = taxonomy_svc.normalize_name(payload.display_name)
    if payload.color is not None:
        label.color = payload.color
    label.version += 1
    _audit(db, actor, "label", label.id, "label_updated", f"Updated label '{label.display_name}'")
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A label with this name already exists") from exc
    db.refresh(label)
    return label


@router.delete("/labels/{label_id}", responses=NOT_FOUND_CONFLICT_409)
def delete_label(
    label_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    label = db.get(Label, label_id)
    if label is None or not can_access_project(accessible_project_ids(db, actor), label.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    require_permission(actor, MANAGE_LABELS)
    label_name, label_ref = label.display_name, label.id
    try:
        taxonomy_svc.delete_label_if_unused(db, label)
    except taxonomy_svc.TaxonomyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(db, actor, "label", label_ref, "label_deleted", f"Deleted label '{label_name}'")
    db.commit()
    return {"message": "Label deleted"}
