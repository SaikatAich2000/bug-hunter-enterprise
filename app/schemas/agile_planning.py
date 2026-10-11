"""Pydantic schemas: agile planning."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import ALLOWED_CAPACITY_UNITS, normalize_choice


# =====================================================================
# Agile — capacity, planning readiness.
# =====================================================================
class SprintCapacityIn(BaseModel):
    user_id: int
    capacity_value: float = Field(ge=0)
    capacity_unit: str
    days_off: list[str] = Field(default_factory=list, max_length=366)
    notes: str = Field(default="", max_length=500)

    @field_validator("capacity_unit")
    @classmethod
    def _check_unit(cls, v: str) -> str:
        return normalize_choice(v, ALLOWED_CAPACITY_UNITS, "capacity_unit")

    @field_validator("days_off")
    @classmethod
    def _check_days_off(cls, v: list[str]) -> list[str]:
        for d in v:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError("days_off entries must be YYYY-MM-DD") from exc
        return v


class SprintCapacityReplaceIn(BaseModel):
    entries: list[SprintCapacityIn] = Field(default_factory=list, max_length=200)


class SprintCapacityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sprint_id: int
    user_id: int
    capacity_value: float
    capacity_unit: str
    days_off: list[str] = Field(default_factory=list, max_length=366)
    notes: str = ""
    version: int = 1


class ReadinessCheckOut(BaseModel):
    key: str
    passed: bool
    message: str
    severity: str = "error"  # "error" | "warning"


class PlanningOut(BaseModel):
    sprint_id: int
    board_id: int
    estimation_mode: str
    item_count: int
    total_estimate: Optional[float] = None
    unestimated_count: int = 0
    capacity: list[SprintCapacityOut] = Field(default_factory=list)
    readiness: list[ReadinessCheckOut] = Field(default_factory=list)
    ready_to_start: bool = False
