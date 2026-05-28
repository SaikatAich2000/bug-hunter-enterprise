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

    # by_type counts (always GLOBAL within the org) — computed first so we
    # can return them even in the zero-state path below.
    if pids:
        by_type = dict(db.execute(
            select(Bug.item_type, func.count(Bug.id))
            .where(Bug.project_id.in_(pids))
            .group_by(Bug.item_type)
        ).all())
    else:
        by_type = {}
    # Event count is org-scoped; events don't live in `bugs` so the
    # type-filter doesn't apply to them.
    events_count = db.scalar(
        select(func.count(Event.id)).where(Event.org_id == user.org_id)
    ) or 0
    by_type["Event"] = int(events_count)

    if not pids:
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

    # Build the base WHERE clause. When item_type is supplied every
    # Bug-side aggregation also filters on Bug.item_type.
    scoped = Bug.project_id.in_(pids)
    if item_type is not None:
        scoped = scoped & (Bug.item_type == item_type)

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

    project_count = len(pids)
    user_count = db.scalar(
        select(func.count(User.id)).where(User.org_id == user.org_id)
    ) or 0

    by_status = dict(db.execute(
        select(Bug.status, func.count(Bug.id)).where(scoped).group_by(Bug.status)
    ).all())
    by_priority = dict(db.execute(
        select(Bug.priority, func.count(Bug.id)).where(scoped).group_by(Bug.priority)
    ).all())
    by_environment = dict(db.execute(
        select(Bug.environment, func.count(Bug.id)).where(scoped).group_by(Bug.environment)
    ).all())

    # by_project — the outer join keeps zero-bug projects in the result.
    # When filtering by item_type we move the type filter onto the JOIN
    # so the project row still shows up with count 0.
    if item_type is None:
        by_project_stmt = (
            select(Project.id, Project.name, Project.color, func.count(Bug.id))
            .outerjoin(Bug, Bug.project_id == Project.id)
            .where(Project.id.in_(pids))
            .group_by(Project.id, Project.name, Project.color)
            .order_by(func.count(Bug.id).desc())
        )
    else:
        by_project_stmt = (
            select(Project.id, Project.name, Project.color, func.count(Bug.id))
            .outerjoin(
                Bug,
                (Bug.project_id == Project.id) & (Bug.item_type == item_type),
            )
            .where(Project.id.in_(pids))
            .group_by(Project.id, Project.name, Project.color)
            .order_by(func.count(Bug.id).desc())
        )
    by_project_rows = db.execute(by_project_stmt).all()

    by_assignee_stmt = (
        select(User.id, User.name, User.email, func.count(bug_assignees.c.bug_id))
        .join(bug_assignees, bug_assignees.c.user_id == User.id)
        .join(Bug, Bug.id == bug_assignees.c.bug_id)
        .where(Bug.project_id.in_(pids), User.org_id == user.org_id)
        .group_by(User.id, User.name, User.email)
        .order_by(func.count(bug_assignees.c.bug_id).desc())
        .limit(10)
    )
    if item_type is not None:
        by_assignee_stmt = by_assignee_stmt.where(Bug.item_type == item_type)
    by_assignee_rows = db.execute(by_assignee_stmt).all()

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
    timeline = [
        {"date": (start + timedelta(days=i)).isoformat(),
         "count": counts_by_day.get((start + timedelta(days=i)).isoformat(), 0)}
        for i in range(14)
    ]

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
