"""Estimation, capacity, and sprint-planning readiness."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.agile.itemtypes import STANDARD_TYPES
from app.models import Board, Bug, Sprint, SprintCapacity


class PlanningError(ValueError):
    pass


def estimate_field_for_mode(estimation_mode: str) -> str:
    return {
        "story_points": "story_points",
        "time": "remaining_estimate_minutes",
    }.get(estimation_mode, "")


def update_estimate(
    db: Session, item: Bug,
    story_points: float | None, original_minutes: int | None, remaining_minutes: int | None,
) -> Bug:
    from app.agile.hierarchy import LEVEL2_TYPES
    if (item.item_type or "Bug") not in LEVEL2_TYPES:
        raise PlanningError("Only Story/Requirement/Task/Bug items carry estimates")
    if story_points is not None:
        item.story_points = story_points
    if original_minutes is not None:
        item.original_estimate_minutes = original_minutes
    if remaining_minutes is not None:
        item.remaining_estimate_minutes = remaining_minutes
    db.flush()
    return item


def get_capacity(db: Session, sprint_id: int) -> list[SprintCapacity]:
    return list(db.scalars(select(SprintCapacity).where(SprintCapacity.sprint_id == sprint_id)).all())


def replace_capacity(
    db: Session, sprint: Sprint, board: Board, entries: list[dict],
) -> list[SprintCapacity]:
    """Upsert capacity rows for `sprint`; each entry's unit must match the
    board's estimation_mode."""
    if board.estimation_mode == "item_count":
        raise PlanningError("item_count boards do not track per-person capacity")
    expected_unit = "points" if board.estimation_mode == "story_points" else "minutes"
    for e in entries:
        if e["capacity_unit"] != expected_unit:
            raise PlanningError(
                f"capacity_unit must be '{expected_unit}' for this board's estimation_mode"
            )

    existing_by_user = {c.user_id: c for c in get_capacity(db, sprint.id)}
    for e in entries:
        row = existing_by_user.get(e["user_id"])
        if row is None:
            row = SprintCapacity(sprint_id=sprint.id, user_id=e["user_id"], capacity_unit=e["capacity_unit"])
            db.add(row)
        row.capacity_value = e["capacity_value"]
        row.capacity_unit = e["capacity_unit"]
        row.days_off = e.get("days_off", [])
        row.notes = e.get("notes", "")
    db.flush()
    return get_capacity(db, sprint.id)


def readiness_checks(db: Session, sprint: Sprint, board: Board) -> list[dict]:
    """readiness checks; each is {key, passed, message, severity}."""
    checks: list[dict] = []

    checks.append({
        "key": "has_goal", "passed": bool(sprint.goal.strip()),
        "message": "Sprint has a goal" if sprint.goal.strip() else "Sprint has no goal",
        "severity": "warning",
    })

    dates_valid = True
    if sprint.start_date and sprint.end_date:
        dates_valid = sprint.end_date > sprint.start_date
    checks.append({
        "key": "valid_dates", "passed": dates_valid,
        "message": "Dates are valid" if dates_valid else "End date must be after start date",
        "severity": "error",
    })

    # Sprint scope is its standard issues; Sub-tasks ride along with them.
    items = list(db.scalars(
        select(Bug).where(Bug.sprint_id == sprint.id, Bug.item_type.in_(STANDARD_TYPES))
    ).all())
    checks.append({
        "key": "has_items", "passed": len(items) > 0,
        "message": f"{len(items)} item(s) in sprint" if items else "Sprint has no items",
        "severity": "error",
    })

    if board.estimation_mode != "item_count":
        field = estimate_field_for_mode(board.estimation_mode)
        unestimated = [i for i in items if getattr(i, field, None) is None]
        checks.append({
            "key": "all_estimated", "passed": len(unestimated) == 0,
            "message": (
                "All items estimated" if not unestimated
                else f"{len(unestimated)} item(s) unestimated"
            ),
            "severity": "warning",
        })

    same_project = all(i.project_id == sprint.project_id for i in items)
    checks.append({
        "key": "same_project", "passed": same_project,
        "message": "All items belong to the sprint's project" if same_project
        else "Some items belong to a different project",
        "severity": "error",
    })

    # Sub-tasks follow their parent's sprint (app/agile/integrity.py); this
    # verifies it for the sprint at hand.
    parent = aliased(Bug)
    strays = db.scalar(
        select(func.count(Bug.id)).where(
            Bug.sprint_id == sprint.id, Bug.item_type == "Sub-task",
            ~Bug.parent_id.in_(select(parent.id).where(parent.sprint_id == sprint.id)),
        )
    ) or 0
    checks.append({
        "key": "subtasks_with_parent", "passed": strays == 0,
        "message": "No sub-task is separated from its parent's sprint" if not strays
        else f"{strays} sub-task(s) are in this sprint without their parent",
        "severity": "error",
    })

    return checks


def build_planning_summary(db: Session, sprint: Sprint, board: Board) -> dict:
    items = list(db.scalars(
        select(Bug).where(Bug.sprint_id == sprint.id, Bug.item_type.in_(STANDARD_TYPES))
    ).all())
    field = estimate_field_for_mode(board.estimation_mode)
    total_estimate = None
    unestimated_count = 0
    if field:
        values = [getattr(i, field) for i in items]
        unestimated_count = sum(1 for v in values if v is None)
        total_estimate = sum(float(v) for v in values if v is not None) if values else 0

    checks = readiness_checks(db, sprint, board)
    hard_errors = [c for c in checks if c["severity"] == "error" and not c["passed"]]

    capacity = get_capacity(db, sprint.id)
    return {
        "sprint_id": sprint.id,
        "board_id": board.id,
        "estimation_mode": board.estimation_mode,
        "item_count": len(items),
        "total_estimate": total_estimate,
        "unestimated_count": unestimated_count,
        "capacity": capacity,
        "readiness": checks,
        "ready_to_start": len(hard_errors) == 0,
    }
