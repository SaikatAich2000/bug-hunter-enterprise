"""Board retrieval, project Agile activation, and board-settings mutation."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agile.ranking import spaced_ranks
from app.models import (
    Board,
    BoardColumn,
    BoardColumnStatus,
    Bug,
    Project,
    Sprint,
    User,
    WorkflowStatus,
)

DEFAULT_BOARD_NAME = "Sprint Board"
_DEFAULT_COLUMNS = (
    ("To Do", "todo"),
    ("In Progress", "in_progress"),
    ("Testing", "testing"),
    ("Done", "done"),
)


class AgileActivationError(ValueError):
    """Raised when activation cannot proceed (e.g. unmapped legacy statuses)."""


def normalize_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def get_default_board(db: Session, project_id: int) -> Board | None:
    return db.scalar(
        select(Board).where(Board.project_id == project_id, Board.is_default.is_(True))
    )


def get_board_or_none(db: Session, board_id: int) -> Board | None:
    return db.get(Board, board_id)


def activate_agile_for_project(
    db: Session, project: Project, actor: User, feature_flags: dict,
) -> Board:
    """Idempotently enable Agile for `project`: create the default board,
    columns, global status→column mappings, and backfill rank for previously
    unranked items.

    Safe to call twice: if a default board already exists, it is returned
    unchanged (feature flags are still refreshed).
    """
    existing = get_default_board(db, project.id)
    if existing is not None:
        project.agile_enabled = True
        project.agile_feature_flags = feature_flags
        db.flush()
        return existing

    board = Board(
        project_id=project.id,
        name=DEFAULT_BOARD_NAME,
        name_normalized=normalize_name(DEFAULT_BOARD_NAME),
        board_type="scrum",
        is_default=True,
        estimation_mode="story_points",
        created_by_id=actor.id,
    )
    db.add(board)
    try:
        db.flush()
    except IntegrityError:
        # Lost a concurrent activation race; the winner's board is now
        # visible — return it (idempotent success).
        db.rollback()
        winner = get_default_board(db, project.id)
        if winner is not None:
            return winner
        raise

    columns_by_category: dict[str, BoardColumn] = {}
    for position, (name, category) in enumerate(_DEFAULT_COLUMNS):
        column = BoardColumn(board_id=board.id, name=name, position=position, category=category)
        db.add(column)
        db.flush()
        columns_by_category[category] = column

    global_statuses = db.scalars(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global", WorkflowStatus.is_active.is_(True),
        )
    ).all()
    unmapped = [s for s in global_statuses if s.category not in columns_by_category]
    if unmapped:
        raise AgileActivationError(
            "Unmapped workflow statuses (no default column for category): "
            + ", ".join(f"{s.work_item_type}/{s.key}" for s in unmapped)
        )
    for wf_status in global_statuses:
        column = columns_by_category[wf_status.category]
        db.add(BoardColumnStatus(board_column_id=column.id, workflow_status_id=wf_status.id))

    from app.agile.workflow import seed_default_transitions
    for work_item_type in {s.work_item_type for s in global_statuses}:
        seed_default_transitions(db, work_item_type)

    _backfill_ranks(db, project.id)

    project.agile_enabled = True
    project.agile_feature_flags = feature_flags
    db.flush()
    return board


def _backfill_ranks(db: Session, project_id: int) -> None:
    """Rank the project's unranked standard issues and Epics in creation
    order (id tie-break), each in the list it belongs to. Lists that already
    hold ranked issues are left to the flush-time rule, which appends."""
    from app.agile.integrity import expected_rank_scope
    from app.agile.itemtypes import EPIC, STANDARD_TYPES

    unranked = db.scalars(
        select(Bug)
        .where(Bug.project_id == project_id, Bug.rank.is_(None), Bug.item_type.in_([*STANDARD_TYPES, EPIC]))
        .order_by(Bug.created_at, Bug.id)
    ).all()
    by_scope: dict[str, list[Bug]] = {}
    for item in unranked:
        by_scope.setdefault(expected_rank_scope(item), []).append(item)
    for scope, items in by_scope.items():
        if db.scalar(select(Bug.id).where(Bug.rank_scope == scope, Bug.rank.is_not(None)).limit(1)):
            continue
        for item, token in zip(items, spaced_ranks(len(items))):
            item.rank_scope = scope
            item.rank = token
    db.flush()


def has_active_sprint(db: Session, project_id: int) -> bool:
    return db.scalar(
        select(Sprint.id).where(Sprint.project_id == project_id, Sprint.state == "active").limit(1)
    ) is not None


def reconcile_testing_column(db: Session, board: Board) -> bool:
    """Idempotent additive backfill: boards activated before the
    Testing column existed only have todo/in_progress/done. Insert a Testing
    column right after In Progress and map the global Testing WorkflowStatus
    rows into it, without touching any existing column/status/card data.
    Also maps any newly-seeded Testing statuses (e.g. Bug/Task/Requirement
    gaining one later) into an already-present Testing column.
    Returns True if a change was made (caller should flush/commit).
    """
    testing_statuses = list(db.scalars(
        select(WorkflowStatus).where(
            WorkflowStatus.scope_key == "global", WorkflowStatus.category == "testing",
            WorkflowStatus.is_active.is_(True),
        )
    ).all())

    existing = next((c for c in board.columns if c.category == "testing"), None)
    if existing is not None:
        mapped_ids = {s.workflow_status_id for s in existing.statuses}
        missing = [s for s in testing_statuses if s.id not in mapped_ids]
        if not missing:
            return False
        for wf_status in missing:
            db.add(BoardColumnStatus(board_column_id=existing.id, workflow_status_id=wf_status.id))
        db.flush()
        return True

    in_progress_positions = [c.position for c in board.columns if c.category == "in_progress"]
    insert_at = (max(in_progress_positions) + 1) if in_progress_positions else len(board.columns)
    for col in board.columns:
        if col.position >= insert_at:
            col.position += 1
    testing_column = BoardColumn(
        board_id=board.id, name="Testing", position=insert_at, category="testing",
    )
    db.add(testing_column)
    db.flush()
    for wf_status in testing_statuses:
        db.add(BoardColumnStatus(board_column_id=testing_column.id, workflow_status_id=wf_status.id))
    db.flush()
    return True


def reconcile_board_status_mappings(db: Session, board: Board) -> bool:
    """Map every active global status to the board column for its category."""
    columns_by_category = {column.category: column for column in board.columns}
    statuses = db.scalars(select(WorkflowStatus).where(
        WorkflowStatus.scope_key == "global",
        WorkflowStatus.is_active.is_(True),
    )).all()
    existing = {
        (mapping.board_column_id, mapping.workflow_status_id)
        for column in board.columns
        for mapping in column.statuses
    }
    changed = False
    for wf_status in statuses:
        column = columns_by_category.get(wf_status.category)
        if column is None or (column.id, wf_status.id) in existing:
            continue
        db.add(BoardColumnStatus(board_column_id=column.id, workflow_status_id=wf_status.id))
        existing.add((column.id, wf_status.id))
        changed = True
    if changed:
        db.flush()
    return changed


def ordered_columns(board: Board) -> list[BoardColumn]:
    return sorted(board.columns, key=lambda c: (c.position, c.id))


def column_status_values(column: BoardColumn) -> set[str]:
    """Status values (bugs.status) mapped into ``column``, across all item types."""
    return {
        mapping.workflow_status.persisted_status_value
        for mapping in column.statuses
        if mapping.workflow_status is not None
    }


def status_to_column(board: Board) -> dict[str, BoardColumn]:
    """status value -> the column it is shown in (first column wins, as the
    board configuration keeps each status in at most one column)."""
    out: dict[str, BoardColumn] = {}
    for column in ordered_columns(board):
        for value in column_status_values(column):
            out.setdefault(value, column)
    return out


def done_status_values(db: Session, board: Board) -> set[str]:
    """Statuses that count as done: those in the board's right-most column
    (Jira's rule for completing a sprint and for every sprint report). A board
    whose last column has no status falls back to the terminal statuses."""
    columns = ordered_columns(board)
    if columns:
        values = column_status_values(columns[-1])
        if values:
            return values
    return set(db.scalars(
        select(WorkflowStatus.persisted_status_value).where(
            WorkflowStatus.scope_key == "global", WorkflowStatus.category == "done",
            WorkflowStatus.is_active.is_(True),
        )
    ).all())


def disable_agile_for_project(db: Session, project: Project) -> None:
    """Disable Agile without deleting any board/sprint/rank/history data. Caller must check has_active_sprint() first (409 if true)."""
    project.agile_enabled = False
    db.flush()


def serialize_board(board: Board) -> dict:
    """Build a BoardOut-shaped dict, resolving each column's mapped status
    `persisted_status_value` list (not directly expressible via from_attributes).
    De-duplicated: several work-item types can share the same persisted value
    (e.g. every type's initial status is "New"), which would otherwise repeat."""
    columns = []
    for col in sorted(board.columns, key=lambda c: c.position):
        mapped_statuses = [
            bcs.workflow_status.persisted_status_value
            for bcs in col.statuses
            if bcs.workflow_status is not None
        ]
        from app.agile.views import ordered_statuses

        statuses = ordered_statuses(dict.fromkeys(mapped_statuses))
        statuses_by_type = {}
        for bcs in col.statuses:
            wf_status = bcs.workflow_status
            if wf_status is not None:
                statuses_by_type.setdefault(wf_status.work_item_type, []).append(
                    wf_status.persisted_status_value
                )
        columns.append({
            "id": col.id,
            "name": col.name,
            "position": col.position,
            "category": col.category,
            "wip_limit": col.wip_limit,
            "min_cards": col.min_cards,
            "wip_enforcement": col.wip_enforcement,
            "color": col.color,
            "statuses": statuses,
            "statuses_by_type": {
                item_type: list(dict.fromkeys(values))
                for item_type, values in statuses_by_type.items()
            },
        })
    return {
        "id": board.id,
        "project_id": board.project_id,
        "name": board.name,
        "board_type": board.board_type,
        "is_default": board.is_default,
        "estimation_mode": board.estimation_mode,
        "swimlane_mode": board.swimlane_mode,
        "card_fields_json": board.card_fields_json or [],
        "wip_enforcement": board.wip_enforcement,
        "card_color_scheme": board.card_color_scheme,
        "timezone": board.timezone,
        "working_weekdays": board.working_weekdays or [1, 2, 3, 4, 5],
        "working_hours_per_day": float(board.working_hours_per_day),
        "version": board.version,
        "created_at": board.created_at,
        "updated_at": board.updated_at,
        "columns": columns,
    }
