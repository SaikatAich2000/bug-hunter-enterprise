"""Pydantic schemas: agile sprints."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.agile_boards import _AGILE_NAME_MIN
from app.schemas.common import (
    _DATE_FORMAT_ERROR,
    ALLOWED_SPRINT_DISPOSITIONS,
    _strip_and_check_min_length,
    normalize_choice,
    plain_text,
)
from app.schemas.users import UserBrief


# --- Sprints ---
class SprintCreateIn(BaseModel):
    name: str = Field(max_length=120)
    goal: str = Field(default="", max_length=2000)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cadence: Optional[str] = None
    assignee_ids: list[int] = Field(default_factory=list, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Sprint name")

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: str) -> str:
        return plain_text(v) if isinstance(v, str) else v

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if v not in ("daily", "weekly", "monthly", "custom"):
            raise ValueError("cadence must be one of: daily, weekly, monthly, custom")
        return v

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
    def _normalize(self) -> "SprintCreateIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class SprintUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    goal: Optional[str] = Field(default=None, max_length=2000)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cadence: Optional[str] = None
    assignee_ids: Optional[list[int]] = Field(default=None, max_length=200)
    version: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Sprint name")

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return plain_text(v) if isinstance(v, str) else v

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if v not in ("daily", "weekly", "monthly", "custom"):
            raise ValueError("cadence must be one of: daily, weekly, monthly, custom")
        return v

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


class SprintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    display_id: str = ""
    board_id: int
    project_id: int
    name: str
    goal: str
    state: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    cadence: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    sequence_number: int
    version: int = 1
    created_at: datetime
    updated_at: datetime
    item_count: int = 0
    assignees: list[UserBrief] = Field(default_factory=list)


class SprintItemsAddIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=500)
    # Optional drop position inside the sprint: right after before_id and/or
    # right before after_id. Omitted = appended at the bottom.
    before_id: Optional[int] = None
    after_id: Optional[int] = None

    @field_validator("item_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        seen: list[int] = []
        for x in v or []:
            if x not in seen:
                seen.append(x)
        return seen


class SprintDispositionEntry(BaseModel):
    """Where one incomplete item goes on Complete/Cancel Sprint."""
    item_id: int
    destination: str = "backlog"
    target_sprint_id: Optional[int] = None

    @field_validator("destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_SPRINT_DISPOSITIONS, "destination")

    @model_validator(mode="after")
    def _check_target(self) -> "SprintDispositionEntry":
        if self.destination == "sprint" and self.target_sprint_id is None:
            raise ValueError("target_sprint_id is required when destination is 'sprint'")
        return self


class SprintMoveIn(BaseModel):
    """Backlog drag-and-drop: move issues (in this order) into a sprint, or to
    the backlog when sprint_id is null, at a position between neighbours."""
    item_ids: list[int] = Field(min_length=1, max_length=500)
    sprint_id: Optional[int] = None
    before_id: Optional[int] = None
    after_id: Optional[int] = None

    @field_validator("item_ids")
    @classmethod
    def _dedup(cls, v: list[int]) -> list[int]:
        return list(dict.fromkeys(v))


class SprintStartIn(BaseModel):
    """The Start Sprint dialog: name, goal and dates may be set on start."""
    version: int = Field(ge=0)
    acknowledged_warnings: bool = False
    name: Optional[str] = Field(default=None, max_length=120)
    goal: Optional[str] = Field(default=None, max_length=2000)
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _strip_and_check_min_length(v, _AGILE_NAME_MIN, "Sprint name")

    @field_validator("goal")
    @classmethod
    def _strip_goal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return plain_text(v)

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
    def _check_order(self) -> "SprintStartIn":
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date cannot be before start_date")
        return self


class SprintCompleteIn(BaseModel):
    closing_note: str = Field(default="Sprint completed", min_length=1, max_length=2000)
    version: int = Field(ge=0)
    # Disposition for each still-incomplete item; a default applied to any
    # incomplete item not explicitly listed. "new_sprint" creates one future
    # sprint (named new_sprint_name, or the next default name) for them.
    dispositions: list[SprintDispositionEntry] = Field(default_factory=list, max_length=5000)
    default_destination: str = "backlog"
    default_target_sprint_id: Optional[int] = None
    new_sprint_name: Optional[str] = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_default_target(self) -> "SprintCompleteIn":
        if self.default_destination == "sprint" and self.default_target_sprint_id is None:
            raise ValueError("default_target_sprint_id is required when default_destination is 'sprint'")
        return self

    @field_validator("closing_note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "closing_note")

    @field_validator("default_destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_SPRINT_DISPOSITIONS, "default_destination")


class SprintCancelIn(BaseModel):
    cancellation_note: str = Field(min_length=1, max_length=2000)
    version: int = Field(ge=0)
    dispositions: list[SprintDispositionEntry] = Field(default_factory=list, max_length=5000)
    default_destination: str = "backlog"
    default_target_sprint_id: Optional[int] = None

    @field_validator("cancellation_note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return _strip_and_check_min_length(v, 1, "cancellation_note")

    @field_validator("default_destination")
    @classmethod
    def _check_dest(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_SPRINT_DISPOSITIONS, "default_destination")


class SprintHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sprint_id: int
    work_item_id: int
    event_type: str
    from_sprint_id: Optional[int] = None
    to_sprint_id: Optional[int] = None
    old_status_id: Optional[int] = None
    new_status_id: Optional[int] = None
    old_estimate: Optional[float] = None
    new_estimate: Optional[float] = None
    occurred_at: datetime
    actor_id: Optional[int] = None
