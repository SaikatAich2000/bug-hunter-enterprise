"""Bugs API + comments + attachments + activity — multi-tenant.

Every read and write is scoped to the caller's organisation AND to
projects the caller has access to. The cheap pattern is:

  1. Compute `accessible_project_ids(db, user)` once per request.
  2. Every bug query gets `.where(Bug.project_id.in_(those_ids))`.
  3. Lookups by bug_id verify the bug's project is in that list before
     handing it back — 404 otherwise so we don't leak existence.

This catches both cross-org leaks (different org's project is never in
the list) and intra-org leaks (a project the user isn't a member of).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Query, Response, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    accessible_project_ids,
    can_access_project,
    can_delete_bug,
    can_edit_bug,
    can_manage_project,
    get_current_user,
)
from app.database import get_db, engine
from app.email_service import (
    BugSnapshot, UserSnapshot,
    notify_assignment, notify_bug_created, notify_bug_updated, notify_comment_added,
)
from app.image_strip import strip_image_metadata
from app.models import (
    ROLE_ADMIN, ROLE_MANAGER,
    Activity, Attachment, Bug, Comment, Project, User,
)
from app.webhooks_delivery import deliver_event
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_ITEM_TYPES, ALLOWED_PRIORITIES, ALLOWED_STATUSES,
    ActivityOut, AttachmentBrief, BugCreate, BugDetail, BugListResponse,
    BugOut, BugUpdate, CommentIn, CommentOut, normalize_choice,
    statuses_for_type,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

# Repeated HTTPException detail strings — extracted so Sonar's S1192
# duplicate-string-literal rule stays quiet and so the wording stays
# consistent across endpoints.
_DETAIL_BUG_NOT_FOUND = "Bug not found"
_DETAIL_COMMENT_NOT_FOUND = "Comment not found"
_DEFAULT_MIME = "application/octet-stream"
# Display label for an empty assignee list in change-tracking diffs.
# Extracted to a constant so Sonar's S1192 duplicate-literal rule stays
# quiet and the wording stays consistent across single + bulk updates.
_NONE_DISPLAY = "(none)"

MAX_FILE_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024

# G2 (v2.8): characters that Excel/Numbers/LibreOffice interpret as a
# formula when they appear at the start of a CSV cell. A bug title like
# `=cmd|'/c calc.exe'!A1` would execute the formula when the exported
# CSV is opened. We neutralise by prefixing such cells with a single
# quote — the de-facto OWASP-recommended approach. The leading quote
# isn't displayed by the spreadsheet, only the text after it.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value) -> str:
    """Defang CSV injection. Always returns a string; non-strings are
    coerced via ``str()`` first."""
    s = "" if value is None else str(value)
    if s and s[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + s
    return s

_ACTIVE_CONTENT_TYPES = {
    "text/html", "application/xhtml+xml", "application/xml", "text/xml",
    "image/svg+xml", "application/javascript", "text/javascript",
    "application/x-javascript", "text/javascript;charset=utf-8",
}

_HEADER_FILENAME_BAD = re.compile(r'[\r\n"\\]+')


def _safe_filename_for_header(name: str) -> str:
    cleaned = _HEADER_FILENAME_BAD.sub("_", name)
    ascii_only = "".join(c if 32 <= ord(c) < 127 else "_" for c in cleaned)
    return ascii_only or "file"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}


def _attachment_brief(a: Attachment) -> dict:
    return {
        "id": a.id, "filename": a.filename, "content_type": a.content_type,
        "size_bytes": a.size_bytes, "uploader_user_id": a.uploader_user_id,
        "uploader_name": a.uploader_name, "comment_id": a.comment_id,
        "created_at": a.created_at,
    }


def _bug_to_out_dict(bug: Bug, attachment_count: int = 0, can_edit: bool = False) -> dict:
    # v2.4: include item_type + event link so the SPA can render the
    # right tab badge / event pill on the list and detail views.
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "project_key": bug.project.key if bug.project else None,
        "item_type": getattr(bug, "item_type", None) or "Bug",
        "event_id": getattr(bug, "event_id", None),
        "event_name": (bug.event.name if getattr(bug, "event", None) else None),
        "title": bug.title,
        "description": bug.description,
        "reporter": _user_brief(bug.reporter) if bug.reporter else None,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "created_at": bug.created_at,
        "updated_at": bug.updated_at,
        "attachment_count": attachment_count,
        "can_edit": can_edit,
    }


def _bug_snapshot(bug: Bug) -> BugSnapshot:
    return BugSnapshot(
        id=bug.id, title=bug.title,
        project_name=bug.project.name if bug.project else "",
        status=bug.status, priority=bug.priority, environment=bug.environment,
        description=bug.description,
        reporter=(UserSnapshot(id=bug.reporter.id, name=bug.reporter.name, email=bug.reporter.email)
                  if bug.reporter else None),
        assignees=tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in bug.assignees),
        item_type=getattr(bug, "item_type", None) or "Bug",
    )


def _resolve_users(db: Session, user_ids: list[int], org_id: int) -> list[User]:
    """Resolve user IDs to User rows — but reject any that belong to a
    different org. Without that check, an attacker who learns a user_id
    in another org could assign tickets to them."""
    if not user_ids:
        return []
    rows = list(db.scalars(
        select(User).where(User.id.in_(user_ids), User.org_id == org_id)
    ).all())
    found = {u.id for u in rows}
    missing = set(user_ids) - found
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {sorted(missing)}")
    return rows


def _resolve_user(db: Session, user_id: int | None, org_id: int) -> User | None:
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None or user.org_id != org_id:
        raise HTTPException(status_code=400, detail=f"User {user_id} does not exist")
    return user


def _log(
    db: Session, org_id: int, bug_id: int | None, actor: User | None,
    action: str, detail: str,
    entity_type: str = "bug", entity_id: int | None = None,
) -> None:
    db.add(Activity(
        org_id=org_id,
        bug_id=bug_id,
        entity_type=entity_type,
        entity_id=entity_id if entity_id is not None else bug_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


def _eager_bug() -> "select":
    return select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        # v2.4: pre-load event so _bug_to_out_dict can return event_name
        # without an N+1 round-trip per row.
        selectinload(Bug.event),
    )


def _attachment_count(db: Session, bug_id: int) -> int:
    return db.scalar(
        select(func.count(Attachment.id)).where(Attachment.bug_id == bug_id)
    ) or 0


def _like_escape(needle: str) -> str:
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _get_bug_or_404(db: Session, bug_id: int, user: User) -> Bug:
    """Look up a bug by ID, but only if its project is in the user's
    accessible set. 404 otherwise so cross-org/cross-project access
    doesn't reveal anything."""
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    if bug.project is None or bug.project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    if not can_access_project(db, user, bug.project):
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    return bug


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
def _normalize_choice_list(values: Optional[list[str]], allowed: list[str], label: str) -> list[str]:
    """Normalize a multi-valued enum query param. Strip empties; reject
    unknown values with 400 (same behavior as the legacy single-value path)."""
    if not values:
        return []
    out: list[str] = []
    for v in values:
        if v is None or v == "":
            continue
        try:
            out.append(normalize_choice(v, allowed, label))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return out


