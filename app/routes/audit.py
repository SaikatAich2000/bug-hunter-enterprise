"""Audit-trail endpoint of the caller's organization; managers and admins only."""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import and_, cast, or_, select
from sqlalchemy.orm import Session, aliased
from sqlalchemy.types import String

from app.access import accessible_project_ids
from app.auth import require_manager_or_admin
from app.database import get_db
from app.models import ROLE_ADMIN, Activity, Board, Bug, Event, Project, Sprint, User
from app.schemas import ActivityOut

router = APIRouter(prefix="/api/audit", tags=["audit"])

# int4 max; longer digit strings would overflow entity_id/bug_id on Postgres (500).
_MAX_PK_INT = 2**31 - 1


def _like_escape(needle: str) -> str:
    """Escape LIKE wildcards so literal '%' and '_' in user input match plainly."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _scope_to_org_and_projects(stmt, user, accessible):
    """Scope rows to the caller's organization; managers also only see their projects'.
    Agile entities do not have an Activity.bug_id, so resolve each supported
    entity type back to its project before applying the access filter.
    Aliased subqueries keep this composable with the search path's OUTER JOIN on bugs."""
    stmt = stmt.where(Activity.org_id == user.org_id)
    if user.role == ROLE_ADMIN:
        return stmt
    sbug = aliased(Bug)
    sevent = aliased(Event)
    sboard = aliased(Board)
    ssprint = aliased(Sprint)
    return stmt.where(or_(
        Activity.bug_id.in_(select(sbug.id).where(sbug.project_id.in_(accessible))),
        and_(
            Activity.entity_type == "project",
            Activity.entity_id.in_(select(Project.id).where(Project.id.in_(accessible))),
        ),
        and_(
            Activity.entity_type == "board",
            Activity.entity_id.in_(select(sboard.id).where(sboard.project_id.in_(accessible))),
        ),
        and_(
            Activity.entity_type == "sprint",
            Activity.entity_id.in_(select(ssprint.id).where(ssprint.project_id.in_(accessible))),
        ),
        and_(
            Activity.entity_type == "event",
            Activity.entity_id.in_(
                select(sevent.id).where(sevent.project_id.in_(accessible))
            ),
        ),
    ))


def _filtered_audit(db: Session, user: User, entity_type: Optional[str],
                    actor_user_id: Optional[int], q: Optional[str]):
    """The caller's audit rows, newest first, narrowed by entity type, actor, or free-text `q`.
    The OUTER JOIN on bugs is added only when `q` is present to keep plain browsing cheap."""
    stmt = select(Activity)
    stmt = _scope_to_org_and_projects(stmt, user, accessible_project_ids(db, user))
    if entity_type:
        stmt = stmt.where(Activity.entity_type == entity_type)
    if actor_user_id is not None:
        stmt = stmt.where(Activity.actor_user_id == actor_user_id)
    if q:
        stmt = stmt.outerjoin(Bug, Bug.id == Activity.bug_id)
        raw = q.strip()
        like = f"%{_like_escape(raw.lower())}%"
        clauses = [
            Activity.action.ilike(like, escape="\\"),
            Activity.detail.ilike(like, escape="\\"),
            Activity.actor_name.ilike(like, escape="\\"),
            Activity.entity_type.ilike(like, escape="\\"),
            # Live bug title, so search still works after a rename.
            Bug.title.ilike(like, escape="\\"),
            Bug.item_type.ilike(like, escape="\\"),
        ]
        # Digit run so "#42", "bug 42", and "42" all resolve to id 42.
        digits_match = re.search(r"\d+", raw)
        if digits_match:
            digits = digits_match.group(0)
            # Skip exact-int compare above int4 max.
            if int(digits) <= _MAX_PK_INT:
                entity_id_val = int(digits)
                clauses.append(Activity.entity_id == entity_id_val)
                clauses.append(Activity.bug_id == entity_id_val)
            digit_like = f"%{_like_escape(digits)}%"
            clauses.append(cast(Activity.entity_id, String).ilike(digit_like, escape="\\"))
        stmt = stmt.where(or_(*clauses))
    return stmt.order_by(Activity.created_at.desc(), Activity.id.desc())


@router.get("", response_model=list[ActivityOut])
def list_audit(
    entity_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=5000, le=10000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_manager_or_admin),
) -> list[Activity]:
    stmt = _filtered_audit(db, user, entity_type, actor_user_id, q)
    return list(db.scalars(stmt.limit(limit).offset(offset)).all())


def _csv_cell(value: object) -> str:
    """Text for one cell; a leading = + - @ or tab is defused so a spreadsheet never runs it as a formula."""
    text = "" if value is None else str(value).replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


@router.get("/export.csv")
def export_audit_csv(
    entity_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=10000, ge=1, le=100000),
    db: Session = Depends(get_db),
    user: User = Depends(require_manager_or_admin),
) -> Response:
    """The audit trail as CSV (same filters and scope as the list), for compliance reviews."""
    rows = db.scalars(_filtered_audit(db, user, entity_type, actor_user_id, q).limit(limit)).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "created_at", "actor_user_id", "actor_name", "action",
                     "entity_type", "entity_id", "bug_id", "detail"])
    for r in rows:
        writer.writerow([
            r.id, r.created_at.isoformat() if r.created_at else "", r.actor_user_id or "",
            _csv_cell(r.actor_name), _csv_cell(r.action), _csv_cell(r.entity_type), r.entity_id or "",
            r.bug_id or "", _csv_cell(r.detail),
        ])
    return Response(
        content=out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )
