"""Reports engine — the single SQL surface for every report.

Public entry points:
  Filters.from_dict({...})        — parse a user-supplied filter blob.
  run_report(key, filters, db)    — run the named report, return ReportResult.

Design notes:

  * Every report respects the SAME Filters dataclass. Reports decide
    which filters are meaningful (e.g. environment is Bug-only; a
    "throughput by user" report ignores reporter_id).
  * No new columns are added to the schema. "Who resolved this bug and
    when" is derived from the activity_log table by parsing the detail
    string written by routes/bugs.py::_persist_update (the format is a
    stable contract — see RESOLUTION_DETAIL_RE below).
  * The engine is pure read-side. No INSERT / UPDATE / DELETE.
  * Queries are eager-loaded where the result row needs related objects
    (project / reporter / assignees) so we don't N+1 on the wire.

Per-item-type resolution map:
  Bug         → Resolved, Closed
  Requirement → Implemented
  Task        → Done

"Open" map:
  Bug         → New, In Progress, Reopened
  Requirement → New, In Review, Approved
  Task        → New, In Progress
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Activity,
    Attachment,
    Bug,
    Project,
    User,
    bug_assignees,
)
from app.reports.catalog import REPORT_CATALOG


# ---------------------------------------------------------------------------
# Resolution maps — what counts as "resolved" / "open" / "final" per item type.
# Keep these in sync with app/schemas.py::STATUSES_BY_TYPE.
# ---------------------------------------------------------------------------
RESOLVED_STATUSES_BY_TYPE: dict[str, list[str]] = {
    "Bug": ["Resolved", "Closed"],
    "Requirement": ["Implemented"],
    "Task": ["Done"],
}

OPEN_STATUSES_BY_TYPE: dict[str, list[str]] = {
    "Bug": ["New", "In Progress", "Reopened"],
    "Requirement": ["New", "In Review", "Approved"],
    "Task": ["New", "In Progress"],
}

# "Final" = resolution states + terminal-but-not-resolved states (Cancelled,
# Rejected, Not a Bug, etc.). Used by the project-breakdown report.
FINAL_STATUSES_BY_TYPE: dict[str, list[str]] = {
    "Bug": ["Resolved", "Closed", "Not a Bug"],
    "Requirement": ["Implemented", "Rejected"],
    "Task": ["Done", "Cancelled"],
}

# Status strings that mean "the user closed this out". Union of all
# per-type resolved sets — used when we don't know the item_type yet
# (parsing the activity_log detail string).
ALL_RESOLVED_STATUSES = sorted({
    s for sts in RESOLVED_STATUSES_BY_TYPE.values() for s in sts
})

# Regex that pulls the NEW status out of the activity log detail line
# written by routes/bugs.py::_persist_update. The format is stable:
#   "#42 'Title' — status: 'In Progress' → 'Resolved'"
# We capture the value after the arrow. Curly-quotes (en-dash dash style)
# matter: we accept both straight ' and unicode ' / '.
RESOLUTION_DETAIL_RE = re.compile(
    r"status:\s*['‘’][^'‘’]*['‘’]\s*"
    r"[→—\->]+\s*"
    r"['‘’]([^'‘’]+)['‘’]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Filters — universal across every report
# ---------------------------------------------------------------------------
@dataclass
class Filters:
    """Universal filter set. Every field defaults to "no filter".

    Date semantics: date_from / date_to are inclusive dates (no times). The
    engine converts them to start-of-day / end-of-day UTC for comparison
    against created_at / updated_at columns.
    """
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    item_types: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    project_ids: list[int] = field(default_factory=list)
    assignee_ids: list[int] = field(default_factory=list)
    reporter_ids: list[int] = field(default_factory=list)
    event_id: Optional[int] = None
    include_not_a_bug: bool = False
    text_search: Optional[str] = None

    # Free-form label users can add to identify a saved or downloaded run.
    label: str = ""

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "Filters":
        d = d or {}
        return cls(
            date_from=_parse_date(d.get("date_from")),
            date_to=_parse_date(d.get("date_to")),
            item_types=_str_list(d.get("item_types")),
            statuses=_str_list(d.get("statuses")),
            priorities=_str_list(d.get("priorities")),
            environments=_str_list(d.get("environments")),
            project_ids=_int_list(d.get("project_ids")),
            assignee_ids=_int_list(d.get("assignee_ids")),
            reporter_ids=_int_list(d.get("reporter_ids")),
            event_id=_parse_int(d.get("event_id")),
            include_not_a_bug=bool(d.get("include_not_a_bug")),
            text_search=(d.get("text_search") or "").strip() or None,
            label=(d.get("label") or "").strip()[:120],
        )

    def to_meta(self) -> dict[str, Any]:
        """Plain-dict representation suitable for the XLSX "Filters applied"
        sheet and the API echo."""
        return {
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "item_types": list(self.item_types),
            "statuses": list(self.statuses),
            "priorities": list(self.priorities),
            "environments": list(self.environments),
            "project_ids": list(self.project_ids),
            "assignee_ids": list(self.assignee_ids),
            "reporter_ids": list(self.reporter_ids),
            "event_id": self.event_id,
            "include_not_a_bug": self.include_not_a_bug,
            "text_search": self.text_search,
            "label": self.label,
        }


def _parse_date(v: Any) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except (ValueError, TypeError):
            return None
    return None


def _parse_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple, set)):
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                s = item.strip()
                if s and s not in out:
                    out.append(s)
        return out
    return []


def _int_list(v: Any) -> list[int]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        out: list[int] = []
        for item in v:
            try:
                n = int(item)
                if n not in out:
                    out.append(n)
            except (TypeError, ValueError):
                continue
        return out
    try:
        return [int(v)]
    except (TypeError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class ReportColumn:
    key: str
    label: str
    width: int = 20
    align: str = "left"
    kind: str = "text"   # text / number / date / datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "width": self.width,
            "align": self.align,
            "kind": self.kind,
        }


@dataclass
class ReportResult:
    report_key: str
    report_label: str
    columns: list[ReportColumn]
    rows: list[dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    # Optional secondary table: aggregated reports include a "drill-down"
    # list of the underlying items for the XLSX export so a manager can
    # click on the rolled-up number and immediately see the details.
    detail_columns: list[ReportColumn] = field(default_factory=list)
    detail_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    def to_api(self) -> dict[str, Any]:
        return {
            "report_key": self.report_key,
            "report_label": self.report_label,
            "columns": [c.to_dict() for c in self.columns],
            "rows": self.rows,
            "summary": self.summary,
            "filters": self.filters,
            "total": self.total,
            "has_detail": bool(self.detail_rows),
            "detail_total": len(self.detail_rows),
        }


# ---------------------------------------------------------------------------
# Shared filter application — every report's "from bugs where ..." starts here
# ---------------------------------------------------------------------------
def _start_of_day(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _end_of_day(d: date) -> datetime:
    return datetime.combine(d, time.max, tzinfo=timezone.utc)


def _apply_text_search(stmt, needle: str):
    n = needle.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{n}%"
    return stmt.where(or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    ))


# Enterprise: every Reports query MUST scope to the calling user's org so
# a manager / admin in org A can never see anything from org B. The route
# layer always supplies the actor's org_id; the engine then enforces the
# WHERE clause via Project.org_id (Bug joins Project) or Activity.org_id
# (the audit trail carries its own org_id column).
def _apply_org_scope_via_project(stmt, org_id: Optional[int]):
    if org_id is None:
        return stmt
    return stmt.where(Bug.project.has(Project.org_id == org_id))


def _apply_entity_filters(stmt, filters: Filters):
    """Who/where filters: item type, project, people, event."""
    if filters.item_types:
        stmt = stmt.where(Bug.item_type.in_(filters.item_types))
    if filters.project_ids:
        stmt = stmt.where(Bug.project_id.in_(filters.project_ids))
    if filters.assignee_ids:
        stmt = stmt.where(Bug.assignees.any(User.id.in_(filters.assignee_ids)))
    if filters.reporter_ids:
        stmt = stmt.where(Bug.reporter_id.in_(filters.reporter_ids))
    if filters.event_id is not None:
        stmt = stmt.where(Bug.event_id == filters.event_id)
    return stmt


def _apply_attribute_filters(
    stmt,
    filters: Filters,
    *,
    apply_status: bool,
    enforce_not_a_bug: bool,
):
    """What-kind filters: priority, environment, status, text search."""
    if filters.priorities:
        stmt = stmt.where(Bug.priority.in_(filters.priorities))
    if filters.environments:
        stmt = stmt.where(Bug.environment.in_(filters.environments))
    if apply_status and filters.statuses:
        stmt = stmt.where(Bug.status.in_(filters.statuses))
    if not filters.include_not_a_bug and enforce_not_a_bug:
        # Exclude Not-a-Bug from "Total" by default (matches dashboard KPI).
        stmt = stmt.where(Bug.status != "Not a Bug")
    if filters.text_search:
        stmt = _apply_text_search(stmt, filters.text_search)
    return stmt


def _apply_date_range(stmt, filters: Filters, date_column):
    if not (filters.date_from or filters.date_to):
        return stmt
    col = date_column if date_column is not None else Bug.created_at
    if filters.date_from:
        stmt = stmt.where(col >= _start_of_day(filters.date_from))
    if filters.date_to:
        stmt = stmt.where(col <= _end_of_day(filters.date_to))
    return stmt


def _apply_bug_filters(
    stmt,
    filters: Filters,
    *,
    date_column=None,           # which column the date range targets
    apply_status: bool = True,  # some reports ignore the status filter
    enforce_not_a_bug: bool = True,
    org_id: Optional[int] = None,
):
    """Layer the universal Filters onto a select(Bug.*) statement.

    `date_column` defaults to Bug.created_at when None — most reports
    "count by when it was filed". The throughput report swaps in
    Activity.created_at via its own date application.

    `org_id` — enterprise multi-tenancy guard. When set, every result
    row must belong to a project owned by that org. The engine never
    runs unscoped queries from a route handler; org_id flows in from
    the actor.
    """
    stmt = _apply_org_scope_via_project(stmt, org_id)
    stmt = _apply_entity_filters(stmt, filters)
    stmt = _apply_attribute_filters(
        stmt, filters,
        apply_status=apply_status, enforce_not_a_bug=enforce_not_a_bug,
    )
    return _apply_date_range(stmt, filters, date_column)


def _eager_bug():
    return select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    )


# ---------------------------------------------------------------------------
# Helpers — bug → row dict
# ---------------------------------------------------------------------------
def _days_open_value(created_at: Optional[datetime],
                     resolved_at: Optional[datetime]) -> Optional[int]:
    """Whole days a bug stayed open: from creation to resolution, or to
    now if still open. None when there's no creation timestamp."""
    if created_at is None:
        return None
    open_until = resolved_at if resolved_at is not None else datetime.now(timezone.utc)
    # Both columns are timezone-aware on every modern row; legacy rows
    # might be naive — coerce defensively.
    ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    ou = open_until if open_until.tzinfo else open_until.replace(tzinfo=timezone.utc)
    return max(0, (ou - ca).days)


