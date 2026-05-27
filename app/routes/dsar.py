"""GDPR / CCPA Data Subject Access Request endpoints.

  GET    /api/auth/data-export   — every row I have a personal claim on,
                                   bundled into a JSON blob the user can
                                   download.
  DELETE /api/auth/account       — the "right to be forgotten" path.
                                   Wipes the user (cascades clean up
                                   sessions, recovery codes, etc.) and
                                   anonymises any remaining FK pointers
                                   (set to NULL via ON DELETE SET NULL).

We deliberately don't expose someone else's data here — admins use the
existing /api/users + /api/audit endpoints for that. This is the
"self-service" view.

Admins are blocked from deleting themselves if they're the LAST admin
in their org (would orphan it). The endpoint returns 409 with a clear
message in that case.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    clear_session_cookie, get_current_user, verify_password,
)
from app.database import get_db
from app.models import (
    Activity, Attachment, Bug, Comment, Invitation, Organization,
    PasswordResetToken, ProjectMembership, ROLE_ADMIN,
    SavedView, Session as SessionRow, TotpRecoveryCode, User,
)

router = APIRouter(prefix="/api/auth", tags=["dsar"])


class DeleteAccountIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=200)


@router.get("/data-export")
def export_my_data(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Bundle every row associated with this user. The response is
    sized so it fits comfortably in memory and ships as a single JSON
    object — at typical usage a user has at most a few hundred
    comments + bugs + activities."""

    def _bug_dict(b: Bug) -> dict:
        return {
            "id": b.id, "project_id": b.project_id, "title": b.title,
            "description": b.description, "status": b.status,
            "priority": b.priority, "environment": b.environment,
            "due_date": b.due_date,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        }

    reported = list(db.scalars(select(Bug).where(Bug.reporter_id == user.id)).all())
    # Bugs I'm assigned to (M2M) — query via the secondary table.
    from app.models import bug_assignees
    assigned_ids = list(db.scalars(
        select(bug_assignees.c.bug_id).where(bug_assignees.c.user_id == user.id)
    ).all())
    assigned = list(db.scalars(select(Bug).where(Bug.id.in_(assigned_ids))).all()) if assigned_ids else []

    my_comments = list(db.scalars(
        select(Comment).where(Comment.author_user_id == user.id)
    ).all())
    my_attachments = list(db.scalars(
        select(Attachment).where(Attachment.uploader_user_id == user.id)
    ).all())
    my_activity = list(db.scalars(
        select(Activity).where(Activity.actor_user_id == user.id)
    ).all())
    my_sessions = list(db.scalars(
        select(SessionRow).where(SessionRow.user_id == user.id)
    ).all())
    my_views = list(db.scalars(
        select(SavedView).where(SavedView.owner_user_id == user.id)
    ).all())

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "id": user.id, "name": user.name, "email": user.email,
            "role": user.role, "is_active": bool(user.is_active),
            "totp_enabled": bool(user.totp_enabled),
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "organization": {
            "id": user.org_id, "name": user.organization.name if user.organization else None,
        },
        "bugs_reported": [_bug_dict(b) for b in reported],
        "bugs_assigned": [_bug_dict(b) for b in assigned],
        "comments": [
            {"id": c.id, "bug_id": c.bug_id, "body": c.body,
             "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in my_comments
        ],
        "attachments_uploaded": [
            {"id": a.id, "bug_id": a.bug_id, "filename": a.filename,
             "size_bytes": a.size_bytes,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in my_attachments
        ],
        "activity_log": [
            {"id": a.id, "action": a.action, "detail": a.detail,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in my_activity
        ],
        "sessions": [
            {"id": s.id, "ip_address": s.ip_address, "user_agent": s.user_agent,
             "created_at": s.created_at.isoformat() if s.created_at else None,
             "expires_at": s.expires_at.isoformat() if s.expires_at else None}
            for s in my_sessions
        ],
        "saved_views": [
            {"id": v.id, "name": v.name, "shared_with_org": bool(v.shared_with_org)}
            for v in my_views
        ],
    }


@router.delete("/account", status_code=204)
def delete_my_account(
    payload: DeleteAccountIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete the caller's account. Cascades clear sessions
    + TOTP rows + saved views + project memberships. Bugs they reported
    keep their content; reporter_id is nulled (ON DELETE SET NULL).

    The last admin in an org can't delete themselves — they'd orphan
    the org. We surface a clear 409 instead.
    """
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")

    if user.role == ROLE_ADMIN:
        other_admins = db.query(User).filter(
            User.org_id == user.org_id,
            User.role == ROLE_ADMIN,
            User.id != user.id,
            User.is_active.is_(True),
        ).count()
        if other_admins == 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    "You're the last admin in this organization. Promote another "
                    "admin first, then delete your account."
                ),
            )

    # Audit BEFORE deletion so the actor name + email are still
    # available. Org-id stays valid even when the user goes away.
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="user", entity_id=user.id,
        actor_user_id=None, actor_name=user.name,
        action="account_self_deleted",
        detail=f"{user.email} deleted their account via DSAR endpoint",
    ))
    db.commit()

    # Cascade-delete the user row.
    db.delete(user)
    db.commit()
    clear_session_cookie(response)
