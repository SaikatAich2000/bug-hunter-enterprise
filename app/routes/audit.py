"""Audit-trail endpoint — every action across the caller's org only."""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import cast, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.types import String

from app.auth import can_view_audit, get_current_user
from app.database import get_db
from app.models import Activity, Bug, User
from app.schemas import ActivityOut

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _like_escape(needle: str) -> str:
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _build_audit_query(
    actor: User,
    entity_type: Optional[str],
    actor_user_id: Optional[int],
    q: Optional[str],
):
    """Build the org-scoped audit SELECT shared by the JSON list and CSV
    export. Both endpoints use the same filters so consistency matters.

    The LEFT OUTER JOIN on bugs gives us the live bug title even when
    the audit row didn't bake it into `detail` — so searching for a
    title finds rows written before v2.4's detail-string changes too.
    The join is outer because most audit rows aren't bug-related, and
    even those that are may have been detached (bug_id NULL) when the
    bug was deleted. Detached rows still carry the original title in
    `detail`, so they remain findable through that field."""
    stmt = (
        select(Activity)
        .outerjoin(Bug, Bug.id == Activity.bug_id)
        .where(Activity.org_id == actor.org_id)
    )
    if entity_type:
        stmt = stmt.where(Activity.entity_type == entity_type)
    if actor_user_id is not None:
        stmt = stmt.where(Activity.actor_user_id == actor_user_id)
    if q:
        raw = q.strip()
        like = f"%{_like_escape(raw.lower())}%"
        clauses = [
            Activity.action.ilike(like, escape="\\"),
            Activity.detail.ilike(like, escape="\\"),
            Activity.actor_name.ilike(like, escape="\\"),
            Activity.entity_type.ilike(like, escape="\\"),
            # Search the live bug title — handy when the bug has been
            # renamed since the audit row was written, or for rows
            # that pre-date v2.4's detail-string changes.
            Bug.title.ilike(like, escape="\\"),
        ]
        # Numeric IDs — strip "#", "bug", "issue", "ticket" prefixes so
        # "#42", "bug 42" and "ticket #42" all behave like a search for
        # entity_id = 42. We also OR a textual `cast(entity_id) LIKE`
        # clause so partial-id searches ("4" → 4, 40, 41, …, 422) work.
        digits_match = re.search(r"\d+", raw)
        if digits_match:
            try:
                entity_id_val = int(digits_match.group(0))
                clauses.append(Activity.entity_id == entity_id_val)
                # Also catch rows still attached to the bug via bug_id.
                clauses.append(Activity.bug_id == entity_id_val)
            except ValueError:
                pass
            digit_like = f"%{_like_escape(digits_match.group(0))}%"
            clauses.append(cast(Activity.entity_id, String).ilike(digit_like, escape="\\"))
        stmt = stmt.where(or_(*clauses))
    return stmt.order_by(Activity.created_at.desc(), Activity.id.desc())


@router.get("", response_model=list[ActivityOut])
def list_audit(
    entity_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(default=200, le=1000),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Activity]:
    """Returns audit events filtered by entity, actor and free-text search.

    The search query (`q`) is broad on purpose so operators can paste in
    anything: bug numbers (`#42` / `42` / `bug 42`), user names, item
    titles (current or historical), actions, entity types — they should
    all hit. We OR every plausible column together rather than parsing
    the query into a structured form. Still org-scoped so cross-tenant
    data never leaks."""
    if not can_view_audit(actor):
        raise HTTPException(status_code=403, detail="Forbidden")
    stmt = _build_audit_query(actor, entity_type, actor_user_id, q).limit(limit)
    return list(db.scalars(stmt).all())


@router.get("/export.csv")
def export_audit_csv(
    entity_type: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(default=10000, le=100000),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Dump filtered audit rows as CSV. Same filters as the JSON
    endpoint; bigger default cap (10k vs 200) so operators can export a
    full compliance window in one shot. Org-scoped — never returns rows
    from another tenant."""
    if not can_view_audit(actor):
        raise HTTPException(status_code=403, detail="Forbidden")
    stmt = _build_audit_query(actor, entity_type, actor_user_id, q).limit(limit)
    rows = list(db.scalars(stmt).all())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "created_at", "actor_user_id", "actor_name",
        "action", "entity_type", "entity_id", "bug_id", "detail",
    ])
    for r in rows:
        writer.writerow([
            r.id,
            r.created_at.isoformat() if r.created_at else "",
            r.actor_user_id or "",
            r.actor_name or "",
            r.action,
            r.entity_type or "",
            r.entity_id or "",
            r.bug_id or "",
            (r.detail or "").replace("\n", " ").replace("\r", " "),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
    )
