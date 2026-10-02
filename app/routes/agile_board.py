"""Agile API: Board (active sprint) view, Backlog planning view and moves,
work-item transitions, estimates, flags, taxonomy, quick filters.
Namespaced under /api/agile.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.agile import boards as boards_svc
from app.agile import sprints as sprints_svc
from app.agile import views as views_svc
from app.agile import workflow as workflow_svc
from app.agile.permissions import (
    EDIT_ITEM,
    MANAGE_BACKLOG,
    MANAGE_BOARD,
    RANK_ITEMS,
    TRANSITION_ITEM,
    require_item_edit,
    require_permission,
)
from app.api_docs import (
    BAD_REQUEST_NOT_FOUND_CONFLICT_422,
    NOT_FOUND_404,
    NOT_FOUND_CONFLICT_409,
    NOT_FOUND_CONFLICT_VALIDATION_422,
)
from app.auth import get_current_user
from app.database import get_db
from app.models import (
    Activity,
    Board,
    Bug,
    Project,
    QuickFilter,
    Sprint,
    User,
)
from app.schemas import (
    AgileIssueOut,
    BoardViewOut,
    HierarchyViewOut,
    PlanningViewOut,
    QuickFilterIn,
    QuickFilterOut,
    QuickFilterUpdateIn,
    SprintMoveIn,
    WorkItemEstimateIn,
    WorkItemFlagIn,
    WorkItemTaxonomyIn,
    WorkItemTransitionIn,
)

router = APIRouter(prefix="/api/agile", tags=["agile-board"])
logger = logging.getLogger("bug_hunter.agile_board")

_DETAIL_BOARD_NOT_FOUND = "Sprint Board not found"
_DETAIL_ITEM_NOT_FOUND = "Work item not found"
_DETAIL_VERSION_CONFLICT = "Item was modified by someone else; reload and retry"
_DETAIL_FILTER_NOT_FOUND = "Quick filter not found"


def _audit(db: Session, actor: User, entity_type: str, entity_id: int, action: str, detail: str) -> None:
    db.add(Activity(
        bug_id=None, entity_type=entity_type, entity_id=entity_id,
        actor_user_id=actor.id, actor_name=actor.name, action=action, detail=detail,
    ))


def _get_board_or_404(db: Session, board_id: int, user: User) -> Board:
    board = boards_svc.get_board_or_none(db, board_id)
    if board is None or not can_access_project(accessible_project_ids(db, user), board.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_BOARD_NOT_FOUND)
    project = db.get(Project, board.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_BOARD_NOT_FOUND)
    return board


def _get_item_or_404(db: Session, item_id: int, user: User) -> Bug:
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, user), item.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_ITEM_NOT_FOUND)
    return item


def _issue_out(db: Session, item: Bug, viewer: User) -> dict:
    """One issue in the Board/Backlog shape, placed on its project's board."""
    board = boards_svc.get_default_board(db, item.project_id)
    done = boards_svc.done_status_values(db, board) if board else set()
    column_of = (
        {value: col.id for value, col in boards_svc.status_to_column(board).items()} if board else None
    )
    return views_svc.issue_payloads(db, [item], viewer, done=done, column_of=column_of)[0]