def _apply_where_both(stmt, count_stmt, clause):
    return stmt.where(clause), count_stmt.where(clause)


def _apply_q_filter(stmt, count_stmt, q: str):
    """Bug-id-or-text search: #123 / 123 → exact id match; otherwise on
    Postgres a tsvector FTS path + ILIKE fallback, on SQLite plain ILIKE."""
    q_clean = q.strip().lstrip("#")
    if q_clean.isdigit():
        return _apply_where_both(stmt, count_stmt, Bug.id == int(q_clean))
    if not q_clean:
        return stmt, count_stmt
    # On Postgres we use the database's full-text search (tsvector over
    # title || description). It's an order of magnitude faster than
    # ILIKE on tables >50k rows AND gives us prefix matching + stemming
    # for free. On SQLite (tests / single-user dev) we fall back to the
    # older ILIKE path.
    if engine.dialect.name == "postgresql":
        # Use plainto_tsquery so user input doesn't have to be
        # well-formed FTS syntax. We OR with an ILIKE fallback so
        # short / partial-word searches ("log" → "login") still hit.
        from sqlalchemy import text as _sqltext
        tsquery = _sqltext(
            "to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(description,'')) "
            "@@ plainto_tsquery('simple', :q)"
        ).bindparams(q=q_clean)
        like = f"%{_like_escape(q_clean.lower())}%"
        clause = or_(
            tsquery,
            func.lower(Bug.title).like(like, escape="\\"),
            func.lower(Bug.description).like(like, escape="\\"),
        )
    else:
        like = f"%{_like_escape(q_clean.lower())}%"
        clause = or_(
            func.lower(Bug.title).like(like, escape="\\"),
            func.lower(Bug.description).like(like, escape="\\"),
        )
    return _apply_where_both(stmt, count_stmt, clause)


def _apply_event_filter(stmt, count_stmt, event_id: int):
    """event_id=0 means "show only items NOT attached to an event"
    (a convenience for the event-detail drill-in's siblings panel)."""
    if event_id == 0:
        return _apply_where_both(stmt, count_stmt, Bug.event_id.is_(None))
    return _apply_where_both(stmt, count_stmt, Bug.event_id == event_id)


def _apply_list_filters(stmt, count_stmt, *, statuses, priorities, environments,
                        item_types, project_ids, assignee_ids, reporter_id,
                        event_id, q):
    """Layer every list_bugs filter onto the select+count statement pair.

    NOTE: `project_ids` here is the *already-intersected* set (caller-asked
    AND user-accessible). Tenant isolation is enforced upstream — this
    helper just composes the WHERE clauses.
    """
    if project_ids:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.project_id.in_(project_ids))
    if statuses:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.status.in_(statuses))
    if priorities:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.priority.in_(priorities))
    if environments:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.environment.in_(environments))
    # v2.4 — type tabs filter implicitly via this list.
    if item_types:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.item_type.in_(item_types))
    if reporter_id is not None:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = _apply_where_both(
            stmt, count_stmt, Bug.assignees.any(User.id.in_(assignee_ids)),
        )
    if event_id is not None:
        stmt, count_stmt = _apply_event_filter(stmt, count_stmt, event_id)
    if q:
        stmt, count_stmt = _apply_q_filter(stmt, count_stmt, q)
    return stmt, count_stmt


