"""Users API. Read: any user; create/update: admin or manager; delete: admin only.

Every endpoint is confined to the caller's organization: users of other organizations
do not exist as far as it is concerned.

Guardrails: only admins grant/revoke admin; no self-demote/deactivate/delete;
the last remaining admin of an organization is protected.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import (
    accessible_project_ids,
    project_ids_for_user,
    set_user_projects,
)
from app.api_docs import BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404 as _CREATE_RESPONSES
from app.api_docs import BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404 as _UPDATE_RESPONSES
from app.api_docs import BAD_REQUEST_NOT_FOUND_404 as _DELETE_RESPONSES
from app.api_docs import NOT_FOUND_404 as _GET_RESPONSES
from app.auth import (
    get_current_user,
    hash_password,
    invalidate_outstanding_reset_tokens,
    require_admin,
    require_manager_or_admin,
)
from app.database import get_db
from app.models import ROLE_ADMIN, Activity, Project, TotpRecoveryCode, User, user_projects
from app.password_breach import is_password_breached
from app.schemas import UserIn, UserOut, UserUpdate


def _reject_if_breached(plain: str) -> None:
    """Reject HIBP-breached passwords. Duplicated from routes/auth.py to avoid a circular import."""
    if is_password_breached(plain):
        raise HTTPException(
            status_code=400,
            detail="This password appears in a known breach corpus. "
                   "Please choose a different one.",
        )

router = APIRouter(prefix="/api/users", tags=["users"])


_DETAIL_USER_NOT_FOUND = "User not found"
_DETAIL_EMAIL_EXISTS = "Email already exists"

def _audit(db: Session, actor: User, action: str, entity_id: int, detail: str) -> None:
    db.add(Activity(
        org_id=actor.org_id, bug_id=None, entity_type="user", entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


def _org_user_or_404(db: Session, user_id: int, actor: User) -> User:
    user = db.get(User, user_id)
    if user is None or user.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail=_DETAIL_USER_NOT_FOUND)
    return user


def _like_escape(needle: str) -> str:
    """Escape LIKE wildcards; pair with escape='\\\\' on the LIKE clause."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _email_in_use(db: Session, email: str, exclude_id: Optional[int] = None) -> bool:
    """Case-insensitive uniqueness check — catches mixed-case rows the DB constraint misses."""
    stmt = select(User.id).where(func.lower(User.email) == email.lower())
    if exclude_id is not None:
        stmt = stmt.where(User.id != exclude_id)
    return db.scalar(stmt) is not None


def _validate_assignable_projects(db: Session, actor: User, project_ids: list[int]) -> None:
    """All ids must be projects of the actor's organization; managers may only assign
    projects in their own scope."""
    if not project_ids:
        return
    existing = set(db.scalars(
        select(Project.id).where(Project.id.in_(project_ids), Project.org_id == actor.org_id)
    ).all())
    missing = [pid for pid in project_ids if pid not in existing]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown project ids: {sorted(missing)}")
    accessible = accessible_project_ids(db, actor)
    outside = [pid for pid in project_ids if pid not in accessible]
    if outside:
        raise HTTPException(
            status_code=403,
            detail="You can only assign projects you have access to.",
        )


def _user_out(db: Session, user: User, viewer: User | None = None) -> dict:
    """Serialize a user including project tags; two-factor status only for an admin viewer."""
    return {
        "id": user.id, "name": user.name, "email": user.email,
        "role": user.role, "is_active": user.is_active,
        "created_at": user.created_at, "updated_at": user.updated_at,
        "project_ids": project_ids_for_user(db, user.id),
        "totp_enabled": bool(user.totp_enabled) if viewer is not None and viewer.role == ROLE_ADMIN else None,
    }


