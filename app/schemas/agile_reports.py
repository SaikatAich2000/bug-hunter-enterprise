"""Pydantic schemas: agile reports."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.agile_planning import ReadinessCheckOut
from app.schemas.agile_taxonomy import EpicProgressOut


class ReportIssueOut(BaseModel):
    id: int
    display_id: str = ""
    title: str
    item_type: str
    status: str
    status_category: str = "todo"
    estimate: Optional[float] = None
    estimate_at_start: Optional[float] = None
    added_during_sprint: bool = False
    assignees: list[str] = Field(default_factory=list)


class SprintReportOut(BaseModel):
    """The Sprint Report. Estimates use the board's statistic."""
    sprint_id: int
    sprint_name: str
    state: str
    goal: str
    estimation_mode: str
    statistic_label: str = "Story points"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    committed_count: int = 0
    committed_estimate: float = 0
    completed_count: int = 0
    completed_estimate: float = 0
    # Committed issues that were completed (the completion-rate numerator).
    completed_committed_count: int = 0
    # Issues already done when they joined the sprint: listed apart, never
    # counted as completed in it or in velocity.
    completed_outside_count: int = 0
    completed_outside_estimate: float = 0
    incomplete_count: int = 0
    incomplete_estimate: float = 0
    added_count: int = 0
    added_estimate: float = 0
    removed_count: int = 0
    removed_estimate: float = 0
    estimate_change_count: int = 0
    completed: list[ReportIssueOut] = Field(default_factory=list)
    not_completed: list[ReportIssueOut] = Field(default_factory=list)
    removed: list[ReportIssueOut] = Field(default_factory=list)
    completed_outside: list[ReportIssueOut] = Field(default_factory=list)
    # False for sprints started before 4.0 kept a full change history; their
    # figures come from the sprint's own start/finish records.
    history_complete: bool = True


class BurndownPointOut(BaseModel):
    at: datetime
    remaining: float
    scope: float
    completed: float


class GuidelinePointOut(BaseModel):
    at: datetime
    value: float


class BurndownEventOut(BaseModel):
    at: datetime
    work_item_id: int
    display_id: str = ""
    title: str = ""
    event: str
    change: float = 0
    remaining: float = 0
    scope_change: bool = False


class BoardDayOut(BaseModel):
    """One board-local calendar day of a chart, as exact instants."""
    date: str
    start: datetime
    end: datetime
    working: bool = True


class BurndownReportOut(BaseModel):
    """The Burndown and Burnup charts: remaining work, total scope and
    completed work as step series, the ideal guideline, and the events."""
    sprint_id: int
    sprint_name: str = ""
    state: str = ""
    estimation_mode: str
    statistic_label: str = "Story points"
    start: Optional[datetime] = None
    planned_end: Optional[datetime] = None
    end: Optional[datetime] = None
    committed_estimate: float = 0
    remaining_estimate: float = 0
    scope_estimate: float = 0
    completed_estimate: float = 0
    points: list[BurndownPointOut] = Field(default_factory=list)
    guideline: list[GuidelinePointOut] = Field(default_factory=list)
    # The board's timezone and its calendar days across the chart.
    timezone: str = "UTC"
    days: list[BoardDayOut] = Field(default_factory=list)
    events: list[BurndownEventOut] = Field(default_factory=list)
    non_working_days: list[str] = Field(default_factory=list)
    history_complete: bool = True


BurnupReportOut = BurndownReportOut


class WorkloadEntryOut(BaseModel):
    user_id: int
    user_name: str
    assigned_estimate: float = 0
    assigned_count: int = 0
    capacity_value: Optional[float] = None
    capacity_unit: Optional[str] = None
    utilization_pct: Optional[float] = None


class WorkloadReportOut(BaseModel):
    sprint_id: int
    unassigned_count: int = 0
    unassigned_estimate: float = 0
    entries: list[WorkloadEntryOut] = Field(default_factory=list)


class EpicSprintRowOut(BaseModel):
    sprint_id: int
    sprint_name: str
    state: str
    completed_estimate: float = 0
    completed_count: int = 0
    scope_estimate: float = 0
    remaining_estimate: float = 0


class EpicReportOut(BaseModel):
    """The Epic Report: progress, work per sprint and the issue lists."""
    epic_id: int
    display_id: str = ""
    title: str
    status: str = ""
    estimation_mode: str = "story_points"
    statistic_label: str = "Story points"
    progress: EpicProgressOut
    sprints: list[EpicSprintRowOut] = Field(default_factory=list)
    done: list[ReportIssueOut] = Field(default_factory=list)
    in_progress: list[ReportIssueOut] = Field(default_factory=list)
    todo: list[ReportIssueOut] = Field(default_factory=list)




