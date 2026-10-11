"""Organization API: the caller's own organization, and its branding.

Only the caller's organization is reachable. No organization id is ever accepted from the
client; it always comes from the authenticated user, so tenant isolation is a property of
the URL space rather than of each handler.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.api_docs import BAD_REQUEST_422, BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404
from app.auth import clear_session_cookie, get_current_user, require_admin, verify_password
from app.database import get_db
from app.models import (
    Activity,
    Attachment,
    Bug,
    Comment,
    Invitation,
    Organization,
    Project,
    SavedView,
    User,
    Webhook,
)
from app.schemas import OrganizationOut, OrganizationUpdate

router = APIRouter(prefix="/api", tags=["organization"])

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3})?$")
_DATA_URL_PREFIX = re.compile(r"^data:image/(png|jpeg|svg\+xml|gif|webp);base64,[A-Za-z0-9+/=\s]+$")
_MAX_LOGO_LEN = 200_000  # characters of base64, about 150 KB of image


def _audit(db: Session, actor: User, action: str, detail: str) -> None:
    db.add(Activity(
        org_id=actor.org_id, bug_id=None, entity_type="organization", entity_id=actor.org_id,
        actor_user_id=actor.id, actor_name=actor.name, action=action, detail=detail,
    ))


@router.get("/organization", response_model=OrganizationOut)
def get_my_organization(user: User = Depends(get_current_user)) -> Organization:
    return user.organization


@router.put("/organization", response_model=OrganizationOut, responses=BAD_REQUEST_422)
def update_my_organization(
    payload: OrganizationUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Organization:
    org = user.organization
    changes = []
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None and getattr(org, key) != value:
            changes.append(f"{key}: {getattr(org, key)!r} → {value!r}")
            setattr(org, key, value)
    if changes:
        _audit(db, user, "organization_updated", "Updated organization: " + "; ".join(changes))
        db.commit()
    return org


class BrandingOut(BaseModel):
    logo_data_url: Optional[str] = None
    accent_color: Optional[str] = None
    email_from_override: Optional[str] = None


class BrandingIn(BaseModel):
    """Empty string or null clears a field."""
    logo_data_url: Optional[str] = None
    accent_color: Optional[str] = None
    email_from_override: Optional[str] = Field(default=None, max_length=254)

    @field_validator("accent_color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        if not v:
            return None
        if not _HEX_COLOR.match(v):
            raise ValueError("accent_color must be a CSS hex colour such as #6366f1")
        return v

    @field_validator("logo_data_url")
    @classmethod
    def _check_logo(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        if not v:
            return None
        if len(v) > _MAX_LOGO_LEN:
            raise ValueError("The logo is too large (about 150 KB at most)")
        if not _DATA_URL_PREFIX.match(v):
            raise ValueError("The logo must be a data: URL (png, jpeg, svg, gif or webp)")
        return v

    @field_validator("email_from_override")
    @classmethod
    def _check_sender(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        if not v:
            return None
        # One address, optionally with a display name: "Acme <bugs@acme.com>" or "bugs@acme.com".
        if not re.fullmatch(r"(?:[^<>@\r\n\"]{1,100}\s)?<?[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}>?", v):
            raise ValueError("email_from_override must be a single email address")
        return v


def _branding_out(org: Organization) -> BrandingOut:
    return BrandingOut(
        logo_data_url=org.logo_data_url, accent_color=org.accent_color,
        email_from_override=org.email_from_override,
    )


@router.get("/branding")
def get_branding(user: User = Depends(require_admin)) -> BrandingOut:
    return _branding_out(user.organization)


@router.put("/branding", responses=BAD_REQUEST_422)
def update_branding(
    payload: BrandingIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> BrandingOut:
    org = user.organization
    changed = [
        key for key, value in payload.model_dump(exclude_unset=True).items()
        if getattr(org, key) != value
    ]
    for key in changed:
        setattr(org, key, getattr(payload, key))
    if changed:
        _audit(db, user, "branding_updated", f"Updated branding fields: {', '.join(changed)}")
        db.commit()
    return _branding_out(org)


# --- deleting the whole organization ---------------------------------------------------------

audit_log = logging.getLogger("bug_hunter.audit")


class DeleteOrganizationIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    # The organization's exact name, typed by the admin.
    confirm_name: str = Field(min_length=1, max_length=200)


def _deletion_summary(db: Session, org: Organization) -> dict[str, int]:
    """What deleting the organization removes, counted inside this organization only."""
    org_bugs = select(Bug.id).join(Project, Project.id == Bug.project_id).where(Project.org_id == org.id)

    def count(stmt) -> int:
        return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    return {
        "users": count(select(User.id).where(User.org_id == org.id)),
        "projects": count(select(Project.id).where(Project.org_id == org.id)),
        "items": count(org_bugs),
        "comments": count(select(Comment.id).where(Comment.bug_id.in_(org_bugs))),
        "attachments": count(select(Attachment.id).where(Attachment.bug_id.in_(org_bugs))),
        "webhooks": count(select(Webhook.id).where(Webhook.org_id == org.id)),
        "invitations": count(select(Invitation.id).where(Invitation.org_id == org.id)),
        "saved_views": count(select(SavedView.id).where(SavedView.org_id == org.id)),
        "audit_entries": count(select(Activity.id).where(Activity.org_id == org.id)),
    }


@router.get("/organization/deletion-summary")
def organization_deletion_summary(
    user: User = Depends(require_admin), db: Session = Depends(get_db),
) -> dict:
    """Dry run: exactly what ``POST /api/organization/delete`` would remove. Changes nothing."""
    return {"organization": user.organization.name, "will_delete": _deletion_summary(db, user.organization)}


@router.post("/organization/delete", status_code=204,
             responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def delete_my_organization(
    payload: DeleteOrganizationIn, response: Response,
    user: User = Depends(require_admin), db: Session = Depends(get_db),
) -> None:
    """Permanently delete the caller's organization and everything in it. Admin only; needs the
    admin's password and the organization's name typed exactly. The database cascades the rows,
    so nothing of the tenant survives; the deletion itself is recorded in the audit log stream
    (the in-database audit trail goes with the organization)."""
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    org = user.organization
    if payload.confirm_name != org.name:
        raise HTTPException(status_code=400, detail="Type the organization's exact name to confirm.")
    summary = _deletion_summary(db, org)
    audit_log.warning(
        "organization_deleted org_id=%s name=%r actor_user_id=%s actor_email=%s removed=%s",
        org.id, org.name, user.id, user.email, summary,
    )
    org_id = org.id
    db.expunge_all()
    db.execute(delete(Organization).where(Organization.id == org_id))
    db.commit()
    clear_session_cookie(response)