def _bug_scalar_fields(b: Bug) -> dict[str, Any]:
    """Plain columns straight off the bug row."""
    return {
        "id": b.id,
        "item_type": getattr(b, "item_type", None) or "Bug",
        "title": b.title or "",
        "description": (b.description or "")[:32000],   # excel cell cap
        "status": b.status or "",
        "priority": b.priority or "",
        "environment": b.environment or "",
        "due_date": b.due_date or "",
        "created_at": _fmt_dt(b.created_at),
        "updated_at": _fmt_dt(b.updated_at),
    }


def _bug_relation_fields(b: Bug) -> dict[str, Any]:
    """Columns derived from eager-loaded related objects."""
    return {
        "project": b.project.name if b.project else "",
        "event": b.event.name if getattr(b, "event", None) else "",
        "reporter_name": b.reporter.name if b.reporter else "",
        "reporter_email": b.reporter.email if b.reporter else "",
        "assignees": ", ".join(a.name for a in b.assignees),
    }


def _bug_to_detail_row(b: Bug, attachments_by_bug: dict[int, int],
                      resolved_info: dict[int, tuple[Optional[str], Optional[datetime]]]) -> dict[str, Any]:
    """Full detail row — every column on the bug + computed extras."""
    resolved_by, resolved_at = resolved_info.get(b.id, (None, None))
    days_open = _days_open_value(b.created_at, resolved_at)
    row = _bug_scalar_fields(b)
    row.update(_bug_relation_fields(b))
    row["resolved_at"] = _fmt_dt(resolved_at) if resolved_at else ""
    row["resolved_by"] = resolved_by or ""
    row["days_open"] = days_open if days_open is not None else ""
    row["attachment_count"] = int(attachments_by_bug.get(b.id, 0))
    return row


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Resolution-event helpers
# ---------------------------------------------------------------------------
def _is_resolved_status(status: str, item_type: str) -> bool:
    return status in RESOLVED_STATUSES_BY_TYPE.get(item_type or "Bug", [])


