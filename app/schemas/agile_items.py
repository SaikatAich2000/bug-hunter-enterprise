"""Pydantic schemas: agile items."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.agile_boards import _AGILE_NAME_MIN
from app.schemas.agile_taxonomy import EpicOut
from app.schemas.bugs import CustomValueIn
from app.schemas.common import (
    _DATE_FORMAT_ERROR,
    _END_DATE_CANNOT_PRECEDE_START,
    AGILE_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    MIN_TITLE_LENGTH,
    _strip_and_check_min_length,
    normalize_choice,
    plain_text,
)
from app.schemas.users import UserBrief


# --- Work-item hierarchy ---
class WorkItemHierarchyIn(BaseModel):
    """Only path to change parent_id/epic_id. version is required."""
    parent_id: Optional[int] = None
    epic_id: Optional[int] = None
    version: int = Field(ge=0)


class ReadyForSprintIn(BaseModel):
    ready_for_sprint: bool
    version: int = Field(ge=0)


class AcceptanceCriterionIn(BaseModel):
    description: str = Field(max_length=2000)
    required: bool = True

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "Acceptance criterion description")


class AcceptanceCriterionUpdateIn(BaseModel):
    description: Optional[str] = Field(default=None, max_length=2000)
    required: Optional[bool] = None
    met: Optional[bool] = None


class AcceptanceCriterionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    bug_id: int
    description: str
    required: bool
    met: bool
    updated_by_id: Optional[int] = None
    updated_at: datetime


class AgileWorkItemCreateIn(BaseModel):
    """Create an Epic/Story/Sub-task via /api/agile/work-items.
    Legacy Bug/Requirement/Task creation stays on /api/bugs."""
    project_id: int
    title: str = Field(max_length=200)
    description: str = Field(default="", max_length=1_000_000)
    item_type: str
    custom_values: list[CustomValueIn] = Field(default_factory=list, max_length=200)
    # Initial workflow status (e.g. the board's To Do column); "New" by default.
    status: str = Field(default="New")
    priority: str = Field(default="Medium")
    parent_id: Optional[int] = None
    epic_id: Optional[int] = None
    story_points: Optional[float] = Field(default=None, ge=0)
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    sprint_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        return _strip_and_check_min_length(v, MIN_TITLE_LENGTH, "Title")

    @field_validator("description")
    @classmethod
    def _strip_desc(cls, v: str) -> str:
        # Plain text in storage and responses (same as BugCreate).
        return plain_text(v) if isinstance(v, str) else v

    @field_validator("item_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        canonical = normalize_choice(v, AGILE_ITEM_TYPES, "item_type")
        return canonical

    @model_validator(mode="after")
    def _estimate_only_on_standard_issues(self):
        # As in PUT /api/agile/work-items/{id}/estimate: Epics and
        # Sub-tasks carry no story points of their own.
        if self.story_points is not None and self.item_type in ("Epic", "Sub-task"):
            raise ValueError(f"A {self.item_type} has no story points; estimate its issues instead")
        return self

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_STATUSES, "status")

    @field_validator("priority")
    @classmethod
    def _check_priority(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_PRIORITIES, "priority")

    @field_validator("start_date", "end_date")
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
    def _check_date_order(self) -> "AgileWorkItemCreateIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(_END_DATE_CANNOT_PRECEDE_START)
        return self


class WorkItemDatesIn(BaseModel):
    """Only path to change a Story/Task's start/end dates after creation."""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    version: int = Field(ge=0)

    @field_validator("start_date", "end_date")
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
    def _check_date_order(self) -> "WorkItemDatesIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError(_END_DATE_CANNOT_PRECEDE_START)
        return self


class WorkItemTaskFlagsIn(BaseModel):
    """Only path to change mandatory/blocked/blocked_reason on a Story or Task."""
    mandatory: Optional[bool] = None
    blocked: Optional[bool] = None
    blocked_reason: Optional[str] = Field(default=None, max_length=1000)
    version: int = Field(ge=0)

    @field_validator("blocked_reason")
    @classmethod
    def _strip_reason(cls, v: Optional[str]) -> Optional[str]:
        return plain_text(v) if isinstance(v, str) else v


# =====================================================================
# Agile — transitions, quick filters, flag.
# =====================================================================
class WorkItemTransitionIn(BaseModel):
    to_status: str = Field(max_length=100)
    version: int = Field(ge=0)
    override_wip: bool = False
    override_reason: str = Field(default="", max_length=500)
    acknowledged: bool = False


class WorkItemEstimateIn(BaseModel):
    story_points: Optional[float] = Field(default=None, ge=0)
    original_estimate_minutes: Optional[int] = Field(default=None, ge=0)
    remaining_estimate_minutes: Optional[int] = Field(default=None, ge=0)
    version: int = Field(ge=0)


class WorkItemFlagIn(BaseModel):
    flagged: bool
    version: int = Field(ge=0)


class WorkItemTaxonomyIn(BaseModel):
    component_ids: Optional[list[int]] = Field(default=None, max_length=200)
    label_ids: Optional[list[int]] = Field(default=None, max_length=200)
    version: int = Field(ge=0)


