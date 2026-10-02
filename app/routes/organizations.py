"""Organization API: the caller's own organization, and its branding.

Only the caller's organization is reachable. No organization id is ever accepted from the
client; it always comes from the authenticated user, so tenant isolation is a property of
the URL space rather than of each handler.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api_docs import BAD_REQUEST_422
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Activity, Organization, User
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