@router.get("", response_model=BugListResponse)
def list_bugs(
    project_id: Optional[list[int]] = Query(default=None),
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    priority: Optional[list[str]] = Query(default=None),
    environment: Optional[list[str]] = Query(default=None),
    reporter_id: Optional[int] = None,
    assignee_id: Optional[list[int]] = Query(default=None),
    item_type: Optional[list[str]] = Query(default=None),  # v2.4
    event_id: Optional[int] = Query(default=None),  # v2.4 — 0 means "no event"
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugListResponse:
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")

    # Tenant + per-project access gate. Resolved once per request.
    accessible = accessible_project_ids(db, user)
    if not accessible:
        return BugListResponse(items=[], page=page, page_size=page_size, total=0, pages=0)

    statuses = _normalize_choice_list(status_filter, ALLOWED_STATUSES, "status")
    priorities = _normalize_choice_list(priority, ALLOWED_PRIORITIES, "priority")
    environments = _normalize_choice_list(environment, ALLOWED_ENVIRONMENTS, "environment")
    item_types = _normalize_choice_list(item_type, ALLOWED_ITEM_TYPES, "item_type")

    # The caller-supplied project filter must be intersected with what
    # they can actually see — otherwise they'd see nothing or, worse,
    # could probe other orgs' project IDs.
    asked = [p for p in (project_id or []) if p]
    if asked:
        project_ids_to_use = [p for p in asked if p in accessible]
    else:
        project_ids_to_use = accessible

    if not project_ids_to_use:
        return BugListResponse(items=[], page=page, page_size=page_size, total=0, pages=0)

    assignee_ids = [a for a in (assignee_id or []) if a]

    stmt, count_stmt = _apply_list_filters(
        _eager_bug(), select(func.count(Bug.id)),
        statuses=statuses, priorities=priorities, environments=environments,
        item_types=item_types, project_ids=project_ids_to_use,
        assignee_ids=assignee_ids, reporter_id=reporter_id,
        event_id=event_id, q=q,
    )

    total = db.scalar(count_stmt) or 0
    offset = (page - 1) * page_size
    stmt = stmt.order_by(Bug.updated_at.desc(), Bug.id.desc()).limit(page_size).offset(offset)
    bugs = list(db.scalars(stmt).all())

    bug_ids = [b.id for b in bugs]
    att_counts: dict[int, int] = {}
    if bug_ids:
        att_counts = dict(db.execute(
            select(Attachment.bug_id, func.count(Attachment.id))
            .where(Attachment.bug_id.in_(bug_ids))
            .group_by(Attachment.bug_id)
        ).all())

    items = []
    for b in bugs:
        items.append(_bug_to_out_dict(
            b,
            int(att_counts.get(b.id, 0)),
            can_edit_bug(db, user, b.project, getattr(b, "item_type", None) or "Bug"),
        ))

    return BugListResponse.model_validate({
        "items": items,
        "page": page, "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size if total else 0,
    })


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@router.get("/{bug_id}", response_model=BugDetail)
def get_bug(
    bug_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugDetail:
    bug = db.scalar(
        _eager_bug().options(
            selectinload(Bug.comments),
            selectinload(Bug.activities),
        ).where(Bug.id == bug_id)
    )
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    if bug.project is None or bug.project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    if not can_access_project(db, user, bug.project):
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)

    # v2.6 — newest evidence first so the most recent attachment is at the
    # top of each bucket on the modal.
    all_atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
    ).all())
    by_comment: dict[int, list[Attachment]] = {}
    bug_level: list[Attachment] = []
    for a in all_atts:
        if a.comment_id is None:
            bug_level.append(a)
        else:
            by_comment.setdefault(a.comment_id, []).append(a)

    # v2.6 — newest comments first. The relationship defaults to ascending
    # order; sort here so the inline detail view matches list_comments.
    ordered_comments = sorted(
        bug.comments,
        key=lambda x: (x.created_at, x.id),
        reverse=True,
    )

    payload = _bug_to_out_dict(
        bug, len(all_atts),
        can_edit_bug(db, user, bug.project, getattr(bug, "item_type", None) or "Bug"),
    )
    payload["attachments"] = [_attachment_brief(a) for a in bug_level]
    payload["comments"] = []
    for c in ordered_comments:
        payload["comments"].append({
            "id": c.id, "bug_id": c.bug_id,
            "author_user_id": c.author_user_id, "author_name": c.author_name,
            "body": c.body, "created_at": c.created_at,
            "attachments": [_attachment_brief(a) for a in by_comment.get(c.id, [])],
        })
    payload["activities"] = [
        {
            "id": a.id, "bug_id": a.bug_id, "entity_type": a.entity_type,
            "entity_id": a.entity_id, "actor_user_id": a.actor_user_id,
            "actor_name": a.actor_name, "action": a.action, "detail": a.detail,
            "created_at": a.created_at,
        }
        for a in bug.activities
    ]
    return BugDetail.model_validate(payload)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def _resolve_create_project(db: Session, project_id: int, actor: User) -> Project:
    """Look up the target project, 400/403 if it isn't real / accessible."""
    project = db.get(Project, project_id)
    if project is None or project.org_id != actor.org_id:
        raise HTTPException(status_code=400, detail="Project does not exist")
    if not can_access_project(db, actor, project):
        raise HTTPException(status_code=403, detail="You don't have access to this project")
    return project


def _validate_create_item_type(item_type: str, actor: User) -> None:
    """v2.4: filing a Task or Requirement is admin/manager-only. Members
    can still file Bugs as before."""
    if item_type in ("Task", "Requirement") and actor.role not in (ROLE_ADMIN, ROLE_MANAGER):
        raise HTTPException(
            status_code=403,
            detail=f"Only admins and managers can file {item_type.lower()}s.",
        )


def _validate_create_event(event_id_val: Optional[int], db: Session, actor: User) -> None:
    """v2.4: validate event_id belongs to the same org if provided."""
    if not event_id_val:
        return
    from app.models import Event as _Event
    ev = db.get(_Event, event_id_val)
    if ev is None or ev.org_id != actor.org_id:
        raise HTTPException(status_code=400, detail="Event does not exist")


def _resolve_create_reporter(payload: BugCreate, project: Project,
                             db: Session, actor: User) -> User:
    """Reporter override is only permitted for admins / org managers /
    project leads of THIS project. Regular members file as themselves."""
    if payload.reporter_id is None or payload.reporter_id == actor.id:
        return actor
    if not (actor.role in (ROLE_ADMIN, ROLE_MANAGER) or can_manage_project(db, actor, project)):
        raise HTTPException(status_code=403, detail="You can only file bugs as yourself")
    return _resolve_user(db, payload.reporter_id, actor.org_id)


