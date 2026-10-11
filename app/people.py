"""Resolving the people a write refers to (assignees, reporters, owners, managers).

Every route that attaches a user to project-scoped data goes through these helpers, so an
unknown, deactivated or out-of-project user is refused the same way everywhere.
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, Project, User, user_projects


def resolve_users(db: Session, user_ids: Iterable[int], org_id: int) -> list[User]:
    """The users of the organization with these ids; any other id (unknown, or another
    tenant's) is a 400."""
    ids = list(dict.fromkeys(user_ids or []))
    if not ids:
        return []
    rows = db.scalars(select(User).where(User.id.in_(ids), User.org_id == org_id)).all()
    missing = set(ids) - {u.id for u in rows}
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {sorted(missing)}")
    return list(rows)


def resolve_user(db: Session, user_id: Optional[int], org_id: int) -> Optional[User]:
    """The organization's user with this id (None stays None); any other id is a 400."""
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or user.org_id != org_id:
        raise HTTPException(status_code=400, detail=f"User {user_id} does not exist")
    return user


def reject_inactive(users: Iterable[Optional[User]], noun: str = "user") -> None:
    """Refuse new references to deactivated accounts (they can't sign in, so notifications
    would be dead ends). Existing references are left alone; only additions are checked."""
    inactive = sorted(u.name for u in users if u is not None and not u.is_active)
    if inactive:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot assign a deactivated {noun}: {', '.join(inactive)}",
        )


def non_members(db: Session, users: Iterable[Optional[User]], project_id: Optional[int]) -> list[User]:
    """The users who cannot see ``project_id``: neither an admin nor a member of it."""
    candidates = {u.id: u for u in users if u is not None and u.role != ROLE_ADMIN}
    if project_id is None or not candidates:
        return []
    project = db.get(Project, project_id)
    if project is None:
        return list(candidates.values())
    # Another tenant's user can never be a member, whatever the join table says.
    members = set(db.scalars(
        select(user_projects.c.user_id).where(
            user_projects.c.project_id == project_id,
            user_projects.c.user_id.in_(list(candidates)),
        )
    ).all())
    return [u for uid, u in candidates.items() if uid not in members or u.org_id != project.org_id]


def require_project_members(
    db: Session, users: Iterable[Optional[User]], project_id: Optional[int], noun: str = "assignee",
) -> None:
    """Refuse to attach people who can't see the project: they would be notified about work
    they can't open, which leaks its title and description."""
    outsiders = non_members(db, users, project_id)
    if not outsiders:
        return
    project = db.get(Project, project_id)
    names = ", ".join(sorted(u.name for u in outsiders))
    verb = "is not a member" if len(outsiders) == 1 else "are not members"
    raise HTTPException(
        status_code=400,
        detail=(f"{names} {verb} of project '{project.name if project else project_id}'. "
                f"Add them to the project first, or choose another {noun}."),
    )
