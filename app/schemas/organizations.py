"""Pydantic schemas: organizations and invitations."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import (
    MIN_NAME_LENGTH,
    MIN_ORG_NAME_LENGTH,
    _strip_and_check_min_length,
    _validate_email,
)
from app.schemas.users import _check_password_strength, _normalize_role


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    slug: str
    description: str
    created_at: datetime


class OrganizationUpdate(BaseModel):
    """Admin-only patch of the organization's details."""
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, MIN_ORG_NAME_LENGTH, "Organization name")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class InvitationCreate(BaseModel):
    """An admin or manager invites someone into their organization."""
    email: str = Field(max_length=254)
    role: str = Field(default="user")
    # Projects the invitee joins on acceptance; foreign ids are refused by the route.
    project_ids: list[int] = Field(default_factory=list, max_length=100)
    # Join those projects as lead instead of plain member.
    as_lead: bool = False

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return _validate_email(v)

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_role(v)

    @field_validator("project_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    role: str
    invited_by_user_id: Optional[int] = None
    invited_by_name: str
    initial_project_ids: str
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime


class InvitationPreview(BaseModel):
    """What an invitee sees before accepting: the organization, role and inviter's name."""
    email: str
    organization_name: str
    role: str
    expires_at: datetime
    invited_by_name: str


class InvitationAccept(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    name: str = Field(max_length=120)
    password: str

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_NAME_LENGTH, "Name")

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _check_password_strength(v)
