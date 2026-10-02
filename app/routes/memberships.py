"""Project membership API: who belongs to a project, and as lead or member.

Nested under /api/projects/{id}/members, so the project in the URL is the authorization
scope; a project of another organization is a 404.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.access import (
    accessible_project_ids,
    can_access_project,
    can_manage_project,
    get_org_project_or_404,
    get_org_user,
)
from app.api_docs import (
    BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404,
    NOT_FOUND_FORBIDDEN_403,
)
from app.auth import get_current_user
from app.database import get_db
from app.models import PROJECT_ROLE_LEAD, Activity, Project, User, user_projects
from app.schemas import ProjectMembershipIn, ProjectMembershipOut, ProjectMembershipUpdate

router = APIRouter(prefix="/api/projects", tags=["memberships"])

_DETAIL_NOT_A_MEMBER = "Membership not found"
_DETAIL_LAST_LEAD = "Cannot {verb} the last project lead. Promote another member first."


def _audit(db: Session, actor: User, project: Project, action: str, detail: str) -> None:
    db.add(Activity(
        org_id=actor.org_id, bug_id=None, entity_type="project_membership", entity_id=project.id,
        actor_user_id=actor.id, actor_name=actor.name, action=action, detail=detail,
    ))


def _row(user: User, role: str) -> dict:
    return {
        "user_id": user.id, "user_name": user.name, "user_email": user.email,
        "user_role": user.role, "project_role": role,
    }


def _managed_project(db: Session, project_id: int, actor: User) -> Project:
    project = get_org_project_or_404(db, project_id, actor)
    if not can_manage_project(db, actor, project):
        raise HTTPException(
            status_code=403,
            detail="Only organization admins or this project's leads can manage members.",
        )
    return project


def _other_leads(db: Session, project_id: int, user_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(user_projects).where(
            user_projects.c.project_id == project_id,
            user_projects.c.role == PROJECT_ROLE_LEAD,
            user_projects.c.user_id != user_id,
        )
    ) or 0


def _role_of(db: Session, project_id: int, user_id: int) -> str:
    role = db.scalar(select(user_projects.c.role).where(
        user_projects.c.project_id == project_id, user_projects.c.user_id == user_id,
    ))
    if role is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_A_MEMBER)
    return role


@router.get("/{project_id}/members", response_model=list[ProjectMembershipOut],
            responses=NOT_FOUND_FORBIDDEN_403)
def list_members(
    project_id: int, actor: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[dict]:
    project = get_org_project_or_404(db, project_id, actor)
    if not can_access_project(accessible_project_ids(db, actor), project.id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = db.execute(
        select(User, user_projects.c.role)
        .join(user_projects, user_projects.c.user_id == User.id)
        .where(user_projects.c.project_id == project_id, User.org_id == actor.org_id)
    ).all()
    out = [_row(user, role) for user, role in rows]
    out.sort(key=lambda r: (r["project_role"] != PROJECT_ROLE_LEAD, r["user_name"].lower()))
    return out


@router.post("/{project_id}/members", response_model=ProjectMembershipOut,
             status_code=status.HTTP_201_CREATED,
             responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def add_member(
    project_id: int,
    payload: ProjectMembershipIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = _managed_project(db, project_id, actor)
    user = get_org_user(db, payload.user_id, actor)
    if user is None:
        raise HTTPException(status_code=400, detail="Unknown user")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="That user account is disabled.")
    already = db.scalar(select(user_projects.c.user_id).where(
        user_projects.c.project_id == project_id, user_projects.c.user_id == user.id,
    ))
    if already is not None:
        raise HTTPException(status_code=409, detail="That user is already a member of this project.")
    db.execute(insert(user_projects).values(
        user_id=user.id, project_id=project_id, role=payload.role,
    ))
    _audit(db, actor, project, "member_added",
           f"Added {user.name} <{user.email}> to '{project.name}' as {payload.role}")
    db.commit()
    return _row(user, payload.role)


@router.put("/{project_id}/members/{user_id}", response_model=ProjectMembershipOut,
            responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def update_member(
    project_id: int,
    user_id: int,
    payload: ProjectMembershipUpdate,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = _managed_project(db, project_id, actor)
    user = get_org_user(db, user_id, actor)
    if user is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_A_MEMBER)
    old = _role_of(db, project_id, user_id)
    if old == PROJECT_ROLE_LEAD and payload.role != PROJECT_ROLE_LEAD \
            and _other_leads(db, project_id, user_id) == 0:
        raise HTTPException(status_code=400, detail=_DETAIL_LAST_LEAD.format(verb="demote"))
    if old != payload.role:
        db.execute(update(user_projects).where(
            user_projects.c.project_id == project_id, user_projects.c.user_id == user_id,
        ).values(role=payload.role))
        _audit(db, actor, project, "member_role_changed",
               f"Changed {user.name}'s role on '{project.name}': {old} → {payload.role}")
        db.commit()
    return _row(user, payload.role)


@router.delete("/{project_id}/members/{user_id}",
               responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def remove_member(
    project_id: int, user_id: int, actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    project = _managed_project(db, project_id, actor)
    user = get_org_user(db, user_id, actor)
    if user is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_A_MEMBER)
    role = _role_of(db, project_id, user_id)
    if role == PROJECT_ROLE_LEAD and _other_leads(db, project_id, user_id) == 0:
        raise HTTPException(status_code=400, detail=_DETAIL_LAST_LEAD.format(verb="remove"))
    db.execute(delete(user_projects).where(
        user_projects.c.project_id == project_id, user_projects.c.user_id == user_id,
    ))
    _audit(db, actor, project, "member_removed",
           f"Removed {user.name} <{user.email}> from '{project.name}'")
    db.commit()
    return {"message": "Member removed"}
