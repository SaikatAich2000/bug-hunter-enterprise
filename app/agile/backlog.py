"""Backlog ranking.

Ranking is server-managed: clients submit `before_id`/`after_id` positioning
hints only (never raw rank tokens) and the server computes the rank token
(app.agile.ranking, placed by app.agile.sprints._place).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agile.hierarchy import LEVEL2_TYPES
from app.models import Bug


class BacklogError(ValueError):
    pass


def backlog_scope(project_id: int) -> str:
    return f"backlog:{project_id}"


def sprint_scope(sprint_id: int) -> str:
    return f"sprint:{sprint_id}"


def list_backlog(db: Session, project_id: int, limit: int = 500, offset: int = 0) -> list[Bug]:
    """Jira's backlog for ``project_id``, in rank order: standard issues in no
    sprint whose status is not in the board's right-most (done) column."""
    from app.agile.boards import done_status_values, get_default_board

    board = get_default_board(db, project_id)
    done = done_status_values(db, board) if board is not None else set()
    stmt = select(Bug).where(
        Bug.project_id == project_id,
        Bug.sprint_id.is_(None),
        Bug.item_type.in_(LEVEL2_TYPES),
    )
    if done:
        stmt = stmt.where(Bug.status.not_in(done))
    stmt = stmt.order_by(Bug.rank.is_(None), Bug.rank, Bug.id).limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


def rank_item(
    db: Session, project_id: int, item_id: int,
    before_id: int | None, after_id: int | None,
) -> Bug:
    """Move ``item_id`` within its current list, right after ``before_id``
    and/or right before ``after_id``, or to the top when neither is given
    (the same placement the board's move endpoint uses)."""
    from app.agile.sprints import SprintLifecycleError, _place

    item = db.get(Bug, item_id)
    if item is None or item.project_id != project_id:
        raise BacklogError(f"Item #{item_id} not found in this project")
    if item.item_type not in LEVEL2_TYPES:
        raise BacklogError("Only Story/Requirement/Task/Bug items can be ranked")
    if item.sprint_id is not None:
        from app.models import Sprint

        sprint = db.get(Sprint, item.sprint_id)
        if sprint is not None and sprint.state in ("closed", "cancelled"):
            raise BacklogError("Issues in a closed sprint keep their order")
    scope = item.rank_scope or backlog_scope(project_id)
    try:
        # Neither neighbour given: top of the list (Jira's "Top of backlog").
        _place(db, [item], scope, project_id, before_id, after_id, "top")
    except SprintLifecycleError as exc:
        raise BacklogError(str(exc)) from exc
    db.flush()
    return item
