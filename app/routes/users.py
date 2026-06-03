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

# S1192: extract duplicated detail string into a module constant.
_DETAIL_USER_NOT_FOUND = "User not found"


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
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)
    return user


# ---------------------------------------------------------------------------
# Update helpers — small, single-purpose, easy to reason about. Each helper
# stays org-aware so the multi-tenant guarantees of the enterprise build
# never leak across orgs.
# ---------------------------------------------------------------------------
def _check_manager_role_limits(actor: User, target: User, fields: dict) -> None:
    """Non-admins can't edit admins and can't grant the admin role.

    Today `can_manage_users` is admin-only on the enterprise build, so this
    is effectively a no-op for the current gate. It stays as defense-in-depth
    in case the gate ever broadens to managers — the moment that happens,
    this check keeps admin escalation locked down.
    """
    if is_admin(actor):
        return
    if target.role == ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only admins can edit admin accounts.",
        )
    if "role" in fields and fields["role"] == ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only admins can grant the admin role.",
        )


def _check_self_edit_guardrails(actor: User, target_id: int, fields: dict) -> None:
    if actor.id != target_id:
        return
    if "role" in fields and fields["role"] != ROLE_ADMIN:
        raise HTTPException(status_code=400, detail="You cannot demote yourself from admin")
    if fields.get("is_active") is False:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")


def _check_last_admin_guardrail(db: Session, target: User, target_id: int, fields: dict) -> None:
    """Don't allow demoting/disabling the last admin — scoped to the target's org."""
    will_be_role = fields.get("role", target.role)
    will_be_active = fields.get("is_active", target.is_active)
    if target.role != ROLE_ADMIN or (will_be_role == ROLE_ADMIN and will_be_active):
        return
    n_other_admins = db.scalar(
        select(func.count(User.id))
        .where(
            User.org_id == target.org_id,
            User.role == ROLE_ADMIN,
            User.is_active.is_(True),
            User.id != target_id,
        )
    ) or 0
    if n_other_admins == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the last admin. Promote another user first.",
        )


def _apply_user_field_changes(user: User, fields: dict, changes: list[str]) -> None:
    """Set every changed field on the user, recording the diff."""
    for key, value in fields.items():
        old = getattr(user, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(user, key, value)


def _apply_admin_password_reset(user: User, db: Session, new_password: str,
                                changes: list[str]) -> None:
    """An admin password-reset is a security event — kick existing sessions
    and revoke any outstanding reset tokens."""
    user.password_hash = hash_password(new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidate_outstanding_reset_tokens(db, user.id)
    changes.append("password reset by admin")


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
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)

    fields = payload.model_dump(exclude_unset=True)
    new_password = fields.pop("password", None)
    changes: list[str] = []

    _check_manager_role_limits(actor, user, fields)
    _check_self_edit_guardrails(actor, user_id, fields)
    _check_last_admin_guardrail(db, user, user_id, fields)

    # If the admin is deactivating someone, kick their existing sessions.
    if fields.get("is_active") is False and user.is_active:
        user.session_version = (user.session_version or 0) + 1

    _apply_user_field_changes(user, fields, changes)

    if new_password:
        _apply_admin_password_reset(user, db, new_password, changes)

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
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)

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
