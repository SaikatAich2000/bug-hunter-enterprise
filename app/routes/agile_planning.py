"""Agile API: sprint planning (readiness, capacity). Namespaced
under /api/agile.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.access import accessible_project_ids, can_access_project
from app.agile import boards as boards_svc
from app.agile import planning as planning_svc
from app.agile.permissions import MANAGE_BACKLOG, require_permission
from app.api_docs import BAD_REQUEST_FORBIDDEN_NOT_FOUND_422, NOT_FOUND_404
from app.auth import get_current_user
from app.database import get_db
from app.models import Project, Sprint, User
from app.people import reject_inactive, require_project_members, resolve_users
from app.schemas import PlanningOut, SprintCapacityReplaceIn
from app.services.audit import audit

router = APIRouter(prefix="/api/agile", tags=["agile-planning"])

_DETAIL_SPRINT_NOT_FOUND = "Sprint not found"


def _get_sprint_or_404(db: Session, sprint_id: int, user: User) -> Sprint:
    sprint = db.get(Sprint, sprint_id)
    if sprint is None or not can_access_project(accessible_project_ids(db, user), sprint.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_SPRINT_NOT_FOUND)
    project = db.get(Project, sprint.project_id)
    if project is None or not project.agile_enabled:
        raise HTTPException(status_code=404, detail=_DETAIL_SPRINT_NOT_FOUND)
    return sprint


@router.get("/sprints/{sprint_id}/planning", response_model=PlanningOut, responses=NOT_FOUND_404)
def get_planning(
    sprint_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, user)
    board = boards_svc.get_board_or_none(db, sprint.board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Sprint Board not found")
    return planning_svc.build_planning_summary(db, sprint, board)


@router.put("/sprints/{sprint_id}/capacity", response_model=PlanningOut, responses=BAD_REQUEST_FORBIDDEN_NOT_FOUND_422)
def update_capacity(
    sprint_id: int, payload: SprintCapacityReplaceIn,
    db: Session = Depends(get_db), actor: User = Depends(get_current_user),
) -> dict:
    sprint = _get_sprint_or_404(db, sprint_id, actor)
    require_permission(actor, MANAGE_BACKLOG)
    board = boards_svc.get_board_or_none(db, sprint.board_id)
    if board is None:
        raise HTTPException(status_code=404, detail="Sprint Board not found")
    entries = [e.model_dump() for e in payload.entries]
    user_ids = [e["user_id"] for e in entries]
    if len(set(user_ids)) != len(user_ids):
        raise HTTPException(status_code=422, detail="Each team member can appear only once")
    # Unknown ids are a 400 here rather than a foreign-key failure at commit; only people who
    # can see the sprint's project can be planned into it.
    members = resolve_users(db, user_ids, actor.org_id)
    planned = {c.user_id for c in planning_svc.get_capacity(db, sprint.id)}
    added = [u for u in members if u.id not in planned]
    reject_inactive(added, "team member")
    require_project_members(db, added, sprint.project_id, noun="team member")
    try:
        planning_svc.replace_capacity(db, sprint, board, entries)
    except planning_svc.PlanningError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit(db, actor, "sprint", sprint.id, "sprint_capacity_updated",
          f"Updated capacity for sprint '{sprint.name}' ({len(entries)} member(s))")
    db.commit()
    return planning_svc.build_planning_summary(db, sprint, board)