@router.post("", response_model=BugOut, status_code=status.HTTP_201_CREATED)
def create_bug(
    payload: BugCreate,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugOut:
    project = _resolve_create_project(db, payload.project_id, actor)

    item_type = getattr(payload, "item_type", None) or "Bug"
    _validate_create_item_type(item_type, actor)

    event_id_val = getattr(payload, "event_id", None)
    _validate_create_event(event_id_val, db, actor)

    reporter = _resolve_create_reporter(payload, project, db, actor)
    assignees = _resolve_users(db, payload.assignee_ids, actor.org_id)

    bug = Bug(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        reporter_id=reporter.id,
        status=payload.status,
        priority=payload.priority,
        environment=payload.environment,
        due_date=payload.due_date,
        item_type=item_type,
        event_id=event_id_val,
    )
    bug.assignees = list(assignees)
    db.add(bug)
    db.flush()
    # v2.4 — bake the item type, id, and title into the detail string
    # so searching the audit trail by any of those still hits this row
    # (even if the item is later renamed or deleted).
    _log(db, actor.org_id, bug.id, actor, "bug_created",
         f"{item_type} #{bug.id} '{bug.title}' created with status '{bug.status}'.")
    if assignees:
        names = ", ".join(a.name for a in assignees)
        _log(db, actor.org_id, bug.id, actor, "assignees_added",
             f"{item_type} #{bug.id} '{bug.title}' assigned to: {names}")
    db.commit()

    fresh = db.scalar(_eager_bug().where(Bug.id == bug.id))
    snap = _bug_snapshot(fresh)
    background.add_task(notify_bug_created, snap, actor.id)
    if assignees:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in assignees),
            actor.name,
        )
    # Fire outbound webhook.
    background.add_task(
        deliver_event, actor.org_id, "bug.created",
        {"bug": _bug_to_out_dict(fresh), "actor": _user_brief(actor)},
    )

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, 0,
        can_edit_bug(db, actor, fresh.project, getattr(fresh, "item_type", None) or "Bug"),
    ))


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
# v2.4 — these are the fields whose old→new transitions get an audit row.
# `assignee_ids` and `reporter_id` are handled separately because they
# resolve to relationship rows / human-readable names rather than scalar
# columns. `item_type` and `event_id` are tracked for the type-tab UX.
_UPDATE_TRACKED_FIELDS = [
    "item_type", "status", "priority", "environment", "project_id",
    "due_date", "title", "description", "event_id",
]


def _validate_update_authorization(bug: Bug, actor: User, db: Session) -> None:
    """Tenant + per-project + per-type edit gate. Mirrors the same matrix
    the SPA uses to enable/disable the edit button."""
    current_type = getattr(bug, "item_type", None) or "Bug"
    if not can_edit_bug(db, actor, bug.project, current_type):
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to edit this {current_type.lower()}.",
        )


def _normalize_update_event_id(fields: dict, db: Session, actor: User) -> None:
    """Validate event_id belongs to the caller's org (v2.4).
    `event_id` is currently not normalized to None on 0 in the enterprise
    flow — clients send JSON null when they want to unlink — but we keep
    the helper for parity with the internal refactor."""
    if "event_id" in fields and fields["event_id"]:
        from app.models import Event as _Event
        ev = db.get(_Event, fields["event_id"])
        if ev is None or ev.org_id != actor.org_id:
            raise HTTPException(status_code=400, detail="Event does not exist")


def _validate_update_item_type(fields: dict, actor: User) -> None:
    """v2.4: changing item_type to Task/Requirement is admin/manager only."""
    if "item_type" in fields and fields["item_type"] is not None:
        new_type = fields["item_type"]
        if new_type in ("Task", "Requirement") and actor.role not in (ROLE_ADMIN, ROLE_MANAGER):
            raise HTTPException(
                status_code=403,
                detail=f"Only admins and managers can change item_type to {new_type}.",
            )


def _validate_update_project(fields: dict, actor: User, db: Session) -> None:
    """Validate a new project_id: same org AND the actor has access."""
    if "project_id" in fields and fields["project_id"] is not None:
        new_proj = db.get(Project, fields["project_id"])
        if new_proj is None or new_proj.org_id != actor.org_id:
            raise HTTPException(status_code=400, detail="Project does not exist")
        if not can_access_project(db, actor, new_proj):
            raise HTTPException(status_code=403, detail="You don't have access to that project")


def _validate_update_status(fields: dict, bug: Bug) -> None:
    """v2.5 — per-type status validation. Pydantic only checks the union
    of all statuses; here we enforce that the status being SET belongs to
    the effective item_type. Tolerates rows whose stored status is already
    out-of-set (legacy data): only blocks CHANGING the status to an
    invalid value."""
    if "status" not in fields or fields["status"] is None:
        return
    effective_type = (
        fields.get("item_type") or (getattr(bug, "item_type", None) or "Bug")
    )
    allowed_for_type = statuses_for_type(effective_type)
    new_status = fields["status"]
    if new_status in allowed_for_type or new_status == bug.status:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Status '{new_status}' is not valid for {effective_type}. "
            f"Allowed: {sorted(allowed_for_type)}"
        ),
    )


def _validate_update_payload(fields: dict, bug: Bug, db: Session, actor: User) -> None:
    """Run every pre-mutation validator. Order matters: item_type/event/
    project are checked first so we 400 before any DB writes."""
    _validate_update_item_type(fields, actor)
    _normalize_update_event_id(fields, db, actor)
    _validate_update_project(fields, actor, db)
    _validate_update_status(fields, bug)


def _compute_tracked_changes(bug: Bug, fields: dict) -> list[tuple[str, str, str]]:
    """List of (field, old, new) tuples for every tracked field that
    differs. Description is included so a description-only edit isn't a
    no-op."""
    changes: list[tuple[str, str, str]] = []
    for f in _UPDATE_TRACKED_FIELDS:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))
    return changes


