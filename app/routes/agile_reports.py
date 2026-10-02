"""Agile API: Jira-style reports (sprint report, burndown/burnup, velocity,
cumulative flow, control chart, epic report, daily summary, workload, scope
changes, release) and their exports."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.agile import boards as boards_svc
from app.agile import reports as reports_svc
from app.agile.permissions import EXPORT_AGILE_REPORTS, VIEW_AGILE_REPORTS, require_permission
from app.api_docs import (
    BAD_REQUEST_NOT_FOUND_404,
    NOT_FOUND_404,
    NOT_FOUND_500,
    NOT_FOUND_VALIDATION_422,
    XLSX_FILE_200,
    XLSX_OR_CSV_FILE_200,
)
from app.auth import get_current_user
from app.database import get_db
from app.models import Bug, Project, Sprint, User
from app.schemas import (
    BurndownReportOut,
    BurnupReportOut,
    ControlChartReportOut,
    CumulativeFlowReportOut,
    DailyReportOut,
    EpicReportOut,
    Release360Out,
    ScopeChangeReportOut,
    SprintReportOut,
    VelocityReportOut,
    WorkloadReportOut,
    display_id_for,
)

router = APIRouter(prefix="/api/agile/reports", tags=["agile-reports"])

_DETAIL_SPRINT_NOT_FOUND = "Sprint not found"
_DETAIL_EPIC_NOT_FOUND = "Epic not found"
_DETAIL_BOARD_NOT_FOUND = "Sprint Board not found"
_DETAIL_VERSION_NOT_FOUND = "Version not found"
_EXPORTABLE_REPORTS = (
    "sprint", "burndown", "burnup", "workload", "epic", "scope-change",
    "velocity", "cumulative-flow", "control-chart", "release",
)


# Longest window the day-by-day flow/control reports accept; each day is
# reconstructed from history, so an unbounded range is an easy way to pin a CPU.
_MAX_REPORT_RANGE_DAYS = 366
# Spreadsheet apps execute a cell that starts with one of these as a formula.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _validate_date_range(date_from: str, date_to: str) -> None:
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD") from exc
    if end < start:
        raise HTTPException(status_code=422, detail="date_to cannot precede date_from")
    if (end - start).days > _MAX_REPORT_RANGE_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"The date range can span at most {_MAX_REPORT_RANGE_DAYS} days",
        )


def _csv_safe(value):
    """Neutralise spreadsheet formula injection in exported text cells."""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _get_sprint_and_board(db: Session, sprint_id: int, user: User):
    sprint = db.get(Sprint, sprint_id)
    if sprint is None or not can_access_project(accessible_project_ids(db, user), sprint.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_SPRINT_NOT_FOUND)
    project = db.get(Project, sprint.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_SPRINT_NOT_FOUND)
    require_permission(user, VIEW_AGILE_REPORTS)
    board = boards_svc.get_board_or_none(db, sprint.board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Sprint Board not found")
    return sprint, board


def _get_report_board(db: Session, board_id: int, user: User):
    board = boards_svc.get_board_or_none(db, board_id)
    if board is None or not can_access_project(accessible_project_ids(db, user), board.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_BOARD_NOT_FOUND)
    project = db.get(Project, board.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_BOARD_NOT_FOUND)
    require_permission(user, VIEW_AGILE_REPORTS)
    return board


@router.get("/sprint/{sprint_id}", response_model=SprintReportOut, responses=NOT_FOUND_404)
def get_sprint_report(
    sprint_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    return reports_svc.sprint_report(db, sprint, board)


def _sprint_summary_rows(sprint, report: dict) -> list[dict]:
    pic = "(not set)"
    pct = (
        round(report["completed_committed_count"] / report["committed_count"] * 100, 1)
        if report["committed_count"] else 0.0
    )
    return [
        {"field": "Sprint", "value": sprint.name},
        {"field": "Goal", "value": sprint.goal or "(none)"},
        {"field": "State", "value": sprint.state},
        {"field": "Sequence number", "value": sprint.sequence_number},
        {"field": "Start date", "value": sprint.start_date or pic},
        {"field": "End date", "value": sprint.end_date or pic},
        {"field": "Cadence", "value": sprint.cadence or pic},
        {"field": "Estimation statistic", "value": report["statistic_label"]},
        {"field": "Committed issues", "value": report["committed_count"]},
        {"field": "Committed estimate", "value": report["committed_estimate"]},
        {"field": "Completed issues", "value": report["completed_count"]},
        {"field": "Completed estimate", "value": report["completed_estimate"]},
        {"field": "Not completed issues", "value": report["incomplete_count"]},
        {"field": "Not completed estimate", "value": report["incomplete_estimate"]},
        {"field": "Completion rate (issues committed)", "value": f"{pct}%"},
        {"field": "Added after start", "value": report["added_count"]},
        {"field": "Removed during sprint", "value": report["removed_count"]},
        {"field": "Estimate changes", "value": report["estimate_change_count"]},
        {"field": "Generated", "value": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    ]


def _status_breakdown_rows(items) -> list[dict]:
    by_status: dict[str, dict] = {}
    for it in items:
        row = by_status.setdefault(it.status, {"status": it.status, "count": 0, "points": 0.0})
        row["count"] += 1
        if it.story_points is not None:
            row["points"] += float(it.story_points)
    total = len(items) or 1
    rows = sorted(by_status.values(), key=lambda r: -r["count"])
    for r in rows:
        r["share"] = f"{round(r['count'] / total * 100, 1)}%"
    return rows


def _work_item_rows(db: Session, items, users_by_id: dict, report: dict) -> list[dict]:
    epic_ids = {it.epic_id for it in items if it.epic_id}
    epics = {e.id: e for e in db.scalars(select(Bug).where(Bug.id.in_(epic_ids))).all()} if epic_ids else {}
    parents_ids = {it.parent_id for it in items if it.parent_id}
    parents = {p.id: p for p in db.scalars(select(Bug).where(Bug.id.in_(parents_ids))).all()} if parents_ids else {}
    outcome = {}
    for key, label in (("completed", "Completed"), ("not_completed", "Not completed"),
                       ("removed", "Removed"), ("completed_outside", "Completed outside the sprint")):
        for row in report.get(key, []):
            outcome[row["id"]] = label
    added = {row["id"] for key in ("completed", "not_completed", "removed", "completed_outside")
             for row in report.get(key, []) if row.get("added_during_sprint")}

    rows = []
    for it in items:
        epic = epics.get(it.epic_id)
        parent = parents.get(it.parent_id)
        rows.append({
            "id": it.id,
            "key": (it.display_id or "").strip() or display_id_for(it.item_type or "Bug", it.id),
            "item_type": it.item_type,
            "title": it.title,
            "status": it.status,
            "outcome": outcome.get(it.id, "Sub-task" if it.item_type == "Sub-task" else ""),
            "added": "Yes" if it.id in added else "No",
            "priority": it.priority,
            "environment": it.environment,
            "epic": epic.title if epic else "",
            "parent": parent.title if parent else "",
            "owner": users_by_id[it.owner_id].name if it.owner_id in users_by_id else "",
            "assignees": ", ".join(a.name for a in it.assignees),
            "reporter": users_by_id[it.reporter_id].name if it.reporter_id in users_by_id else "",
            "story_points": float(it.story_points) if it.story_points is not None else "",
            "start_date": it.start_date or "",
            "due_date": it.due_date or "",
            "blocked": "Yes" if it.blocked else "No",
            "blocked_reason": it.blocked_reason or "",
            "mandatory": "Yes" if it.mandatory else "No",
            "created_at": it.created_at,
            "updated_at": it.updated_at,
        })
    return rows


_WORK_ITEM_COLS = [
    ("key", "Key", 12, "left"), ("item_type", "Type", 12, "left"),
    ("title", "Title", 44, "left"), ("status", "Status", 14, "left"),
    ("outcome", "Sprint outcome", 20, "left"), ("added", "Added after start", 10, "left"),
    ("priority", "Priority", 10, "left"), ("environment", "Env", 8, "left"),
    ("epic", "Epic", 24, "left"), ("parent", "Parent", 24, "left"), ("owner", "Owner", 18, "left"),
    ("assignees", "Assignees", 26, "left"), ("reporter", "Reporter", 18, "left"),
    ("story_points", "Points", 9, "right"), ("start_date", "Start", 12, "left"),
    ("due_date", "Due", 12, "left"), ("blocked", "Blocked", 9, "left"),
    ("blocked_reason", "Blocked Reason", 26, "left"), ("mandatory", "Mandatory", 10, "left"),
    ("created_at", "Created", 20, "left"), ("updated_at", "Updated", 20, "left"),
]


@router.get("/sprint/{sprint_id}/export.xlsx", response_class=Response,
            responses={**XLSX_FILE_200, **NOT_FOUND_500})
def export_sprint_report_xlsx(
    sprint_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Full Sprint Report workbook: Summary, Status Breakdown, every Work Item
    with its full hierarchy/ownership/date context, Team Workload, Scope
    Changes, and the complete immutable activity history."""
    from openpyxl import Workbook

    from app.models import SprintItemHistory, WorkflowStatus
    from app.reports.engine import ReportColumn
    from app.reports.xlsx import XlsxBuildError, _ensure_openpyxl, _write_table

    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    require_permission(user, EXPORT_AGILE_REPORTS)
    report = reports_svc.sprint_report(db, sprint, board)
    scope = reports_svc.scope_change_report(db, sprint, board)
    workload = reports_svc.workload_report(db, sprint, board)
    ids = {row["id"] for key in ("completed", "not_completed", "removed", "completed_outside")
           for row in report[key]}
    ids |= set(db.scalars(select(Bug.id).where(Bug.sprint_id == sprint.id)).all())
    items = list(db.scalars(select(Bug).where(Bug.id.in_(ids)).order_by(Bug.id)).all()) if ids else []
    users_by_id = {u.id: u for u in db.scalars(select(User).where(User.org_id == user.org_id)).all()}

    try:
        _ensure_openpyxl()
        wb = Workbook()

        summary_ws = wb.active
        summary_ws.title = "Sprint Summary"
        _write_table(
            summary_ws,
            [ReportColumn(key="field", label="Field", width=28),
             ReportColumn(key="value", label="Value", width=52)],
            _sprint_summary_rows(sprint, report),
            banner=f"Sprint Report — {sprint.name}",
        )

        status_ws = wb.create_sheet("Status Breakdown")
        _write_table(
            status_ws,
            [ReportColumn(key="status", label="Status", width=22),
             ReportColumn(key="count", label="Items", width=10, align="right"),
             ReportColumn(key="points", label="Points", width=10, align="right"),
             ReportColumn(key="share", label="Share", width=10, align="right")],
            _status_breakdown_rows(items),
            banner=f"Status Breakdown — {len(items)} item(s)",
        )

        items_ws = wb.create_sheet("Work Items")
        _write_table(
            items_ws,
            [ReportColumn(key=k, label=lbl, width=w, align=a) for k, lbl, w, a in _WORK_ITEM_COLS],
            _work_item_rows(db, items, users_by_id, report),
            banner=f"Work Items ({len(items)})",
        )

        workload_ws = wb.create_sheet("Team Workload")
        _write_table(
            workload_ws,
            [ReportColumn(key="user_name", label="Team Member", width=26),
             ReportColumn(key="assigned_count", label="Items", width=10, align="right"),
             ReportColumn(key="assigned_estimate", label="Points", width=10, align="right"),
             ReportColumn(key="capacity_value", label="Capacity", width=12, align="right"),
             ReportColumn(key="capacity_unit", label="Unit", width=10),
             ReportColumn(key="utilization_pct", label="Utilisation %", width=14, align="right")],
            workload["entries"],
            banner=f"Team Workload ({len(workload['entries'])} member(s))",
        )

        scope_ws = wb.create_sheet("Scope Changes")
        _write_table(
            scope_ws,
            [ReportColumn(key="display_id", label="Key", width=12),
             ReportColumn(key="title", label="Item", width=44),
             ReportColumn(key="event_type", label="Event", width=16),
             ReportColumn(key="change", label="Change", width=10, align="right"),
             ReportColumn(key="actor_name", label="By", width=22),
             ReportColumn(key="occurred_at", label="When", width=24)],
            scope["entries"],
            banner=f"Scope Changes ({len(scope['entries'])})",
        )

        history = list(db.scalars(
            select(SprintItemHistory)
            .where(SprintItemHistory.sprint_id == sprint.id)
            .order_by(SprintItemHistory.occurred_at)
        ).all())
        titles = {it.id: it.title for it in items}
        status_names = {
            s.id: s.persisted_status_value
            for s in db.scalars(select(WorkflowStatus)).all()
        }
        history_ws = wb.create_sheet("Activity History")
        _write_table(
            history_ws,
            [ReportColumn(key="work_item_id", label="Item ID", width=10, align="right"),
             ReportColumn(key="title", label="Item", width=44),
             ReportColumn(key="event_type", label="Event", width=20),
             ReportColumn(key="from_status", label="From Status", width=16),
             ReportColumn(key="to_status", label="To Status", width=16),
             ReportColumn(key="old_estimate", label="Old Est.", width=10, align="right"),
             ReportColumn(key="new_estimate", label="New Est.", width=10, align="right"),
             ReportColumn(key="actor", label="Actor", width=22),
             ReportColumn(key="occurred_at", label="When", width=24)],
            [{
                "work_item_id": h.work_item_id,
                "title": titles.get(h.work_item_id, f"#{h.work_item_id}"),
                "event_type": h.event_type,
                "from_status": status_names.get(h.old_status_id, ""),
                "to_status": status_names.get(h.new_status_id, ""),
                "old_estimate": float(h.old_estimate) if h.old_estimate is not None else "",
                "new_estimate": float(h.new_estimate) if h.new_estimate is not None else "",
                "actor": users_by_id[h.actor_id].name if h.actor_id in users_by_id else "",
                "occurred_at": h.occurred_at,
            } for h in history],
            banner=f"Activity History ({len(history)} event(s))",
        )

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
    except XlsxBuildError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ASCII only: the name goes into an HTTP header, which is Latin-1 encoded.
    safe_name = "".join(
        c if (c.isascii() and c.isalnum()) or c in (" ", "-", "_") else "_" for c in sprint.name
    ).strip() or f"sprint-{sprint.id}"
    filename = f"sprint-report-{safe_name}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/burndown", response_model=BurndownReportOut, responses=NOT_FOUND_404)