def _is_open_status(status: str, item_type: str) -> bool:
    return status in OPEN_STATUSES_BY_TYPE.get(item_type or "Bug", [])


def _parse_resolution_status(detail: str) -> Optional[str]:
    """Pull the NEW status from an activity_log detail string. Returns
    None if the detail isn't a status_changed row or doesn't parse."""
    if not detail:
        return None
    m = RESOLUTION_DETAIL_RE.search(detail)
    if not m:
        return None
    return m.group(1).strip()


def _fetch_resolution_info(
    db: Session,
    bug_ids: list[int],
) -> dict[int, tuple[Optional[str], Optional[datetime]]]:
    """For each bug id, return (resolver_name, resolved_at) — the most
    recent status_changed activity that transitioned the bug INTO a
    resolved state for its item_type. Empty values when never resolved
    (still open) or when no audit row matches.
    """
    if not bug_ids:
        return {}
    # Pull every status_changed activity for these bugs + the item_type.
    rows = db.execute(
        select(
            Activity.bug_id,
            Activity.actor_name,
            Activity.created_at,
            Activity.detail,
            Bug.item_type,
        )
        .join(Bug, Bug.id == Activity.bug_id)
        .where(
            Activity.action == "status_changed",
            Activity.bug_id.in_(bug_ids),
        )
        .order_by(Activity.bug_id.asc(), Activity.created_at.desc())
    ).all()
    out: dict[int, tuple[Optional[str], Optional[datetime]]] = {}
    seen: set[int] = set()
    for bug_id, actor_name, created_at, detail, item_type in rows:
        if bug_id in seen:
            # We already captured the most recent resolution-into event
            # for this bug. A later status change away from resolved
            # (e.g. Reopened) doesn't undo the prior resolution event
            # for "who resolved it last" purposes — but if the current
            # status isn't resolved, the caller will have skipped this
            # bug anyway via the open/resolved check.
            continue
        new_status = _parse_resolution_status(detail or "")
        if new_status and _is_resolved_status(new_status, item_type or "Bug"):
            out[bug_id] = (actor_name, created_at)
            seen.add(bug_id)
    return out


def _attachments_by_bug(db: Session, bug_ids: list[int]) -> dict[int, int]:
    if not bug_ids:
        return {}
    rows = db.execute(
        select(Attachment.bug_id, func.count(Attachment.id))
        .where(Attachment.bug_id.in_(bug_ids))
        .group_by(Attachment.bug_id)
    ).all()
    return {bug_id: int(cnt) for bug_id, cnt in rows}


