"""Pydantic schemas: agile taxonomy."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.agile_boards import _AGILE_NAME_MIN
from app.schemas.common import (
    _DATE_FORMAT_ERROR,
    _RELEASE_BEFORE_START,
    ALLOWED_COMPONENT_ASSIGNEE_POLICIES,
    ALLOWED_EPIC_HEALTH,
    ALLOWED_VERSION_STATUSES,
    MIN_LABEL_NAME_LENGTH,
    _strip_and_check_min_length,
    normalize_choice,
    plain_text,
)


# =====================================================================
# Agile — Epics, Releases/Versions, Components, Labels, Collections.
# =====================================================================
class EpicUpdateIn(BaseModel):
    title: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=20000)
    color: Optional[str] = Field(default=None, max_length=20)
    owner_id: Optional[int] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    health: Optional[str] = None
    summary_note: Optional[str] = Field(default=None, max_length=4000)
    version: int = Field(ge=0)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Epic title")

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return plain_text(v)

    @field_validator("health")
    @classmethod
    def _check_health(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_EPIC_HEALTH, "health")

    @field_validator("summary_note")
    @classmethod
    def _strip_note(cls, v: Optional[str]) -> Optional[str]:
        return plain_text(v) if isinstance(v, str) else v

    @field_validator("start_date", "target_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v


class EpicProgressOut(BaseModel):
    child_count: int = 0
    completed_child_count: int = 0
    in_progress_child_count: int = 0
    total_estimate: float = 0
    completed_estimate: float = 0
    unestimated_count: int = 0


class EpicOut(BaseModel):
    id: int
    display_id: str = ""
    title: str
    description: str = ""
    status: str
    status_category: str = "todo"
    project_id: int
    color: str = ""
    owner_id: Optional[int] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    health: str = "unknown"
    summary_note: str = ""
    archived: bool = False
    version: int = 1
    progress: EpicProgressOut = Field(default_factory=EpicProgressOut)


class VersionCreateIn(BaseModel):
    project_id: int
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=2000)
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    owner_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Version name")

    @field_validator("start_date", "release_date")
    @classmethod
    def _check_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(_DATE_FORMAT_ERROR) from exc
        return v

    @model_validator(mode="after")
    def _check_order(self) -> "VersionCreateIn":
        if self.start_date and self.release_date and self.release_date < self.start_date:
            raise ValueError(_RELEASE_BEFORE_START)
        return self


class VersionUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = None
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    owner_id: Optional[int] = None
    version: int = Field(ge=0)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_VERSION_STATUSES, "status")


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    description: str
    status: str
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    owner_id: Optional[int] = None
    archived: bool
    version: int = 1
    created_at: datetime
    updated_at: datetime


class ComponentCreateIn(BaseModel):
    project_id: int
    name: str = Field(max_length=120)
    description: str = Field(default="", max_length=2000)
    lead_id: Optional[int] = None
    default_assignee_policy: str = "none"
    default_assignee_id: Optional[int] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Component name")

    @field_validator("default_assignee_policy")
    @classmethod
    def _check_policy(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_COMPONENT_ASSIGNEE_POLICIES, "default_assignee_policy")


class ComponentUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    lead_id: Optional[int] = None
    default_assignee_policy: Optional[str] = None
    default_assignee_id: Optional[int] = None
    version: int = Field(ge=0)

    @field_validator("default_assignee_policy")
    @classmethod
    def _check_policy(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_COMPONENT_ASSIGNEE_POLICIES, "default_assignee_policy")


class ComponentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    description: str
    lead_id: Optional[int] = None
    default_assignee_policy: str
    default_assignee_id: Optional[int] = None
    archived: bool
    version: int = 1


class LabelCreateIn(BaseModel):
    project_id: int
    display_name: str = Field(max_length=120)
    color: str = Field(default="", max_length=20)

    @field_validator("display_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_LABEL_NAME_LENGTH, "Label name")


class LabelUpdateIn(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    color: Optional[str] = Field(default=None, max_length=20)
    version: int = Field(ge=0)


class LabelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    normalized_name: str
    display_name: str
    color: str
    usage_count: int
    version: int = 1
