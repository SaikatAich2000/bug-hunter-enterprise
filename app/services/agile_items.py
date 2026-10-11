"""Agile work-item rules and response shaping."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agile import idempotency as idem_svc
from app.agile import sprints as sprints_svc
from app.models import AcceptanceCriterion, Bug, Sprint, User
from app.schemas import (
    WorkItemTaskFlagsIn,
)


def validate_story_ready_requirements(db: Session, item: Bug) -> bool:
    """Check if a Story has all mandatory fields for Ready for Sprint.
    Returns True if all requirements are met, False otherwise."""
    if not (
        item.title and item.description and item.priority
        and item.story_points is not None and (item.owner_id or item.assignees)
        and item.start_date and item.due_date
    ):
        return False
    # Check for at least one acceptance criterion
    has_criterion = db.scalar(
        select(AcceptanceCriterion.id).where(AcceptanceCriterion.bug_id == item.id).limit(1)
    )
    return has_criterion is not None


def apply_task_flag_updates(item: Bug, payload: WorkItemTaskFlagsIn) -> bool:
    """Apply task flag updates to an item. Returns True if blocked status changed."""
    was_blocked = item.blocked
    if payload.mandatory is not None:
        item.mandatory = payload.mandatory
    if payload.blocked is not None:
        item.blocked = payload.blocked
    if payload.blocked_reason is not None:
        item.blocked_reason = payload.blocked_reason
    if payload.blocked is False:
        item.blocked_reason = ""
    return payload.blocked is not None and payload.blocked != was_blocked


def sprint_item_count(db: Session, sprint_id: int) -> int:
    return sprints_svc.sprint_item_count(db, sprint_id)


def backlog_item_out(item: Bug) -> dict:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "title": item.title,
        "item_type": item.item_type,
        "status": item.status,
        "priority": item.priority,
        "assignees": item.assignees,
        "epic_id": item.epic_id,
        "owner_id": item.owner_id,
        "story_points": float(item.story_points) if item.story_points is not None else None,
        "rank": item.rank,
        "flagged": item.flagged,
        "ready_for_sprint": item.ready_for_sprint,
        "blocked": item.blocked,
        "blocked_reason": item.blocked_reason,
        "mandatory": item.mandatory,
        "start_date": item.start_date,
        "end_date": item.due_date,
        "version": item.version,
    }


def sprint_out(db: Session, sprint: Sprint, item_count: int | None = None) -> dict:
    return {
        "id": sprint.id,
        "display_id": f"SPRINT-{sprint.id}",
        "board_id": sprint.board_id,
        "assignees": [{"id": a.id, "name": a.name, "email": a.email, "role": a.role} for a in sprint.assignees],
        "project_id": sprint.project_id,
        "name": sprint.name,
        "goal": sprint.goal,
        "state": sprint.state,
        "start_date": sprint.start_date,
        "end_date": sprint.end_date,
        "cadence": sprint.cadence,
        "started_at": sprint.started_at,
        "completed_at": sprint.completed_at,
        "cancelled_at": sprint.cancelled_at,
        "sequence_number": sprint.sequence_number,
        "version": sprint.version,
        "created_at": sprint.created_at,
        "updated_at": sprint.updated_at,
        "item_count": sprint_item_count(db, sprint.id) if item_count is None else item_count,
    }


def idempotent_lifecycle(
    db: Session, actor: User, sprint: Sprint, operation_key: str,
    idempotency_key: str | None, payload: dict,
):
    """Returns cached (status, body) tuple to short-circuit, or None to proceed.
    Raises 409 for a conflicting replay."""
    if not idempotency_key:
        return None
    try:
        cached = idem_svc.get_cached_response(
            db, actor.id, sprint.project_id, operation_key, idempotency_key, payload,
        )
    except idem_svc.IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return cached
