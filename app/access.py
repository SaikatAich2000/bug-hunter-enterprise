"""Tenant and project-scoped access control (pure logic, no HTTP).

Every organization is a tenant. ``accessible_project_ids`` returns the project ids an
actor may see: every project of the actor's own organization for an admin, the projects
the actor is a member of for anyone else (an empty set sees nothing). Nothing outside the
actor's organization is ever reachable, so a route that filters by this set cannot leak
across tenants. Routes return out-of-scope resources as 404 so the restriction doesn't
leak what exists.
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.models import (
    PROJECT_ROLE_LEAD,
    PROJECT_ROLE_MEMBER,
    ROLE_ADMIN,
    Bug,
    Event,
    Project,
    User,
    user_projects,
)


def accessible_project_ids(db: Session, user: User) -> set[int]:
    """Project ids ``user`` may access; an empty set means they see nothing."""
    if user.role == ROLE_ADMIN:
        stmt = select(Project.id).where(Project.org_id == user.org_id)
    else:
        stmt = (
            select(user_projects.c.project_id)
            .join(Project, Project.id == user_projects.c.project_id)
            .where(user_projects.c.user_id == user.id, Project.org_id == user.org_id)
        )
    return {int(pid) for pid in db.scalars(stmt).all()}


def project_ids_for_user(db: Session, user_id: int) -> list[int]:
    """Project ids the user is a member of, sorted ascending (role-agnostic)."""
    rows = db.scalars(
        select(user_projects.c.project_id)
        .where(user_projects.c.user_id == user_id)
        .order_by(user_projects.c.project_id)
    ).all()
    return [int(pid) for pid in rows]


def can_access_project(accessible: set[int], project_id: Optional[int]) -> bool:
    """True if ``project_id`` is in scope; an item without a project never matches."""
    return project_id is not None and project_id in accessible


def scope_bug_query(stmt, accessible: set[int]):
    """Restrict a Bug statement to the actor's projects."""
    return stmt.where(Bug.project_id.in_(accessible))


def scope_event_query(stmt, user: User, accessible: set[int]):
    """Restrict an Event statement to the actor's organization. Admins see every event of
    the organization, including those without a project; others only their projects'."""
    stmt = stmt.where(Event.org_id == user.org_id)
    if user.role == ROLE_ADMIN:
        return stmt
    return stmt.where(Event.project_id.in_(accessible))


def event_visible(user: User, accessible: set[int], event: Event) -> bool:
    """True if ``user`` may see ``event``: it belongs to their organization and, unless they
    are an admin, to one of their projects."""
    if event.org_id != user.org_id:
        return False
    return user.role == ROLE_ADMIN or can_access_project(accessible, event.project_id)


def get_org_project_or_404(db: Session, project_id: int, user: User) -> Project:
    """The project, or 404 if it is missing or belongs to another organization."""
    project = db.get(Project, project_id)
    if project is None or project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def get_org_user(db: Session, user_id: int, actor: User) -> Optional[User]:
    """The user if they belong to the actor's organization, else None."""
    user = db.get(User, user_id)
    return user if user is not None and user.org_id == actor.org_id else None


def project_role(db: Session, user_id: int, project_id: int) -> Optional[str]:
    """The user's role on the project, or None when they are not a member."""
    return db.scalar(
        select(user_projects.c.role).where(
            user_projects.c.user_id == user_id, user_projects.c.project_id == project_id
        )
    )


def can_manage_project(db: Session, user: User, project: Project) -> bool:
    """Edit a project's members and custom fields: admins of the organization and the
    project's leads."""
    if project.org_id != user.org_id:
        return False
    return user.role == ROLE_ADMIN or project_role(db, user.id, project.id) == PROJECT_ROLE_LEAD


# Membership mutation helpers; callers validate project ids and commit.
def set_user_projects(db: Session, user_id: int, project_ids: Iterable[int]) -> None:
    """Replace the user's memberships with ``project_ids`` (deduped), keeping the role of
    every project they stay on. Does not commit."""
    wanted = list(dict.fromkeys(project_ids))
    kept = {
        int(pid): role
        for pid, role in db.execute(
            select(user_projects.c.project_id, user_projects.c.role).where(
                user_projects.c.user_id == user_id
            )
        ).all()
    }
    db.execute(delete(user_projects).where(user_projects.c.user_id == user_id))
    for pid in wanted:
        db.execute(insert(user_projects).values(
            user_id=user_id, project_id=pid, role=kept.get(pid, PROJECT_ROLE_MEMBER)
        ))


def add_user_project(
    db: Session, user_id: int, project_id: int, role: str = PROJECT_ROLE_MEMBER
) -> None:
    """Add a membership if absent (idempotent). Does not commit."""
    exists = db.scalar(
        select(user_projects.c.project_id).where(
            user_projects.c.user_id == user_id,
            user_projects.c.project_id == project_id,
        )
    )
    if exists is None:
        db.execute(insert(user_projects).values(
            user_id=user_id, project_id=project_id, role=role
        ))


__all__ = [
    "accessible_project_ids",
    "project_ids_for_user",
    "can_access_project",
    "scope_bug_query",
    "scope_event_query",
    "event_visible",
    "get_org_project_or_404",
    "get_org_user",
    "project_role",
    "can_manage_project",
    "set_user_projects",
    "add_user_project",
]
