"""Pydantic schemas: projects."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import MIN_PROJECT_NAME_LENGTH, _strip_and_check_min_length
from app.schemas.users import _normalize_project_role


# --- Project ---
class ProjectIn(BaseModel):
    name: str = Field(max_length=120)
    # Short identifier (e.g. "WEB"); generated from the name when omitted.
    key: Optional[str] = Field(default=None, max_length=16)
    description: str = Field(default="", max_length=1000)
    color: str = Field(default="#c9764f", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_PROJECT_NAME_LENGTH, "Project name")

    @field_validator("key")
    @classmethod
    def _check_key(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip().upper()
        if not v:
            return None
        if not re.match(r"^[A-Z][A-Z0-9]{1,15}$", v):
            raise ValueError(
                "Project key must be 2-16 characters, start with a letter and use only A-Z and 0-9"
            )
        return v

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


class ProjectCreateIn(ProjectIn):
    """Create payload. `agile_enabled` is honored only on POST so a freshly
    created project gets its default Sprint Board provisioned in the same
    transaction; PUT deliberately ignores the field (Agile is turned on/off
    through /api/agile/projects/{id}/enable|disable, which enforces the
    board/status provisioning and the active-sprint guard)."""
    agile_enabled: bool = False


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    key: str = ""
    description: str
    color: str
    created_at: datetime
    updated_at: datetime
    # Route-populated: whether the caller may manage the project's members and fields.
    can_manage: bool = False
    member_count: int = 0


class ProjectMembershipIn(BaseModel):
    user_id: int
    role: str = Field(default="member")

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_project_role(v)


class ProjectMembershipUpdate(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _normalize_project_role(v)


class ProjectMembershipOut(BaseModel):
    """One member of a project: organization role and project role together."""
    user_id: int
    user_name: str
    user_email: str
    user_role: str
    project_role: str