def _apply_reporter_change(bug: Bug, db: Session, actor: User,
                           new_reporter_id: Optional[int],
                           changes: list[tuple[str, str, str]]) -> None:
    """Swap the reporter and append the audit row. Caller has already
    gated on permission AND verified the reporter actually changes.
    Resolves through the org-scoped `_resolve_user` so cross-tenant
    user IDs are rejected."""
    old_reporter_label = bug.reporter.name if bug.reporter else "—"
    if new_reporter_id is None:
        bug.reporter_id = None
        new_reporter_label = "—"
    else:
        new_reporter = _resolve_user(db, new_reporter_id, actor.org_id)
        bug.reporter_id = new_reporter.id
        new_reporter_label = new_reporter.name if new_reporter else "—"
    if old_reporter_label != new_reporter_label:
        changes.append(("reporter", old_reporter_label, new_reporter_label))


def _apply_assignee_diff(bug: Bug, db: Session, actor: User,
                         assignee_ids: Optional[list[int]],
                         changes: list[tuple[str, str, str]]) -> list[User]:
    """Diff and re-bind assignees if the set actually changed. Returns
    the list of NEWLY-added users so the caller can notify them. Uses
    the org-scoped `_resolve_users` so cross-tenant IDs 400."""
    if assignee_ids is None:
        return []
    new_users = _resolve_users(db, assignee_ids, actor.org_id)
    old_ids = {a.id for a in bug.assignees}
    new_ids = {u.id for u in new_users}
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    if not (added_ids or removed_ids):
        return []
    old_names = sorted(a.name for a in bug.assignees)
    new_names = sorted(u.name for u in new_users)
    changes.append((
        "assignees",
        ", ".join(old_names) or _NONE_DISPLAY,
        ", ".join(new_names) or _NONE_DISPLAY,
    ))
    bug.assignees = new_users  # only re-bind when actually different
    return [u for u in new_users if u.id in added_ids]


def _persist_update(db: Session, bug: Bug, actor: User,
                    changes: list[tuple[str, str, str]]) -> None:
    """Commit when there are tracked changes; rollback otherwise so a
    no-op PUT doesn't bump updated_at. Audit rows are org-scoped."""
    if not changes:
        db.rollback()
        return
    # v2.4 — prefix each change with the bug id+title so audit
    # search by title catches update events too.
    prefix = f"#{bug.id} '{bug.title}' — "
    for field, old, new in changes:
        _log(db, actor.org_id, bug.id, actor, f"{field}_changed",
             f"{prefix}{field}: '{old}' → '{new}'")
    db.commit()


def _schedule_update_notifications(background: BackgroundTasks, snap: BugSnapshot,
                                   changes: list[tuple[str, str, str]],
                                   newly_assigned: list[User], actor: User,
                                   fresh: Bug) -> None:
    """Fan out email notifications, the assignee notification, and the
    org-scoped outbound webhook. Webhook only fires on genuine changes."""
    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor.name, actor.id,
        )
        # Webhook fire — only if there were genuine changes.
        background.add_task(
            deliver_event, actor.org_id, "bug.updated",
            {"bug": _bug_to_out_dict(fresh),
             "changes": [{"field": f, "old": o, "new": n} for f, o, n in changes],
             "actor_name": actor.name},
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor.name,
        )


@router.put("/{bug_id}", response_model=BugOut)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugOut:
    bug = _get_bug_or_404(db, bug_id, actor)
    _validate_update_authorization(bug, actor, db)

    fields = payload.model_dump(exclude_unset=True)
    _validate_update_payload(fields, bug, db, actor)

    assignee_ids = fields.pop("assignee_ids", None)
    has_reporter_in_payload = "reporter_id" in fields
    new_reporter_id = fields.pop("reporter_id", None)

    # Only gate on the reporter-change role when it ACTUALLY changes —
    # otherwise a regular member touching any other field while the
    # payload still echoes the existing reporter would 403.
    reporter_actually_changes = (
        has_reporter_in_payload and new_reporter_id != bug.reporter_id
    )
    if reporter_actually_changes and not (
        actor.role in (ROLE_ADMIN, ROLE_MANAGER) or can_manage_project(db, actor, bug.project)
    ):
        raise HTTPException(
            status_code=403,
            detail="Only admins, managers, or project leads can change the reporter",
        )

    changes = _compute_tracked_changes(bug, fields)
    for key, value in fields.items():
        setattr(bug, key, value)

    if reporter_actually_changes:
        _apply_reporter_change(bug, db, actor, new_reporter_id, changes)
    newly_assigned = _apply_assignee_diff(bug, db, actor, assignee_ids, changes)

    _persist_update(db, bug, actor, changes)

    fresh = db.scalar(_eager_bug().where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)
    _schedule_update_notifications(background, snap, changes, newly_assigned, actor, fresh)

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, _attachment_count(db, bug_id),
        can_edit_bug(db, actor, fresh.project, getattr(fresh, "item_type", None) or "Bug"),
    ))


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{bug_id}")
def delete_bug(
    bug_id: int,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    bug = _get_bug_or_404(db, bug_id, actor)
    item_type = getattr(bug, "item_type", None) or "Bug"
    if not can_delete_bug(db, actor, bug.project, item_type):
        raise HTTPException(
            status_code=403,
            detail=f"Only admins can delete {item_type.lower()}s.",
        )
    title = bug.title
    org_id = actor.org_id
    deleted_snapshot = {
        "id": bug.id, "title": title, "project_id": bug.project_id,
        "item_type": item_type,
    }
    # v2.4: detach the bug's audit history BEFORE the delete so the
    # trail survives. Works on the new schema (ondelete=SET NULL) AND
    # on legacy production DBs that still have ondelete=CASCADE —
    # by the time the DELETE runs, no activity row references this
    # bug, so cascade has nothing left to cascade. The audit rows keep
    # entity_id pointing at the original bug id and the detail string
    # preserves the title, so the trail stays searchable.
    db.execute(
        update(Activity)
        .where(Activity.bug_id == bug_id)
        .values(bug_id=None)
    )
    db.flush()
    db.delete(bug)
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="bug", entity_id=bug_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action="bug_deleted",
        detail=f"Deleted {item_type.lower()} #{bug_id} '{title}'",
    ))
    db.commit()
    background.add_task(
        deliver_event, org_id, "bug.deleted",
        {"bug": deleted_snapshot, "actor": _user_brief(actor)},
    )
    return {"message": f"{item_type} deleted"}


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------
from pydantic import BaseModel as _BulkModel, Field as _BulkField


