"""Agile API: project activation, boards, backlog, sprint
lifecycle, and Epic/Story/Sub-task hierarchy. Namespaced under /api/agile. Legacy /api/bugs is untouched by this router.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.agile import backlog as backlog_svc
from app.agile import boards as boards_svc
from app.agile import idempotency as idem_svc
from app.agile import sprints as sprints_svc
from app.agile.hierarchy import HierarchyError, assign_hierarchy
from app.agile.permissions import (
    CANCEL_SPRINT,
    COMPLETE_SPRINT,
    CONFIGURE_AGILE_SETTINGS,
    CREATE_ITEM,
    CREATE_SPRINT,
    EDIT_ITEM,
    MANAGE_BACKLOG,
    MANAGE_BOARD,
    RANK_ITEMS,
    START_SPRINT,
    require_item_edit,
    require_permission,
)
from app.agile.sprints import SprintLifecycleError
from app.api_docs import (
    NOT_FOUND_404,
    NOT_FOUND_CONFLICT_409,
    NOT_FOUND_CONFLICT_VALIDATION_422,
    NOT_FOUND_VALIDATION_422,
)
from app.auth import get_current_user
from app.database import get_db
from app.models import AcceptanceCriterion, Activity, Board, Bug, Project, Sprint, User
from app.schemas import (
    AcceptanceCriterionIn,
    AcceptanceCriterionOut,
    AcceptanceCriterionUpdateIn,
    AgileActivateIn,
    AgileSettingsOut,
    AgileWorkItemCreateIn,
    BacklogItemOut,
    BacklogRankIn,
    BoardColumnsReplaceIn,
    BoardOut,
    BoardUpdateIn,
    ReadyForSprintIn,
    SprintCancelIn,
    SprintCompleteIn,
    SprintCreateIn,
    SprintHistoryOut,
    SprintItemsAddIn,
    SprintOut,
    SprintStartIn,
    SprintUpdateIn,
    WorkItemDatesIn,
    WorkItemHierarchyIn,
    WorkItemTaskFlagsIn,
)

router = APIRouter(prefix="/api/agile", tags=["agile"])

_DETAIL_PROJECT_NOT_FOUND = "Project not found"
# Kept as 404 so an inaccessible project stays indistinguishable, but the reason
# is stated honestly: callers can already read agile_enabled from the settings
# endpoint, and "Project not found" for a project they can list is simply wrong.
_DETAIL_AGILE_NOT_ENABLED = "Agile is not enabled for this project"
_DETAIL_BOARD_NOT_FOUND = "Sprint Board not found"
_DETAIL_SPRINT_NOT_FOUND = "Sprint not found"
_DETAIL_VERSION_CONFLICT = "Sprint was modified by someone else; reload and retry"
_DETAIL_SPRINT_NAME_TAKEN = "A Sprint with this name already exists on this Board"
_DETAIL_WORK_ITEM_NOT_FOUND = "Work item not found"
_DETAIL_ITEM_MODIFIED = "Item was modified by someone else; reload and retry"
_DETAIL_ACCEPTANCE_CRITERION_NOT_FOUND = "Acceptance criterion not found"
_READY_REQUIRED_FIELDS_ERROR = (
    "Ready for Sprint requires: title, description, valid dates, priority, story points, "
    "an owner or assignee, and at least one acceptance criterion"
)


def _validate_story_ready_requirements(db: Session, item: Bug) -> bool:
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


def _apply_task_flag_updates(item: Bug, payload: WorkItemTaskFlagsIn) -> bool:
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


def _audit(db: Session, actor: User, entity_type: str, entity_id: int, action: str, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type=entity_type, entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name, action=action, detail=detail,
    ))


def _get_project_or_404(db: Session, project_id: int, user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None or not can_access_project(accessible_project_ids(db, user), project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_PROJECT_NOT_FOUND)
    return project


def _get_agile_project_or_404(db: Session, project_id: int, user: User) -> Project:
    """Like _get_project_or_404 but also 404s when Agile is disabled —
    use for real Agile data endpoints, never for the settings/enable/disable routes."""
    project = _get_project_or_404(db, project_id, user)
    if not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_AGILE_NOT_ENABLED)
    return project


def _get_board_or_404(db: Session, board_id: int, user: User) -> Board:
    board = boards_svc.get_board_or_none(db, board_id)
    if board is None or not can_access_project(accessible_project_ids(db, user), board.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_BOARD_NOT_FOUND)
    project = db.get(Project, board.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_BOARD_NOT_FOUND)
    return board


def _get_sprint_or_404(db: Session, sprint_id: int, user: User, *, lock: bool = False) -> Sprint:
    """The sprint, if the user can see it. ``lock`` takes a row lock for the
    rest of the transaction (SELECT ... FOR UPDATE on PostgreSQL), so two
    concurrent lifecycle requests are serialized and the second one sees the
    first one's version instead of both passing the version check."""
    if lock:
        sprint = db.scalar(
            select(Sprint).where(Sprint.id == sprint_id).with_for_update()
            .execution_options(populate_existing=True)
        )
    else:
        sprint = db.get(Sprint, sprint_id)
    if sprint is None or not can_access_project(accessible_project_ids(db, user), sprint.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_SPRINT_NOT_FOUND)
    project = db.get(Project, sprint.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_SPRINT_NOT_FOUND)
    return sprint


