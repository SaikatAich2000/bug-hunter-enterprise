"""Authentication + tenant-aware authorization primitives.

Responsibilities:
  - Hash + verify passwords (bcrypt).
  - Sign + verify session cookies (itsdangerous).
  - Generate + verify password-reset tokens and invitation tokens.
  - FastAPI dependencies that resolve the current user from the session
    cookie. The user object carries the org_id we use to scope every
    other query in the system.
  - Permission helpers — both org-level role gates and per-project
    membership gates.

Tenant isolation is enforced *by the route handlers*, not by SQLAlchemy
events. Every query that touches user/project/bug/activity data must
filter by the current user's org_id. Project-scoped reads must also pass
through `accessible_project_ids()` so non-admins can't see projects
they aren't a member of.

Token payload format:
  `user_id:session_version[:jti]`

Session-version invalidation (global, blunt): bumped on password change /
admin reset / forced logout — every previously-issued cookie for that
user fails validation immediately.

Per-session revocation (precise, Keycloak-style): admins delete the
matching `sessions` row; just that one device is booted.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import (
    PROJECT_ROLE_LEAD,
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_MEMBER,
    PasswordResetToken,
    Project,
    ProjectMembership,
    Session as SessionRow,
    User,
)

COOKIE_NAME = "bh_session"

# Process-local fallback so dev works without setting SESSION_SECRET.
# In production, set SESSION_SECRET so it survives restarts AND is
# shared across multi-worker uvicorn deployments.
_FALLBACK_SECRET = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt. Cost factor is configurable
    via env (BCRYPT_ROUNDS) because the deployment target is a 0.1-vCPU
    box where 12 rounds is painful on every login. 10 rounds is still
    well within NIST 800-63B guidance for a non-banking workload."""
    if not plain:
        raise ValueError("Password cannot be empty")
    # bcrypt has a 72-byte input limit. Pre-hash with sha256 so long
    # passwords are handled deterministically.
    pre = hashlib.sha256(plain.encode("utf-8")).digest()
    rounds = max(4, min(15, get_settings().BCRYPT_ROUNDS))
    return bcrypt.hashpw(pre, bcrypt.gensalt(rounds=rounds)).decode("utf-8")


