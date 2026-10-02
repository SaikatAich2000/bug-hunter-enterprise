"""Workflow transitions, WIP enforcement, and board-view assembly."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Board,
    BoardColumn,
    BoardColumnStatus,
    Bug,
    Sprint,
    SprintItemHistory,
    User,
    WorkflowStatus,
    WorkflowTransition,
)


class TransitionError(ValueError):
    pass


class WipLimitError(ValueError):
    """Raised when a move would exceed a column's WIP limit and enforcement blocks it."""


_WORK_ITEM_TYPE_ALIASES = {
    "bug": "Bug",
    "requirement": "Requirement",
    "task": "Task",
    "story": "Story",
    "epic": "Epic",
    "sub-task": "Sub-task",
}


def canonical_work_item_type(value: str | None) -> str:
    """Normalize an item type's spelling for workflow lookups ("task" ->
    "Task"). Every type, Task and Sub-task included, has its own statuses and
    transitions; stored item types are never rewritten."""
    raw = (value or "Bug").strip()
    return _WORK_ITEM_TYPE_ALIASES.get(raw.lower(), raw)


def get_status_by_value(db: Session, work_item_type: str, persisted_value: str) -> WorkflowStatus | None:
    work_item_type = canonical_work_item_type(work_item_type)
    return db.scalar(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global",
            WorkflowStatus.work_item_type == work_item_type,
            WorkflowStatus.persisted_status_value == persisted_value,
            WorkflowStatus.is_active.is_(True),
        )
    )


def _resolve_target_status(
    db: Session, work_item_type: str, persisted_value: str,
) -> WorkflowStatus | None:
    """Resolve a board status, translating shared column values by category.

    Board columns are category-based and therefore expose statuses from all
    legacy item types. A drag from a Requirement or Task can consequently send
    Bug's ``Closed`` value for the Done column. Resolve that value to the
    current type's active status in the same category instead of rejecting it.
    """
    work_item_type = canonical_work_item_type(work_item_type)
    direct = get_status_by_value(db, work_item_type, persisted_value)
    if direct is not None:
        return direct
    source = db.scalar(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global",
            WorkflowStatus.persisted_status_value == persisted_value,
            WorkflowStatus.is_active.is_(True),
        ).order_by(WorkflowStatus.id)
    )
    if source is None:
        return None
    return db.scalar(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global",
            WorkflowStatus.work_item_type == work_item_type,
            WorkflowStatus.category == source.category,
            WorkflowStatus.is_active.is_(True),
        ).order_by(WorkflowStatus.id)
    )


def allowed_transition_targets(db: Session, work_item_type: str, from_status_id: int) -> list[WorkflowStatus]:
    """Statuses reachable from `from_status_id` via an active WorkflowTransition."""
    work_item_type = canonical_work_item_type(work_item_type)
    rows = db.scalars(
        select(WorkflowTransition).where(
            WorkflowTransition.work_item_type == work_item_type,
            WorkflowTransition.from_status_id == from_status_id,
            WorkflowTransition.is_active.is_(True),
        )
    ).all()
    ids = [r.to_status_id for r in rows]
    if not ids:
        return []
    return list(db.scalars(select(WorkflowStatus).where(WorkflowStatus.id.in_(ids))).all())


def seed_default_transitions(db: Session, work_item_type: str) -> None:
    """Idempotent: allow every status -> every other status for `work_item_type`
    (the default — a permissive baseline; project-specific restricted
    workflows are a later slice). Skipped if any transition already exists."""
    existing = db.scalar(
        select(WorkflowTransition.id).where(WorkflowTransition.work_item_type == work_item_type).limit(1)
    )
    if existing is not None:
        return
    statuses = list(db.scalars(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global",
            WorkflowStatus.work_item_type == work_item_type,
            WorkflowStatus.is_active.is_(True),
        )
    ).all())
    for src in statuses:
        for dst in statuses:
            if src.id == dst.id:
                continue
            db.add(WorkflowTransition(
                work_item_type=work_item_type, from_status_id=src.id, to_status_id=dst.id,
                name=f"{src.name} -> {dst.name}",
            ))
    db.flush()


def reconcile_transitions(db: Session, work_item_type: str) -> bool:
    """Idempotent additive backfill: if new WorkflowStatus rows were added for
    `work_item_type` after seed_default_transitions() already ran (e.g. the
    Testing status added later to Bug/Task/Requirement), those new statuses
    have no WorkflowTransition rows at all — connect them to every other
    active status for the type (permissive baseline, same as the initial
    seed). Returns True if any transition rows were inserted."""
    work_item_type = canonical_work_item_type(work_item_type)
    statuses = list(db.scalars(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global",
            WorkflowStatus.work_item_type == work_item_type,
            WorkflowStatus.is_active.is_(True),
        )
    ).all())
    if len(statuses) < 2:
        return False
    existing_pairs = {
        (r.from_status_id, r.to_status_id)
        for r in db.scalars(
            select(WorkflowTransition).where(WorkflowTransition.work_item_type == work_item_type)
        ).all()
    }
    changed = False
    for src in statuses:
        for dst in statuses:
            if src.id == dst.id:
                continue
            if (src.id, dst.id) in existing_pairs:
                continue
            db.add(WorkflowTransition(
                work_item_type=work_item_type, from_status_id=src.id, to_status_id=dst.id,
                name=f"{src.name} -> {dst.name}",
            ))
            changed = True
    if changed:
        db.flush()
    return changed


