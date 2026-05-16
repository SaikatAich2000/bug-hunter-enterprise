"""Users API — strictly scoped to the caller's organization."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    can_manage_users,
    get_current_user,
    hash_password,
    invalidate_outstanding_reset_tokens,
    is_admin,
    require_admin,
)
from app.database import get_db
from app.models import ROLE_ADMIN, Activity, User
from app.schemas import UserIn, UserOut, UserUpdate

router = APIRouter(prefix="/api/users", tags=["users"])


def _audit(db: Session, org_id: int, actor: User | None, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="user", entity_id=entity_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action, detail=detail,
    ))


def _like_escape(needle: str) -> str:
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


# ---------------------------------------------------------------------------
# List — anyone authenticated, only same-org users.
# ---------------------------------------------------------------------------
@router.get("", response_model=list[UserOut])
def list_users(
    include_inactive: bool = Query(default=True),
    q: Optional[str] = None,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[User]:
    stmt = select(User).where(User.org_id == actor.org_id)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    if q:
        like = f"%{_like_escape(q.lower())}%"
        stmt = stmt.where(or_(
            func.lower(User.name).like(like, escape="\\"),
            func.lower(User.email).like(like, escape="\\"),
            func.lower(User.role).like(like, escape="\\"),
        ))
    stmt = stmt.order_by(func.lower(User.name))
    return list(db.scalars(stmt).all())


# ---------------------------------------------------------------------------
# Create — admin only. Bypass the invite flow when the admin wants
# to pre-provision an account with a known password.
# ---------------------------------------------------------------------------
@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if not can_manage_users(actor):
        raise HTTPException(status_code=403, detail="Only org admins can directly create users.")

    user = User(
        org_id=actor.org_id,
        name=payload.name,
        email=payload.email,
        role=payload.role,
        is_active=payload.is_active,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Email already exists. Try inviting them instead, or use a different email.",
        ) from exc
    _audit(
        db, actor.org_id, actor, "user_created", user.id,
        f"Created user '{user.name}' <{user.email}> ({user.role})",
    )
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Read one — same-org only.
# ---------------------------------------------------------------------------
@router.get("/{user_id}", response_model=UserOut)
def get_user(
    user_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if user is None or user.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# ---------------------------------------------------------------------------
# Update — admin only (within their org).
# ---------------------------------------------------------------------------
@router.put("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if not can_manage_users(actor):
        raise HTTPException(status_code=403, detail="Only org admins can edit users.")

    user = db.get(User, user_id)
    if user is None or user.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="User not found")

    fields = payload.model_dump(exclude_unset=True)
    new_password = fields.pop("password", None)
    changes: list[str] = []

    if actor.id == user_id:
        if "role" in fields and fields["role"] != ROLE_ADMIN:
            raise HTTPException(status_code=400, detail="You cannot demote yourself from admin")
        if fields.get("is_active") is False:
            raise HTTPException(status_code=400, detail="You cannot deactivate yourself")

    will_be_role = fields.get("role", user.role)
    will_be_active = fields.get("is_active", user.is_active)
    if user.role == ROLE_ADMIN and (will_be_role != ROLE_ADMIN or not will_be_active):
        n_other_admins = db.scalar(
            select(func.count(User.id))
            .where(
                User.org_id == actor.org_id,
                User.role == ROLE_ADMIN,
                User.is_active.is_(True),
                User.id != user_id,
            )
        ) or 0
        if n_other_admins == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove the last admin. Promote another user first.",
            )

    if fields.get("is_active") is False and user.is_active:
        # Boot every existing session for the deactivated user.
        user.session_version = (user.session_version or 0) + 1

    for key, value in fields.items():
        old = getattr(user, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(user, key, value)

    if new_password:
        user.password_hash = hash_password(new_password)
        user.session_version = (user.session_version or 0) + 1
        invalidate_outstanding_reset_tokens(db, user.id)
        changes.append("password reset by admin")

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already exists") from exc

    if changes:
        _audit(db, actor.org_id, actor, "user_updated", user.id,
               f"Updated user '{user.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Delete — admin only, same-org.
# ---------------------------------------------------------------------------
@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    actor: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    user = db.get(User, user_id)
    if user is None or user.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="User not found")

    if actor.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")

    if user.role == ROLE_ADMIN:
        n_other_admins = db.scalar(
            select(func.count(User.id))
            .where(
                User.org_id == actor.org_id,
                User.role == ROLE_ADMIN,
                User.is_active.is_(True),
                User.id != user_id,
            )
        ) or 0
        if n_other_admins == 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the last admin. Promote another user first.",
            )

    label = f"{user.name} <{user.email}>"
    db.delete(user)
    _audit(db, actor.org_id, actor, "user_deleted", user_id, f"Deleted user {label}")
    db.commit()
    return {"message": "User deleted"}
