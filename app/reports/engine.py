"""Reports engine: the single read-only SQL surface for every report.

Entry points: Filters.from_dict({...}) and run_report(key, filters, db). Every
report shares the Filters dataclass. Resolution info is derived from activity_log
detail strings written by routes/bugs.py::_persist_update (see RESOLUTION_DETAIL_RE).
Per-type resolved/open status maps live in the constants below.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.models import (
    Activity,
    Attachment,
    Bug,
    Project,
    User,
)
from app.reports.catalog import REPORT_CATALOG

# Resolution maps — keep in sync with app/schemas.py::STATUSES_BY_TYPE.
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

# "Final" = resolved plus terminal-but-not-resolved states; used by project-breakdown.
FINAL_STATUSES_BY_TYPE: dict[str, list[str]] = {
    "Bug": ["Resolved", "Closed", "Not a Bug"],
    "Requirement": ["Implemented", "Rejected"],
    "Task": ["Done", "Cancelled"],
}

# Captures the new status after the arrow in a "status: 'x' → 'y'" detail line;
# accepts straight and curly quotes.
RESOLUTION_DETAIL_RE = re.compile(
    r"status:\s*['‘’][^'‘’]*['‘’]\s*"
    r"[→—\->]+\s*"
    r"['‘’]([^'‘’]+)['‘’]",
    re.IGNORECASE,
)

# Captures the old (pre-arrow) status so throughput only credits genuine
# open→resolved crossings (not e.g. Resolved→Closed).
RESOLUTION_OLD_STATUS_RE = re.compile(
    r"status:\s*['‘’]([^'‘’]*)['‘’]\s*[→—\->]+\s*['‘’][^'‘’]+['‘’]",
    re.IGNORECASE,
)


@dataclass
class Filters:
    """Universal filter set; every field defaults to "no filter". date_from/
    date_to are inclusive dates, converted to start/end-of-day UTC for comparison."""
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

    # Free-form label to identify a saved or downloaded run.
    label: str = ""

    # Route-set from the actor's accessible projects (never user-supplied, so
    # scope can't be widened via the payload). None = unrestricted; empty set =
    # see nothing. Omitted from from_dict/to_meta since it isn't user-chosen.
    restrict_project_ids: Optional[set[int]] = None

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
        """Serialized filters for the XLSX "Filters applied" sheet and API."""
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

    def with_overrides(self, **overrides: Any) -> "Filters":
        """Typed wrapper over dataclasses.replace() so callers get back a
        properly-typed Filters, not the generic DataclassInstance."""
        return replace(self, **overrides)


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
    # Drill-down of the underlying items for the XLSX export (aggregate reports).
    detail_columns: list[ReportColumn] = field(default_factory=list)
    detail_rows: list[dict[str, Any]] = field(default_factory=list)
    # True when the detail query hit _detail_cap(); export route returns 413
    # rather than let an aggregate under-count over a partial scan.
    truncated: bool = False

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
    # Route-set project scope; empty set matches nothing (tagless manager).
    if filters.restrict_project_ids is not None:
        stmt = stmt.where(Bug.project_id.in_(filters.restrict_project_ids))
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
        # Exclude Not-a-Bug by default (matches dashboard KPI).
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
):
    """Layer the universal Filters onto a select(Bug.*) statement. `date_column`
    defaults to Bug.created_at (most reports count by filed date)."""
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


def _days_open_value(created_at: Optional[datetime],
                     resolved_at: Optional[datetime]) -> Optional[int]:
    """Days open, creation to resolution (or now); None without a creation time."""
    if created_at is None:
        return None
    open_until = resolved_at if resolved_at is not None else datetime.now(timezone.utc)
    # Coerce legacy naive timestamps defensively.
    ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    ou = open_until if open_until.tzinfo else open_until.replace(tzinfo=timezone.utc)
    return max(0, (ou - ca).days)


def _bug_scalar_fields(b: Bug) -> dict[str, Any]:
    """Scalar columns directly from the bug row."""
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
    """Columns from eager-loaded relationships."""
    return {
        "project": b.project.name if b.project else "",
        "event": b.event.name if getattr(b, "event", None) else "",
        "reporter_name": b.reporter.name if b.reporter else "",
        "reporter_email": b.reporter.email if b.reporter else "",
        "assignees": ", ".join(a.name for a in b.assignees),
    }


def _bug_to_detail_row(b: Bug, attachments_by_bug: dict[int, int],
                      resolved_info: dict[int, tuple[Optional[str], Optional[datetime]]]) -> dict[str, Any]:
    """Full detail row: every column on the bug plus computed extras."""
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


def _is_resolved_status(status: str, item_type: str) -> bool:
    return status in RESOLVED_STATUSES_BY_TYPE.get(item_type or "Bug", [])


def _is_open_status(status: str, item_type: str) -> bool:
    return status in OPEN_STATUSES_BY_TYPE.get(item_type or "Bug", [])


def _parse_resolution_status(detail: str) -> Optional[str]:
    """Pull the new status from an activity_log detail string, or None."""
    if not detail:
        return None
    # Rightmost match so a crafted title can't spoof the resolution.
    matches = RESOLUTION_DETAIL_RE.findall(detail)
    if not matches:
        return None
    return matches[-1].strip()


def _parse_prior_status(detail: str) -> Optional[str]:
    """Pull the old (pre-arrow) status; rightmost match, as _parse_resolution_status."""
    if not detail:
        return None
    matches = RESOLUTION_OLD_STATUS_RE.findall(detail)
    if not matches:
        return None
    return matches[-1].strip()


def _fetch_resolution_info(
    db: Session,
    bug_ids: list[int],
) -> dict[int, tuple[Optional[str], Optional[datetime]]]:
    """Return (resolver_name, resolved_at) per bug id from the most recent
    status_changed activity into a resolved state. Absent when never resolved."""
    if not bug_ids:
        return {}
    rows = db.execute(
        select(
            Activity.bug_id,
            Activity.actor_name,
            Activity.created_at,
            Activity.detail,
            Bug.item_type,
            Bug.status.label("current_status"),
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
    for bug_id, actor_name, created_at, detail, item_type, current_status in rows:
        if bug_id in seen:
            continue
        # Only attribute resolution when currently resolved, so a reopened bug
        # doesn't carry a stale resolved_at/resolver.
        if not _is_resolved_status(current_status or "", item_type or "Bug"):
            seen.add(bug_id)
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


def _detail_cap() -> int:
    """Hard row ceiling per report; the +1 over MAX_REPORT_ROWS lets the export
    route detect overflow and return 413."""
    return get_settings().MAX_REPORT_ROWS + 1


# Caps the day-by-day timeline against a pathological date_from.
_MAX_TIMELINE_DAYS = 366


def _report_item_detail(db: Session, filters: Filters) -> ReportResult:
    """Full detail export: every item matching filters."""
    # Bound the read; the route turns an over-limit result into a 413.
    stmt = (_apply_bug_filters(_eager_bug(), filters)
            .order_by(Bug.id.desc())
            .limit(_detail_cap()))
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


def _report_pending_snapshot(db: Session, filters: Filters) -> ReportResult:
    """Currently-open items; the user's status filter is intersected with the
    per-type open set."""
    types_to_use = filters.item_types or ["Bug", "Requirement", "Task"]
    open_set: set[str] = set()
    for t in types_to_use:
        open_set.update(OPEN_STATUSES_BY_TYPE.get(t, []))
    if filters.statuses:
        open_set &= set(filters.statuses)
    if not open_set:
        # Empty intersection: nothing can match.
        return ReportResult(
            report_key="pending_snapshot",
            report_label="Pending Items Snapshot",
            columns=_detail_columns(),
            rows=[],
            summary={"total_items": 0},
            filters=filters.to_meta(),
        )
    stmt = _apply_bug_filters(_eager_bug(), filters, apply_status=False).where(
        Bug.status.in_(list(open_set))
    ).order_by(Bug.priority.desc(), Bug.created_at.asc()).limit(_detail_cap())
    bugs = list(db.scalars(stmt).all())
    bug_ids = [b.id for b in bugs]
    attach = _attachments_by_bug(db, bug_ids)
    # Pending items aren't resolved; pass an empty resolved_info dict.
    rows = [_bug_to_detail_row(b, attach, {}) for b in bugs]
    by_priority: dict[str, int] = {}
    by_assignee: dict[str, int] = {}
    for r in rows:
        by_priority[r["priority"]] = by_priority.get(r["priority"], 0) + 1
    # Count from the ORM relationship; splitting the display string would produce
    # phantom names.
    for b in bugs:
        for a in b.assignees:
            by_assignee[a.name] = by_assignee.get(a.name, 0) + 1
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


def _build_throughput_query(filters: Filters):
    """Build the status_changed activity query, joined to Bug for item_type.
    Deleted-user actors are kept, bucketed by their preserved name snapshot."""
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
    # Date range anchored on the audit row, not Bug.created_at.
    if filters.date_from:
        stmt = stmt.where(Activity.created_at >= _start_of_day(filters.date_from))
    if filters.date_to:
        stmt = stmt.where(Activity.created_at <= _end_of_day(filters.date_to))
    # Bug-side filters still apply.
    if filters.item_types:
        stmt = stmt.where(Bug.item_type.in_(filters.item_types))
    if filters.project_ids:
        stmt = stmt.where(Bug.project_id.in_(filters.project_ids))
    # Route-set project scope (audit-log reports don't go through _apply_bug_filters).
    if filters.restrict_project_ids is not None:
        stmt = stmt.where(Bug.project_id.in_(filters.restrict_project_ids))
    if filters.event_id is not None:
        stmt = stmt.where(Bug.event_id == filters.event_id)
    if filters.assignee_ids:
        stmt = stmt.where(Bug.assignees.any(User.id.in_(filters.assignee_ids)))
    if filters.reporter_ids:
        # Unusual but valid: "how many bugs I filed got resolved?"
        stmt = stmt.where(Bug.reporter_id.in_(filters.reporter_ids))
    if filters.priorities:
        stmt = stmt.where(Bug.priority.in_(filters.priorities))
    if filters.environments:
        stmt = stmt.where(Bug.environment.in_(filters.environments))
    return stmt.order_by(Activity.bug_id.asc(), Activity.created_at.asc())


def _fold_throughput_row(
    raw,
    per_user: dict[Any, dict[str, Any]],
    detail_rows: list[dict[str, Any]],
) -> None:
    """Count one throughput tuple into its user bucket and append a detail row;
    no-op unless it's a transition into a resolved state."""
    (bug_id, actor_id, actor_name, created_at, detail,
     item_type, title, priority, current_status, project_name) = raw
    it = item_type or "Bug"
    new_status = _parse_resolution_status(detail or "")
    if not new_status or not _is_resolved_status(new_status, it):
        return
    # Skip resolved→resolved (e.g. Resolved→Closed) to avoid double-counting.
    old_status = _parse_prior_status(detail or "")
    if old_status and _is_resolved_status(old_status, it):
        return
    name = actor_name or "(deleted user)"
    # actor_user_id is NULL for deleted users; bucket on the name snapshot so
    # distinct ex-users don't collapse onto one row.
    key = actor_id if actor_id is not None else f"deleted:{name}"
    bucket = per_user.setdefault(key, {
        "user_id": actor_id,
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


def _accumulate_throughput(rows_raw) -> tuple[dict[Any, dict[str, Any]], list[dict[str, Any]]]:
    """Fold throughput tuples into per-user buckets and detail rows (resolved
    transitions only)."""
    per_user: dict[Any, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    for raw in rows_raw:
        _fold_throughput_row(raw, per_user, detail_rows)
    return per_user, detail_rows


def _report_throughput(db: Session, filters: Filters) -> ReportResult:
    """Per-user count of items resolved in the window (a reopened-then-re-resolved
    bug counts twice — two units of work)."""
    # Bound the detail list so the 413 guard can't be outrun into an OOM.
    rows_raw = db.execute(_build_throughput_query(filters).limit(_detail_cap())).all()
    truncated = len(rows_raw) >= _detail_cap()
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
        truncated=truncated,
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
) -> ReportResult:
    """Shared helper for status / priority distribution (column is the grouped column)."""
    base = _apply_bug_filters(select(Bug.id), filters)
    base_subq = base.subquery()
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
    # Drill-down detail, capped; the GROUP BY counts above stay exact regardless.
    detail_stmt = (_apply_bug_filters(_eager_bug(), filters)
                   .order_by(Bug.id.desc())
                   .limit(_detail_cap()))
    detail_bugs = list(db.scalars(detail_stmt).all())
    attach = _attachments_by_bug(db, [b.id for b in detail_bugs])
    # Fill resolved-at/by in the drill-down (consistent with item_detail).
    resolved_info = _fetch_resolution_info(db, [b.id for b in detail_bugs])
    detail_rows = [_bug_to_detail_row(b, attach, resolved_info) for b in detail_bugs]
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


def _report_status_distribution(db: Session, filters: Filters) -> ReportResult:
    return _distribution_report(
        db, filters,
        column=Bug.status, label_key="status", label_header="Status",
        report_key="status_distribution", report_label="Status Distribution",
    )


def _report_priority_distribution(db: Session, filters: Filters) -> ReportResult:
    return _distribution_report(
        db, filters,
        column=Bug.priority, label_key="priority", label_header="Priority",
        report_key="priority_distribution", report_label="Priority Distribution",
    )


def _add_breakdown_counts(bucket: dict[str, int], status: str,
                          item_type: str, count: int) -> None:
    """Fold one (status, item_type) group's count into a project bucket."""
    bucket["created"] += count
    if _is_open_status(status, item_type):
        bucket["open"] += count
    if _is_resolved_status(status, item_type):
        bucket["resolved"] += count
    if status in FINAL_STATUSES_BY_TYPE.get(item_type, []):
        bucket["final"] += count


def _report_project_breakdown(db: Session, filters: Filters) -> ReportResult:
    """Per-project counts (created, open, resolved/final). Rolled-up counts are a
    GROUP BY over the full set (exact); only the drill-down is capped."""
    agg_stmt = _apply_bug_filters(
        select(Project.name, Bug.item_type, Bug.status, func.count(Bug.id))
        .select_from(Bug)
        .join(Project, Project.id == Bug.project_id),
        filters,
    ).group_by(Project.name, Bug.item_type, Bug.status)
    by_project: dict[str, dict[str, int]] = {}
    for proj, item_type, statusv, count in db.execute(agg_stmt).all():
        bucket = by_project.setdefault(proj or "(no project)", {
            "created": 0, "open": 0, "resolved": 0, "final": 0,
        })
        _add_breakdown_counts(bucket, statusv or "", item_type or "Bug", int(count))
    rows = [{"project": name, **counts} for name, counts in by_project.items()]
    rows.sort(key=lambda r: (-r["created"], r["project"].lower()))
    columns = [
        ReportColumn("project", "Project", 28),
        ReportColumn("created", "Created", 12, kind="number", align="right"),
        ReportColumn("open", "Open", 10, kind="number", align="right"),
        ReportColumn("resolved", "Resolved", 12, kind="number", align="right"),
        ReportColumn("final", "Final", 10, kind="number", align="right"),
    ]
    detail_stmt = (_apply_bug_filters(_eager_bug(), filters)
                   .order_by(Bug.id.desc())
                   .limit(_detail_cap()))
    bugs = list(db.scalars(detail_stmt).all())
    attach = _attachments_by_bug(db, [b.id for b in bugs])
    resolved_info = _fetch_resolution_info(db, [b.id for b in bugs])
    detail_rows = [_bug_to_detail_row(b, attach, resolved_info) for b in bugs]
    return ReportResult(
        report_key="project_breakdown",
        report_label="Project Breakdown",
        columns=columns,
        rows=rows,
        summary={"project_count": len(rows),
                 "item_count": sum(r["created"] for r in rows)},
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


def _report_aging(db: Session, filters: Filters) -> ReportResult:
    """Open items sorted oldest first, with an age bucket."""
    types_to_use = filters.item_types or ["Bug", "Requirement", "Task"]
    open_set: set[str] = set()
    for t in types_to_use:
        open_set.update(OPEN_STATUSES_BY_TYPE.get(t, []))
    no_open_match = False
    if filters.statuses:
        open_set &= set(filters.statuses)
        no_open_match = not open_set
    stmt = _apply_bug_filters(_eager_bug(), filters, apply_status=False)
    if open_set:
        stmt = stmt.where(Bug.status.in_(list(open_set)))
    stmt = stmt.order_by(Bug.created_at.asc()).limit(_detail_cap())
    # If the user's status filter excludes every open status, return nothing
    # rather than silently returning resolved/closed items with a bogus age.
    bugs = [] if no_open_match else list(db.scalars(stmt).all())
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


def _utc_date(db: Session, col):
    """``func.date(col)`` normalized to UTC. Postgres truncates timestamptz in the
    session tz (shifts day counts), so coerce explicitly; SQLite is already UTC."""
    try:
        dialect = db.get_bind().dialect.name
    except Exception:  # pragma: no cover - defensive; bind is always present in practice
        dialect = ""
    if dialect == "postgresql":
        return func.date(func.timezone("UTC", col))
    return func.date(col)


def _utc_day_key(value) -> str:
    """ISO UTC date string matching _utc_date's SQL bucketing, so resolved and
    created counts align."""
    if isinstance(value, datetime):
        v = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).date().isoformat()
    return value.isoformat()


def _bucket_resolved_by_day(res_rows) -> dict[str, int]:
    """Fold throughput rows into per-day resolved counts for _report_timeline."""
    resolved_by_day: dict[str, int] = {}
    for (_bug_id, _actor_id, _actor_name, created_at, detail,
         item_type, *_rest) in res_rows:
        it = item_type or "Bug"
        ns = _parse_resolution_status(detail or "")
        if ns and _is_resolved_status(ns, it):
            # Skip resolved→resolved (e.g. Resolved→Closed) to avoid double-counting,
            # same guard _fold_throughput_row uses for the identical purpose.
            old = _parse_prior_status(detail or "")
            if old and _is_resolved_status(old, it):
                continue
            # Bucket on the UTC day to match the created side (_utc_date) and
            # the window keys below; without this, a non-UTC session would bucket
            # created and resolved onto different days.
            key = _utc_day_key(created_at)
            resolved_by_day[key] = resolved_by_day.get(key, 0) + 1
    return resolved_by_day


def _report_timeline(db: Session, filters: Filters) -> ReportResult:
    """Per-day created vs resolved counts; defaults to the last 30 days."""
    today = datetime.now(timezone.utc).date()
    start = filters.date_from or (today - timedelta(days=29))
    end = filters.date_to or today
    # Clamp the span to _MAX_TIMELINE_DAYS.
    if end < start:
        start = end
    if (end - start).days > _MAX_TIMELINE_DAYS:
        start = end - timedelta(days=_MAX_TIMELINE_DAYS)
    # Created side: bugs with created_at in [start, end]. statuses=[] so the
    # created side isn't status-filtered; restrict_project_ids carries through
    # unchanged, or the timeline leaks other projects.
    created_filters = filters.with_overrides(date_from=start, date_to=end, statuses=[])
    created_stmt = _apply_bug_filters(
        select(_utc_date(db, Bug.created_at), func.count(Bug.id)),
        created_filters,
    ).group_by(_utc_date(db, Bug.created_at))
    created_by_day = {str(d): int(c) for d, c in db.execute(created_stmt).all()}
    # Resolved side: reuse the throughput query and bucket by day. statuses is
    # left as-is since _build_throughput_query never reads Filters.statuses.
    res_filters = filters.with_overrides(date_from=start, date_to=end)
    # The 366-day window clamps the date span but not the number of
    # status-change rows within it, so an unbounded query here could stream the
    # entire activity history into memory on a busy instance.
    res_rows = db.execute(
        _build_throughput_query(res_filters).limit(_detail_cap())
    ).all()
    truncated = len(res_rows) >= _detail_cap()
    resolved_by_day = _bucket_resolved_by_day(res_rows)
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
        truncated=truncated,
    )


def _ttr_row(raw, bug_created: Optional[datetime]) -> Optional[dict[str, Any]]:
    """Build one Time-to-Resolution row from a throughput-query tuple.
    Returns None if the row isn't a resolved transition or the creation time is unknown."""
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
    """Compute avg, median, p95, fastest, and slowest from the TTR rows."""
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


def _report_time_to_resolution(db: Session, filters: Filters) -> ReportResult:
    """Per-resolved-item hours from creation to resolution, plus aggregate
    avg / median / p95 across the whole filtered set."""
    rows_raw = db.execute(_build_throughput_query(filters).limit(_detail_cap())).all()
    truncated = len(rows_raw) >= _detail_cap()
    # Load all bug creation times in a single query.
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
        truncated=truncated,
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    # Linear interpolation between adjacent ranks (NIST method).
    k = (len(s) - 1) * (pct / 100)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    d0 = s[f] * (c - k)
    d1 = s[c] * (k - f)
    return d0 + d1


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
    """Raised when the report key is not in REPORT_CATALOG."""


def run_report(key: str, filters: Filters, db: Session) -> ReportResult:
    """Run the named report and return its result. Raises UnknownReportError for unrecognized keys."""
    if key not in _DISPATCH:
        raise UnknownReportError(
            f"Unknown report '{key}'. "
            f"Known: {', '.join(sorted(REPORT_CATALOG.keys()))}"
        )
    return _DISPATCH[key](db, filters)


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