def _sprint_item_count(db: Session, sprint_id: int) -> int:
    return sprints_svc.sprint_item_count(db, sprint_id)


# =====================================================================
# Project activation / settings
# =====================================================================

@router.get("/projects/{project_id}/settings", response_model=AgileSettingsOut, responses=NOT_FOUND_404)
def get_agile_settings(
    project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    project = _get_project_or_404(db, project_id, user)
    board = boards_svc.get_default_board(db, project_id)
    # Self-heal: a project whose board row is missing (e.g. a data reset removed
    # the board while `agile_enabled` stayed true) would otherwise leave the
    # Sprint Board and Sprint Items tabs permanently blank, because both panels
    # only render when `agile_enabled && board_id`. Repairing the flag here makes
    # the state consistent again and points the UI at the Enable Agile panel.
    if project.agile_enabled and board is None:
        project.agile_enabled = False
        db.commit()
    return {
        "project_id": project.id,
        "agile_enabled": project.agile_enabled,
        "agile_feature_flags": project.agile_feature_flags or {},
        "board_id": board.id if board else None,
    }


@router.post("/projects/{project_id}/enable", response_model=AgileSettingsOut, responses=NOT_FOUND_VALIDATION_422)
def enable_agile(
    project_id: int, payload: AgileActivateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    project = _get_project_or_404(db, project_id, actor)
    require_permission(actor, CONFIGURE_AGILE_SETTINGS)
    try:
        board = boards_svc.activate_agile_for_project(
            db, project, actor, payload.feature_flags.model_dump(),
        )
    except boards_svc.AgileActivationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(db, actor, "project", project.id, "agile_enabled", f"Enabled Agile for project '{project.name}'")
    db.commit()
    return {
        "project_id": project.id,
        "agile_enabled": True,
        "agile_feature_flags": project.agile_feature_flags or {},
        "board_id": board.id,
    }


@router.post("/projects/{project_id}/disable", response_model=AgileSettingsOut, responses=NOT_FOUND_CONFLICT_409)
def disable_agile(
    project_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    project = _get_project_or_404(db, project_id, actor)
    require_permission(actor, CONFIGURE_AGILE_SETTINGS)
    if boards_svc.has_active_sprint(db, project_id):
        raise HTTPException(
            status_code=409,
            detail="Cannot disable Agile while a Sprint is active; complete or cancel it first",
        )
    boards_svc.disable_agile_for_project(db, project)
    _audit(db, actor, "project", project.id, "agile_disabled", f"Disabled Agile for project '{project.name}'")
    db.commit()
    return {
        "project_id": project.id,
        "agile_enabled": False,
        "agile_feature_flags": project.agile_feature_flags or {},
        "board_id": None,
    }


# =====================================================================
# Boards
# =====================================================================

@router.get("/boards", response_model=list[BoardOut], responses=NOT_FOUND_404)
def list_boards(
    project_id: int = Query(...),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[dict]:
    _get_agile_project_or_404(db, project_id, user)
    board = boards_svc.get_default_board(db, project_id)
    return [boards_svc.serialize_board(board)] if board else []


@router.get("/boards/{board_id}", response_model=BoardOut, responses=NOT_FOUND_404)
def get_board(
    board_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    board = _get_board_or_404(db, board_id, user)
    return boards_svc.serialize_board(board)


@router.put("/boards/{board_id}", response_model=BoardOut, responses=NOT_FOUND_CONFLICT_409)
def update_board(
    board_id: int, payload: BoardUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    board = _get_board_or_404(db, board_id, actor)
    require_permission(actor, MANAGE_BOARD)
    if board.version != payload.version:
        raise HTTPException(status_code=409, detail="Sprint Board was modified by someone else; reload and retry")
    fields = payload.model_dump(exclude_unset=True, exclude={"version"})
    for key, value in fields.items():
        if value is not None:
            setattr(board, key, value)
    board.version += 1
    db.flush()
    _audit(db, actor, "board", board.id, "board_updated", f"Updated board '{board.name}' settings")
    db.commit()
    db.refresh(board)
    return boards_svc.serialize_board(board)


@router.put("/boards/{board_id}/columns", response_model=BoardOut, responses=NOT_FOUND_CONFLICT_VALIDATION_422)
def replace_board_columns(
    board_id: int, payload: BoardColumnsReplaceIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    """Board configuration > Columns (Jira): the ordered columns and the
    statuses each one shows. Existing columns are updated in place when their
    id is sent; the rest are created, and columns left out are removed. A
    status may sit in one column only; the right-most column is the one that
    means "done" for sprint completion and every report."""
    from app.models import BoardColumn, BoardColumnStatus, WorkflowStatus

    board = _get_board_or_404(db, board_id, actor)
    require_permission(actor, MANAGE_BOARD)
    if board.version != payload.version:
        raise HTTPException(status_code=409, detail="Board was modified by someone else; reload and retry")
    known_values = set(db.scalars(
        select(WorkflowStatus.persisted_status_value).where(
            WorkflowStatus.scope_key == "global", WorkflowStatus.is_active.is_(True),
        )
    ).all())
    seen_statuses: dict[str, str] = {}
    seen_names: set[str] = set()
    for col_in in payload.columns:
        key = col_in.name.strip().lower()
        if key in seen_names:
            raise HTTPException(status_code=422, detail=f"Two columns are named '{col_in.name}'")
        seen_names.add(key)
        if col_in.wip_limit is not None and col_in.min_cards is not None and col_in.min_cards > col_in.wip_limit:
            raise HTTPException(
                status_code=422,
                detail=f"Column '{col_in.name}': the minimum cannot exceed the maximum",
            )
        for value in col_in.statuses:
            if value not in known_values:
                raise HTTPException(status_code=422, detail=f"Unknown status '{value}'")
            if value in seen_statuses and seen_statuses[value] != col_in.name:
                raise HTTPException(
                    status_code=422,
                    detail=f"Status '{value}' is in both '{seen_statuses[value]}' and '{col_in.name}'; "
                           "a status can be shown in one column only",
                )
            seen_statuses[value] = col_in.name
    if payload.columns[-1].category != "done":
        raise HTTPException(
            status_code=422,
            detail="The right-most column is where issues are done: give it the 'done' category",
        )
    if not payload.columns[-1].statuses:
        raise HTTPException(
            status_code=422,
            detail="The right-most column needs at least one status, or nothing could ever be done",
        )

    existing = {c.id: c for c in board.columns}
    kept_ids = set()
    for position, col_in in enumerate(payload.columns):
        column = existing.get(col_in.id) if col_in.id is not None else None
        if col_in.id is not None and column is None:
            raise HTTPException(status_code=422, detail=f"Column #{col_in.id} is not on this board")
        if column is None:
            column = BoardColumn(board_id=board.id)
            db.add(column)
        column.name = col_in.name
        column.position = position
        column.category = col_in.category
        column.wip_limit = col_in.wip_limit
        column.min_cards = col_in.min_cards
        column.wip_enforcement = col_in.wip_enforcement
        column.color = col_in.color
        db.flush()
        kept_ids.add(column.id)
        wanted = set(db.scalars(
            select(WorkflowStatus.id).where(
                WorkflowStatus.scope_key == "global",
                WorkflowStatus.persisted_status_value.in_(col_in.statuses or [""]),
            )
        ).all())
        current = {m.workflow_status_id: m for m in column.statuses}
        for status_id, mapping in current.items():
            if status_id not in wanted:
                db.delete(mapping)
        for status_id in wanted - set(current):
            db.add(BoardColumnStatus(board_column_id=column.id, workflow_status_id=status_id))
    for column_id, column in existing.items():
        if column_id not in kept_ids:
            db.delete(column)
    board.version += 1
    db.flush()
    _audit(db, actor, "board", board.id, "board_columns_updated", f"Updated columns of board '{board.name}'")
    db.commit()
    db.expire(board)
    return boards_svc.serialize_board(board)


# =====================================================================
# Backlog
# =====================================================================

def _backlog_item_out(item: Bug) -> dict:
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


@router.get("/boards/{board_id}/backlog", response_model=list[BacklogItemOut], responses=NOT_FOUND_404)
def get_backlog(
    board_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[dict]:
    board = _get_board_or_404(db, board_id, user)
    items = backlog_svc.list_backlog(db, board.project_id, limit=limit, offset=offset)
    return [_backlog_item_out(i) for i in items]


@router.post("/boards/{board_id}/rank", response_model=BacklogItemOut, responses=NOT_FOUND_VALIDATION_422)
def rank_backlog_item(
    board_id: int, payload: BacklogRankIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    board = _get_board_or_404(db, board_id, actor)
    require_permission(actor, RANK_ITEMS)
    try:
        item = backlog_svc.rank_item(
            db, board.project_id, payload.item_id, payload.before_id, payload.after_id,
        )
    except backlog_svc.BacklogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(db, actor, "bug", item.id, "backlog_rank_changed",
           f"Updated backlog rank for #{item.id} on board '{board.name}'")
    db.commit()
    db.refresh(item)
    return _backlog_item_out(item)


# =====================================================================
# Sprints
# =====================================================================

def _sprint_out(db: Session, sprint: Sprint, item_count: int | None = None) -> dict:
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
        "item_count": _sprint_item_count(db, sprint.id) if item_count is None else item_count,
    }


@router.get("/sprints", response_model=list[SprintOut], responses=NOT_FOUND_404)
def list_sprints_route(
    board_id: int = Query(...), state: str | None = Query(default=None),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[dict]:
    board = _get_board_or_404(db, board_id, user)
    sprints = sprints_svc.list_sprints(db, board.id, state)
    counts = sprints_svc.sprint_item_counts(db, [s.id for s in sprints])
    return [_sprint_out(db, s, counts.get(s.id, 0)) for s in sprints]


@router.post("/sprints", status_code=status.HTTP_201_CREATED, responses=NOT_FOUND_CONFLICT_409)
def create_sprint_route(
    payload: SprintCreateIn,
    board_id: int = Query(...),
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    """Create one sprint, or many from a pre-computed period list.

    Single-sprint mode (default) returns a SprintOut dict exactly as before.
    Bulk mode (``generate=True``) returns ``{"created": [SprintOut, ...]}``
    so the caller can distinguish the two shapes without an envelope guess.
    """
    board = _get_board_or_404(db, board_id, actor)
    require_permission(actor, CREATE_SPRINT)

    if payload.generate and payload.periods:
        # Bulk generation: every period becomes one sprint. All-or-nothing —
        # a single IntegrityError after any successful insert rolls the whole
        # batch back so we never leave a partial series.
        created: list[Sprint] = []
        try:
            from app.routes.bugs import _reject_inactive, _resolve_users
            assignees = []
            if payload.assignee_ids:
                assignees = _resolve_users(db, payload.assignee_ids, actor.org_id)
                _reject_inactive(assignees)
            for idx, period in enumerate(payload.periods, start=1):
                sprint = sprints_svc.create_sprint(
                    db, board, actor,
                    name=f"{payload.name} {idx}",
                    goal=payload.goal,
                    start_date=period.start_date,
                    end_date=period.end_date,
                    cadence=payload.cadence,
                )
                sprint.assignees = list(assignees)
                created.append(sprint)
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=_DETAIL_SPRINT_NAME_TAKEN) from exc
        for sprint in created:
            _audit(db, actor, "sprint", sprint.id, "sprint_created",
                   f"Created sprint '{sprint.name}'")
        db.commit()
        for sprint in created:
            db.refresh(sprint)
        return {"created": [_sprint_out(db, s) for s in created]}

    assignees = None
    if payload.assignee_ids:
        from app.routes.bugs import _reject_inactive, _resolve_users
        assignees = _resolve_users(db, payload.assignee_ids, actor.org_id)
        _reject_inactive(assignees)
    try:
        # create_sprint flushes, so the unique (board, name) check fires here.
        sprint = sprints_svc.create_sprint(
            db, board, actor, payload.name, payload.goal, payload.start_date, payload.end_date,
            payload.cadence,
        )
        if assignees is not None:
            sprint.assignees = assignees
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_SPRINT_NAME_TAKEN) from exc
    _audit(db, actor, "sprint", sprint.id, "sprint_created", f"Created sprint '{sprint.name}'")
    db.commit()
    db.refresh(sprint)
    return _sprint_out(db, sprint)


@router.get("/sprints/{sprint_id}", response_model=SprintOut, responses=NOT_FOUND_404)
def get_sprint(
    sprint_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, user)
    return _sprint_out(db, sprint)


@router.put("/sprints/{sprint_id}", response_model=SprintOut, responses=NOT_FOUND_CONFLICT_409)
def update_sprint_route(
    sprint_id: int, payload: SprintUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, actor, lock=True)
    require_permission(actor, MANAGE_BACKLOG)
    if sprint.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    if payload.assignee_ids is not None:
        from app.routes.bugs import _reject_inactive, _resolve_users
        assignees = _resolve_users(db, payload.assignee_ids, actor.org_id)
        _reject_inactive(assignees)
        sprint.assignees = assignees
    try:
        sprints_svc.update_sprint(
            db, sprint, payload.name, payload.goal, payload.start_date, payload.end_date,
            payload.cadence,
            # A date sent as null clears it; a date left out is unchanged.
            clear_start="start_date" in payload.model_fields_set and payload.start_date is None,
            clear_end="end_date" in payload.model_fields_set and payload.end_date is None,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_SPRINT_NAME_TAKEN) from exc
    except SprintLifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sprint.version += 1
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_SPRINT_NAME_TAKEN) from exc
    _audit(db, actor, "sprint", sprint.id, "sprint_updated", f"Updated sprint '{sprint.name}'")
    db.commit()
    db.refresh(sprint)
    return _sprint_out(db, sprint)


@router.post("/sprints/{sprint_id}/items", response_model=list[BacklogItemOut], responses=NOT_FOUND_CONFLICT_409)
def add_sprint_items(
    sprint_id: int, payload: SprintItemsAddIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> list[dict]:
    sprint = _get_sprint_or_404(db, sprint_id, actor, lock=True)
    require_permission(actor, MANAGE_BACKLOG)
    try:
        items = sprints_svc.add_items_to_sprint(
            db, sprint, payload.item_ids, actor,
            before_id=payload.before_id, after_id=payload.after_id,
        )
    except SprintLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _audit(db, actor, "sprint", sprint.id, "sprint_item_added",
           f"Added {len(items)} item(s) to sprint '{sprint.name}'")
    db.commit()
    return [_backlog_item_out(i) for i in items]


@router.delete("/sprints/{sprint_id}/items/{work_item_id}", responses=NOT_FOUND_404)
def remove_sprint_item(
    sprint_id: int, work_item_id: int,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, actor, lock=True)
    require_permission(actor, MANAGE_BACKLOG)
    try:
        item = sprints_svc.remove_item_from_sprint(db, sprint, work_item_id, actor)
    except SprintLifecycleError as exc:
        db.rollback()
        code = 404 if "is not in this sprint" in str(exc) else 409
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    _audit(db, actor, "sprint", sprint.id, "sprint_item_removed",
           f"Removed item #{item.id} from sprint '{sprint.name}'")
    db.commit()
    return {"message": "Item removed from sprint"}


def _idempotent_lifecycle(
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


@router.post("/sprints/{sprint_id}/start", response_model=SprintOut, responses=NOT_FOUND_CONFLICT_409)
def start_sprint_route(
    sprint_id: int, payload: SprintStartIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, actor, lock=True)
    require_permission(actor, START_SPRINT)
    raw_payload = payload.model_dump()
    cached = _idempotent_lifecycle(db, actor, sprint, "start_sprint", idempotency_key, raw_payload)
    if cached is not None:
        return json.loads(cached[1])
    if sprint.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    project = db.get(Project, sprint.project_id)
    allow_parallel = bool((project.agile_feature_flags or {}).get("parallel_sprints")) if project else False
    try:
        sprints_svc.start_sprint(
            db, sprint, actor, allow_parallel,
            name=payload.name, goal=payload.goal,
            start_date=payload.start_date, end_date=payload.end_date,
            board=db.get(Board, sprint.board_id),
        )
        sprint.version += 1
        db.flush()
    except SprintLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_SPRINT_NAME_TAKEN) from exc
    _audit(db, actor, "sprint", sprint.id, "sprint_started", f"Started sprint '{sprint.name}'")
    result = _sprint_out(db, sprint)
    if idempotency_key:
        idem_svc.store_result(
            db, actor.id, sprint.project_id, "start_sprint", idempotency_key, raw_payload,
            200, json.dumps(result, default=str),
        )
    db.commit()
    return result


@router.post("/sprints/{sprint_id}/complete", response_model=SprintOut, responses=NOT_FOUND_CONFLICT_409)
def complete_sprint_route(
    sprint_id: int, payload: SprintCompleteIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, actor, lock=True)
    require_permission(actor, COMPLETE_SPRINT)
    raw_payload = payload.model_dump()
    cached = _idempotent_lifecycle(db, actor, sprint, "complete_sprint", idempotency_key, raw_payload)
    if cached is not None:
        return json.loads(cached[1])
    if sprint.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    dispositions = {
        d.item_id: (d.destination, d.target_sprint_id) for d in payload.dispositions
    }
    try:
        sprints_svc.complete_sprint(
            db, sprint, actor, payload.closing_note, dispositions,
            payload.default_destination, payload.default_target_sprint_id,
            board=db.get(Board, sprint.board_id), new_sprint_name=payload.new_sprint_name,
        )
        sprint.version += 1
        db.flush()
    except SprintLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=_DETAIL_SPRINT_NAME_TAKEN) from exc
    _audit(db, actor, "sprint", sprint.id, "sprint_completed",
           f"Completed sprint '{sprint.name}': {payload.closing_note}")
    result = _sprint_out(db, sprint)
    if idempotency_key:
        idem_svc.store_result(
            db, actor.id, sprint.project_id, "complete_sprint", idempotency_key, raw_payload,
            200, json.dumps(result, default=str),
        )
    db.commit()
    return result


@router.post("/sprints/{sprint_id}/cancel", response_model=SprintOut, responses=NOT_FOUND_CONFLICT_409)
def cancel_sprint_route(
    sprint_id: int, payload: SprintCancelIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, actor, lock=True)
    require_permission(actor, CANCEL_SPRINT)
    raw_payload = payload.model_dump()
    cached = _idempotent_lifecycle(db, actor, sprint, "cancel_sprint", idempotency_key, raw_payload)
    if cached is not None:
        return json.loads(cached[1])
    if sprint.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    dispositions = {
        d.item_id: (d.destination, d.target_sprint_id) for d in payload.dispositions
    }
    try:
        sprints_svc.cancel_sprint(
            db, sprint, actor, payload.cancellation_note, dispositions,
            payload.default_destination, payload.default_target_sprint_id,
        )
    except SprintLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sprint.version += 1
    db.flush()
    _audit(db, actor, "sprint", sprint.id, "sprint_cancelled",
           f"Cancelled sprint '{sprint.name}': {payload.cancellation_note}")
    result = _sprint_out(db, sprint)
    if idempotency_key:
        idem_svc.store_result(
            db, actor.id, sprint.project_id, "cancel_sprint", idempotency_key, raw_payload,
            200, json.dumps(result, default=str),
        )
    db.commit()
    return result


@router.get("/sprints/{sprint_id}/history", response_model=list[SprintHistoryOut], responses=NOT_FOUND_404)
def get_sprint_history(
    sprint_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list:
    sprint = _get_sprint_or_404(db, sprint_id, user)
    return sprints_svc.get_history(db, sprint.id)


# =====================================================================
# Work-item hierarchy + Agile-type creation
# =====================================================================

@router.post("/work-items", response_model=BacklogItemOut, status_code=status.HTTP_201_CREATED, responses=NOT_FOUND_VALIDATION_422)
def create_agile_work_item(
    payload: AgileWorkItemCreateIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    from app import notification_service
    from app.email_service import UserSnapshot, notify_assignment
    from app.routes.bugs import (
        _bug_snapshot,
        _bug_to_out_dict,
        _reject_inactive,
        _resolve_users,
        _user_brief,
    )
    from app.webhooks_delivery import deliver_event

    project = _get_agile_project_or_404(db, payload.project_id, actor)
    require_permission(actor, CREATE_ITEM)

    sprint = None
    if payload.sprint_id is not None:
        # Planning into a sprint needs the backlog permission (as /move does).
        require_permission(actor, MANAGE_BACKLOG)
        if payload.item_type == "Epic":
            raise HTTPException(status_code=422, detail="Epics are not planned into sprints")
        if payload.item_type == "Sub-task":
            raise HTTPException(status_code=422, detail="A Sub-task is always in its parent's sprint")
        sprint = db.get(Sprint, payload.sprint_id)
        if sprint is None or sprint.project_id != project.id or sprint.state in ("closed", "cancelled"):
            raise HTTPException(status_code=422, detail="sprint_id must reference an open Sprint in this project")
    # Same assignee rules as /api/bugs: unknown ids are an error (never
    # silently dropped) and deactivated accounts cannot be assigned.
    assignees = _resolve_users(db, payload.assignee_ids, actor.org_id)
    _reject_inactive(assignees)

    item = Bug(
        project_id=project.id,
        reporter_id=actor.id,
        title=payload.title,
        description=payload.description,
        item_type=payload.item_type,
        status=payload.status,
        priority=payload.priority,
        story_points=payload.story_points,
        start_date=payload.start_date,
        due_date=payload.end_date,
    )
    item.assignees = list(assignees)
    db.add(item)
    db.flush()

    try:
        assign_hierarchy(db, item, payload.parent_id, payload.epic_id)
    except HierarchyError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # New standard issues are ranked at the bottom of the backlog by the
    # flush hook; a requested sprint goes through the sprint service so an
    # active sprint records the scope change.
    if sprint is not None:
        try:
            sprints_svc.add_items_to_sprint(db, sprint, [item.id], actor)
        except SprintLifecycleError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.flush()
    _audit(db, actor, "bug", item.id, "agile_item_created",
           f"{item.item_type} #{item.id} '{item.title}' created with status '{item.status}'")
    if assignees:
        _audit(db, actor, "bug", item.id, "assignees_added",
               f"{item.item_type} #{item.id} '{item.title}' assigned to: "
               + ", ".join(a.name for a in assignees))
        notification_service.notify(
            db, [a.id for a in assignees], kind="assigned", background=background,
            title=f"Assigned to {item.item_type.lower()} #{item.id}",
            body=f"{actor.name} assigned you to “{item.title}”.",
            bug_id=item.id, actor_name=actor.name,
        )
    db.commit()
    db.refresh(item)
    if assignees:
        background.add_task(
            notify_assignment, _bug_snapshot(item),
            tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in assignees),
            actor.name,
        )
    background.add_task(
        deliver_event, actor.org_id, "bug.created",
        {"bug": _bug_to_out_dict(item), "actor": _user_brief(actor)},
    )
    return _backlog_item_out(item)


@router.put("/work-items/{item_id}/hierarchy", response_model=BacklogItemOut, responses=NOT_FOUND_CONFLICT_VALIDATION_422)
def update_hierarchy(
    item_id: int, payload: WorkItemHierarchyIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_ITEM_MODIFIED)
    try:
        assign_hierarchy(db, item, payload.parent_id, payload.epic_id)
    except HierarchyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item.version += 1
    db.flush()
    _audit(db, actor, "bug", item.id, "hierarchy_changed",
           f"Updated hierarchy for #{item.id}: parent={payload.parent_id}, epic={payload.epic_id}")
    db.commit()
    db.refresh(item)
    return _backlog_item_out(item)


@router.put("/work-items/{item_id}/ready-for-sprint", response_model=BacklogItemOut, responses=NOT_FOUND_CONFLICT_VALIDATION_422)
def set_ready_for_sprint(
    item_id: int, payload: ReadyForSprintIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    """Ready for Sprint can only become true when the Story's
    mandatory fields are complete. Setting it false is always allowed (a Story
    already in an active Sprint is not silently removed by this — that
    requires an explicit Sprint action, which this endpoint never performs)."""
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.item_type != "Story":
        raise HTTPException(status_code=422, detail="Only a Story supports Ready for Sprint")
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_ITEM_MODIFIED)
    if payload.ready_for_sprint:
        if not _validate_story_ready_requirements(db, item):
            raise HTTPException(status_code=422, detail=_READY_REQUIRED_FIELDS_ERROR)
    item.ready_for_sprint = payload.ready_for_sprint
    item.version += 1
    db.flush()
    _audit(db, actor, "bug", item.id, "ready_for_sprint_changed",
           f"Set ready_for_sprint={payload.ready_for_sprint} on #{item.id}")
    db.commit()
    db.refresh(item)
    return _backlog_item_out(item)


@router.put("/work-items/{item_id}/dates", response_model=BacklogItemOut, responses=NOT_FOUND_CONFLICT_409)
def update_work_item_dates(
    item_id: int, payload: WorkItemDatesIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    """Only path to change a Story/Task's start_date/end_date after creation
    ('do not silently alter dates' — this is the sole controlled entry point)."""
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_ITEM_MODIFIED)
    item.start_date = payload.start_date
    item.due_date = payload.end_date
    item.version += 1
    db.flush()
    _audit(db, actor, "bug", item.id, "dates_changed",
           f"Updated dates for #{item.id}: start={payload.start_date}, end={payload.end_date}")
    db.commit()
    db.refresh(item)
    return _backlog_item_out(item)


@router.put("/work-items/{item_id}/task-flags", response_model=BacklogItemOut, responses=NOT_FOUND_CONFLICT_VALIDATION_422)
def update_task_flags(
    item_id: int, payload: WorkItemTaskFlagsIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    """Only path to change mandatory/blocked/blocked_reason on a Story or Task
    (blocked is a flag+reason, never a workflow column)."""
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.item_type not in ("Story", "Sub-task"):
        raise HTTPException(status_code=422, detail="Only a Story or Sub-task supports mandatory/blocked flags")
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_ITEM_MODIFIED)
    if payload.blocked is True and not (payload.blocked_reason or item.blocked_reason):
        raise HTTPException(status_code=422, detail="blocked_reason is required when blocking an item")
    
    was_mandatory = item.mandatory
    blocked_status_changed = _apply_task_flag_updates(item, payload)
    item.version += 1
    db.flush()
    if blocked_status_changed:
        _audit(db, actor, "bug", item.id,
               "item_blocked" if item.blocked else "item_unblocked",
               f"{'Blocked' if item.blocked else 'Unblocked'} #{item.id}: {item.blocked_reason}")
    if item.mandatory != was_mandatory:
        _audit(db, actor, "bug", item.id, "mandatory_changed",
               f"Set mandatory={item.mandatory} on #{item.id}")
    db.commit()
    db.refresh(item)
    return _backlog_item_out(item)


@router.get("/work-items/{item_id}/acceptance-criteria", response_model=list[AcceptanceCriterionOut], responses=NOT_FOUND_404)
def list_acceptance_criteria(
    item_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> list[AcceptanceCriterion]:
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    return list(db.scalars(
        select(AcceptanceCriterion).where(AcceptanceCriterion.bug_id == item_id)
    ).all())


@router.post(
    "/work-items/{item_id}/acceptance-criteria",
    response_model=AcceptanceCriterionOut, status_code=status.HTTP_201_CREATED,
    responses=NOT_FOUND_404,
)
def add_acceptance_criterion(
    item_id: int, payload: AcceptanceCriterionIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> AcceptanceCriterion:
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_WORK_ITEM_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    row = AcceptanceCriterion(
        bug_id=item_id, description=payload.description, required=payload.required,
        updated_by_id=actor.id,
    )
    db.add(row)
    _audit(db, actor, "bug", item_id, "acceptance_criterion_added",
           f"Added acceptance criterion to #{item_id}")
    db.commit()
    db.refresh(row)
    return row


@router.put("/acceptance-criteria/{criterion_id}", response_model=AcceptanceCriterionOut, responses=NOT_FOUND_404)
def update_acceptance_criterion(
    criterion_id: int, payload: AcceptanceCriterionUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> AcceptanceCriterion:
    row = db.get(AcceptanceCriterion, criterion_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_DETAIL_ACCEPTANCE_CRITERION_NOT_FOUND)
    item = db.get(Bug, row.bug_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_ACCEPTANCE_CRITERION_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    fields = payload.model_dump(exclude_unset=True)
    for key, value in fields.items():
        if value is not None:
            setattr(row, key, value)
    row.updated_by_id = actor.id
    changed = ", ".join(sorted(k for k, v in fields.items() if v is not None)) or "no fields"
    _audit(db, actor, "bug", item.id, "acceptance_criterion_updated",
           f"Acceptance criterion #{criterion_id} on #{item.id} updated ({changed}); met={row.met}")
    db.commit()
    db.refresh(row)
    return row


@router.delete("/acceptance-criteria/{criterion_id}", responses=NOT_FOUND_404)
def delete_acceptance_criterion(
    criterion_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    row = db.get(AcceptanceCriterion, criterion_id)
    if row is None:
        raise HTTPException(status_code=404, detail=_DETAIL_ACCEPTANCE_CRITERION_NOT_FOUND)
    item = db.get(Bug, row.bug_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_ACCEPTANCE_CRITERION_NOT_FOUND)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    db.delete(row)
    _audit(db, actor, "bug", item.id, "acceptance_criterion_deleted",
           f"Deleted acceptance criterion #{criterion_id} from #{item.id}")
    db.commit()
    return {"message": "Acceptance criterion deleted"}
