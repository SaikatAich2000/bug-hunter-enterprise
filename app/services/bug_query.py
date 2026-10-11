"""Work-item list filtering."""

from __future__ import annotations

from typing import Optional

from fastapi import (
    HTTPException,
)
from sqlalchemy import func, or_

from app.models import (
    Bug,
    User,
)
from app.schemas import (
    normalize_choice,
)
from app.util import MAX_PK_INT, like_escape


def normalize_choice_list(values: Optional[list[str]], allowed: list[str], label: str) -> list[str]:
    """Normalize a multi-valued enum query param; strip empties, reject unknowns with 400."""
    if not values:
        return []
    out: list[str] = []
    for v in values:
        if v is None or v == "":
            continue
        try:
            out.append(normalize_choice(v, allowed, label))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return out


def apply_where_both(stmt, count_stmt, clause):
    return stmt.where(clause), count_stmt.where(clause)


def apply_q_filter(stmt, count_stmt, q: str):
    """Bug-id-or-text search: #123 or 123 does an exact id match, otherwise LIKE."""
    q_clean = q.strip().lstrip("#")
    if q_clean.isascii() and q_clean.isdigit() and int(q_clean) <= MAX_PK_INT:
        return apply_where_both(stmt, count_stmt, Bug.id == int(q_clean))
    if not q_clean:
        return stmt, count_stmt
    # Use the cleaned string for the LIKE pattern so leading/trailing spaces in
    # the raw query (e.g. "?q=  needle  ") don't prevent any match.
    like = f"%{like_escape(q_clean.lower())}%"
    clause = or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    )
    return apply_where_both(stmt, count_stmt, clause)


def reject_overflow_ids(**named_ids) -> None:
    """Reject id-valued query params that would overflow Postgres int4.

    Without this, ?reporter_id=99999999999999 reaches the DB and raises a
    DataError (500). SQLite tolerates oversized ints, so this only matters
    on Postgres. Returns a clean 422 instead.
    """
    for name, value in named_ids.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            # Small negatives like -1 are in range and simply match nothing;
            # reject only values truly outside int4.
            if v is not None and (v > MAX_PK_INT or v < -MAX_PK_INT - 1):
                raise HTTPException(status_code=422, detail=f"{name} is out of range")


def apply_event_filter(stmt, count_stmt, event_id: int):
    """event_id=0 means "no event"; any other value filters by that event id."""
    if event_id == 0:
        return apply_where_both(stmt, count_stmt, Bug.event_id.is_(None))
    return apply_where_both(stmt, count_stmt, Bug.event_id == event_id)


def apply_list_filters(stmt, count_stmt, *, statuses, priorities, environments,
                        item_types, project_ids, assignee_ids, reporter_id,
                        due_date, event_id, q):
    """Apply all list_bugs filters to the select+count statement pair."""
    if project_ids:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.project_id.in_(project_ids))
    if statuses:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.status.in_(statuses))
    if priorities:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.priority.in_(priorities))
    if environments:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.environment.in_(environments))
    if item_types:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.item_type.in_(item_types))
    if reporter_id is not None:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = apply_where_both(
            stmt, count_stmt, Bug.assignees.any(User.id.in_(assignee_ids)),
        )
    if due_date:
        stmt, count_stmt = apply_where_both(stmt, count_stmt, Bug.due_date == due_date)
    if event_id is not None:
        stmt, count_stmt = apply_event_filter(stmt, count_stmt, event_id)
    if q:
        stmt, count_stmt = apply_q_filter(stmt, count_stmt, q)
    return stmt, count_stmt