def _column_for_status(db: Session, board: Board, status_row: WorkflowStatus) -> BoardColumn | None:
    mapping = db.scalar(
        select(BoardColumnStatus)
        .join(BoardColumn, BoardColumn.id == BoardColumnStatus.board_column_id)
        .where(
            BoardColumnStatus.workflow_status_id == status_row.id,
            BoardColumn.board_id == board.id,
        )
    )
    if mapping is None:
        return None
    return db.get(BoardColumn, mapping.board_column_id)


def _column_card_count(
    db: Session, column: BoardColumn, board: Board, item: Bug | None = None,
) -> int:
    """Cards currently in `column` on the board the moving item is shown on.

    The board displays one sprint at a time, so the WIP count is scoped to the
    moving item's sprint (the cards the user actually sees) and excludes the
    item itself; counting every item in the project, backlog included, made
    the server block moves the board showed as under the limit.
    """
    status_ids = [
        r.workflow_status_id for r in
        db.scalars(select(BoardColumnStatus).where(BoardColumnStatus.board_column_id == column.id)).all()
    ]
    if not status_ids:
        return 0
    values = [
        s.persisted_status_value for s in
        db.scalars(select(WorkflowStatus).where(WorkflowStatus.id.in_(status_ids))).all()
    ]
    stmt = select(func.count(Bug.id)).where(
        Bug.project_id == board.project_id, Bug.status.in_(values),
    )
    if item is not None:
        stmt = stmt.where(Bug.id != item.id)
        stmt = stmt.where(
            Bug.sprint_id == item.sprint_id if item.sprint_id is not None else Bug.sprint_id.is_(None)
        )
    return db.scalar(stmt) or 0


def _validate_transition_allowed(db: Session, item: Bug, to_status_value: str):
    from_status = get_status_by_value(db, item.item_type or "Bug", item.status)
    to_status = _resolve_target_status(db, item.item_type or "Bug", to_status_value)
    if to_status is None:
        raise TransitionError(f"'{to_status_value}' is not a known status for {item.item_type}")
    if from_status is not None:
        targets = allowed_transition_targets(db, item.item_type or "Bug", from_status.id)
        if to_status.id not in {t.id for t in targets} and from_status.id != to_status.id:
            raise TransitionError(
                f"No workflow transition from '{item.status}' to '{to_status_value}' for {item.item_type}"
            )
    return from_status, to_status


def _enforce_wip_limit(
    db: Session, board: Board, to_status, override_wip: bool, override_reason: str, acknowledged: bool,
    item: Bug | None = None,
) -> None:
    target_column = _column_for_status(db, board, to_status)
    if target_column is None or target_column.wip_limit is None:
        return
    current_count = _column_card_count(db, target_column, board, item)
    enforcement = target_column.wip_enforcement or "off"
    if current_count < target_column.wip_limit or enforcement == "off":
        return
    if enforcement == "block" and not override_wip:
        raise WipLimitError(
            f"Column '{target_column.name}' is at its WIP limit ({target_column.wip_limit})"
        )
    if enforcement == "warn" and not acknowledged:
        raise WipLimitError(
            f"Column '{target_column.name}' is at its WIP limit ({target_column.wip_limit}); "
            f"acknowledge to proceed"
        )
    if override_wip and not override_reason.strip():
        raise TransitionError("override_reason is required when overriding a WIP limit")


def _record_sprint_status_history(
    db: Session, item: Bug, actor: User, old_status_row, to_status,
) -> None:
    if item.sprint_id is None:
        return
    sprint = db.get(Sprint, item.sprint_id)
    if sprint is not None and sprint.state == "active":
        db.add(SprintItemHistory(
            sprint_id=sprint.id, work_item_id=item.id, event_type="status_changed",
            old_status_id=old_status_row.id if old_status_row else None,
            new_status_id=to_status.id, actor_id=actor.id,
        ))


def record_status_change(db: Session, item: Bug, actor: User, old_status_value: str | None) -> None:
    """Bookkeeping for a status change made outside the board transition.

    The item form, bulk actions and Sleuth set ``bugs.status`` directly; this
    keeps them consistent with a board drag: a terminal status stamps
    ``resolved_at`` and an item in an active sprint gets a ``status_changed``
    history row, which the burndown/flow/control-chart reports read.
    """
    if item.status == old_status_value:
        return
    item_type = canonical_work_item_type(item.item_type)
    new_row = get_status_by_value(db, item_type, item.status)
    if new_row is None:
        return
    old_row = get_status_by_value(db, item_type, old_status_value) if old_status_value else None
    if new_row.is_terminal and item.resolved_at is None:
        item.resolved_at = datetime.now(timezone.utc).replace(microsecond=0)
    _record_sprint_status_history(db, item, actor, old_row, new_row)


def transition_work_item(
    db: Session, item: Bug, board: Board, actor: User,
    to_status_value: str, override_wip: bool, override_reason: str, acknowledged: bool,
) -> Bug:
    """Validate and apply a status transition using the board's column mapping
    and WIP enforcement."""
    item_type = canonical_work_item_type(item.item_type)
    # A long-running deployment may have seeded statuses before Blocked or
    # its permissive transition rows were introduced. Repair that state at
    # request time so the first live drag does not depend on a process restart.
    reconcile_transitions(db, item_type)
    from_status, to_status = _validate_transition_allowed(db, item, to_status_value)
    _enforce_wip_limit(db, board, to_status, override_wip, override_reason, acknowledged, item)

    old_status_row = from_status
    item.status = to_status.persisted_status_value
    if to_status.is_terminal:
        item.resolved_at = item.resolved_at or datetime.now(timezone.utc).replace(microsecond=0)

    _record_sprint_status_history(db, item, actor, old_status_row, to_status)
    db.flush()
    return item