# A quick filter is a handful of ids and choices; it is stored and sent with every board load.
_QUICK_FILTER_MAX_BYTES = 16 * 1024


def _check_quick_filter_size(v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if v is not None and len(json.dumps(v, separators=(",", ":"))) > _QUICK_FILTER_MAX_BYTES:
        raise ValueError(f"filter_json must be at most {_QUICK_FILTER_MAX_BYTES // 1024} KB")
    return v


class QuickFilterIn(BaseModel):
    name: str = Field(max_length=120)
    filter_json: dict[str, Any] = Field(default_factory=dict)
    is_shared: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Quick filter name")

    @field_validator("filter_json")
    @classmethod
    def _check_filter(cls, v: dict[str, Any]) -> dict[str, Any]:
        return _check_quick_filter_size(v)


class QuickFilterUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    filter_json: Optional[dict[str, Any]] = None
    is_shared: Optional[bool] = None
    position: Optional[int] = None
    version: int = Field(ge=0)

    @field_validator("filter_json")
    @classmethod
    def _check_filter(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        return _check_quick_filter_size(v)


class QuickFilterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    board_id: int
    name: str
    filter_json: dict[str, Any] = Field(default_factory=dict)
    position: int
    is_shared: bool
    version: int = 1


class IssueLabelOut(BaseModel):
    id: int
    name: str
    color: str = ""


class AgileIssueOut(BaseModel):
    """An issue as the Backlog and Board show it."""
    id: int
    display_id: str = ""
    title: str
    item_type: str
    status: str
    status_category: str = "todo"
    priority: str
    story_points: Optional[float] = None
    original_estimate_minutes: Optional[int] = None
    assignees: list[UserBrief] = Field(default_factory=list)
    epic_id: Optional[int] = None
    parent_id: Optional[int] = None
    parent_display_id: Optional[str] = None
    parent_title: Optional[str] = None
    subtask_count: int = 0
    subtasks_done: int = 0
    flagged: bool = False
    blocked: bool = False
    labels: list[IssueLabelOut] = Field(default_factory=list)
    sprint_id: Optional[int] = None
    rank: Optional[str] = None
    due_date: Optional[str] = None
    updated_at: Optional[datetime] = None
    version: int = 1
    # Whether the viewer may move/edit it (Tasks and Requirements are
    # admin/manager-only, see app.auth.can_edit_bug).
    can_edit: bool = True
    # Board only: the column the issue's status is mapped to.
    column_id: Optional[int] = None




class BoardViewColumnOut(BaseModel):
    id: int
    name: str
    position: int
    category: str
    wip_limit: Optional[int] = None
    min_cards: Optional[int] = None
    wip_enforcement: str = "off"
    statuses: list[str] = Field(default_factory=list)
    card_count: int = 0
    cards: list[AgileIssueOut] = Field(default_factory=list)


class SprintBriefOut(BaseModel):
    id: int
    name: str
    goal: str = ""
    state: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    version: int = 1


class EpicBriefOut(BaseModel):
    id: int
    display_id: str = ""
    title: str
    color: str = ""
    status: str = ""
    status_category: str = "todo"
    archived: bool = False


class BoardViewOut(BaseModel):
    board_id: int
    board_name: str = ""
    estimation_mode: str = "story_points"
    swimlane_mode: str = "none"
    card_color_scheme: str = "none"
    sprint_id: Optional[int] = None
    sprint_name: Optional[str] = None
    sprint: Optional[SprintBriefOut] = None
    active_sprints: list[SprintBriefOut] = Field(default_factory=list)
    columns: list[BoardViewColumnOut] = Field(default_factory=list)
    # Sprint issues whose status is in no column (hidden from the columns, as
    # on the board) so the board can say so instead of losing them silently.
    unmapped: list[AgileIssueOut] = Field(default_factory=list)
    epics: list[EpicBriefOut] = Field(default_factory=list)


class HierarchyViewOut(BaseModel):
    """Epic > issue > Sub-task tree of one project."""
    project_id: int
    epics: list["EpicOut"] = Field(default_factory=list)
    issues: list[AgileIssueOut] = Field(default_factory=list)
    subtasks: list[AgileIssueOut] = Field(default_factory=list)
    unparented_total: int = 0
    unparented_limit: int = 500
    sprints: list[SprintBriefOut] = Field(default_factory=list)


class PlanningSprintOut(SprintBriefOut):
    sequence_number: int = 1
    issues: list[AgileIssueOut] = Field(default_factory=list)


class PlanningViewOut(BaseModel):
    """The Backlog view: open sprints with their issues, then the backlog."""
    board_id: int
    estimation_mode: str = "story_points"
    parallel_sprints: bool = False
    sprints: list[PlanningSprintOut] = Field(default_factory=list)
    backlog: list[AgileIssueOut] = Field(default_factory=list)
    backlog_total: int = 0
    epics: list[EpicBriefOut] = Field(default_factory=list)