def _projects_by_user(db: Session, user_ids: list[int]) -> dict[int, list[int]]:
    """{user_id: [project_id, ...]} for a batch of users in one query."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(user_projects.c.user_id, user_projects.c.project_id)
        .where(user_projects.c.user_id.in_(user_ids))
        .order_by(user_projects.c.project_id)
    ).all()
    out: dict[int, list[int]] = {}
    for uid, pid in rows:
        out.setdefault(int(uid), []).append(int(pid))
    return out


@router.get("", response_model=list[UserOut])
def list_users(
    include_inactive: bool = Query(default=True),
    q: Optional[str] = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    stmt = select(User).where(User.org_id == _user.org_id)
    if not include_inactive:
        stmt = stmt.where(User.is_active.is_(True))
    if q:
        # name/email only — searching role would let anyone enumerate admins
        like = f"%{_like_escape(q.lower())}%"
        stmt = stmt.where(or_(
            func.lower(User.name).like(like, escape="\\"),
            func.lower(User.email).like(like, escape="\\"),
        ))
    stmt = stmt.order_by(func.lower(User.name)).limit(500)
    rows = list(db.scalars(stmt).all())
    # not project-scoped: pickers need to show the whole organization
    proj_map = _projects_by_user(db, [u.id for u in rows])
    return [
        {
            "id": u.id, "name": u.name, "email": u.email, "role": u.role,
            "is_active": u.is_active, "created_at": u.created_at,
            "updated_at": u.updated_at, "project_ids": proj_map.get(u.id, []),
            "totp_enabled": bool(u.totp_enabled) if _user.role == ROLE_ADMIN else None,
        }
        for u in rows
    ]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             responses=_CREATE_RESPONSES)
def create_user(
    payload: UserIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    # managers can create users but cannot grant admin
    if payload.role == "admin" and actor.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can create admin users.",
        )
    _reject_if_breached(payload.password)
    # validate first so bad ids surface as 400/403, not a constraint error
    _validate_assignable_projects(db, actor, payload.project_ids)
    if _email_in_use(db, payload.email):
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS)
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
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS) from exc
    if payload.project_ids:
        set_user_projects(db, user.id, payload.project_ids)
    proj_note = (f"; projects: {sorted(payload.project_ids)}"
                 if payload.project_ids else "")
    _audit(db, actor, "user_created", user.id,
           f"Created user '{user.name}' <{user.email}> ({user.role}){proj_note}")
    db.commit()
    db.refresh(user)
    return _user_out(db, user, actor)


@router.get("/{user_id}", response_model=UserOut, responses=_GET_RESPONSES)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> dict:
    return _user_out(db, _org_user_or_404(db, user_id, _user), _user)


def _check_manager_role_limits(actor: User, target: User, fields: dict) -> None:
    """Managers can't edit admins and can't grant admin. Admins skip this."""
    if actor.role == "admin":
        return
    if target.role == "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can edit admin accounts.",
        )
    if "role" in fields and fields["role"] == "admin":
        raise HTTPException(
            status_code=403,
            detail="Only admins can grant the admin role.",
        )


def _check_self_edit_guardrails(actor: User, target_id: int, fields: dict) -> None:
    if actor.id != target_id:
        return
    if "role" in fields and fields["role"] != "admin":
        raise HTTPException(status_code=400, detail="You cannot demote yourself from admin")
    if fields.get("is_active") is False:
        raise HTTPException(status_code=400, detail="You cannot deactivate yourself")


def _count_other_active_admins(db: Session, org_id: int, exclude_id: int) -> int:
    """Count the organization's active admins other than exclude_id.
    FOR UPDATE closes the concurrent double-demote race (no-op on SQLite)."""
    admin_ids = db.scalars(
        select(User.id)
        .where(User.org_id == org_id, User.role == "admin", User.is_active.is_(True))
        .with_for_update()
    ).all()
    return sum(1 for aid in admin_ids if aid != exclude_id)


def _check_last_admin_guardrail(db: Session, target: User, target_id: int, fields: dict) -> None:
    """Block demoting/disabling the last admin."""
    will_be_role = fields.get("role", target.role)
    will_be_active = fields.get("is_active", target.is_active)
    if target.role != "admin" or (will_be_role == "admin" and will_be_active):
        return
    if _count_other_active_admins(db, target.org_id, target_id) == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot remove the last admin. Promote another user first.",
        )


def _apply_user_field_changes(user: User, fields: dict, changes: list[str]) -> None:
    """Apply changed fields, recording the diff."""
    for key, value in fields.items():
        old = getattr(user, key)
        if old != value:
            changes.append(f"{key}: {old!r} → {value!r}")
            setattr(user, key, value)


def _apply_project_tag_changes(db: Session, user_id: int,
                               new_project_ids: Optional[list[int]],
                               changes: list[str]) -> None:
    """Replace project memberships. None = omitted (keep); an explicit list (even empty) replaces."""
    if new_project_ids is None:
        return
    old = set(project_ids_for_user(db, user_id))
    new = set(new_project_ids)
    if old == new:
        return
    set_user_projects(db, user_id, new_project_ids)
    changes.append(f"projects: {sorted(old)} → {sorted(new)}")


def _apply_admin_password_reset(user: User, db: Session, new_password: str,
                                changes: list[str]) -> None:
    """Admin password reset: invalidate existing sessions and outstanding reset tokens."""
    # still HIBP-checked — the user may keep the assigned password
    _reject_if_breached(new_password)
    user.password_hash = hash_password(new_password)
    user.session_version = (user.session_version or 0) + 1
    invalidate_outstanding_reset_tokens(db, user.id)
    changes.append("password reset by admin")


@router.put("/{user_id}", response_model=UserOut, responses=_UPDATE_RESPONSES)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> dict:
    user = _org_user_or_404(db, user_id, actor)

    fields = payload.model_dump(exclude_unset=True)
    new_password = fields.pop("password", None)
    # project_ids is not a User column; pop before the generic setattr loop
    new_project_ids = fields.pop("project_ids", None)
    changes: list[str] = []

    _check_manager_role_limits(actor, user, fields)
    _check_self_edit_guardrails(actor, user_id, fields)
    _check_last_admin_guardrail(db, user, user_id, fields)
    if new_project_ids is not None:
        _validate_assignable_projects(db, actor, new_project_ids)

    new_email = fields.get("email")
    if new_email and _email_in_use(db, new_email, exclude_id=user_id):
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS)

    # deactivation invalidates existing sessions immediately
    if fields.get("is_active") is False and user.is_active:
        user.session_version = (user.session_version or 0) + 1

    _apply_user_field_changes(user, fields, changes)
    _apply_project_tag_changes(db, user_id, new_project_ids, changes)

    if new_password:
        _apply_admin_password_reset(user, db, new_password, changes)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_EMAIL_EXISTS) from exc

    if changes:
        _audit(db, actor, "user_updated", user.id,
               f"Updated user '{user.name}': " + "; ".join(changes))
    db.commit()
    db.refresh(user)
    return _user_out(db, user, actor)


@router.post("/{user_id}/reset-2fa", status_code=204, responses=_DELETE_RESPONSES)
def reset_user_two_factor(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> None:
    """For a person who lost their authenticator and recovery codes: switch their two-factor
    sign-in off so they can sign in with the password and enrol again."""
    user = _org_user_or_404(db, user_id, actor)
    if not user.totp_enabled and not user.totp_secret:
        raise HTTPException(status_code=400, detail="Two-factor sign-in is not on for this user")
    user.totp_secret = None
    user.totp_enabled = False
    user.totp_enrolled_at = None
    user.totp_last_step = None
    db.execute(delete(TotpRecoveryCode).where(TotpRecoveryCode.user_id == user.id))
    _audit(db, actor, "2fa_reset_by_admin", user.id, f"Turned off two-factor sign-in of {user.email}")
    db.commit()


@router.delete("/{user_id}", responses=_DELETE_RESPONSES)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> dict[str, str]:
    user = _org_user_or_404(db, user_id, actor)

    if actor.id == user_id:
        raise HTTPException(status_code=400, detail="You cannot delete yourself")

    if user.role == "admin" and _count_other_active_admins(db, user.org_id, user_id) == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the last admin. Promote another user first.",
        )

    label = f"{user.name} <{user.email}>"
    db.delete(user)
    _audit(db, actor, "user_deleted", user_id, f"Deleted user {label}")
    db.commit()
    return {"message": "User deleted"}
