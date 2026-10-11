"""Pydantic schemas: agile boards."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import (
    ALLOWED_CARD_COLOR_SCHEMES,
    ALLOWED_COLUMN_CATEGORIES,
    ALLOWED_ESTIMATION_MODES,
    ALLOWED_SWIMLANE_MODES,
    ALLOWED_WIP_ENFORCEMENT,
    DEFAULT_WORKING_WEEKDAYS,
    _strip_and_check_min_length,
    normalize_choice,
    validate_timezone,
)
from app.schemas.users import UserBrief

# =====================================================================
# Agile — foundation, boards, backlog, sprint lifecycle.
#
# =====================================================================
_AGILE_NAME_MIN = 2


class AgileFeatureFlagsIn(BaseModel):
    """Allow-listed boolean project feature flags. Unknown keys
    are rejected (422) — no silent passthrough of arbitrary JSON."""
    automation: bool = False
    teams: bool = False
    parallel_sprints: bool = False
    model_config = ConfigDict(extra="forbid")


class AgileActivateIn(BaseModel):
    feature_flags: AgileFeatureFlagsIn = Field(default_factory=AgileFeatureFlagsIn)


class AgileSettingsOut(BaseModel):
    project_id: int
    agile_enabled: bool
    agile_feature_flags: dict[str, Any] = Field(default_factory=dict)
    board_id: Optional[int] = None


class BoardColumnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    position: int
    category: str
    wip_limit: Optional[int] = None
    min_cards: Optional[int] = None
    wip_enforcement: str = "off"
    color: str = ""
    statuses: list[str] = Field(default_factory=list)
    statuses_by_type: dict[str, list[str]] = Field(default_factory=dict)


class BoardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    board_type: str
    is_default: bool
    estimation_mode: str
    swimlane_mode: str
    card_fields_json: list[Any] = Field(default_factory=list)
    wip_enforcement: str = "off"
    card_color_scheme: str = "none"
    timezone: str = "UTC"
    working_weekdays: list[int] = Field(default_factory=lambda: list(DEFAULT_WORKING_WEEKDAYS))
    working_hours_per_day: float = 8
    version: int = 1
    created_at: datetime
    updated_at: datetime
    columns: list[BoardColumnOut] = Field(default_factory=list)


class BoardUpdateIn(BaseModel):
    """Board settings write. All fields optional; omitted fields
    are unchanged. `version` is required (optimistic concurrency)."""
    name: Optional[str] = Field(default=None, max_length=120)
    estimation_mode: Optional[str] = None
    swimlane_mode: Optional[str] = None
    card_fields_json: Optional[list[Any]] = Field(default=None, max_length=50)
    wip_enforcement: Optional[str] = None
    card_color_scheme: Optional[str] = None
    timezone: Optional[str] = Field(default=None, max_length=64)
    working_weekdays: Optional[list[int]] = Field(default=None, max_length=7)
    working_hours_per_day: Optional[float] = Field(default=None, gt=0, le=24)
    version: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Board name")

    @field_validator("estimation_mode")
    @classmethod
    def _check_estimation(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_ESTIMATION_MODES, "estimation_mode")

    @field_validator("swimlane_mode")
    @classmethod
    def _check_swimlane(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_SWIMLANE_MODES, "swimlane_mode")

    @field_validator("wip_enforcement")
    @classmethod
    def _check_wip(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_WIP_ENFORCEMENT, "wip_enforcement")

    @field_validator("card_color_scheme")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_choice(v, ALLOWED_CARD_COLOR_SCHEMES, "card_color_scheme")

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else validate_timezone(v)

    @field_validator("working_weekdays")
    @classmethod
    def _check_weekdays(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None:
            return None
        if not v or any((not isinstance(d, int) or d < 1 or d > 7) for d in v):
            raise ValueError("working_weekdays must be ISO weekday integers 1-7")
        return sorted(set(v))


class BoardColumnIn(BaseModel):
    """One column in a PUT .../columns request; send ``id`` to keep an existing column."""
    id: Optional[int] = None
    name: str = Field(max_length=80)
    category: str
    wip_limit: Optional[int] = Field(default=None, ge=1)
    min_cards: Optional[int] = Field(default=None, ge=1)
    wip_enforcement: str = "off"
    color: str = Field(default="", max_length=20)
    statuses: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "Column name")

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_COLUMN_CATEGORIES, "category")

    @field_validator("wip_enforcement")
    @classmethod
    def _check_wip(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_WIP_ENFORCEMENT, "wip_enforcement")


class BoardColumnsReplaceIn(BaseModel):
    version: int = Field(ge=0)
    columns: list[BoardColumnIn] = Field(min_length=1, max_length=20)


# --- Backlog ---
class BacklogItemOut(BaseModel):
    id: int
    project_id: int
    title: str
    item_type: str
    status: str
    priority: str
    assignees: list[UserBrief] = Field(default_factory=list)
    epic_id: Optional[int] = None
    owner_id: Optional[int] = None
    story_points: Optional[float] = None
    rank: Optional[str] = None
    flagged: bool = False
    ready_for_sprint: bool = False
    blocked: bool = False
    blocked_reason: str = ""
    mandatory: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    version: int = 1


class BacklogRankIn(BaseModel):
    """Move `item_id` to a new backlog position, between `before_id`/`after_id`
    (either or both may be null = move to an end)."""
    item_id: int
    before_id: Optional[int] = None
    after_id: Optional[int] = None