class BulkUpdateIn(_BulkModel):
    """Payload for /api/bugs/bulk-update. Apply the same set of changes
    to every bug ID in `bug_ids`. We intentionally support a narrow
    set of fields — anything more complex deserves per-bug PUTs."""
    bug_ids: list[int] = _BulkField(..., min_length=1, max_length=200)
    status: Optional[str] = None
    priority: Optional[str] = None
    environment: Optional[str] = None
    add_assignee_ids: Optional[list[int]] = None
    remove_assignee_ids: Optional[list[int]] = None


class BulkDeleteIn(_BulkModel):
    bug_ids: list[int] = _BulkField(..., min_length=1, max_length=200)


def _normalize_bulk_choices(payload: BulkUpdateIn) -> None:
    """In-place: normalise the three scalar choice fields. Raises 400
    on an invalid value via HTTPException."""
    try:
        if payload.status is not None:
            payload.status = normalize_choice(payload.status, ALLOWED_STATUSES, "status")
        if payload.priority is not None:
            payload.priority = normalize_choice(payload.priority, ALLOWED_PRIORITIES, "priority")
        if payload.environment is not None:
            payload.environment = normalize_choice(payload.environment, ALLOWED_ENVIRONMENTS, "environment")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bulk_can_touch(bug: Bug, actor: User, accessible: set[int], db: Session) -> bool:
    """Tenant + accessibility + edit-permission gate for a single bug
    in a bulk request. Mirrors the silent-skip semantics of the route."""
    if bug.project is None or bug.project.org_id != actor.org_id:
        return False
    if bug.project_id not in accessible:
        return False
    return can_edit_bug(db, actor, bug.project)


def _bulk_apply_scalars(bug: Bug, payload: BulkUpdateIn,
                        local_changes: list[tuple[str, str, str]]) -> None:
    """Apply status/priority/environment diffs to one bug and record
    the field-level audit tuples."""
    for field, new_val in (
        ("status", payload.status),
        ("priority", payload.priority),
        ("environment", payload.environment),
    ):
        if new_val is None:
            continue
        old = getattr(bug, field)
        if old != new_val:
            local_changes.append((field, str(old), str(new_val)))
            setattr(bug, field, new_val)


def _bulk_apply_add_assignees(bug: Bug, add_users: list[User],
                              local_changes: list[tuple[str, str, str]]) -> None:
    """Union the supplied users into the bug's assignee set. No-op if
    every requested user is already assigned."""
    if not add_users:
        return
    current = {a.id for a in bug.assignees}
    new_assignees = list(bug.assignees)
    for u in add_users:
        if u.id not in current:
            new_assignees.append(u)
    if len(new_assignees) == len(bug.assignees):
        return
    old_names = sorted(a.name for a in bug.assignees)
    new_names = sorted(u.name for u in new_assignees)
    local_changes.append(("assignees",
                          ", ".join(old_names) or _NONE_DISPLAY,
                          ", ".join(new_names) or _NONE_DISPLAY))
    bug.assignees = new_assignees


def _bulk_apply_remove_assignees(bug: Bug, remove_ids: set[int],
                                 local_changes: list[tuple[str, str, str]]) -> None:
    """Drop the supplied user ids from the bug's assignee set."""
    if not remove_ids:
        return
    kept = [u for u in bug.assignees if u.id not in remove_ids]
    if len(kept) == len(bug.assignees):
        return
    old_names = sorted(a.name for a in bug.assignees)
    new_names = sorted(u.name for u in kept)
    local_changes.append(("assignees",
                          ", ".join(old_names) or _NONE_DISPLAY,
                          ", ".join(new_names) or _NONE_DISPLAY))
    bug.assignees = kept


def _bulk_log_changes(db: Session, actor: User, bug: Bug,
                      local_changes: list[tuple[str, str, str]]) -> None:
    """Write one audit row per field that actually changed."""
    for field, old, new in local_changes:
        _log(db, actor.org_id, bug.id, actor, f"{field}_changed",
             f"{field}: '{old}' → '{new}' (bulk)")


def _bulk_process_one(bug: Bug, payload: BulkUpdateIn, add_users: list[User],
                      remove_ids: set[int], actor: User, accessible: set[int],
                      db: Session) -> Optional[bool]:
    """Apply the bulk diff to a single bug.

    Returns True if the bug was mutated + logged, False if the bug was
    skipped by the tenant/access/edit gate, or None if it passed the
    gate but had no diff (matches the legacy loop's tri-state counting:
    no-op rows are NEITHER updated nor skipped)."""
    if not _bulk_can_touch(bug, actor, accessible, db):
        return False
    local_changes: list[tuple[str, str, str]] = []
    _bulk_apply_scalars(bug, payload, local_changes)
    _bulk_apply_add_assignees(bug, add_users, local_changes)
    _bulk_apply_remove_assignees(bug, remove_ids, local_changes)
    if not local_changes:
        return None
    _bulk_log_changes(db, actor, bug, local_changes)
    return True


