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
from app.models import (
    ROLE_ADMIN, ROLE_MANAGER,
    Activity, Attachment, Bug, Comment, Project, User,
)
from app.webhooks_delivery import deliver_event
from app.schemas import (
    ALLOWED_ENVIRONMENTS, ALLOWED_PRIORITIES, ALLOWED_STATUSES,
    ActivityOut, AttachmentBrief, BugCreate, BugDetail, BugListResponse,
    BugOut, BugUpdate, CommentIn, CommentOut, normalize_choice,
)

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

MAX_FILE_BYTES = 50 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024

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
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "project_key": bug.project.key if bug.project else None,
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
        raise HTTPException(status_code=404, detail="Bug not found")
    if bug.project is None or bug.project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Bug not found")
    if not can_access_project(db, user, bug.project):
        raise HTTPException(status_code=404, detail="Bug not found")
    return bug


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
@router.get("/export.csv")
def export_bugs_csv(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    pids = accessible_project_ids(db, user)
    rows = []
    if pids:
        rows = list(db.scalars(
            _eager_bug().where(Bug.project_id.in_(pids)).order_by(Bug.id.asc())
        ).all())
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "project", "project_key", "title", "status", "priority", "environment",
        "reporter_name", "reporter_email", "assignees", "due_date",
        "created_at", "updated_at", "description",
    ])
    for b in rows:
        writer.writerow([
            b.id,
            b.project.name if b.project else "",
            b.project.key if b.project else "",
            b.title, b.status, b.priority, b.environment,
            b.reporter.name if b.reporter else "",
            b.reporter.email if b.reporter else "",
            "; ".join(f"{a.name} <{a.email}>" for a in b.assignees),
            b.due_date or "",
            b.created_at.isoformat(),
            b.updated_at.isoformat(),
            b.description.replace("\n", " ").replace("\r", " "),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bugs.csv"'},
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("", response_model=BugListResponse)
def list_bugs(
    project_id: Optional[list[int]] = Query(default=None),
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    priority: Optional[list[str]] = Query(default=None),
    environment: Optional[list[str]] = Query(default=None),
    reporter_id: Optional[int] = None,
    assignee_id: Optional[list[int]] = Query(default=None),
    q: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugListResponse:
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")

    accessible = accessible_project_ids(db, user)
    if not accessible:
        return BugListResponse(items=[], page=page, page_size=page_size, total=0, pages=0)

    def _normalize_list(values, allowed, label):
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

    statuses = _normalize_list(status_filter, ALLOWED_STATUSES, "status")
    priorities = _normalize_list(priority, ALLOWED_PRIORITIES, "priority")
    environments = _normalize_list(environment, ALLOWED_ENVIRONMENTS, "environment")

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

    stmt = _eager_bug().where(Bug.project_id.in_(project_ids_to_use))
    count_stmt = select(func.count(Bug.id)).where(Bug.project_id.in_(project_ids_to_use))

    def apply(both, clause):
        return both[0].where(clause), both[1].where(clause)

    if statuses:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.status.in_(statuses))
    if priorities:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.priority.in_(priorities))
    if environments:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.environment.in_(environments))
    if reporter_id is not None:
        stmt, count_stmt = apply((stmt, count_stmt), Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = apply(
            (stmt, count_stmt),
            Bug.assignees.any(User.id.in_(assignee_ids)),
        )
    if q:
        q_clean = q.strip().lstrip("#")
        if q_clean.isdigit():
            stmt, count_stmt = apply((stmt, count_stmt), Bug.id == int(q_clean))
        elif q_clean:
            # On Postgres we use the database's full-text search (tsvector
            # over title || description). It's an order of magnitude
            # faster than ILIKE on tables >50k rows AND gives us prefix
            # matching + stemming for free. On SQLite (tests / single-
            # user dev) we fall back to the older ILIKE path.
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
            stmt, count_stmt = apply((stmt, count_stmt), clause)

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
            can_edit_bug(db, user, b.project),
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
        raise HTTPException(status_code=404, detail="Bug not found")
    if bug.project is None or bug.project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Bug not found")
    if not can_access_project(db, user, bug.project):
        raise HTTPException(status_code=404, detail="Bug not found")

    all_atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id)
        .order_by(Attachment.created_at.asc())
    ).all())
    by_comment: dict[int, list[Attachment]] = {}
    bug_level: list[Attachment] = []
    for a in all_atts:
        if a.comment_id is None:
            bug_level.append(a)
        else:
            by_comment.setdefault(a.comment_id, []).append(a)

    payload = _bug_to_out_dict(bug, len(all_atts), can_edit_bug(db, user, bug.project))
    payload["attachments"] = [_attachment_brief(a) for a in bug_level]
    payload["comments"] = []
    for c in bug.comments:
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
@router.post("", response_model=BugOut, status_code=status.HTTP_201_CREATED)
def create_bug(
    payload: BugCreate,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugOut:
    project = db.get(Project, payload.project_id)
    if project is None or project.org_id != actor.org_id:
        raise HTTPException(status_code=400, detail="Project does not exist")
    if not can_access_project(db, actor, project):
        raise HTTPException(status_code=403, detail="You don't have access to this project")

    # Reporter override is only permitted for admins / org managers /
    # project leads of THIS project. Regular members file as themselves.
    if payload.reporter_id is not None and payload.reporter_id != actor.id:
        if not (actor.role in (ROLE_ADMIN, ROLE_MANAGER) or can_manage_project(db, actor, project)):
            raise HTTPException(status_code=403, detail="You can only file bugs as yourself")
        reporter = _resolve_user(db, payload.reporter_id, actor.org_id)
    else:
        reporter = actor

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
    )
    bug.assignees = list(assignees)
    db.add(bug)
    db.flush()
    # v2.4 — bake the bug id+title into the detail string so searching
    # the audit trail by title hits this row directly (even if the
    # bug is later renamed or deleted).
    _log(db, actor.org_id, bug.id, actor, "bug_created",
         f"Bug #{bug.id} '{bug.title}' created with status '{bug.status}'.")
    if assignees:
        names = ", ".join(a.name for a in assignees)
        _log(db, actor.org_id, bug.id, actor, "assignees_added",
             f"Bug #{bug.id} '{bug.title}' assigned to: {names}")
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
        fresh, 0, can_edit_bug(db, actor, fresh.project),
    ))


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put("/{bug_id}", response_model=BugOut)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    background: BackgroundTasks,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BugOut:
    bug = _get_bug_or_404(db, bug_id, actor)
    if not can_edit_bug(db, actor, bug.project):
        raise HTTPException(status_code=403, detail="You don't have permission to edit this bug.")

    fields = payload.model_dump(exclude_unset=True)
    actor_name = actor.name

    # Validate new project_id (must be same org, user must have access)
    if "project_id" in fields and fields["project_id"] is not None:
        new_proj = db.get(Project, fields["project_id"])
        if new_proj is None or new_proj.org_id != actor.org_id:
            raise HTTPException(status_code=400, detail="Project does not exist")
        if not can_access_project(db, actor, new_proj):
            raise HTTPException(status_code=403, detail="You don't have access to that project")

    assignee_ids = fields.pop("assignee_ids", None)
    has_reporter_in_payload = "reporter_id" in fields
    new_reporter_id = fields.pop("reporter_id", None)

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

    tracked = ["status", "priority", "environment", "project_id",
               "due_date", "title", "description"]
    changes: list[tuple[str, str, str]] = []
    for f in tracked:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))

    for key, value in fields.items():
        setattr(bug, key, value)

    if reporter_actually_changes:
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

    newly_assigned: list[User] = []
    if assignee_ids is not None:
        new_users = _resolve_users(db, assignee_ids, actor.org_id)
        old_ids = {a.id for a in bug.assignees}
        new_ids = {u.id for u in new_users}
        added_ids = new_ids - old_ids
        removed_ids = old_ids - new_ids
        if added_ids or removed_ids:
            old_names = sorted(a.name for a in bug.assignees)
            new_names = sorted(u.name for u in new_users)
            changes.append((
                "assignees",
                ", ".join(old_names) or "(none)",
                ", ".join(new_names) or "(none)",
            ))
            newly_assigned = [u for u in new_users if u.id in added_ids]
            bug.assignees = new_users

    if changes:
        # v2.4 — prefix each change with the bug id+title so audit
        # search by title catches update events too.
        prefix = f"#{bug.id} '{bug.title}' — "
        for field, old, new in changes:
            _log(db, actor.org_id, bug.id, actor, f"{field}_changed",
                 f"{prefix}{field}: '{old}' → '{new}'")
        db.commit()
    else:
        db.rollback()

    fresh = db.scalar(_eager_bug().where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)

    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor_name, actor.id,
        )
        # Webhook fire — only if there were genuine changes.
        background.add_task(
            deliver_event, actor.org_id, "bug.updated",
            {"bug": _bug_to_out_dict(fresh),
             "changes": [{"field": f, "old": o, "new": n} for f, o, n in changes],
             "actor_name": actor_name},
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor_name,
        )

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, _attachment_count(db, bug_id),
        can_edit_bug(db, actor, fresh.project),
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
    if not can_delete_bug(db, actor, bug.project):
        raise HTTPException(
            status_code=403,
            detail="Only admins and project leads can delete bugs.",
        )
    title = bug.title
    org_id = actor.org_id
    deleted_snapshot = {"id": bug.id, "title": title, "project_id": bug.project_id}
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
        detail=f"Deleted bug #{bug_id} '{title}'",
    ))
    db.commit()
    background.add_task(
        deliver_event, org_id, "bug.deleted",
        {"bug": deleted_snapshot, "actor": _user_brief(actor)},
    )
    return {"message": "Bug deleted"}


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
    try:
        if payload.status is not None:
            payload.status = normalize_choice(payload.status, ALLOWED_STATUSES, "status")
        if payload.priority is not None:
            payload.priority = normalize_choice(payload.priority, ALLOWED_PRIORITIES, "priority")
        if payload.environment is not None:
            payload.environment = normalize_choice(payload.environment, ALLOWED_ENVIRONMENTS, "environment")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    accessible = set(accessible_project_ids(db, actor))
    bugs = list(db.scalars(_eager_bug().where(Bug.id.in_(payload.bug_ids))).all())
    updated = 0
    skipped = 0
    add_users: list[User] = []
    if payload.add_assignee_ids:
        add_users = _resolve_users(db, payload.add_assignee_ids, actor.org_id)
    remove_ids = set(payload.remove_assignee_ids or [])

    for bug in bugs:
        if bug.project is None or bug.project.org_id != actor.org_id:
            skipped += 1
            continue
        if bug.project_id not in accessible:
            skipped += 1
            continue
        if not can_edit_bug(db, actor, bug.project):
            skipped += 1
            continue
        local_changes: list[tuple[str, str, str]] = []
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
        if add_users:
            current = {a.id for a in bug.assignees}
            new_assignees = list(bug.assignees)
            for u in add_users:
                if u.id not in current:
                    new_assignees.append(u)
            if len(new_assignees) != len(bug.assignees):
                old_names = sorted(a.name for a in bug.assignees)
                new_names = sorted(u.name for u in new_assignees)
                local_changes.append(("assignees",
                                      ", ".join(old_names) or "(none)",
                                      ", ".join(new_names) or "(none)"))
                bug.assignees = new_assignees
        if remove_ids:
            kept = [u for u in bug.assignees if u.id not in remove_ids]
            if len(kept) != len(bug.assignees):
                old_names = sorted(a.name for a in bug.assignees)
                new_names = sorted(u.name for u in kept)
                local_changes.append(("assignees",
                                      ", ".join(old_names) or "(none)",
                                      ", ".join(new_names) or "(none)"))
                bug.assignees = kept
        if local_changes:
            for field, old, new in local_changes:
                _log(db, actor.org_id, bug.id, actor, f"{field}_changed",
                     f"{field}: '{old}' → '{new}' (bulk)")
            updated += 1
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
        if not can_delete_bug(db, actor, bug.project):
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
    comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.asc(), Comment.id.asc())
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
    bug = _get_bug_or_404(db, bug_id, uploader)
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    att = Attachment(
        bug_id=bug_id,
        comment_id=comment_id,
        uploader_user_id=uploader.id,
        uploader_name=uploader.name,
        filename=(file.filename or "unnamed")[:255],
        content_type=(file.content_type or "application/octet-stream")[:120],
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
    safe_ct = "application/octet-stream" if is_active else (a.content_type or "application/octet-stream")
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
    bug = _get_bug_or_404(db, bug_id, actor)
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Attachment not found")

    can_delete = (
        actor.role == ROLE_ADMIN
        or a.uploader_user_id == actor.id
        or can_manage_project(db, actor, bug.project)
    )
    if not can_delete:
        raise HTTPException(status_code=403, detail="You can't delete this attachment")
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
