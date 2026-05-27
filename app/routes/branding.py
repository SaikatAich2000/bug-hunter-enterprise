"""Per-organization branding settings.

Admins set a logo (data URL), accent colour, and an outgoing email
from-address override. Whoami exposes these so the SPA can theme
itself without an extra round trip.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.database import get_db
from app.models import Activity, Organization, User

router = APIRouter(prefix="/api/branding", tags=["branding"])

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3})?$")
_DATA_URL_PREFIX = re.compile(r"^data:image/(png|jpeg|jpg|svg\+xml|gif|webp);base64,")
_MAX_LOGO_LEN = 200_000  # ~150 KB base64 → fine for inline embedding


class BrandingOut(BaseModel):
    logo_data_url: Optional[str] = None
    accent_color: Optional[str] = None
    email_from_override: Optional[str] = None


class BrandingIn(BaseModel):
    logo_data_url: Optional[str] = Field(None)
    accent_color: Optional[str] = Field(None)
    email_from_override: Optional[str] = Field(None, max_length=254)

    @field_validator("accent_color")
    @classmethod
    def _validate_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        v = v.strip()
        if not _HEX_COLOR.match(v):
            raise ValueError("accent_color must be a CSS hex like #6366f1")
        return v

    @field_validator("logo_data_url")
    @classmethod
    def _validate_logo(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        v = v.strip()
        if len(v) > _MAX_LOGO_LEN:
            raise ValueError("logo too large (max ~150 KB base64)")
        if not _DATA_URL_PREFIX.match(v):
            raise ValueError("logo must be a data: URL (image/png|jpeg|svg+xml|gif|webp)")
        return v

    @field_validator("email_from_override")
    @classmethod
    def _validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        v = v.strip()
        if "@" not in v or " " in v:
            raise ValueError("email_from_override must be a single email address")
        return v


@router.get("", response_model=BrandingOut)
def get_branding(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> BrandingOut:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return BrandingOut(
        logo_data_url=org.logo_data_url,
        accent_color=org.accent_color,
        email_from_override=org.email_from_override,
    )


@router.put("", response_model=BrandingOut)
def update_branding(
    payload: BrandingIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> BrandingOut:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    fields = payload.model_dump(exclude_unset=True)
    changes = []
    for k, v in fields.items():
        old = getattr(org, k, None)
        if v != old:
            setattr(org, k, v)
            changes.append(k)
    if changes:
        db.add(Activity(
            org_id=org.id, bug_id=None, entity_type="organization", entity_id=org.id,
            actor_user_id=user.id, actor_name=user.name,
            action="branding_updated",
            detail=f"Updated branding fields: {', '.join(changes)}",
        ))
        db.commit()
    return BrandingOut(
        logo_data_url=org.logo_data_url,
        accent_color=org.accent_color,
        email_from_override=org.email_from_override,
    )