@router.post("/bulk-update")
def bulk_update(
    payload: BulkUpdateIn,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Apply the supplied diff to many bugs in one shot. Skips bugs
    the actor can't edit (silent — the response lists how many were
    actually touched). All tenant-isolation checks still apply."""
    _normalize_bulk_choices(payload)

    accessible = set(accessible_project_ids(db, actor))
    bugs = list(db.scalars(_eager_bug().where(Bug.id.in_(payload.bug_ids))).all())
    add_users: list[User] = (
        _resolve_users(db, payload.add_assignee_ids, actor.org_id)
        if payload.add_assignee_ids else []
    )
    remove_ids = set(payload.remove_assignee_ids or [])

    updated = 0
    skipped = 0
    for bug in bugs:
        result = _bulk_process_one(bug, payload, add_users, remove_ids, actor, accessible, db)
        if result is True:
            updated += 1
        elif result is False:
            skipped += 1
        # result is None → passed gate but no diff: neither updated nor skipped

    if updated:
        db.commit()
        background.add_task(
            deliver_event, actor.org_id, "bugs.bulk_updated",
            {"bug_ids": [b.id for b in bugs if b.project and b.project.org_id == actor.org_id],
             "updated": updated, "skipped": skipped,
             "actor": _user_brief(actor)},
        )
    else:
        db.rollback()
    return {"updated": updated, "skipped": skipped}


@router.post("/bulk-delete")
def bulk_delete(
    payload: BulkDeleteIn,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Delete many bugs at once. Same per-bug permission as DELETE /bugs/{id}."""
    accessible = set(accessible_project_ids(db, actor))
    bugs = list(db.scalars(_eager_bug().where(Bug.id.in_(payload.bug_ids))).all())
    deleted = 0
    skipped = 0
    deleted_ids: list[int] = []
    for bug in bugs:
        if bug.project is None or bug.project.org_id != actor.org_id:
            skipped += 1
            continue
        if bug.project_id not in accessible:
            skipped += 1
            continue
        bulk_item_type = getattr(bug, "item_type", None) or "Bug"
        if not can_delete_bug(db, actor, bug.project, bulk_item_type):
            skipped += 1
            continue
        bid = bug.id
        title = bug.title
        # v2.4 audit retention — same detach-before-delete pattern as
        # the single-bug delete handler above. Keeps history intact.
        db.execute(
            update(Activity)
            .where(Activity.bug_id == bid)
            .values(bug_id=None)
        )
        db.flush()
        db.delete(bug)
        db.add(Activity(
            org_id=actor.org_id, bug_id=None, entity_type="bug", entity_id=bid,
            actor_user_id=actor.id, actor_name=actor.name,
            action="bug_deleted",
            detail=f"Deleted bug #{bid} '{title}' (bulk)",
        ))
        deleted_ids.append(bid)
        deleted += 1
    if deleted:
        db.commit()
        background.add_task(
            deliver_event, actor.org_id, "bugs.bulk_deleted",
            {"bug_ids": deleted_ids, "actor": _user_brief(actor)},
        )
    return {"deleted": deleted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/comments", response_model=list[CommentOut])
def list_comments(
    bug_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    _get_bug_or_404(db, bug_id, user)
    # v2.6 — newest comments first (matches the inline detail view).
    comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
    ).all())
    atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id, Attachment.comment_id.isnot(None))
    ).all())
    by_cid: dict[int, list[Attachment]] = {}
    for a in atts:
        by_cid.setdefault(a.comment_id, []).append(a)
    return [
        {
            "id": c.id, "bug_id": c.bug_id,
            "author_user_id": c.author_user_id, "author_name": c.author_name,
            "body": c.body, "created_at": c.created_at,
            "attachments": [_attachment_brief(a) for a in by_cid.get(c.id, [])],
        }
        for c in comments
    ]


