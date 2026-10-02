"""Projects API. Every project belongs to the caller's organization; others are invisible.

Permissions:
  - Read   : projects the caller is a member of (every project of the organization for admins).
  - Create : admin or manager (require_manager_or_admin).
  - Update : admin or manager.
  - Delete : admin only.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import (
    accessible_project_ids,
    add_user_project,
    can_access_project,
    can_manage_project,
)
from app.agile import boards as boards_svc
from app.api_docs import CONFLICT_409, NOT_FOUND_404, NOT_FOUND_CONFLICT_409
from app.auth import get_current_user, require_admin, require_manager_or_admin
from app.database import get_db
from app.models import (
    PROJECT_ROLE_LEAD,
    ROLE_ADMIN,
    Activity,
    Bug,
    Project,
    User,
    user_projects,
)
from app.schemas import ProjectCreateIn, ProjectIn, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


_DETAIL_PROJECT_NOT_FOUND = "Project not found"

def _audit(db: Session, actor: User, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        org_id=actor.org_id, bug_id=None, entity_type="project", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


def _name_taken(db: Session, org_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Project.id).where(Project.org_id == org_id, Project.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Project.id != exclude_id)
    return db.scalar(stmt) is not None


def _key_taken(db: Session, org_id: int, key: str, exclude_id: int | None = None) -> bool:
    stmt = select(Project.id).where(Project.org_id == org_id, Project.key == key)
    if exclude_id is not None:
        stmt = stmt.where(Project.id != exclude_id)
    return db.scalar(stmt) is not None


def _project_out(db: Session, user: User, project: Project, member_count: int | None = None,
                 can_manage: bool | None = None) -> dict:
    if can_manage is None:
        can_manage = can_manage_project(db, user, project)
    if member_count is None:
        member_count = db.scalar(
            select(func.count()).select_from(user_projects)
            .where(user_projects.c.project_id == project.id)
        ) or 0
    return {
        "id": project.id, "name": project.name, "key": project.key,
        "description": project.description, "color": project.color,
        "created_at": project.created_at, "updated_at": project.updated_at,
        "can_manage": can_manage,
        "member_count": int(member_count),
    }


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    accessible = accessible_project_ids(db, user)
    if not accessible:
        return []
    rows = list(db.scalars(
        select(Project).where(Project.id.in_(accessible))
        .order_by(func.lower(Project.name)).limit(500)
    ).all())
    counts = dict(db.execute(
        select(user_projects.c.project_id, func.count())
        .where(user_projects.c.project_id.in_([p.id for p in rows]))
        .group_by(user_projects.c.project_id)
    ).all())
    led = set(db.scalars(
        select(user_projects.c.project_id)
        .where(user_projects.c.user_id == user.id, user_projects.c.role == PROJECT_ROLE_LEAD)
    ).all())
    return [
        _project_out(db, user, p, counts.get(p.id, 0), can_manage=user.role == ROLE_ADMIN or p.id in led)
        for p in rows
    ]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED, responses=CONFLICT_409)
def create_project(
    payload: ProjectCreateIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    fields = payload.model_dump(exclude={"agile_enabled", "key"})
    if _name_taken(db, actor.org_id, fields["name"]):
        raise HTTPException(status_code=409, detail="Project name already exists")
    if payload.key and _key_taken(db, actor.org_id, payload.key):
        raise HTTPException(status_code=409, detail="Project key already exists")
    p = Project(org_id=actor.org_id, key=payload.key or "", **fields)
    db.add(p)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name or key already exists") from exc
    # Optional Agile activation in the same transaction: a failure rolls the
    # whole creation back, so a project is never left half-configured (no board
    # while agile_enabled says otherwise).
    if payload.agile_enabled:
        try:
            boards_svc.activate_agile_for_project(db, p, actor, {})
        except boards_svc.AgileActivationError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    # auto-enroll the creating manager so they keep visibility of their new project
    if actor.role != ROLE_ADMIN:
        add_user_project(db, actor.id, p.id, PROJECT_ROLE_LEAD)
    _audit(db, actor, "project_created", p.id, f"Created project '{p.name}' ({p.key})")
    if payload.agile_enabled:
        _audit(db, actor, "agile_enabled", p.id, f"Enabled Agile for project '{p.name}'")
    db.commit()
    db.refresh(p)
    return _project_out(db, actor, p)


@router.get("/{project_id}", response_model=ProjectOut, responses=NOT_FOUND_404)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    p = db.get(Project, project_id)
    # 404 not 403 — scoping must not leak existence
    if p is None or not can_access_project(accessible_project_ids(db, user), project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)
    return _project_out(db, user, p)


@router.put("/{project_id}", response_model=ProjectOut, responses=NOT_FOUND_CONFLICT_409)
def update_project(
    project_id: int,
    payload: ProjectIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    p = db.get(Project, project_id)
    # 404 not 403 — scoping must not leak existence
    if p is None or not can_access_project(accessible_project_ids(db, actor), project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)
    # exclude_unset so omitted fields aren't reset to schema defaults
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("key") is None:
        fields.pop("key", None)
    if "name" in fields and _name_taken(db, p.org_id, fields["name"], exclude_id=p.id):
        raise HTTPException(status_code=409, detail="Project name already exists")
    if "key" in fields and _key_taken(db, p.org_id, fields["key"], exclude_id=p.id):
        raise HTTPException(status_code=409, detail="Project key already exists")
    changes = []
    for key, value in fields.items():
        old = getattr(p, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(p, key, value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name or key already exists") from exc
    if changes:
        _audit(db, actor, "project_updated", p.id,
               f"Updated project '{p.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(p)
    return _project_out(db, actor, p)


@router.delete("/{project_id}", responses=NOT_FOUND_CONFLICT_409)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    # FOR UPDATE closes the count-then-delete race with a concurrent bug insert
    p = db.get(Project, project_id, with_for_update=True)
    if p is None or p.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)

    bug_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.project_id == project_id)
    ) or 0
    if bug_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {bug_count} bug(s) belong to this project. Move or delete them first.",
        )
    name = p.name
    db.delete(p)
    _audit(db, actor, "project_deleted", project_id, f"Deleted project '{name}'")
    db.commit()
    return {"message": "Project deleted"}
