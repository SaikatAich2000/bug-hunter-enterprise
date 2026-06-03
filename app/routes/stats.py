"""Stats — scoped to the caller's org and accessible projects.

v2.4: accepts an optional `?item_type=Bug|Requirement|Task` query
param. When set, every Bug-touching aggregation (KPIs, status /
priority / env / project / assignee / timeline) is filtered to that
type. `by_type` and the event count are always GLOBAL so the SPA's
tab-count badges keep showing reality regardless of which tab is
active.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import accessible_project_ids, get_current_user
from app.database import get_db
from app.models import Bug, Event, Project, User, bug_assignees
from app.schemas import ALLOWED_ITEM_TYPES, EXCLUDED_FROM_TOTAL_STATUSES, StatsOut

router = APIRouter(prefix="/api/stats", tags=["stats"])

_VALID_TYPES = set(ALLOWED_ITEM_TYPES)


def _compute_by_type(db: Session, user: User, pids: list[int]) -> dict:
    """by_type counts are always GLOBAL within the org. Computed first
    so we can return them even when the user has no accessible projects."""
    if pids:
        by_type = dict(db.execute(
            select(Bug.item_type, func.count(Bug.id))
            .where(Bug.project_id.in_(pids))
            .group_by(Bug.item_type)
        ).all())
    else:
        by_type = {}
    events_count = db.scalar(
        select(func.count(Event.id)).where(Event.org_id == user.org_id)
    ) or 0
    by_type["Event"] = int(events_count)
    return by_type


def _empty_stats(by_type: dict) -> StatsOut:
    """Zero-state response when the caller has no accessible projects.
    by_type and the event count are still returned so tab badges work."""
    return StatsOut(
        bugs=0, open=0, resolved=0, closed=0, resolve_later=0,
        projects=0, users=0,
        by_status={}, by_priority={}, by_environment={},
        by_type=by_type,
        by_project=[], by_assignee=[],
        timeline=[
            {"date": (datetime.now(timezone.utc).date() - timedelta(days=13 - i)).isoformat(),
             "count": 0}
            for i in range(14)
        ],
    )


def _build_by_project_stmt(pids: list[int], item_type: Optional[str]):
    """Outer-join Bug onto Project so zero-bug projects still show up.
    When filtering by item_type the type filter rides the JOIN."""
    base = (
        select(Project.id, Project.name, Project.color, func.count(Bug.id))
        .where(Project.id.in_(pids))
        .group_by(Project.id, Project.name, Project.color)
        .order_by(func.count(Bug.id).desc())
    )
    if item_type is None:
        return base.outerjoin(Bug, Bug.project_id == Project.id)
    return base.outerjoin(
        Bug, (Bug.project_id == Project.id) & (Bug.item_type == item_type),
    )


def _build_by_assignee_stmt(db_user: User, pids: list[int], item_type: Optional[str]):
    """Top-10 assignees by bug count, scoped to the caller's org."""
    stmt = (
        select(User.id, User.name, User.email, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
        .join(Bug, Bug.id == bug_assignees.c.bug_id)
        .where(Bug.project_id.in_(pids), User.org_id == db_user.org_id)
        .group_by(User.id, User.name, User.email)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(10)
    )
    if item_type is not None:
        stmt = stmt.where(Bug.item_type == item_type)
    return stmt


def _build_timeline(db: Session, scoped) -> list[dict]:
    """14-day rolling timeline of created_at counts ending today (UTC)."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=13)
    timeline_rows = db.execute(
        select(func.date(Bug.created_at), func.count(Bug.id))
        .where(scoped, func.date(Bug.created_at) >= start)
        .group_by(func.date(Bug.created_at))
    ).all()
    counts_by_day: dict[str, int] = {}
    for raw_day, cnt in timeline_rows:
        key = raw_day if isinstance(raw_day, str) else raw_day.isoformat()
        counts_by_day[key] = int(cnt)
    return [
        {"date": (start + timedelta(days=i)).isoformat(),
         "count": counts_by_day.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(14)
    ]


def _compute_kpis(db: Session, scoped) -> tuple[int, int, int, int, int]:
    """The five top-of-page KPI counts: total, open, resolved, closed,
    resolve_later."""
    bug_count = db.scalar(
        select(func.count(Bug.id)).where(scoped, Bug.status.notin_(EXCLUDED_FROM_TOTAL_STATUSES))
    ) or 0
    open_count = db.scalar(
        select(func.count(Bug.id)).where(scoped, Bug.status.in_(("New", "In Progress", "Reopened")))
    ) or 0
    resolved_count = db.scalar(
        select(func.count(Bug.id)).where(scoped, Bug.status == "Resolved")
    ) or 0
    closed_count = db.scalar(
        select(func.count(Bug.id)).where(scoped, Bug.status == "Closed")
    ) or 0
    resolve_later_count = db.scalar(
        select(func.count(Bug.id)).where(scoped, Bug.status == "Resolve Later")
    ) or 0
    return bug_count, open_count, resolved_count, closed_count, resolve_later_count


def _compute_breakdowns(db: Session, scoped) -> tuple[dict, dict, dict]:
    """The three single-column GROUP BY breakdowns: status, priority,
    environment."""
    by_status = dict(db.execute(
        select(Bug.status, func.count(Bug.id)).where(scoped).group_by(Bug.status)
    ).all())
    by_priority = dict(db.execute(
        select(Bug.priority, func.count(Bug.id)).where(scoped).group_by(Bug.priority)
    ).all())
    by_environment = dict(db.execute(
        select(Bug.environment, func.count(Bug.id)).where(scoped).group_by(Bug.environment)
    ).all())
    return by_status, by_priority, by_environment


@router.get("", response_model=StatsOut)
def stats(
    item_type: Optional[str] = Query(
        default=None,
        description=(
            "Scope every aggregation (KPIs, status/priority/env/project/"
            "assignee breakdowns and the 14-day timeline) to a single "
            "item type. Omit for global stats. by_type and event count "
            "stay global so tab badges still match reality."
        ),
    ),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatsOut:
    if item_type is not None and item_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"item_type must be one of {sorted(_VALID_TYPES)}",
        )

    pids = accessible_project_ids(db, user)
    by_type = _compute_by_type(db, user, pids)

    if not pids:
        return _empty_stats(by_type)

    # Base WHERE clause. When item_type is supplied every Bug-side
    # aggregation also filters on Bug.item_type.
    scoped = Bug.project_id.in_(pids)
    if item_type is not None:
        scoped = scoped & (Bug.item_type == item_type)

    bug_count, open_count, resolved_count, closed_count, resolve_later_count = (
        _compute_kpis(db, scoped)
    )

    project_count = len(pids)
    user_count = db.scalar(
        select(func.count(User.id)).where(User.org_id == user.org_id)
    ) or 0

    by_status, by_priority, by_environment = _compute_breakdowns(db, scoped)
    by_project_rows = db.execute(_build_by_project_stmt(pids, item_type)).all()
    by_assignee_rows = db.execute(_build_by_assignee_stmt(user, pids, item_type)).all()
    timeline = _build_timeline(db, scoped)

    return StatsOut(
        bugs=bug_count,
        open=open_count,
        resolved=resolved_count,
        closed=closed_count,
        resolve_later=resolve_later_count,
        projects=project_count,
        users=user_count,
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