def get_burndown_report(
    sprint_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    return reports_svc.burndown_report(db, sprint, board)


@router.get("/burnup", response_model=BurnupReportOut, responses=NOT_FOUND_404)
def get_burnup_report(
    sprint_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    return reports_svc.burnup_report(db, sprint, board)


@router.get("/workload", response_model=WorkloadReportOut, responses=NOT_FOUND_404)
def get_workload_report(
    sprint_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    return reports_svc.workload_report(db, sprint, board)


@router.get("/daily", response_model=DailyReportOut, responses=NOT_FOUND_VALIDATION_422)
def get_daily_report(
    sprint_id: int = Query(...), report_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    try:
        # The pattern admits impossible dates such as 0000-00-00.
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="report_date must be a real date (YYYY-MM-DD)") from exc
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    try:
        result = reports_svc.daily_report(db, sprint, board, report_date, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.get("/epic/{epic_id}", response_model=EpicReportOut, responses=NOT_FOUND_404)
def get_epic_progress_report(
    epic_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    epic = db.get(Bug, epic_id)
    if epic is None or epic.item_type != "Epic" or not can_access_project(
        accessible_project_ids(db, user), epic.project_id
    ):
        raise HTTPException(status_code=404, detail=_DETAIL_EPIC_NOT_FOUND)
    project = db.get(Project, epic.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_EPIC_NOT_FOUND)
    require_permission(user, VIEW_AGILE_REPORTS)
    return reports_svc.epic_report(db, epic, boards_svc.get_default_board(db, epic.project_id))


@router.get("/scope-change", response_model=ScopeChangeReportOut, responses=NOT_FOUND_404)
def get_scope_change_report(
    sprint_id: int = Query(...), db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    return reports_svc.scope_change_report(db, sprint, board)


# --- Reporting follow-on: velocity, cumulative flow, control chart ---

@router.get("/velocity", response_model=VelocityReportOut, responses=NOT_FOUND_404)
def get_velocity_report(
    board_id: int = Query(...), sprint_count: int = Query(default=7, ge=1, le=50),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    board = _get_report_board(db, board_id, user)
    return reports_svc.velocity_report(db, board, sprint_count)


@router.get("/cumulative-flow", response_model=CumulativeFlowReportOut, responses=NOT_FOUND_404)
def get_cumulative_flow_report(
    board_id: int = Query(...), date_from: str = Query(...), date_to: str = Query(...),
    include_subtasks: bool = Query(default=True),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    board = _get_report_board(db, board_id, user)
    _validate_date_range(date_from, date_to)
    return reports_svc.cumulative_flow_report(db, board, date_from, date_to, include_subtasks)


@router.get("/control-chart", response_model=ControlChartReportOut, responses=NOT_FOUND_404)
def get_control_chart_report(
    board_id: int = Query(...), date_from: str = Query(...), date_to: str = Query(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    board = _get_report_board(db, board_id, user)
    _validate_date_range(date_from, date_to)
    return reports_svc.control_chart_report(db, board, date_from, date_to)


@router.get("/release/{version_id}", response_model=Release360Out, responses=NOT_FOUND_404)
def get_release_report(
    version_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    from app.models import Version

    v = db.get(Version, version_id)
    if v is None or not can_access_project(accessible_project_ids(db, user), v.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_VERSION_NOT_FOUND)
    project = db.get(Project, v.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_VERSION_NOT_FOUND)
    require_permission(user, VIEW_AGILE_REPORTS)
    return reports_svc.release_report(db, v)


_SPRINT_SCOPED_REPORTS = {
    "sprint": reports_svc.sprint_report,
    "burndown": reports_svc.burndown_report,
    "burnup": reports_svc.burnup_report,
    "workload": reports_svc.workload_report,
    "scope-change": reports_svc.scope_change_report,
}


def _export_sprint_scoped(db: Session, user: User, report_key: str, sprint_id: int | None) -> dict:
    if sprint_id is None:
        raise HTTPException(status_code=400, detail="sprint_id is required for this report")
    sprint, board = _get_sprint_and_board(db, sprint_id, user)
    return _SPRINT_SCOPED_REPORTS[report_key](db, sprint, board)


def _export_epic(db: Session, user: User, epic_id: int | None) -> dict:
    if epic_id is None:
        raise HTTPException(status_code=400, detail="epic_id is required for this report")
    return get_epic_progress_report(epic_id, db, user)


def _export_release(db: Session, user: User, version_id: int | None) -> dict:
    if version_id is None:
        raise HTTPException(status_code=400, detail="version_id is required for this report")
    return get_release_report(version_id, db, user)


def _export_board_scoped(
    db: Session, user: User, report_key: str, board_id: int | None,
    date_from: str | None, date_to: str | None, sprint_count: int, include_subtasks: bool = True,
) -> dict:
    if board_id is None:
        raise HTTPException(status_code=400, detail="board_id is required for this report")
    board = _get_report_board(db, board_id, user)
    if report_key == "velocity":
        return reports_svc.velocity_report(db, board, sprint_count)
    if date_from is None or date_to is None:
        raise HTTPException(status_code=400, detail="date_from and date_to are required for this report")
    _validate_date_range(date_from, date_to)
    if report_key == "cumulative-flow":
        return _flatten_cfd(reports_svc.cumulative_flow_report(
            db, board, date_from, date_to, include_subtasks=include_subtasks,
        ))
    return reports_svc.control_chart_report(db, board, date_from, date_to)


def _flatten_cfd(data: dict) -> dict:
    """One CSV column per board column (in board order) instead of a nested
    counts map; two columns with the same name stay apart."""
    seen: dict[str, int] = {}
    names: dict[str, str] = {}
    for column in data["columns"]:
        name = column["name"]
        seen[name] = seen.get(name, 0) + 1
        names[str(column["id"])] = name if seen[name] == 1 else f"{name} ({seen[name]})"
    return {
        **data,
        "points": [
            {"date": p["date"], **{names[k]: p["counts"].get(k, 0) for k in names}}
            for p in data["points"]
        ],
    }


def _report_data_for_export(
    db: Session, user: User, report_key: str,
    sprint_id: int | None, board_id: int | None, epic_id: int | None, version_id: int | None,
    date_from: str | None, date_to: str | None, sprint_count: int, include_subtasks: bool = True,
) -> dict:
    if report_key in _SPRINT_SCOPED_REPORTS:
        return _export_sprint_scoped(db, user, report_key, sprint_id)
    if report_key == "epic":
        return _export_epic(db, user, epic_id)
    if report_key == "release":
        return _export_release(db, user, version_id)
    return _export_board_scoped(
        db, user, report_key, board_id, date_from, date_to, sprint_count, include_subtasks,
    )


def _outcome_rows(data: dict, groups: tuple[tuple[str, str], ...], column: str) -> list[dict]:
    return [{column: label, **row} for key, label in groups for row in data.get(key) or []]


# The table each report exports: one row per issue, day or sprint.
_CSV_ROWS = {
    "sprint": lambda d: _outcome_rows(d, (
        ("completed", "Completed"), ("not_completed", "Not completed"),
        ("removed", "Removed from sprint"), ("completed_outside", "Completed outside the sprint"),
    ), "outcome"),
    "burndown": lambda d: d.get("points") or [],
    "burnup": lambda d: d.get("points") or [],
    "workload": lambda d: d.get("entries") or [],
    "scope-change": lambda d: d.get("entries") or [],
    "velocity": lambda d: d.get("points") or [],
    "cumulative-flow": lambda d: d.get("points") or [],
    "control-chart": lambda d: d.get("entries") or [],
    "epic": lambda d: _outcome_rows(d, (("done", "Done"), ("in_progress", "In progress"), ("todo", "To do")), "progress"),
}


def _csv_cell(value):
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v.get("name", v) if isinstance(v, dict) else v) for v in value)
    elif isinstance(value, dict):
        value = ", ".join(f"{k}: {v}" for k, v in value.items())
    return _csv_safe(value)


def _report_to_csv_bytes(data: dict, report_key: str | None = None) -> bytes:
    """The report's table as CSV (see _CSV_ROWS); a report without one (or
    with an empty table) exports its summary figures as a single row."""
    builder = _CSV_ROWS.get(report_key or "")
    if builder is not None:
        rows = builder(data)
    else:
        rows = next(
            (v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)),
            [],
        )
    buf = io.StringIO()
    if rows:
        fieldnames = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({k: _csv_cell(row.get(k)) for k in fieldnames} for row in rows)
    else:
        scalar = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
        writer = csv.DictWriter(buf, fieldnames=list(scalar.keys()))
        writer.writeheader()
        writer.writerow({k: _csv_safe(v) for k, v in scalar.items()})
    return buf.getvalue().encode("utf-8")


@router.post("/export", response_class=Response,
             responses={**XLSX_OR_CSV_FILE_200, **BAD_REQUEST_NOT_FOUND_404})
def export_report(
    report_key: str = Query(..., description="|".join(_EXPORTABLE_REPORTS)),
    sprint_id: int | None = Query(default=None),
    board_id: int | None = Query(default=None),
    epic_id: int | None = Query(default=None),
    version_id: int | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    sprint_count: int = Query(default=7, ge=1, le=50),
    include_subtasks: bool = Query(default=True),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Streams the report as CSV."""
    if report_key not in _EXPORTABLE_REPORTS:
        raise HTTPException(status_code=400, detail=f"Unknown report_key '{report_key}'")
    require_permission(user, EXPORT_AGILE_REPORTS)
    data = _report_data_for_export(
        db, user, report_key, sprint_id, board_id, epic_id, version_id, date_from, date_to, sprint_count,
        include_subtasks,
    )
    csv_bytes = _report_to_csv_bytes(data, report_key)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"agile-report-{report_key}-{stamp}.csv"
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
            "Cache-Control": "private, no-store, max-age=0",
        },
    )