@router.post("/{bug_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED)
def add_comment(
    bug_id: int,
    payload: CommentIn,
    background: BackgroundTasks,
    author: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    bug = _get_bug_or_404(db, bug_id, author)

    c = Comment(
        bug_id=bug_id,
        author_user_id=author.id,
        author_name=author.name,
        body=payload.body,
    )
    db.add(c)
    db.flush()
    _log(db, author.org_id, bug_id, author, "comment_added",
         f"#{bug.id} '{bug.title}' — comment by {author.name}: {payload.body[:80]}")
    db.commit()
    db.refresh(c)

    snap = _bug_snapshot(bug)
    background.add_task(
        notify_comment_added, snap, author.name, author.id, payload.body,
    )
    background.add_task(
        deliver_event, author.org_id, "comment.added",
        {"bug_id": bug_id, "comment_id": c.id,
         "body": c.body, "author": _user_brief(author)},
    )
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at, "attachments": [],
    }


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
async def _read_upload_with_limit(file: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max {limit // (1024 * 1024)} MB.",
            )
    return bytes(buf)


@router.post("/{bug_id}/attachments", response_model=AttachmentBrief, status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    bug_id: int,
    file: UploadFile = File(...),
    comment_id: Optional[int] = Form(default=None),
    uploader: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # Side-effect call: validates the bug exists, belongs to the actor's
    # org, and the actor has access. We don't need the row itself here.
    _get_bug_or_404(db, bug_id, uploader)
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # T6 (v2.8): strip EXIF / GPS / camera-serial / XMP / ICC from raster
    # image uploads. No-op for non-images and fail-open on errors so an
    # exotic image format never blocks the upload.
    data = strip_image_metadata(data, file.content_type)

    att = Attachment(
        bug_id=bug_id,
        comment_id=comment_id,
        uploader_user_id=uploader.id,
        uploader_name=uploader.name,
        filename=(file.filename or "unnamed")[:255],
        content_type=(file.content_type or _DEFAULT_MIME)[:120],
        size_bytes=len(data),
        data=data,
    )
    db.add(att)
    db.flush()
    _log(
        db, uploader.org_id, bug_id, uploader, "attachment_added",
        f"{uploader.name} uploaded '{att.filename}' ({len(data)} bytes)"
        + (f" on comment #{comment_id}" if comment_id else ""),
        entity_type="attachment", entity_id=att.id,
    )
    db.commit()
    db.refresh(att)
    return _attachment_brief(att)


@router.get("/{bug_id}/attachments/{att_id}/download")
def download_attachment(
    bug_id: int, att_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Authorisation through the bug — also validates org + access.
    _get_bug_or_404(db, bug_id, user)
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    ct_lower = (a.content_type or "").lower().split(";")[0].strip()
    is_active = ct_lower in _ACTIVE_CONTENT_TYPES
    safe_ct = _DEFAULT_MIME if is_active else (a.content_type or _DEFAULT_MIME)
    disposition = "attachment" if is_active else "inline"

    safe_fname = _safe_filename_for_header(a.filename)
    cd = (
        f'{disposition}; filename="{safe_fname}"; '
        f"filename*=UTF-8''{quote(a.filename, safe='')}"
    )

    return StreamingResponse(
        io.BytesIO(a.data),
        media_type=safe_ct,
        headers={
            "Content-Disposition": cd,
            "Content-Length": str(a.size_bytes),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Frame-Options": "DENY",
            "Cache-Control": "private, max-age=0, no-cache",
        },
    )


@router.delete("/{bug_id}/attachments/{att_id}")
def delete_attachment(
    bug_id: int, att_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # _get_bug_or_404 still performs the tenant + access gate so cross-org
    # / cross-project lookups 404 before we leak existence.
    _get_bug_or_404(db, bug_id, actor)
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # v2.5 — attachment deletion is admin-only across the board (both
    # bug-level and comment-level). Members and managers can no longer
    # remove evidence — admins curate it. Project leads, uploaders, and
    # the legacy "manager" pathway are all denied here.
    if actor.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete attachments.",
        )
    fname = a.filename
    db.delete(a)
    _log(
        db, actor.org_id, bug_id, actor, "attachment_deleted",
        f"Deleted attachment '{fname}'",
        entity_type="attachment", entity_id=att_id,
    )
    db.commit()
    return {"message": "Attachment deleted"}


# ---------------------------------------------------------------------------
# Comment edit / delete — admin only (v2.5)
#
# Per the v2.5 spec: "Comments and Attachments must not be editable or
# deletable by anyone except the admin." Authors, project leads, and
# managers all lose the rewrite/destroy buttons; admins curate the
# record. Cross-org and cross-project access is still blocked by
# `_get_bug_or_404` before we ever consult `actor.role`.
# ---------------------------------------------------------------------------
def _require_admin(actor: User, action_label: str) -> None:
    """Raise 403 unless the caller is an org admin. Centralises the
    permission gate for the comment edit/delete endpoints so the role
    check + status code stay consistent (and so members + managers stay
    out)."""
    if actor.role != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail=f"Only admins can {action_label} comments.",
        )


def _get_comment_for_bug_or_404(db: Session, bug_id: int, comment_id: int) -> Comment:
    """Fetch a comment by id, 404 if it doesn't exist or belongs to a
    different bug. Caller has already verified the bug itself is
    accessible via `_get_bug_or_404`, so this is just a parent/child
    integrity check."""
    c = db.get(Comment, comment_id)
    if c is None or c.bug_id != bug_id:
        raise HTTPException(status_code=404, detail=_DETAIL_COMMENT_NOT_FOUND)
    return c


def _comment_attachments(db: Session, comment_id: int) -> list[Attachment]:
    """Return every attachment hung off this comment so the edited
    CommentOut response carries the same shape as the listing route."""
    return list(db.scalars(
        select(Attachment).where(Attachment.comment_id == comment_id)
    ).all())


def _comment_to_out_dict(c: Comment, atts: list[Attachment]) -> dict:
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at,
        "attachments": [_attachment_brief(a) for a in atts],
    }


@router.put("/{bug_id}/comments/{comment_id}", response_model=CommentOut)
def update_comment(
    bug_id: int, comment_id: int,
    payload: CommentIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Admin-only comment edit. Audit row captures old + new previews so
    a moderator can review what changed even after the original body is
    overwritten."""
    bug = _get_bug_or_404(db, bug_id, actor)
    _require_admin(actor, "edit")
    c = _get_comment_for_bug_or_404(db, bug_id, comment_id)

    old_preview = (c.body or "")[:200]
    new_preview = (payload.body or "")[:200]
    c.body = payload.body
    # Defensive: the enterprise Comment model currently has no
    # `updated_at` column, so setattr would create a transient Python
    # attribute that never reaches the DB. Guard with hasattr so we
    # only touch real mapped columns — keeps the DB-safety contract
    # (zero schema changes) while staying ready for a future column.
    if hasattr(c, "updated_at"):
        c.updated_at = datetime.now(timezone.utc)
    db.flush()
    _log(
        db, actor.org_id, bug_id, actor, "comment_edited",
        f"Comment #{c.id} on #{bug.id} '{bug.title}' edited by {actor.name}: "
        f"'{old_preview}' → '{new_preview}'",
        entity_type="comment", entity_id=c.id,
    )
    db.commit()
    db.refresh(c)
    return _comment_to_out_dict(c, _comment_attachments(db, c.id))


@router.delete("/{bug_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    bug_id: int, comment_id: int,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Admin-only comment delete. Attachments hung off the comment ride
    the FK cascade (Attachment.comment_id ondelete=CASCADE) so no manual
    sweep is needed. The audit row keeps the first 200 chars of the body
    for context."""
    bug = _get_bug_or_404(db, bug_id, actor)
    _require_admin(actor, "delete")
    c = _get_comment_for_bug_or_404(db, bug_id, comment_id)

    preview = (c.body or "")[:200]
    author_name = c.author_name
    db.delete(c)
    _log(
        db, actor.org_id, bug_id, actor, "comment_deleted",
        f"Comment #{comment_id} by {author_name} on #{bug.id} "
        f"'{bug.title}' deleted: {preview}",
        entity_type="comment", entity_id=comment_id,
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Activity (per-bug)
# ---------------------------------------------------------------------------
@router.get("/{bug_id}/activity", response_model=list[ActivityOut])
def list_activity(
    bug_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Activity]:
    _get_bug_or_404(db, bug_id, user)
    return list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
    ).all())