def verify_password(plain: str, hashed: Optional[str]) -> bool:
    if not hashed or not plain:
        return False
    pre = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return bcrypt.checkpw(pre, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------
def _signer() -> TimestampSigner:
    s = get_settings().SESSION_SECRET or _FALLBACK_SECRET
    return TimestampSigner(s, salt="bh-session-v4")


def make_session_token(user_id: int, session_version: int = 0, jti: str | None = None) -> str:
    if jti:
        payload = f"{user_id}:{session_version}:{jti}"
    else:
        payload = f"{user_id}:{session_version}"
    return _signer().sign(payload.encode("utf-8")).decode("utf-8")


def parse_session_token(token: str) -> Optional[tuple[int, int, Optional[str]]]:
    if not token:
        return None
    try:
        raw = _signer().unsign(token, max_age=get_settings().SESSION_TTL_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return None
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), parts[2] or None
        if len(parts) == 2:
            return int(parts[0]), int(parts[1]), None
        if len(parts) == 1:
            return int(parts[0]), 0, None
        return None
    except ValueError:
        return None


def new_jti() -> str:
    return secrets.token_urlsafe(24)


def set_session_cookie(response: Response, user: User, jti: str | None = None) -> None:
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=make_session_token(user.id, user.session_version or 0, jti=jti),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# Tokens (password reset + invitations)
# ---------------------------------------------------------------------------
PASSWORD_RESET_TTL = timedelta(hours=2)
INVITATION_TTL = timedelta(days=7)


def generate_random_token() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hex_hash). Email the plaintext, store the hash."""
    raw = secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, h


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# Aliases kept for code that still uses the older names.
generate_reset_token = generate_random_token
hash_reset_token = hash_token


def invalidate_outstanding_reset_tokens(db: Session, user_id: int) -> int:
    now = datetime.now(timezone.utc)
    rows = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None))
        .all()
    )
    for r in rows:
        r.used_at = now
    return len(rows)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
_LAST_SEEN_THROTTLE_SECONDS = 60


def _user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return None
    user_id, session_version, jti = parsed
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    if (user.session_version or 0) != session_version:
        return None

    if jti is not None:
        sess = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
        if sess is None or sess.user_id != user.id:
            return None
        now = datetime.now(timezone.utc)
        expires = sess.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            try:
                db.delete(sess)
                db.commit()
            except Exception:
                db.rollback()
            return None
        last_seen = sess.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if (now - last_seen).total_seconds() >= _LAST_SEEN_THROTTLE_SECONDS:
            try:
                sess.last_seen_at = now
                db.commit()
            except Exception:
                db.rollback()
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    user = _user_from_request(request, db)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_current_user_optional(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    return _user_from_request(request, db)


# ---------------------------------------------------------------------------
# Role gates
# ---------------------------------------------------------------------------
def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_manager_or_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in (ROLE_ADMIN, ROLE_MANAGER):
        raise HTTPException(status_code=403, detail="Manager or admin access required")
    return user


def is_admin(user: User) -> bool:
    return user.role == ROLE_ADMIN


def is_manager_or_admin(user: User) -> bool:
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


# ---------------------------------------------------------------------------
# Project access helpers — the heart of tenant + project isolation.
# ---------------------------------------------------------------------------
def accessible_project_ids(db: Session, user: User) -> list[int]:
    """Project IDs this user can see.

    - Admin sees every project in their org.
    - Everyone else sees only projects they have a ProjectMembership for.
      A user has a row per project they belong to; the role on that row
      decides whether they can manage the project (lead) or just work
      on its bugs (member).

    Cross-org access is impossible because both queries filter on
    `Project.org_id == user.org_id`.
    """
    if user.role == ROLE_ADMIN:
        rows = db.scalars(select(Project.id).where(Project.org_id == user.org_id)).all()
        return list(rows)
    rows = db.scalars(
        select(Project.id)
        .join(ProjectMembership, ProjectMembership.project_id == Project.id)
        .where(
            Project.org_id == user.org_id,
            ProjectMembership.user_id == user.id,
        )
    ).all()
    return list(rows)


def can_access_project(db: Session, user: User, project: Project) -> bool:
    """True if the user can SEE this project (cross-org is always False)."""
    if project.org_id != user.org_id:
        return False
    if user.role == ROLE_ADMIN:
        return True
    pm = db.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.user_id == user.id,
        )
    )
    return pm is not None


def can_manage_project(db: Session, user: User, project: Project) -> bool:
    """True if the user can edit project settings & manage its members.
    Org admins always can; project leads of THIS project also can."""
    if project.org_id != user.org_id:
        return False
    if user.role == ROLE_ADMIN:
        return True
    pm = db.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.user_id == user.id,
        )
    )
    return pm is not None and pm.role == PROJECT_ROLE_LEAD


def can_delete_project(db: Session, user: User, project: Project) -> bool:
    """Project deletion is intentionally narrower than `manage` — only
    org admins can blow a project away. Leads can manage members and
    edit metadata but not nuke the thing."""
    return project.org_id == user.org_id and user.role == ROLE_ADMIN


def can_create_project(user: User) -> bool:
    """Org admins and managers can create projects. Plain members cannot."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_manage_users(user: User) -> bool:
    """Admin-only: directly create users, edit roles, deactivate, delete."""
    return user.role == ROLE_ADMIN


def can_invite(user: User) -> bool:
    """Admins and managers can send invitations. Members cannot."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_view_audit(user: User) -> bool:
    """Audit trail visible to admins and managers — both have
    project-management responsibilities and benefit from the history."""
    return user.role in (ROLE_ADMIN, ROLE_MANAGER)


def can_manage_sessions(user: User) -> bool:
    """Only admins can list / revoke sessions inside their org."""
    return user.role == ROLE_ADMIN


# ---------------------------------------------------------------------------
# Bug-level permission helpers (called from routes/bugs.py)
# ---------------------------------------------------------------------------
def can_edit_bug(db: Session, user: User, project: Project) -> bool:
    """Anyone with access to the project can edit any bug in it. Same
    relaxed policy as v3.x — Jira's default is roughly this too."""
    return can_access_project(db, user, project)


def can_delete_bug(db: Session, user: User, project: Project) -> bool:
    """Bug deletion: admin OR a project lead of THIS project. Members
    can edit but not delete — this protects the audit story."""
    if project.org_id != user.org_id:
        return False
    if user.role == ROLE_ADMIN:
        return True
    return can_manage_project(db, user, project)


# ---------------------------------------------------------------------------
# Resolve-or-404 helpers used by routes to enforce isolation cleanly.
# ---------------------------------------------------------------------------
def get_org_project_or_404(db: Session, project_id: int, user: User) -> Project:
    """Look up a project by ID, but 404 if it doesn't belong to the
    caller's org — same response as truly-missing so we don't leak
    existence across tenants."""
    p = db.get(Project, project_id)
    if p is None or p.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return p