class ScopeChangeEntryOut(BaseModel):
    work_item_id: int
    display_id: str = ""
    title: str
    event_type: str
    occurred_at: datetime
    change: float = 0
    actor_name: str = ""


class ScopeChangeReportOut(BaseModel):
    sprint_id: int
    entries: list[ScopeChangeEntryOut] = Field(default_factory=list)


# --- Daily Sprint Report ---
class DailyReportTransitionOut(BaseModel):
    work_item_id: int
    title: str
    event_type: str
    occurred_at: datetime
    detail: str = ""


class DailyReportBlockerOut(BaseModel):
    work_item_id: int
    title: str
    item_type: str
    blocked_reason: str
    status: str


class DailyReportItemOut(BaseModel):
    work_item_id: int
    display_id: str = ""
    title: str
    item_type: str
    status: str
    category: str
    column: str = ""
    story_points: float = 0


class DailyReportOut(BaseModel):
    sprint_id: int
    sprint_name: str
    goal: str
    report_date: str
    generated_at: datetime
    generated_by_id: Optional[int] = None
    committed_count: int = 0
    story_count: int = 0
    bug_count: int = 0
    todo_count: int = 0
    in_progress_count: int = 0
    testing_count: int = 0
    done_count: int = 0
    blocked_count: int = 0
    committed_estimate: float = 0
    completed_estimate: float = 0
    remaining_estimate: float = 0
    completed_estimate_today: float = 0
    scope_added_today: int = 0
    scope_removed_today: int = 0
    transitions: list[DailyReportTransitionOut] = Field(default_factory=list)
    blockers: list[DailyReportBlockerOut] = Field(default_factory=list)
    items: list[DailyReportItemOut] = Field(default_factory=list)


# =====================================================================
# release readiness/roadmap, reporting follow-on.
# =====================================================================
class ReleaseActionIn(BaseModel):
    version: int = Field(ge=0)


class Release360Out(BaseModel):
    id: int
    name: str
    status: str
    start_date: Optional[str] = None
    release_date: Optional[str] = None
    released_at: Optional[datetime] = None
    version: int = 1
    progress: dict[str, Any] = Field(default_factory=dict)
    readiness: list[ReadinessCheckOut] = Field(default_factory=list)


class RoadmapRowOut(BaseModel):
    kind: str  # "epic" | "release"
    id: int
    title: str
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    scheduled: bool = True
    progress: dict[str, Any] = Field(default_factory=dict)


class RoadmapOut(BaseModel):
    project_id: int
    epics: list[RoadmapRowOut] = Field(default_factory=list)
    releases: list[RoadmapRowOut] = Field(default_factory=list)
    unscheduled_epics: list[RoadmapRowOut] = Field(default_factory=list)
    unscheduled_releases: list[RoadmapRowOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class VelocityPointOut(BaseModel):
    sprint_id: int
    sprint_name: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    committed_estimate: float
    completed_estimate: float


class VelocityReportOut(BaseModel):
    """The Velocity Chart: commitment vs completed for recent closed sprints."""
    board_id: int
    estimation_mode: str
    statistic_label: str = "Story points"
    average_committed: float = 0
    average_completed: float = 0
    points: list[VelocityPointOut] = Field(default_factory=list)


class FlowColumnOut(BaseModel):
    id: int
    name: str
    category: str


class CumulativeFlowPointOut(BaseModel):
    date: str
    # column id (as text) -> issues in that column at the end of the day
    counts: dict[str, int] = Field(default_factory=dict)


class CumulativeFlowReportOut(BaseModel):
    """The Cumulative Flow Diagram: issues per board column per day."""
    board_id: int
    project_id: int
    columns: list[FlowColumnOut] = Field(default_factory=list)
    points: list[CumulativeFlowPointOut] = Field(default_factory=list)


class ControlChartEntryOut(BaseModel):
    work_item_id: int
    display_id: str = ""
    title: str = ""
    item_type: str = ""
    started_at: Optional[datetime] = None
    completed_at: datetime
    cycle_time_days: float
    lead_time_days: float


class ControlChartPointOut(BaseModel):
    completed_at: datetime
    value: float


class ControlChartReportOut(BaseModel):
    """The Control Chart: cycle time (time spent in the board's working
    columns) of every issue completed in the range, with statistics."""
    board_id: int
    work_columns: list[str] = Field(default_factory=list)
    count: int = 0
    average_days: float = 0
    median_days: float = 0
    min_days: float = 0
    max_days: float = 0
    std_dev_days: float = 0
    entries: list[ControlChartEntryOut] = Field(default_factory=list)
    rolling_average: list[ControlChartPointOut] = Field(default_factory=list)
