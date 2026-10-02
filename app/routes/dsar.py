"""Data-subject requests (GDPR / CCPA): export everything about me, and delete my account.

  GET    /api/auth/data-export   the rows the caller has a personal claim on, as one JSON document
  DELETE /api/auth/account       the right to be forgotten

These are strictly self-service; an admin who needs someone else's data uses the user and
audit endpoints. The last admin of an organization cannot delete their account (it would
orphan the organization).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api_docs import BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404
from app.auth import clear_session_cookie, get_current_user, verify_password
from app.database import get_db
from app.models import (
    ROLE_ADMIN,
    Activity,
    Attachment,
    Bug,
    Comment,
    SavedView,
    User,
    bug_assignees,
)
from app.models import Session as SessionRow

router = APIRouter(prefix="/api/auth", tags=["dsar"])

# Enough for any real account; the export is held in memory and returned as one document.
_EXPORT_ROW_CAP = 5000


class DeleteAccountIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _bug_dict(b: Bug) -> dict[str, Any]:
    return {
        "id": b.id, "display_id": b.display_id, "project_id": b.project_id, "title": b.title,
        "description": b.description, "status": b.status, "priority": b.priority,
        "environment": b.environment, "item_type": b.item_type, "due_date": b.due_date,
        "created_at": _iso(b.created_at), "updated_at": _iso(b.updated_at),
    }


@router.get("/data-export")
def export_my_data(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    def rows(model, *where):
        return db.scalars(select(model).where(*where).limit(_EXPORT_ROW_CAP)).all()

    reported = rows(Bug, Bug.reporter_id == user.id)
    assigned_ids = db.scalars(
        select(bug_assignees.c.bug_id).where(bug_assignees.c.user_id == user.id).limit(_EXPORT_ROW_CAP)
    ).all()
    assigned = rows(Bug, Bug.id.in_(assigned_ids)) if assigned_ids else []
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {
            "id": user.id, "name": user.name, "email": user.email, "role": user.role,
            "is_active": bool(user.is_active), "totp_enabled": bool(user.totp_enabled),
            "created_at": _iso(user.created_at),
        },
        "organization": {"id": user.org_id, "name": user.organization.name},
        "bugs_reported": [_bug_dict(b) for b in reported],
        "bugs_assigned": [_bug_dict(b) for b in assigned],
        "comments": [
            {"id": c.id, "bug_id": c.bug_id, "body": c.body, "created_at": _iso(c.created_at)}
            for c in rows(Comment, Comment.author_user_id == user.id)
        ],
        "attachments_uploaded": [
            {"id": a.id, "bug_id": a.bug_id, "filename": a.filename, "size_bytes": a.size_bytes,
             "created_at": _iso(a.created_at)}
            for a in rows(Attachment, Attachment.uploader_user_id == user.id)
        ],
        "activity_log": [
            {"id": a.id, "action": a.action, "detail": a.detail, "created_at": _iso(a.created_at)}
            for a in rows(Activity, Activity.actor_user_id == user.id)
        ],
        "sessions": [
            {"id": s.id, "ip_address": s.ip_address, "user_agent": s.user_agent,
             "created_at": _iso(s.created_at), "expires_at": _iso(s.expires_at)}
            for s in rows(SessionRow, SessionRow.user_id == user.id)
        ],
        "saved_views": [
            {"id": v.id, "name": v.name, "shared_with_org": bool(v.shared_with_org)}
            for v in rows(SavedView, SavedView.owner_user_id == user.id)
        ],
    }


@router.delete("/account", status_code=204, responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def delete_my_account(
    payload: DeleteAccountIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Permanently delete the caller's account. Sessions, 2FA, saved views, memberships and
    notifications go with it; items they reported keep their content with the reporter cleared."""
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    if user.role == ROLE_ADMIN:
        other_admins = db.scalar(
            select(func.count(User.id)).where(
                User.org_id == user.org_id, User.role == ROLE_ADMIN, User.id != user.id,
                User.is_active.is_(True),
            )
        ) or 0
        if other_admins == 0:
            raise HTTPException(
                status_code=409,
                detail=("You're the last admin of this organization. Promote another admin "
                        "first, then delete your account."),
            )
    # Audit before deleting so the name and email are still known; the actor link is cleared
    # by the foreign key, the written name stays.
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="user", entity_id=user.id,
        actor_user_id=None, actor_name=user.name, action="account_self_deleted",
        detail=f"{user.email} deleted their account",
    ))
    db.delete(user)
    db.commit()
    clear_session_cookie(response)