# ---------------------------------------------------------------------------
# Column catalogs — kept as functions so each report can share / extend
# ---------------------------------------------------------------------------
def _detail_columns() -> list[ReportColumn]:
    return [
        ReportColumn("id", "ID", 8, kind="number"),
        ReportColumn("item_type", "Type", 12),
        ReportColumn("title", "Title", 50),
        ReportColumn("project", "Project", 20),
        ReportColumn("event", "Event", 20),
        ReportColumn("status", "Status", 14),
        ReportColumn("priority", "Priority", 12),
        ReportColumn("environment", "Env", 8),
        ReportColumn("reporter_name", "Reporter", 22),
        ReportColumn("reporter_email", "Reporter Email", 28),
        ReportColumn("assignees", "Assignees", 36),
        ReportColumn("due_date", "Due Date", 12),
        ReportColumn("created_at", "Created", 22, kind="datetime"),
        ReportColumn("updated_at", "Updated", 22, kind="datetime"),
        ReportColumn("resolved_at", "Resolved", 22, kind="datetime"),
        ReportColumn("resolved_by", "Resolved By", 22),
        ReportColumn("days_open", "Days Open", 10, kind="number", align="right"),
        ReportColumn("attachment_count", "Attachments", 12, kind="number", align="right"),
        ReportColumn("description", "Description", 80),
    ]


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _report_item_detail(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Universal SQL-like detail export — every item matching filters."""
    stmt = _apply_bug_filters(_eager_bug(), filters, org_id=org_id).order_by(Bug.id.desc())
    bugs = list(db.scalars(stmt).all())
    bug_ids = [b.id for b in bugs]
    attach = _attachments_by_bug(db, bug_ids)
    resolved = _fetch_resolution_info(db, bug_ids)
    rows = [_bug_to_detail_row(b, attach, resolved) for b in bugs]
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for r in rows:
        by_type[r["item_type"]] = by_type.get(r["item_type"], 0) + 1
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_priority[r["priority"]] = by_priority.get(r["priority"], 0) + 1
    summary = {
        "total_items": len(rows),
        "by_type": by_type,
        "by_status": by_status,
        "by_priority": by_priority,
    }
    return ReportResult(
        report_key="item_detail",
        report_label="Item Detail Export",
        columns=_detail_columns(),
        rows=rows,
        summary=summary,
        filters=filters.to_meta(),
    )


def _report_pending_snapshot(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Items that are currently OPEN. Status filter from the user is
    intersected with the open-status set per item_type."""
    types_to_use = filters.item_types or ["Bug", "Requirement", "Task"]
    open_set: set[str] = set()
    for t in types_to_use:
        open_set.update(OPEN_STATUSES_BY_TYPE.get(t, []))
    if filters.statuses:
        open_set &= set(filters.statuses)
    if not open_set:
        # Empty intersection — no matches by construction.
        return ReportResult(
            report_key="pending_snapshot",
            report_label="Pending Items Snapshot",
            columns=_detail_columns(),
            rows=[],
            summary={"total_items": 0},
            filters=filters.to_meta(),
        )
    stmt = _apply_bug_filters(_eager_bug(), filters, apply_status=False, org_id=org_id).where(
        Bug.status.in_(list(open_set))
    ).order_by(Bug.priority.desc(), Bug.created_at.asc())
    bugs = list(db.scalars(stmt).all())
    bug_ids = [b.id for b in bugs]
    attach = _attachments_by_bug(db, bug_ids)
    # Pending items aren't resolved; resolved_info stays empty.
    rows = [_bug_to_detail_row(b, attach, {}) for b in bugs]
    by_priority: dict[str, int] = {}
    by_assignee: dict[str, int] = {}
    for r in rows:
        by_priority[r["priority"]] = by_priority.get(r["priority"], 0) + 1
        # Count one per assignee, not per item.
        for name in (r["assignees"] or "").split(", "):
            n = name.strip()
            if n:
                by_assignee[n] = by_assignee.get(n, 0) + 1
    return ReportResult(
        report_key="pending_snapshot",
        report_label="Pending Items Snapshot",
        columns=_detail_columns(),
        rows=rows,
        summary={
            "total_items": len(rows),
            "by_priority": by_priority,
            "by_assignee": by_assignee,
        },
        filters=filters.to_meta(),
    )


def _build_throughput_query(filters: Filters, *, org_id: Optional[int] = None):
    """SELECT every status_changed activity for the filter set within the
    date window. Joined back to Bug so we can check item_type for the
    resolution-state map. Excludes activities by deleted users (NULL
    actor_user_id) since "by user" needs a user."""
    stmt = (
        select(
            Activity.bug_id,
            Activity.actor_user_id,
            Activity.actor_name,
            Activity.created_at,
            Activity.detail,
            Bug.item_type,
            Bug.title,
            Bug.priority,
            Bug.status.label("current_status"),
            Project.name.label("project_name"),
        )
        .join(Bug, Bug.id == Activity.bug_id)
        .outerjoin(Project, Project.id == Bug.project_id)
        .where(Activity.action == "status_changed")
    )
    if org_id is not None:
        # Activity carries its own org_id, but Bug doesn't — scope on the
        # cheaper indexed column AND on the project's org for defense in
        # depth (in case a row was historically logged with the wrong org).
        stmt = stmt.where(Activity.org_id == org_id, Project.org_id == org_id)
    # Date range — anchored on the audit row's timestamp.
    if filters.date_from:
        stmt = stmt.where(Activity.created_at >= _start_of_day(filters.date_from))
    if filters.date_to:
        stmt = stmt.where(Activity.created_at <= _end_of_day(filters.date_to))
    # Bug-side filters still apply.
    if filters.item_types:
        stmt = stmt.where(Bug.item_type.in_(filters.item_types))
    if filters.project_ids:
        stmt = stmt.where(Bug.project_id.in_(filters.project_ids))
    if filters.event_id is not None:
        stmt = stmt.where(Bug.event_id == filters.event_id)
    if filters.assignee_ids:
        stmt = stmt.where(Bug.assignees.any(User.id.in_(filters.assignee_ids)))
    if filters.reporter_ids:
        # The reporter filter on a throughput report is unusual but valid —
        # "how many of MY filed bugs got resolved last week, and by whom".
        stmt = stmt.where(Bug.reporter_id.in_(filters.reporter_ids))
    if filters.priorities:
        stmt = stmt.where(Bug.priority.in_(filters.priorities))
    if filters.environments:
        stmt = stmt.where(Bug.environment.in_(filters.environments))
    return stmt.order_by(Activity.bug_id.asc(), Activity.created_at.asc())


def _fold_throughput_row(
    raw,
    per_user: dict[int, dict[str, Any]],
    detail_rows: list[dict[str, Any]],
) -> None:
    """Process one throughput-query tuple: count it into the right user
    bucket and append a detail row. No-op if the row isn't a transition
    into a resolved state for the item's type."""
    (bug_id, actor_id, actor_name, created_at, detail,
     item_type, title, priority, current_status, project_name) = raw
    it = item_type or "Bug"
    new_status = _parse_resolution_status(detail or "")
    if not new_status or not _is_resolved_status(new_status, it):
        return
    key = actor_id if actor_id is not None else -1  # -1 = deleted-user actor
    name = actor_name or "(deleted user)"
    bucket = per_user.setdefault(key, {
        "user_id": key if key != -1 else None,
        "user_name": name,
        "resolved_count": 0,
        "by_status": {},
        "by_type": {},
    })
    bucket["resolved_count"] += 1
    bucket["by_status"][new_status] = bucket["by_status"].get(new_status, 0) + 1
    bucket["by_type"][it] = bucket["by_type"].get(it, 0) + 1
    detail_rows.append({
        "user_name": name,
        "bug_id": bug_id,
        "item_type": it,
        "title": (title or "")[:200],
        "project": project_name or "",
        "priority": priority or "",
        "new_status": new_status,
        "current_status": current_status or "",
        "resolved_at": _fmt_dt(created_at),
    })


def _accumulate_throughput(rows_raw) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Fold throughput-query tuples into per-user buckets + detail rows.

    Only rows whose detail string records a transition INTO a resolved
    state for the item's type are counted. Deleted-user actors collapse
    onto the -1 sentinel key.
    """
    per_user: dict[int, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    for raw in rows_raw:
        _fold_throughput_row(raw, per_user, detail_rows)
    return per_user, detail_rows


def _report_throughput(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Per-user count of items they moved INTO a resolved state during
    the time window. Multi-counts intentionally if the same bug was
    reopened and re-resolved by different people in the same window —
    that's two real units of work."""
    rows_raw = db.execute(_build_throughput_query(filters, org_id=org_id)).all()
    per_user, detail_rows = _accumulate_throughput(rows_raw)
    rows: list[dict[str, Any]] = []
    for bucket in per_user.values():
        rows.append({
            "user_name": bucket["user_name"],
            "user_id": bucket["user_id"],
            "resolved": bucket["resolved_count"],
            "bugs": bucket["by_type"].get("Bug", 0),
            "requirements": bucket["by_type"].get("Requirement", 0),
            "tasks": bucket["by_type"].get("Task", 0),
        })
    rows.sort(key=lambda r: (-r["resolved"], r["user_name"].lower()))
    columns = [
        ReportColumn("user_name", "User", 28),
        ReportColumn("resolved", "Total Resolved", 14, kind="number", align="right"),
        ReportColumn("bugs", "Bugs", 10, kind="number", align="right"),
        ReportColumn("requirements", "Requirements", 14, kind="number", align="right"),
        ReportColumn("tasks", "Tasks", 10, kind="number", align="right"),
    ]
    detail_columns = [
        ReportColumn("user_name", "User", 24),
        ReportColumn("bug_id", "Item ID", 10, kind="number"),
        ReportColumn("item_type", "Type", 12),
        ReportColumn("title", "Title", 50),
        ReportColumn("project", "Project", 20),
        ReportColumn("priority", "Priority", 12),
        ReportColumn("new_status", "Resolved To", 14),
        ReportColumn("current_status", "Current Status", 14),
        ReportColumn("resolved_at", "Resolved At", 22, kind="datetime"),
    ]
    total = sum(r["resolved"] for r in rows)
    return ReportResult(
        report_key="throughput",
        report_label="Resolution Throughput",
        columns=columns,
        rows=rows,
        summary={"total_resolved": total, "user_count": len(rows)},
        filters=filters.to_meta(),
        detail_columns=detail_columns,
        detail_rows=detail_rows,
    )


def _distribution_report(
    db: Session,
    filters: Filters,
    *,
    column: Any,
    label_key: str,
    label_header: str,
    report_key: str,
    report_label: str,
    org_id: Optional[int] = None,
) -> ReportResult:
    """Shared helper for status / priority distribution.

    column = Bug.status or Bug.priority (SQLAlchemy column).
    """
    base = _apply_bug_filters(select(Bug.id), filters, org_id=org_id)
    base_subq = base.subquery()
    # We GROUP BY the chosen column over the filtered set.
    grouped = db.execute(
        select(column, func.count(Bug.id))
        .where(Bug.id.in_(select(base_subq.c.id)))
        .group_by(column)
        .order_by(func.count(Bug.id).desc())
    ).all()
    rows = [{label_key: v or "(unset)", "count": int(c)} for v, c in grouped]
    total = sum(r["count"] for r in rows)
    for r in rows:
        r["percentage"] = round((r["count"] / total) * 100, 1) if total else 0.0
    # Drill-down: every bug that contributed to a non-zero bucket.
    detail_stmt = _apply_bug_filters(_eager_bug(), filters, org_id=org_id).order_by(Bug.id.desc())
    detail_bugs = list(db.scalars(detail_stmt).all())
    attach = _attachments_by_bug(db, [b.id for b in detail_bugs])
    detail_rows = [_bug_to_detail_row(b, attach, {}) for b in detail_bugs]
    columns = [
        ReportColumn(label_key, label_header, 18),
        ReportColumn("count", "Count", 12, kind="number", align="right"),
        ReportColumn("percentage", "Percent", 12, kind="number", align="right"),
    ]
    return ReportResult(
        report_key=report_key,
        report_label=report_label,
        columns=columns,
        rows=rows,
        summary={"total_items": total},
        filters=filters.to_meta(),
        detail_columns=_detail_columns(),
        detail_rows=detail_rows,
    )


def _report_status_distribution(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    return _distribution_report(
        db, filters,
        column=Bug.status, label_key="status", label_header="Status",
        report_key="status_distribution", report_label="Status Distribution",
        org_id=org_id,
    )


def _report_priority_distribution(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    return _distribution_report(
        db, filters,
        column=Bug.priority, label_key="priority", label_header="Priority",
        report_key="priority_distribution", report_label="Priority Distribution",
        org_id=org_id,
    )


def _report_project_breakdown(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Per-project: created, still open, resolved/done. Drill-down is the
    full item list for the active filter set."""
    base = _apply_bug_filters(_eager_bug(), filters, org_id=org_id).order_by(Bug.id.desc())
    bugs = list(db.scalars(base).all())
    by_project: dict[str, dict[str, int]] = {}
    for b in bugs:
        proj = b.project.name if b.project else "(no project)"
        bucket = by_project.setdefault(proj, {
            "created": 0, "open": 0, "resolved": 0, "final": 0,
        })
        bucket["created"] += 1
        it = getattr(b, "item_type", None) or "Bug"
        if _is_open_status(b.status or "", it):
            bucket["open"] += 1
        if _is_resolved_status(b.status or "", it):
            bucket["resolved"] += 1
        if b.status in FINAL_STATUSES_BY_TYPE.get(it, []):
            bucket["final"] += 1
    rows = [
        {"project": name, **counts}
        for name, counts in by_project.items()
    ]
    rows.sort(key=lambda r: (-r["created"], r["project"].lower()))
    columns = [
        ReportColumn("project", "Project", 28),
        ReportColumn("created", "Created", 12, kind="number", align="right"),
        ReportColumn("open", "Open", 10, kind="number", align="right"),
        ReportColumn("resolved", "Resolved", 12, kind="number", align="right"),
        ReportColumn("final", "Final", 10, kind="number", align="right"),
    ]
    attach = _attachments_by_bug(db, [b.id for b in bugs])
    resolved_info = _fetch_resolution_info(db, [b.id for b in bugs])
    detail_rows = [_bug_to_detail_row(b, attach, resolved_info) for b in bugs]
    return ReportResult(
        report_key="project_breakdown",
        report_label="Project Breakdown",
        columns=columns,
        rows=rows,
        summary={"project_count": len(rows), "item_count": len(bugs)},
        filters=filters.to_meta(),
        detail_columns=_detail_columns(),
        detail_rows=detail_rows,
    )


def _age_bucket(days: int) -> str:
    if days <= 7:
        return "0-7 days"
    if days <= 30:
        return "8-30 days"
    if days <= 60:
        return "31-60 days"
    if days <= 90:
        return "61-90 days"
    return "90+ days"


def _report_aging(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Open items sorted oldest first, with an age bucket."""
    types_to_use = filters.item_types or ["Bug", "Requirement", "Task"]
    open_set: set[str] = set()
    for t in types_to_use:
        open_set.update(OPEN_STATUSES_BY_TYPE.get(t, []))
    if filters.statuses:
        open_set &= set(filters.statuses)
    stmt = _apply_bug_filters(_eager_bug(), filters, apply_status=False, org_id=org_id)
    if open_set:
        stmt = stmt.where(Bug.status.in_(list(open_set)))
    stmt = stmt.order_by(Bug.created_at.asc())
    bugs = list(db.scalars(stmt).all())
    bug_ids = [b.id for b in bugs]
    attach = _attachments_by_bug(db, bug_ids)
    rows: list[dict[str, Any]] = []
    by_bucket: dict[str, int] = {}
    for b in bugs:
        base_row = _bug_to_detail_row(b, attach, {})
        days = base_row["days_open"] if isinstance(base_row["days_open"], int) else 0
        bucket = _age_bucket(days)
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        base_row["age_bucket"] = bucket
        rows.append(base_row)
    columns = [
        ReportColumn("id", "ID", 8, kind="number"),
        ReportColumn("item_type", "Type", 12),
        ReportColumn("title", "Title", 50),
        ReportColumn("project", "Project", 20),
        ReportColumn("status", "Status", 14),
        ReportColumn("priority", "Priority", 12),
        ReportColumn("assignees", "Assignees", 28),
        ReportColumn("created_at", "Created", 22, kind="datetime"),
        ReportColumn("days_open", "Days Open", 10, kind="number", align="right"),
        ReportColumn("age_bucket", "Bucket", 14),
    ]
    return ReportResult(
        report_key="aging",
        report_label="Aging Report",
        columns=columns,
        rows=rows,
        summary={"total_open": len(rows), "by_bucket": by_bucket},
        filters=filters.to_meta(),
    )


def _report_timeline(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Per-day counts of created vs resolved within the window. If no
    date range is provided, defaults to the last 30 days."""
    today = datetime.now(timezone.utc).date()
    start = filters.date_from or (today - timedelta(days=29))
    end = filters.date_to or today
    # Created: bugs.created_at in [start, end]
    created_stmt = _apply_bug_filters(
        select(func.date(Bug.created_at), func.count(Bug.id)),
        Filters(
            date_from=start, date_to=end,
            item_types=filters.item_types, statuses=[],   # don't filter status
            priorities=filters.priorities,
            environments=filters.environments,
            project_ids=filters.project_ids,
            assignee_ids=filters.assignee_ids,
            reporter_ids=filters.reporter_ids,
            event_id=filters.event_id,
            include_not_a_bug=filters.include_not_a_bug,
            text_search=filters.text_search,
        ),
        org_id=org_id,
    ).group_by(func.date(Bug.created_at))
    created_by_day = {str(d): int(c) for d, c in db.execute(created_stmt).all()}
    # Resolved: rely on the throughput query, then bucket per day.
    res_filters = Filters(
        date_from=start, date_to=end,
        item_types=filters.item_types,
        priorities=filters.priorities,
        environments=filters.environments,
        project_ids=filters.project_ids,
        assignee_ids=filters.assignee_ids,
        reporter_ids=filters.reporter_ids,
        event_id=filters.event_id,
        include_not_a_bug=filters.include_not_a_bug,
        text_search=filters.text_search,
    )
    resolved_by_day: dict[str, int] = {}
    for (_bug_id, _actor_id, _actor_name, created_at, detail,
         item_type, *_rest) in db.execute(_build_throughput_query(res_filters, org_id=org_id)).all():
        ns = _parse_resolution_status(detail or "")
        if ns and _is_resolved_status(ns, item_type or "Bug"):
            key = (created_at.date() if isinstance(created_at, datetime) else created_at).isoformat()
            resolved_by_day[key] = resolved_by_day.get(key, 0) + 1
    rows: list[dict[str, Any]] = []
    day = start
    total_created = 0
    total_resolved = 0
    while day <= end:
        key = day.isoformat()
        c = int(created_by_day.get(key, 0))
        r = int(resolved_by_day.get(key, 0))
        total_created += c
        total_resolved += r
        rows.append({
            "date": key,
            "created": c,
            "resolved": r,
            "delta": c - r,
        })
        day = day + timedelta(days=1)
    columns = [
        ReportColumn("date", "Date", 14, kind="date"),
        ReportColumn("created", "Created", 12, kind="number", align="right"),
        ReportColumn("resolved", "Resolved", 12, kind="number", align="right"),
        ReportColumn("delta", "Delta (Created-Resolved)", 22, kind="number", align="right"),
    ]
    return ReportResult(
        report_key="timeline",
        report_label="Created vs Resolved Timeline",
        columns=columns,
        rows=rows,
        summary={
            "window_days": len(rows),
            "total_created": total_created,
            "total_resolved": total_resolved,
            "net": total_created - total_resolved,
        },
        filters=filters.to_meta(),
    )


def _ttr_row(raw, bug_created: Optional[datetime]) -> Optional[dict[str, Any]]:
    """Build one Time-to-Resolution row from a throughput-query tuple, or
    return None if the row isn't a resolved transition with a known
    creation time."""
    (bug_id, _actor_id, actor_name, created_at, detail,
     item_type, title, priority, _current_status, project_name) = raw
    ns = _parse_resolution_status(detail or "")
    if not (ns and _is_resolved_status(ns, item_type or "Bug")):
        return None
    if bug_created is None or created_at is None:
        return None
    ca = bug_created if bug_created.tzinfo else bug_created.replace(tzinfo=timezone.utc)
    ra = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    hours = round(max(0.0, (ra - ca).total_seconds()) / 3600, 2)
    return {
        "bug_id": bug_id,
        "item_type": item_type or "Bug",
        "title": (title or "")[:200],
        "project": project_name or "",
        "priority": priority or "",
        "resolved_by": actor_name or "",
        "created_at": _fmt_dt(bug_created),
        "resolved_at": _fmt_dt(created_at),
        "hours_to_resolve": hours,
        "days_to_resolve": round(hours / 24, 2),
    }


def _ttr_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate avg / median / p95 / fastest / slowest over built rows."""
    durations = [r["hours_to_resolve"] for r in rows]
    if not durations:
        return {"count": 0, "average_hours": 0, "median_hours": 0,
                "p95_hours": 0, "fastest_hours": 0, "slowest_hours": 0}
    return {
        "count": len(rows),
        "average_hours": round(sum(durations) / len(durations), 2),
        "median_hours": round(statistics.median(durations), 2),
        "p95_hours": round(_percentile(durations, 95), 2),
        "fastest_hours": rows[0]["hours_to_resolve"],
        "slowest_hours": rows[-1]["hours_to_resolve"],
    }


def _report_time_to_resolution(db: Session, filters: Filters, *, org_id: Optional[int] = None) -> ReportResult:
    """Per-resolved-item: hours from creation to resolution. Plus aggregate
    avg / median / p95 across the whole filtered set."""
    rows_raw = db.execute(_build_throughput_query(filters, org_id=org_id)).all()
    # Load bug creation times in one shot.
    bug_ids = list({row[0] for row in rows_raw})
    creation = dict(db.execute(
        select(Bug.id, Bug.created_at).where(Bug.id.in_(bug_ids))
    ).all()) if bug_ids else {}
    rows: list[dict[str, Any]] = []
    seen_bug: set[int] = set()
    for raw in rows_raw:
        bug_id = raw[0]
        if bug_id in seen_bug:
            continue
        row = _ttr_row(raw, creation.get(bug_id))
        if row is None:
            continue
        seen_bug.add(bug_id)
        rows.append(row)
    rows.sort(key=lambda r: r["hours_to_resolve"])
    columns = [
        ReportColumn("bug_id", "Item ID", 10, kind="number"),
        ReportColumn("item_type", "Type", 12),
        ReportColumn("title", "Title", 50),
        ReportColumn("project", "Project", 20),
        ReportColumn("priority", "Priority", 12),
        ReportColumn("resolved_by", "Resolved By", 22),
        ReportColumn("created_at", "Created", 22, kind="datetime"),
        ReportColumn("resolved_at", "Resolved", 22, kind="datetime"),
        ReportColumn("hours_to_resolve", "Hours", 12, kind="number", align="right"),
        ReportColumn("days_to_resolve", "Days", 10, kind="number", align="right"),
    ]
    return ReportResult(
        report_key="time_to_resolution",
        report_label="Time to Resolution",
        columns=columns,
        rows=rows,
        summary=_ttr_summary(rows),
        filters=filters.to_meta(),
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    # Linear interpolation between closest ranks (NIST style).
    k = (len(s) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    d0 = s[f] * (c - k)
    d1 = s[c] * (k - f)
    return d0 + d1


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------
_DISPATCH = {
    "item_detail":             _report_item_detail,
    "pending_snapshot":        _report_pending_snapshot,
    "throughput":              _report_throughput,
    "status_distribution":     _report_status_distribution,
    "priority_distribution":   _report_priority_distribution,
    "project_breakdown":       _report_project_breakdown,
    "aging":                   _report_aging,
    "timeline":                _report_timeline,
    "time_to_resolution":      _report_time_to_resolution,
}


class UnknownReportError(ValueError):
    """Raised when the report key isn't in REPORT_CATALOG."""


def run_report(
    key: str,
    filters: Filters,
    db: Session,
    *,
    org_id: Optional[int] = None,
) -> ReportResult:
    """Run the named report. Raises UnknownReportError if `key` is bogus.

    `org_id` — enterprise multi-tenancy scope. The route layer ALWAYS
    passes `actor.org_id`; the engine refuses to leak data across orgs.
    Tests may omit it to keep fixture setup simple — in that case the
    engine returns ALL rows (single-org tests).
    """
    if key not in _DISPATCH:
        raise UnknownReportError(
            f"Unknown report '{key}'. "
            f"Known: {', '.join(sorted(REPORT_CATALOG.keys()))}"
        )
    return _DISPATCH[key](db, filters, org_id=org_id)


__all__ = [
    "Filters",
    "ReportColumn",
    "ReportResult",
    "UnknownReportError",
    "run_report",
    "RESOLVED_STATUSES_BY_TYPE",
    "OPEN_STATUSES_BY_TYPE",
    "FINAL_STATUSES_BY_TYPE",
]
