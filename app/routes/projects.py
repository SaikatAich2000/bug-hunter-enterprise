"""Projects API — org-scoped, with project-membership visibility rules."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    accessible_project_ids,
    can_access_project,
    can_create_project,
    can_delete_project,
    can_manage_project,
    get_current_user,
    get_org_project_or_404,
)
from app.database import get_db
from app.models import (
    PROJECT_ROLE_LEAD,
    ROLE_ADMIN,
    Activity,
    Bug,
    Project,
    ProjectMembership,
    User,
)
from app.schemas import ProjectIn, ProjectOut

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _audit(db: Session, org_id: int, actor: User, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="project", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


_KEY_BAD = re.compile(r"[^A-Z0-9]+")


def _derive_key(name: str) -> str:
    """Pick a reasonable default project key from the project name.
    e.g. "Marketing Site" → "MS"; "Web" → "WEB"; "1" → "P1".
    Caller appends a numeric suffix on collision."""
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    if not words:
        return "P"
    if len(words) == 1:
        s = words[0].upper()
        s = _KEY_BAD.sub("", s)
        if not s:
            return "P"
        return s[:6] if s[0].isalpha() else f"P{s[:5]}"
    initials = "".join(w[0] for w in words[:4]).upper()
    initials = _KEY_BAD.sub("", initials)
    if not initials:
        return "P"
    if not initials[0].isalpha():
        initials = "P" + initials
    return initials[:6]


def _unique_key(db: Session, org_id: int, base: str) -> str:
    cand = base
    n = 2
    while db.scalar(select(Project.id).where(Project.org_id == org_id, Project.key == cand)):
        cand = f"{base}{n}"
        n += 1
        if n > 999:
            raise HTTPException(status_code=500, detail="Could not generate a unique project key")
    return cand


def _to_out(p: Project, can_manage: bool, member_count: int) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "key": p.key or "",
        "description": p.description,
        "color": p.color,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "can_manage": can_manage,
        "member_count": member_count,
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("", response_model=list[ProjectOut])
def list_projects(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    ids = accessible_project_ids(db, user)
    if not ids:
        return []
    rows = list(db.scalars(
        select(Project).where(Project.id.in_(ids)).order_by(func.lower(Project.name))
    ).all())

    # Bulk member counts for the visible projects.
    counts: dict[int, int] = {}
    if rows:
        for pid, cnt in db.execute(
            select(ProjectMembership.project_id, func.count(ProjectMembership.id))
            .where(ProjectMembership.project_id.in_([p.id for p in rows]))
            .group_by(ProjectMembership.project_id)
        ).all():
            counts[pid] = int(cnt)

    out = []
    for p in rows:
        out.append(_to_out(p, can_manage_project(db, user, p), counts.get(p.id, 0)))
    return out


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not can_create_project(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can create projects.",
        )

    # Key handling: if the user explicitly named a key, it must be exactly
    # that — otherwise auto-suffixing would silently break their intent
    # (e.g. they typed "WEB", we'd hand them back "WEB2"). Only auto-pick
    # when no key was supplied.
    if payload.key:
        key = payload.key
        if db.scalar(select(Project.id).where(
            Project.org_id == actor.org_id, Project.key == key,
        )):
            raise HTTPException(
                status_code=409,
                detail=f"Project key '{key}' already in use in your organization.",
            )
    else:
        key = _unique_key(db, actor.org_id, _derive_key(payload.name))

    p = Project(
        org_id=actor.org_id,
        name=payload.name,
        key=key,
        description=payload.description,
        color=payload.color,
    )
    db.add(p)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Project name or key already exists in your organization.",
        ) from exc

    # Auto-add the creator as a project lead. Admins technically already
    # have access without this row, but giving them an explicit lead row
    # keeps the member list visible and consistent.
    db.add(ProjectMembership(
        project_id=p.id, user_id=actor.id, role=PROJECT_ROLE_LEAD,
    ))

    _audit(db, actor.org_id, actor, "project_created", p.id,
           f"Created project '{p.name}' ({p.key})")
    db.commit()
    db.refresh(p)
    return _to_out(p, can_manage=True, member_count=1)


# ---------------------------------------------------------------------------
# Get one
# ---------------------------------------------------------------------------
@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    p = get_org_project_or_404(db, project_id, user)
    if not can_access_project(db, user, p):
        raise HTTPException(status_code=404, detail="Project not found")
    cnt = db.scalar(
        select(func.count(ProjectMembership.id))
        .where(ProjectMembership.project_id == p.id)
    ) or 0
    return _to_out(p, can_manage_project(db, user, p), int(cnt))


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    p = get_org_project_or_404(db, project_id, actor)
    if not can_manage_project(db, actor, p):
        raise HTTPException(
            status_code=403,
            detail="Only admins or this project's leads can edit it.",
        )

    fields = payload.model_dump()
    # Key handling: only update if explicitly provided.
    new_key = fields.pop("key", None)
    changes = []
    for k, v in fields.items():
        old = getattr(p, k)
        if old != v:
            changes.append(f"{k}: {old!r} → {v!r}")
            setattr(p, k, v)
    if new_key and new_key != p.key:
        # Validator already uppercased / shape-checked.
        if db.scalar(select(Project.id).where(
            Project.org_id == p.org_id, Project.key == new_key, Project.id != p.id,
        )):
            raise HTTPException(
                status_code=409,
                detail="Project key already in use in this organization.",
            )
        changes.append(f"key: {p.key!r} → {new_key!r}")
        p.key = new_key

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Project name or key already exists in your organization.",
        ) from exc

    if changes:
        _audit(db, actor.org_id, actor, "project_updated", p.id,
               f"Updated project '{p.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(p)
    cnt = db.scalar(
        select(func.count(ProjectMembership.id))
        .where(ProjectMembership.project_id == p.id)
    ) or 0
    return _to_out(p, can_manage_project(db, actor, p), int(cnt))


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    p = get_org_project_or_404(db, project_id, actor)
    if not can_delete_project(db, actor, p):
        raise HTTPException(
            status_code=403,
            detail="Only org admins can delete projects.",
        )

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
    _audit(db, actor.org_id, actor, "project_deleted", project_id,
           f"Deleted project '{name}'")
    db.commit()
    return {"message": "Project deleted"}