@router.get("/boards/{board_id}/view", response_model=BoardViewOut, responses=NOT_FOUND_404)
def get_board_view(
    board_id: int, sprint_id: int | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    """The active sprint's board (Jira's "Active sprints"). ``sprint_id`` picks
    one of several active sprints, or previews a future or closed one."""
    board = _get_board_or_404(db, board_id, user)
    sprint = None
    if sprint_id is not None:
        sprint = db.get(Sprint, sprint_id)
        if sprint is None or sprint.board_id != board.id:
            raise HTTPException(status_code=404, detail="Sprint not found on this board")
    return views_svc.board_view(db, board, sprint, user)


@router.get("/boards/{board_id}/planning", response_model=PlanningViewOut, responses=NOT_FOUND_404)
def get_planning_view(
    board_id: int,
    backlog_limit: int = Query(default=views_svc.BACKLOG_DEFAULT_LIMIT, ge=1, le=5000),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    """Jira's Backlog view: open sprints with their issues, then the backlog."""
    board = _get_board_or_404(db, board_id, user)
    return views_svc.planning_view(db, board, user, backlog_limit)


@router.get("/projects/{project_id}/hierarchy", response_model=HierarchyViewOut, responses=NOT_FOUND_404)
def get_hierarchy_view(
    project_id: int,
    q: str = Query(default="", max_length=200),
    include_done: bool = Query(default=True),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    """Epic > issue > Sub-task tree with each Epic's progress."""
    project = db.get(Project, project_id)
    if project is None or not can_access_project(accessible_project_ids(db, user), project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.agile_enabled:
        raise HTTPException(status_code=404, detail="Agile is not enabled for this project")
    board = boards_svc.get_default_board(db, project_id)
    return views_svc.hierarchy_view(db, project_id, user, board, q=q, include_done=include_done)


@router.post("/boards/{board_id}/move", response_model=list[AgileIssueOut], responses=NOT_FOUND_CONFLICT_409)
def move_issues(
    board_id: int, payload: SprintMoveIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> list[dict]:
    """Drag-and-drop in the Backlog: move issues into a sprint (or the backlog
    when ``sprint_id`` is null), positioned between neighbours. Re-ordering
    within the same list needs the rank permission; changing a sprint's
    content needs backlog management."""
    board = _get_board_or_404(db, board_id, actor)
    current = {
        item.id: item.sprint_id
        for item in db.scalars(select(Bug).where(Bug.id.in_(payload.item_ids))).all()
    }
    changes_sprint = any(current.get(i, object()) != payload.sprint_id for i in payload.item_ids)
    require_permission(actor, MANAGE_BACKLOG if changes_sprint else RANK_ITEMS)
    target = None
    if payload.sprint_id is not None:
        target = db.get(Sprint, payload.sprint_id)
        if target is None or target.board_id != board.id:
            raise HTTPException(status_code=404, detail="Sprint not found on this board")
    try:
        items = sprints_svc.move_items(
            db, board.project_id, payload.item_ids, target, actor,
            before_id=payload.before_id, after_id=payload.after_id,
        )
    except sprints_svc.SprintLifecycleError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    where = f"sprint '{target.name}'" if target else "the backlog"
    for item in items:
        _audit(db, actor, "bug", item.id, "backlog_moved", f"Moved #{item.id} to {where}")
    db.commit()
    done = boards_svc.done_status_values(db, board)
    return views_svc.issue_payloads(db, items, actor, done=done)


@router.post("/work-items/{item_id}/transition", responses=BAD_REQUEST_NOT_FOUND_CONFLICT_422)
def transition_item(
    item_id: int, payload: WorkItemTransitionIn, background: BackgroundTasks,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    from app.routes.bugs import _bug_to_out_dict, _user_brief
    from app.webhooks_delivery import deliver_event

    item = _get_item_or_404(db, item_id, actor)
    require_permission(actor, TRANSITION_ITEM)
    require_item_edit(actor, item)
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    board = boards_svc.get_default_board(db, item.project_id)
    if board is None:
        raise HTTPException(status_code=404, detail="No Sprint Board configured for this project")
    if payload.override_wip:
        require_permission(actor, MANAGE_BOARD)
    old_status = item.status
    try:
        workflow_svc.transition_work_item(
            db, item, board, actor, payload.to_status,
            payload.override_wip, payload.override_reason, payload.acknowledged,
        )
        item.version += 1
        db.flush()
        _audit(db, actor, "bug", item.id, "item_transitioned",
               f"#{item.id} '{old_status}' -> '{item.status}'")
        db.commit()
    except workflow_svc.WipLimitError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except workflow_svc.TransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Transition of work item %s failed", item_id)
        raise HTTPException(
            status_code=409,
            detail="The work item could not be moved because it changed meanwhile; reload and retry",
        ) from exc
    db.refresh(item)
    if item.status != old_status:
        background.add_task(
            deliver_event, actor.org_id, "bug.updated",
            {"bug": _bug_to_out_dict(item),
             "changes": [{"field": "status", "old": old_status, "new": item.status}],
             "actor": _user_brief(actor)},
        )
    return _issue_out(db, item, actor)


@router.put("/work-items/{item_id}/estimate", responses=NOT_FOUND_CONFLICT_VALIDATION_422)
def update_item_estimate(
    item_id: int, payload: WorkItemEstimateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    from app.agile import planning as planning_svc
    from app.models import Sprint as SprintModel
    from app.models import SprintItemHistory

    item = _get_item_or_404(db, item_id, actor)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    old_points = item.story_points
    try:
        planning_svc.update_estimate(
            db, item, payload.story_points, payload.original_estimate_minutes,
            payload.remaining_estimate_minutes,
        )
    except planning_svc.PlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if item.sprint_id is not None:
        sprint = db.get(SprintModel, item.sprint_id)
        if sprint is not None and sprint.state == "active" and old_points != item.story_points:
            db.add(SprintItemHistory(
                sprint_id=sprint.id, work_item_id=item.id, event_type="estimate_changed",
                old_estimate=old_points, new_estimate=item.story_points, actor_id=actor.id,
            ))
    item.version += 1
    db.flush()
    _audit(db, actor, "bug", item.id, "estimate_changed", f"#{item.id} estimate updated")
    db.commit()
    db.refresh(item)
    return _issue_out(db, item, actor)


@router.post("/work-items/{item_id}/flag", responses=NOT_FOUND_CONFLICT_409)
def flag_item(
    item_id: int, payload: WorkItemFlagIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    item = _get_item_or_404(db, item_id, actor)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    item.flagged = payload.flagged
    item.version += 1
    db.flush()
    _audit(db, actor, "bug", item.id, "flag_changed", f"#{item.id} flagged={payload.flagged}")
    db.commit()
    db.refresh(item)
    return _issue_out(db, item, actor)


@router.put("/work-items/{item_id}/taxonomy", responses=NOT_FOUND_CONFLICT_409)
def update_item_taxonomy(
    item_id: int, payload: WorkItemTaxonomyIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    from app.agile import taxonomy as taxonomy_svc

    item = _get_item_or_404(db, item_id, actor)
    require_permission(actor, EDIT_ITEM)
    require_item_edit(actor, item)
    if item.version != payload.version:
        raise HTTPException(status_code=409, detail=_DETAIL_VERSION_CONFLICT)
    try:
        if payload.component_ids is not None:
            taxonomy_svc.set_work_item_components(db, item, payload.component_ids)
        if payload.label_ids is not None:
            taxonomy_svc.set_work_item_labels(db, item, payload.label_ids)
    except taxonomy_svc.TaxonomyError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item.version += 1
    db.flush()
    _audit(db, actor, "bug", item.id, "taxonomy_changed", f"#{item.id} taxonomy updated")
    db.commit()
    db.refresh(item)
    return _issue_out(db, item, actor)


# --- Quick filters ---

@router.get("/boards/{board_id}/quick-filters", response_model=list[QuickFilterOut], responses=NOT_FOUND_404)
def list_quick_filters(
    board_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> list[QuickFilter]:
    board = _get_board_or_404(db, board_id, user)
    return list(db.scalars(
        select(QuickFilter).where(QuickFilter.board_id == board.id).order_by(QuickFilter.position)
    ).all())


@router.post("/boards/{board_id}/quick-filters", response_model=QuickFilterOut, status_code=status.HTTP_201_CREATED, responses=NOT_FOUND_404)
def create_quick_filter(
    board_id: int, payload: QuickFilterIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> QuickFilter:
    board = _get_board_or_404(db, board_id, actor)
    require_permission(actor, MANAGE_BOARD)
    next_pos = (db.scalar(
        select(QuickFilter.position).where(QuickFilter.board_id == board.id)
        .order_by(QuickFilter.position.desc()).limit(1)
    ) or 0) + 1
    qf = QuickFilter(
        board_id=board.id, name=payload.name, filter_json=payload.filter_json,
        is_shared=payload.is_shared, position=next_pos, created_by_id=actor.id,
    )
    db.add(qf)
    db.flush()
    _audit(db, actor, "board", board.id, "quick_filter_created",
           f"Created quick filter '{qf.name}' on board '{board.name}'")
    db.commit()
    db.refresh(qf)
    return qf


@router.put("/quick-filters/{filter_id}", response_model=QuickFilterOut, responses=NOT_FOUND_CONFLICT_409)
def update_quick_filter(
    filter_id: int, payload: QuickFilterUpdateIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> QuickFilter:
    qf = db.get(QuickFilter, filter_id)
    if qf is None:
        raise HTTPException(status_code=404, detail=_DETAIL_FILTER_NOT_FOUND)
    board = _get_board_or_404(db, qf.board_id, actor)
    require_permission(actor, MANAGE_BOARD)
    if qf.version != payload.version:
        raise HTTPException(status_code=409, detail="Quick filter was modified by someone else; reload and retry")
    for field in ("name", "filter_json", "is_shared", "position"):
        value = getattr(payload, field)
        if value is not None:
            setattr(qf, field, value)
    qf.version += 1
    _audit(db, actor, "board", board.id, "quick_filter_updated",
           f"Updated quick filter '{qf.name}' on board '{board.name}'")
    db.commit()
    db.refresh(qf)
    del board
    return qf


@router.delete("/quick-filters/{filter_id}", responses=NOT_FOUND_404)
def delete_quick_filter(
    filter_id: int, db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    qf = db.get(QuickFilter, filter_id)
    if qf is None:
        raise HTTPException(status_code=404, detail=_DETAIL_FILTER_NOT_FOUND)
    board = _get_board_or_404(db, qf.board_id, actor)
    require_permission(actor, MANAGE_BOARD)
    _audit(db, actor, "board", board.id, "quick_filter_deleted",
           f"Deleted quick filter '{qf.name}' from board '{board.name}'")
    db.delete(qf)
    db.commit()
    return {"message": "Quick filter deleted"}
