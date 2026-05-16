"""Project membership API — who belongs to which project, and as what.

URL design: nested under /api/projects/{id}/members so the project_id
in the URL doubles as the authorization scope. Cross-org access is
blocked by `get_org_project_or_404`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    can_manage_project,
    get_current_user,
    get_org_project_or_404,
)
from app.database import get_db
from app.models import (
    PROJECT_ROLE_LEAD,
    Activity,
    Project,
    ProjectMembership,
    User,
)
from app.schemas import (
    ProjectMembershipIn,
    ProjectMembershipOut,
    ProjectMembershipUpdate,
)

router = APIRouter(prefix="/api/projects", tags=["memberships"])


def _audit(
    db: Session, org_id: int, actor: User, action: str, detail: str,
    project_id: int | None = None,
) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="project_membership",
        entity_id=project_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


def _row(pm: ProjectMembership, user: User) -> dict:
    return {
        "id": pm.id,
        "user_id": user.id,
        "user_name": user.name,
        "user_email": user.email,
        "user_role": user.role,
        "project_role": pm.role,
        "created_at": pm.created_at,
    }


# ---------------------------------------------------------------------------
# List members of a project
# ---------------------------------------------------------------------------
@router.get("/{project_id}/members", response_model=list[ProjectMembershipOut])
def list_members(
    project_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    project = get_org_project_or_404(db, project_id, actor)

    # Visibility: anyone with project access can see who else is on it.
    # Org admins always. Otherwise need a membership row.
    from app.auth import can_access_project
    if not can_access_project(db, actor, project):
        raise HTTPException(status_code=404, detail="Project not found")

    rows = db.scalars(
        select(ProjectMembership).where(ProjectMembership.project_id == project_id)
    ).all()
    user_ids = sorted({r.user_id for r in rows})
    user_map = {}
    if user_ids:
        for u in db.scalars(select(User).where(User.id.in_(user_ids))).all():
            user_map[u.id] = u

    out = []
    for r in rows:
        u = user_map.get(r.user_id)
        if u is None:
            continue
        out.append(_row(r, u))
    # Sort: leads first, then alphabetical by name.
    out.sort(key=lambda r: (r["project_role"] != PROJECT_ROLE_LEAD, r["user_name"].lower()))
    return out


# ---------------------------------------------------------------------------
# Add a member
# ---------------------------------------------------------------------------
@router.post(
    "/{project_id}/members",
    response_model=ProjectMembershipOut,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    project_id: int,
    payload: ProjectMembershipIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = get_org_project_or_404(db, project_id, actor)
    if not can_manage_project(db, actor, project):
        raise HTTPException(
            status_code=403,
            detail="Only org admins or this project's leads can manage members.",
        )

    user = db.get(User, payload.user_id)
    if user is None or user.org_id != actor.org_id:
        raise HTTPException(status_code=400, detail="Unknown user")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="That user account is disabled.")

    pm = ProjectMembership(
        project_id=project.id, user_id=user.id, role=payload.role,
    )
    db.add(pm)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="That user is already a member of this project.",
        ) from exc

    _audit(
        db, actor.org_id, actor, "member_added",
        f"Added {user.name} <{user.email}> to '{project.name}' as {payload.role}",
        project_id=project.id,
    )
    db.commit()
    db.refresh(pm)
    return _row(pm, user)


# ---------------------------------------------------------------------------
# Update a member's project role
# ---------------------------------------------------------------------------
@router.put("/{project_id}/members/{user_id}", response_model=ProjectMembershipOut)
def update_member(
    project_id: int,
    user_id: int,
    payload: ProjectMembershipUpdate,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = get_org_project_or_404(db, project_id, actor)
    if not can_manage_project(db, actor, project):
        raise HTTPException(status_code=403, detail="Forbidden")

    pm = db.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user_id,
        )
    )
    if pm is None:
        raise HTTPException(status_code=404, detail="Membership not found")

    user = db.get(User, user_id)
    if user is None or user.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="Membership not found")

    # If demoting the last lead, block it — the project would become
    # unmanageable for non-admin users.
    if pm.role == PROJECT_ROLE_LEAD and payload.role != PROJECT_ROLE_LEAD:
        other_leads = db.scalar(
            select(ProjectMembership.id).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.role == PROJECT_ROLE_LEAD,
                ProjectMembership.user_id != user_id,
            )
        )
        if other_leads is None:
            raise HTTPException(
                status_code=400,
                detail="Cannot demote the last project lead. Promote another member first.",
            )

    if pm.role != payload.role:
        old = pm.role
        pm.role = payload.role
        _audit(
            db, actor.org_id, actor, "member_role_changed",
            f"Changed {user.name}'s role on '{project.name}': {old} → {pm.role}",
            project_id=project.id,
        )
    db.commit()
    db.refresh(pm)
    return _row(pm, user)


# ---------------------------------------------------------------------------
# Remove a member
# ---------------------------------------------------------------------------
@router.delete("/{project_id}/members/{user_id}")
def remove_member(
    project_id: int,
    user_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    project = get_org_project_or_404(db, project_id, actor)
    if not can_manage_project(db, actor, project):
        raise HTTPException(status_code=403, detail="Forbidden")

    pm = db.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user_id,
        )
    )
    if pm is None:
        raise HTTPException(status_code=404, detail="Membership not found")

    # Block removing the last lead.
    if pm.role == PROJECT_ROLE_LEAD:
        other_leads = db.scalar(
            select(ProjectMembership.id).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.role == PROJECT_ROLE_LEAD,
                ProjectMembership.user_id != user_id,
            )
        )
        if other_leads is None:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove the last project lead. Promote another member first.",
            )

    user = db.get(User, user_id)
    label = (user.name + " <" + user.email + ">") if user else f"user #{user_id}"
    db.delete(pm)
    _audit(
        db, actor.org_id, actor, "member_removed",
        f"Removed {label} from '{project.name}'",
        project_id=project.id,
    )
    db.commit()
    return {"message": "Member removed"}
