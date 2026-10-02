"""Stats / analytics API: KPI strip plus all Analytics breakdowns.

"Not a Bug" items are excluded from the bugs total but kept for audit history.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, scope_bug_query, scope_event_query
from app.api_docs import BAD_REQUEST_400
from app.auth import get_current_user
from app.database import get_db
from app.models import Bug, Event, Project, User, bug_assignees
from app.reports.engine import (
    OPEN_STATUSES_BY_TYPE,
    RESOLVED_STATUSES_BY_TYPE,
    _utc_date,
)
from app.schemas import EXCLUDED_FROM_TOTAL_STATUSES, StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])

_VALID_TYPES = {"Bug", "Requirement", "Task"}


def _by_project_stmt(item_type: Optional[str], status_filter: list[str], accessible: set[int]):
    """Bugs-per-project, scoped to item_type/status.
    Predicates live on the OUTER join condition so zero-count projects survive."""
    join_cond = Bug.project_id == Project.id
    if item_type is not None:
        join_cond &= Bug.item_type == item_type
    if status_filter:
        join_cond &= Bug.status.in_(status_filter)
    stmt = (
        select(Project.id, Project.name, Project.color, func.count(Bug.id))
        .outerjoin(Bug, join_cond)
        .group_by(Project.id, Project.name, Project.color)
        .order_by(func.count(Bug.id).desc())
    )
    return stmt.where(Project.id.in_(accessible))


def _by_assignee_stmt(
    item_type: Optional[str], status_filter: list[str], accessible: set[int], org_id: int,
):
    """Top 10 assignees by item count within the organization's visible projects."""
    stmt = (
        select(User.id, User.name, User.email, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
        .join(Bug, Bug.id == bug_assignees.c.bug_id)
        .where(User.org_id == org_id)
    )
    if item_type is not None:
        stmt = stmt.where(Bug.item_type == item_type)
    if status_filter:
        stmt = stmt.where(Bug.status.in_(status_filter))
    stmt = scope_bug_query(stmt, accessible)
    return (
        stmt.group_by(User.id, User.name, User.email)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(10)
    )


def _timeline_14d(db: Session, scoped_f):
    """Return per-day created counts for the last 14 days as a dense, zero-filled list."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=13)
    # raw-column comparison keeps the created_at index usable
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    # _utc_date forces UTC bucketing — Postgres func.date() uses session tz
    day_col = _utc_date(db, Bug.created_at)
    rows = db.execute(
        scoped_f(
            select(day_col, func.count(Bug.id))
            .where(Bug.created_at >= start_dt)
            .group_by(day_col)
        )
    ).all()
    counts_by_day: dict[str, int] = {}
    for raw_day, count in rows:
        key = raw_day if isinstance(raw_day, str) else raw_day.isoformat()
        counts_by_day[key] = int(count)
    return [
        {"date": (start + timedelta(days=i)).isoformat(),
         "count": counts_by_day.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(14)
    ]


def _derive_kpis(
    by_status_global: dict[str, int], item_type: Optional[str],
) -> tuple[int, int, int, int]:
    """Headline KPI counts (open, resolved, closed, resolve_later).
    Each type has its own open/resolved set; Closed and Resolve Later are Bug-only."""
    if item_type and item_type != "Bug":
        open_count = sum(
            by_status_global.get(s, 0) for s in OPEN_STATUSES_BY_TYPE.get(item_type, [])
        )
        resolved_count = sum(
            by_status_global.get(s, 0) for s in RESOLVED_STATUSES_BY_TYPE.get(item_type, [])
        )
        return open_count, resolved_count, 0, 0
    open_count = sum(
        by_status_global.get(s, 0) for s in ("New", "In Progress", "Reopened")
    )
    return (
        open_count,
        by_status_global.get("Resolved", 0),
        by_status_global.get("Closed", 0),
        by_status_global.get("Resolve Later", 0),
    )


@router.get("", responses=BAD_REQUEST_400)
def stats(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    item_type: Optional[str] = Query(
        default=None,
        description=(
            "Scope every aggregation (KPIs, status/priority/env/project/"
            "assignee breakdowns and the 14-day timeline) to a single item "
            "type. Omit to get global stats. by_type and the event count "
            "stay global so the tab-count badges always match reality."
        ),
    ),
    status: Optional[list[str]] = Query(
        default=None,
        description=(
            "Scope the CHART breakdowns (by_status/priority/env/project/"
            "assignee + timeline) to one or more statuses — this is what makes "
            "the Analytics view react to a KPI tile click. The headline KPI "
            "counts (bugs/open/resolved/closed/resolve_later) stay GLOBAL so "
            "the strip still shows real totals and every tile stays toggleable."
        ),
    ),
) -> StatsOut:
    if item_type is not None and item_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"item_type must be one of {sorted(_VALID_TYPES)}",
        )

    # empty list means no chart scoping
    status_filter = [s for s in (status or []) if s]

    accessible = accessible_project_ids(db, user)

    # item_type + project scope (KPIs and by_status)
    def _scoped(stmt):
        stmt = scope_bug_query(stmt, accessible)
        if item_type is not None:
            return stmt.where(Bug.item_type == item_type)
        return stmt

    # _scoped plus the status filter (chart breakdowns and timeline)
    def _scoped_f(stmt):
        stmt = _scoped(stmt)
        if status_filter:
            stmt = stmt.where(Bug.status.in_(status_filter))
        return stmt

    # no status filter here — headline KPIs stay global so tiles remain toggleable
    by_status_global: dict[str, int] = {}
    for status_name, cnt in db.execute(
        _scoped(select(Bug.status, func.count(Bug.id)).group_by(Bug.status))
    ).all():
        by_status_global[status_name] = int(cnt)

    # total excludes non-bug statuses like "Not a Bug"
    bug_count = sum(
        c for s, c in by_status_global.items() if s not in EXCLUDED_FROM_TOTAL_STATUSES
    )
    open_count, resolved_count, closed_count, resolve_later_count = _derive_kpis(
        by_status_global, item_type,
    )

    # re-query only when a status filter is active; otherwise reuse the KPI result
    if status_filter:
        by_status = {
            s: int(c)
            for s, c in db.execute(
                _scoped_f(select(Bug.status, func.count(Bug.id)).group_by(Bug.status))
            ).all()
        }
    else:
        by_status = by_status_global

    # str()/int() casts guard against NULL keys the response model would reject
    by_priority = {
        str(p): int(c)
        for p, c in db.execute(
            _scoped_f(select(Bug.priority, func.count(Bug.id)).group_by(Bug.priority))
        ).all()
    }
    by_environment = {
        str(e): int(c)
        for e, c in db.execute(
            _scoped_f(select(Bug.environment, func.count(Bug.id)).group_by(Bug.environment))
        ).all()
    }
    # by_type drives the tab badges; project-scoped for restricted actors
    by_type = {
        str(t): int(c)
        for t, c in db.execute(
            scope_bug_query(
                select(Bug.item_type, func.count(Bug.id)).group_by(Bug.item_type),
                accessible,
            )
        ).all()
    }
    # event badge scoped to match the Events view
    events_count = db.scalar(
        scope_event_query(select(func.count(Event.id)), user, accessible)
    ) or 0
    by_type["Event"] = int(events_count)

    by_project_rows = db.execute(
        _by_project_stmt(item_type, status_filter, accessible)
    ).all()
    by_assignee_rows = db.execute(
        _by_assignee_stmt(item_type, status_filter, accessible, user.org_id)
    ).all()

    timeline = _timeline_14d(db, _scoped_f)

    return StatsOut(
        bugs=bug_count,
        open=open_count,
        resolved=resolved_count,
        closed=closed_count,
        resolve_later=resolve_later_count,
        by_status=by_status,
        by_priority=by_priority,
        by_environment=by_environment,
        by_type=by_type,
        by_project=[{"id": pid, "name": name, "color": color, "count": int(cnt)}
                    for pid, name, color, cnt in by_project_rows],
        by_assignee=[{"id": uid, "name": name, "email": email, "count": int(cnt)}
                     for uid, name, email, cnt in by_assignee_rows],
        timeline=timeline,
    )
