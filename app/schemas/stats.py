"""Pydantic schemas: stats."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --- Stats ---
class StatsOut(BaseModel):
    # Total excluding "Not a Bug".
    bugs: int
    # KPI strip buckets on the dashboard.
    open: int
    resolved: int
    closed: int
    resolve_later: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    by_environment: dict[str, int]
    by_type: dict[str, int] = Field(default_factory=dict)
    by_project: list[dict[str, Any]]
    by_assignee: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
