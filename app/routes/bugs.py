"""Bugs API + comments + attachments + activity + links + bulk actions (per-bug)."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy import func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import bulk_import, notification_service
from app.access import (
    accessible_project_ids,
    can_access_project,
    event_visible,
    scope_bug_query,
)
from app.agile import sprints as sprints_svc
from app.agile.itemtypes import is_epic, is_subtask
from app.agile.permissions import MANAGE_BACKLOG, require_permission
from app.agile.workflow import record_status_change
from app.api_docs import (
    ATTACHMENT_FILE_200,
    BAD_REQUEST_400,
    BAD_REQUEST_413,
    BAD_REQUEST_422,
    BAD_REQUEST_FORBIDDEN_422,
    BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404,
    BAD_REQUEST_FORBIDDEN_NOT_FOUND_422,
    BAD_REQUEST_FORBIDDEN_NOT_FOUND_PAYLOAD_TOO_LARGE_429,
    FORBIDDEN_NOT_FOUND_429,
    NOT_FOUND_404,
    NOT_FOUND_FORBIDDEN_403,
    SERVER_ERROR_500,
    XLSX_FILE_200,
)
from app.auth import (
    can_delete_attachment,
    can_delete_bug,
    can_delete_comment,
    can_edit_bug,
    can_edit_comment,
    get_current_user,
    require_manager_or_admin,
)
from app.database import get_db
from app.email_service import (
    BugSnapshot,
    UserSnapshot,
    notify_assignment,
    notify_bug_created,
    notify_bug_updated,
    notify_comment_added,
)
from app.image_strip import strip_image_metadata
from app.models import (
    Activity,
    Attachment,
    Bug,
    BugLink,
    Comment,
    Event,
    Project,
    Sprint,
    User,
    WorkItemBranch,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    LEGACY_ITEM_TYPES,
    ActivityOut,
    AttachmentBrief,
    BugCreate,
    BugDetail,
    BugLinkIn,
    BugLinkOut,
    BugListResponse,
    BugOut,
    BugUpdate,
    BulkActionIn,
    BulkActionResult,
    BulkImportResult,
    BulkImportRowIssue,
    BulkImportRowWarning,
    CommentIn,
    CommentOut,
    display_id_for,
    normalize_choice,
    rich_text_to_plain,
    statuses_for_type,
)
from app.webhooks_delivery import deliver_event

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

_DETAIL_BUG_NOT_FOUND = "Bug not found"
_DETAIL_ATTACHMENT_NOT_FOUND = "Attachment not found"
_DEFAULT_MIME = "application/octet-stream"

# Item-type conversion errors (Phase 7 contract): stable codes carried in the
# X-Error-Code header and message fragments kept human-readable.
_CONVERSION_NOT_SUPPORTED_CODE = "item_conversion_not_supported"
_CONVERSION_BLOCKED_CODE = "item_conversion_blocked_by_active_branch"
_VERSION_CONFLICT_CODE = "version_conflict"

# Same-row conversions supported through PUT /api/bugs/{id}. Story is included
# because a Story created through /api/agile/work-items lives in the same bugs
# table and must be able to move to/from the legacy types handled here.
_CONVERTIBLE_ITEM_TYPES = ("Bug", "Requirement", "Task", "Story")


def _conversion_refused(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={"X-Error-Code": code})


MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB per attachment

# int4 max; larger numeric search tokens are treated as free text (avoids DataError).
_MAX_PK_INT = 2**31 - 1

# Stream uploads in 1 MB chunks so oversize requests fail before consuming RAM.
_UPLOAD_CHUNK = 1024 * 1024

# Per-user upload rate limit; stops chained 50 MB POSTs from bloating the DB.
_UPLOAD_RATE_WINDOW_SECONDS = 60
_UPLOAD_RATE_MAX = 20
_upload_buckets: dict[int, deque] = {}
_upload_rate_lock = threading.Lock()

# Bound the dict size to avoid unbounded growth under high user churn.
_UPLOAD_BUCKETS_MAX = 5_000

# Per-user comment rate limit; each comment fans out notifications/emails (amplification risk).
_COMMENT_RATE_WINDOW_SECONDS = 60
_COMMENT_RATE_MAX = 30
_comment_buckets: dict[int, deque] = {}
_comment_rate_lock = threading.Lock()


def _check_user_rate(
    buckets: dict[int, deque], lock: threading.Lock, user_id: int,
    *, max_req: int, window: int, detail: str, cap: int = _UPLOAD_BUCKETS_MAX,
) -> None:
    """Per-user sliding-window rate guard; 429 on breach.
    In-process only: multi-worker deployments get per-worker buckets."""
    now = time.monotonic()
    cutoff = now - window
    with lock:
        bucket = buckets.get(user_id)
        if bucket is None:
            if len(buckets) >= cap:
                buckets.pop(next(iter(buckets)), None)
            bucket = deque()
            buckets[user_id] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= max_req:
            retry_after = max(1, int(window - (now - bucket[0])))
            raise HTTPException(
                status_code=429, detail=detail,
                headers={"Retry-After": str(retry_after)},
            )
        bucket.append(now)


def _check_upload_rate(user_id: int) -> None:
    _check_user_rate(
        _upload_buckets, _upload_rate_lock, user_id,
        max_req=_UPLOAD_RATE_MAX, window=_UPLOAD_RATE_WINDOW_SECONDS,
        detail="Too many uploads, slow down a moment.",
    )


def _check_comment_rate(user_id: int) -> None:
    _check_user_rate(
        _comment_buckets, _comment_rate_lock, user_id,
        max_req=_COMMENT_RATE_MAX, window=_COMMENT_RATE_WINDOW_SECONDS,
        detail="Too many comments, slow down a moment.",
    )


# Browser-executable types (same-origin risk); downgraded to octet-stream attachments.
_ACTIVE_CONTENT_TYPES = {
    "text/html", "application/xhtml+xml", "application/xml", "text/xml",
    "image/svg+xml", "application/javascript", "text/javascript",
    "application/x-javascript",
}

# Types safe to render inline. Everything else is forced to
# Content-Disposition: attachment — a blocklist can't cover every executable
# type. SVG is absent because it's scriptable.
_INLINE_SAFE_PREFIXES = ("image/", "video/", "audio/")
_INLINE_SAFE_TYPES = {"application/pdf", "text/plain", "text/csv"}

# Server-side denylist checked at upload time (the frontend list is advisory
# only). The extension is taken from the filename after stripping trailing dots
# and spaces; Windows silently strips them on download, so "evil.exe." would
# slip past a naive suffix check. .js is omitted: JS source is a legitimate
# attachment, and the download path already neutralizes it via octet-stream +
# Content-Disposition: attachment.
_DANGEROUS_UPLOAD_EXTS = frozenset({
    "exe", "msi", "bat", "cmd", "com", "scr", "pif", "cpl", "hta", "jar",
    "jse", "vbs", "vbe", "wsf", "wsh", "ps1", "psm1", "sh", "bash",
    "app", "dmg", "pkg", "deb", "rpm", "apk", "msc", "reg", "lnk", "gadget",
    "dll", "sys", "elf",
})


def _dangerous_upload_ext(filename: str) -> Optional[str]:
    """Return the blocked extension if the filename is executable, else None.

    Strips trailing dots and whitespace before checking (Windows drops them on
    download, so "evil.exe." would otherwise bypass the check)."""
    name = (filename or "").strip().rstrip(". \t")
    if "." not in name:
        return None
    ext = name.rsplit(".", 1)[1].strip().lower()
    return ext if ext in _DANGEROUS_UPLOAD_EXTS else None


# The original filename lives in the DB; this sanitized copy is used only in
# headers. RFC 6266 restricts plain filename= to US-ASCII; non-ASCII goes in
# filename*= (percent-encoded), added by the caller.
_HEADER_FILENAME_BAD = re.compile(r'[\r\n"\\]+')


def _safe_filename_for_header(name: str) -> str:
    """Return an ASCII-only, header-safe version of the filename.

    Strips CR/LF/quotes/backslashes (they break Content-Disposition) and
    replaces non-ASCII characters with underscores. The HTTP layer would
    otherwise Latin-1 encode a non-ASCII filename and it would arrive as
    garbage. The original Unicode form is sent separately via filename*=
    (RFC 5987).
    """
    cleaned = _HEADER_FILENAME_BAD.sub("_", name)
    # Replace everything outside printable ASCII.
    ascii_only = "".join(c if 32 <= ord(c) < 127 else "_" for c in cleaned)
    return ascii_only or "file"


def _item_type(bug: Bug) -> str:
    """Return the work-item type, defaulting to 'Bug' for rows with no item_type."""
    return getattr(bug, "item_type", None) or "Bug"


def _display_id(bug: Bug) -> str:
    """The stable user-facing reference for a work item ("USRSTR-12").

    A stored value always wins: once an item has been created the id people
    reference must not drift, even when the item is reclassified from Story to
    Task. Rows that predate the column fall back to ``<PREFIX>-<id>``.
    """
    stored = (getattr(bug, "display_id", None) or "").strip()
    return stored or display_id_for(_item_type(bug), bug.id)


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
        "display_id": _display_id(bug),
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "title": bug.title,
        # Flatten legacy editor HTML so every consumer reads plain text,
        # even rows stored before the plain-text switch.
        "description": rich_text_to_plain(bug.description or ""),
        "reporter": _user_brief(bug.reporter) if bug.reporter else None,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "item_type": _item_type(bug),
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "event_id": getattr(bug, "event_id", None),
        "event_name": bug.event.name if getattr(bug, "event", None) else None,
        "created_at": bug.created_at,
        "updated_at": bug.updated_at,
        "version": getattr(bug, "version", 1),
        "attachment_count": attachment_count,
        "can_edit": can_edit,
        "epic_id": getattr(bug, "epic_id", None),
        "parent_id": getattr(bug, "parent_id", None),
        "story_points": float(bug.story_points) if getattr(bug, "story_points", None) is not None else None,
        "owner_id": getattr(bug, "owner_id", None),
        "mandatory": getattr(bug, "mandatory", False),
        "blocked": getattr(bug, "blocked", False),
        "blocked_reason": getattr(bug, "blocked_reason", ""),
        "sprint_id": getattr(bug, "sprint_id", None),
        "sprint_name": bug.sprint.name if getattr(bug, "sprint", None) else None,
    }


def _bug_snapshot(bug: Bug) -> BugSnapshot:
    return BugSnapshot(
        id=bug.id, title=bug.title,
        project_name=bug.project.name if bug.project else "",
        status=bug.status, priority=bug.priority, environment=bug.environment,
        # Notification snapshot also reads plain text (legacy HTML flattened).
        description=rich_text_to_plain(bug.description or ""),
        reporter=(UserSnapshot(id=bug.reporter.id, name=bug.reporter.name, email=bug.reporter.email)
                  if bug.reporter else None),
        assignees=tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in bug.assignees),
        # _item_type() handles old rows with no item_type set.
        item_type=_item_type(bug),
        event_name=bug.event.name if getattr(bug, "event", None) else None,
        from_address=bug.project.organization.email_from_override if bug.project else None,
    )


def _resolve_users(db: Session, user_ids: list[int], org_id: int) -> list[User]:
    """Users of the organization with these ids; any other id is "unknown"."""
    if not user_ids: return []
    rows = db.scalars(select(User).where(User.id.in_(user_ids), User.org_id == org_id)).all()
    found = {u.id for u in rows}
    missing = set(user_ids) - found
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {sorted(missing)}")
    return list(rows)


def _resolve_user(db: Session, user_id: int | None, org_id: int) -> User | None:
    if user_id is None: return None
    user = db.get(User, user_id)
    if user is None or user.org_id != org_id:
        raise HTTPException(status_code=400, detail=f"User {user_id} does not exist")
    return user


def _reject_inactive(users: list[User], noun: str = "user") -> None:
    """Reject new assignments to deactivated accounts.

    A disabled user can't log in, so notifications would be dead-ends. Existing
    references are left in place; only new additions are checked.
    """
    inactive = sorted((u.name for u in users if not u.is_active))
    if inactive:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot assign a deactivated {noun}: {', '.join(inactive)}",
        )


# Human-readable labels for each link type, as (outgoing, incoming) pairs.
_LINK_LABELS: dict[str, tuple[str, str]] = {
    "relates": ("relates to", "relates to"),
    "blocks": ("blocks", "is blocked by"),
    "duplicate": ("duplicates", "is duplicated by"),
}


def _link_phrase(link_type: str, direction: str) -> str:
    out_label, in_label = _LINK_LABELS.get(link_type, (link_type, link_type))
    return out_label if direction == "outgoing" else in_label


def _serialize_link(link: BugLink, from_bug_id: int) -> dict:
    """Render a BugLink from the perspective of from_bug_id.

    Both FKs are NOT NULL with cascade-delete, so the other end is always
    present.
    """
    outgoing = link.source_bug_id == from_bug_id
    other = link.target if outgoing else link.source
    direction = "outgoing" if outgoing else "incoming"
    return {
        "id": link.id,
        "link_type": link.link_type,
        "direction": direction,
        "label": _link_phrase(link.link_type, direction),
        "other_bug_id": other.id,
        "other_bug_title": other.title,
        "other_bug_status": other.status,
        "other_bug_item_type": _item_type(other),
        "created_at": link.created_at,
    }


def _bug_links(db: Session, bug_id: int) -> list[dict]:
    """Return all links touching this bug (either direction), newest first."""
    rows = list(db.scalars(
        select(BugLink)
        .options(selectinload(BugLink.source), selectinload(BugLink.target))
        .where(or_(BugLink.source_bug_id == bug_id, BugLink.target_bug_id == bug_id))
        .order_by(BugLink.created_at.desc(), BugLink.id.desc())
    ).all())
    return [_serialize_link(link, bug_id) for link in rows]


def _log(
    db: Session, bug_id: int | None, actor: User | None, action: str, detail: str,
    entity_type: str = "bug", entity_id: int | None = None,
) -> None:
    row = Activity(
        bug_id=bug_id,
        entity_type=entity_type,
        entity_id=entity_id if entity_id is not None else bug_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    )
    if actor is not None:
        row.org_id = actor.org_id
    db.add(row)


def _eager_bug() -> "select":
    return select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    )


def _attachment_count(db: Session, bug_id: int) -> int:
    return db.scalar(
        select(func.count(Attachment.id)).where(Attachment.bug_id == bug_id)
    ) or 0


# Tenant- and project-scoped access. Managers and regular users can only act on
# items in their projects, admins on those of their own organization. Items
# outside scope are surfaced as 404 so the response never leaks whether a bug or
# project exists.
def _assert_bug_accessible(accessible, bug: Bug) -> None:
    """Raise 404 when the bug is outside the actor's project scope."""
    if not can_access_project(accessible, bug.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)


def _assert_project_accessible(db: Session, accessible, project_id: int) -> None:
    """Raise 400 when the project is missing or outside the actor's scope.

    A project the caller can't see is treated as nonexistent.
    """
    if db.get(Project, project_id) is None or not can_access_project(accessible, project_id):
        raise HTTPException(status_code=400, detail="Project does not exist")


def _assert_event_accessible(db: Session, actor: User, accessible, event_id: int) -> None:
    """Raise 400 when the event is missing or out of the actor's scope."""
    ev = db.get(Event, event_id)
    if ev is None or not event_visible(actor, accessible, ev):
        raise HTTPException(status_code=400, detail="Event does not exist")


def _load_accessible_bug(db: Session, bug_id: int, accessible) -> Bug:
    """Load a bug and raise 404 if it's missing or out of scope."""
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _assert_bug_accessible(accessible, bug)
    return bug


def _assert_attachment_in_scope(db: Session, accessible, bug_id: int) -> None:
    """Raise 404 when the owning bug is outside the actor's project scope."""
    proj_id = db.scalar(select(Bug.project_id).where(Bug.id == bug_id))
    if proj_id is None or proj_id not in accessible:
        raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)


def _like_escape(needle: str) -> str:
    """Escape SQL LIKE wildcards so user-typed `_` and `%` match literally."""
    return (
        needle.replace("\\", "\\\\")
              .replace("%", "\\%")
              .replace("_", "\\_")
    )


def _normalize_choice_list(values: Optional[list[str]], allowed: list[str], label: str) -> list[str]:
    """Normalize a multi-valued enum query param; strip empties, reject unknowns with 400."""
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
    """Bug-id-or-text search: #123 or 123 does an exact id match, otherwise LIKE."""
    q_clean = q.strip().lstrip("#")
    if q_clean.isascii() and q_clean.isdigit() and int(q_clean) <= _MAX_PK_INT:
        return _apply_where_both(stmt, count_stmt, Bug.id == int(q_clean))
    if not q_clean:
        return stmt, count_stmt
    # Use the cleaned string for the LIKE pattern so leading/trailing spaces in
    # the raw query (e.g. "?q=  needle  ") don't prevent any match.
    like = f"%{_like_escape(q_clean.lower())}%"
    clause = or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    )
    return _apply_where_both(stmt, count_stmt, clause)


def _reject_overflow_ids(**named_ids) -> None:
    """Reject id-valued query params that would overflow Postgres int4.

    Without this, ?reporter_id=99999999999999 reaches the DB and raises a
    DataError (500). SQLite tolerates oversized ints, so this only matters
    on Postgres. Returns a clean 422 instead.
    """
    for name, value in named_ids.items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            # Small negatives like -1 are in range and simply match nothing;
            # reject only values truly outside int4.
            if v is not None and (v > _MAX_PK_INT or v < -_MAX_PK_INT - 1):
                raise HTTPException(status_code=422, detail=f"{name} is out of range")


def _apply_event_filter(stmt, count_stmt, event_id: int):
    """event_id=0 means "no event"; any other value filters by that event id."""
    if event_id == 0:
        return _apply_where_both(stmt, count_stmt, Bug.event_id.is_(None))
    return _apply_where_both(stmt, count_stmt, Bug.event_id == event_id)


def _apply_list_filters(stmt, count_stmt, *, statuses, priorities, environments,
                        item_types, project_ids, assignee_ids, reporter_id,
                        due_date, event_id, q):
    """Apply all list_bugs filters to the select+count statement pair."""
    if project_ids:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.project_id.in_(project_ids))
    if statuses:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.status.in_(statuses))
    if priorities:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.priority.in_(priorities))
    if environments:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.environment.in_(environments))
    if item_types:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.item_type.in_(item_types))
    if reporter_id is not None:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.reporter_id == reporter_id)
    if assignee_ids:
        stmt, count_stmt = _apply_where_both(
            stmt, count_stmt, Bug.assignees.any(User.id.in_(assignee_ids)),
        )
    if due_date:
        stmt, count_stmt = _apply_where_both(stmt, count_stmt, Bug.due_date == due_date)
    if event_id is not None:
        stmt, count_stmt = _apply_event_filter(stmt, count_stmt, event_id)
    if q:
        stmt, count_stmt = _apply_q_filter(stmt, count_stmt, q)
    return stmt, count_stmt


@router.get("", responses=BAD_REQUEST_422)
def list_bugs(
    project_id: Optional[list[int]] = Query(default=None),
    status_filter: Optional[list[str]] = Query(default=None, alias="status"),
    priority: Optional[list[str]] = Query(default=None),
    environment: Optional[list[str]] = Query(default=None),
    item_type: Optional[list[str]] = Query(default=None),
    reporter_id: Optional[int] = None,
    assignee_id: Optional[list[int]] = Query(default=None),
    event_id: Optional[int] = None,
    due_date: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> BugListResponse:
    """List bugs with optional filters. Enum filters accept repeated params
    (?status=New&status=Resolved); a single value is matched with .in_()."""
    if page < 1 or page_size < 1 or page_size > 200:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")
    _reject_overflow_ids(
        reporter_id=reporter_id, event_id=event_id,
        project_id=project_id, assignee_id=assignee_id,
    )

    statuses = _normalize_choice_list(status_filter, ALLOWED_STATUSES, "status")
    priorities = _normalize_choice_list(priority, ALLOWED_PRIORITIES, "priority")
    environments = _normalize_choice_list(environment, ALLOWED_ENVIRONMENTS, "environment")
    item_types = _normalize_choice_list(item_type, ALLOWED_ITEM_TYPES, "item_type")

    # Drop None/0 from the int lists so callers can send blanks safely.
    project_ids = [p for p in (project_id or []) if p]
    assignee_ids = [a for a in (assignee_id or []) if a]

    stmt, count_stmt = _apply_list_filters(
        _eager_bug(), select(func.count(Bug.id)),
        statuses=statuses, priorities=priorities, environments=environments,
        item_types=item_types, project_ids=project_ids, assignee_ids=assignee_ids,
        reporter_id=reporter_id, due_date=due_date, event_id=event_id, q=q,
    )

    # Restrict to the actor's projects. Applied to both page and count so
    # totals and pagination stay correct. Admins are unrestricted.
    accessible = accessible_project_ids(db, _user)
    stmt = scope_bug_query(stmt, accessible)
    count_stmt = scope_bug_query(count_stmt, accessible)

    total = db.scalar(count_stmt) or 0
    offset = (page - 1) * page_size
    # Skip the DB query when the page is past the end; avoids a huge OFFSET
    # scan for requests like ?page=10000000.
    if total and offset >= total:
        bugs: list[Bug] = []
    else:
        stmt = stmt.order_by(Bug.updated_at.desc(), Bug.id.desc()).limit(page_size).offset(offset)
        bugs = list(db.scalars(stmt).all())

    # Batch attachment counts in one query to avoid N+1.
    bug_ids_in_page = [b.id for b in bugs]
    if bug_ids_in_page:
        att_counts = dict(db.execute(
            select(Attachment.bug_id, func.count(Attachment.id))
            .where(Attachment.bug_id.in_(bug_ids_in_page))
            .group_by(Attachment.bug_id)
        ).all())
    else:
        att_counts = {}

    items = []
    for b in bugs:
        items.append(_bug_to_out_dict(
            b,
            int(att_counts.get(b.id, 0)),
            can_edit_bug(_user, b.reporter_id, [a.id for a in b.assignees],
                         item_type=_item_type(b)),
        ))

    return BugListResponse.model_validate({
        "items": items,
        "page": page, "page_size": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size if total else 0,
    })


@router.get("/import/template.xlsx", response_class=Response,
            responses={**XLSX_FILE_200, **SERVER_ERROR_500})
def download_import_template(
    _user: User = Depends(require_manager_or_admin),
) -> Response:
    """Blank spreadsheet with the exact bulk-import columns (same layout as
    the Item Detail Export report), plus an Instructions sheet."""
    try:
        payload = bulk_import.build_template_workbook()
    except bulk_import.BulkImportError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="bug-hunter-bulk-import-template.xlsx"',
            "Cache-Control": "private, no-store, max-age=0",
        },
    )


@router.post("/import", responses=BAD_REQUEST_413)
async def bulk_import_bugs(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: User = Depends(require_manager_or_admin),
) -> BulkImportResult:
    """Create many bugs/requirements/tasks from an uploaded .xlsx or .csv
    file. Notifications are in-app only — see app/bulk_import.py for why a
    large import must not fan out into an email/push storm."""
    data = await _read_upload_with_limit(file, bulk_import.MAX_IMPORT_FILE_BYTES)
    try:
        outcome = bulk_import.run_import(db, actor, file.filename or "", data)
    except bulk_import.BulkImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BulkImportResult(
        created=outcome.created,
        failed=outcome.failed,
        created_ids=outcome.created_ids,
        errors=[BulkImportRowIssue(**e) for e in outcome.errors],
        warnings=[BulkImportRowWarning(**w) for w in outcome.warnings],
        errors_truncated=outcome.errors_truncated,
        warnings_truncated=outcome.warnings_truncated,
        message=outcome.message,
    )


# Caps for the detail view. Older history is still reachable via the dedicated
# /activity and /comments endpoints.
_DETAIL_COMMENTS_MAX = 500
_DETAIL_ACTIVITIES_MAX = 500
_DETAIL_ATTACHMENTS_MAX = 500


@router.get("/{bug_id}", responses=NOT_FOUND_404)
def get_bug(
    bug_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BugDetail:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # A bug outside the actor's projects reads as not-found.
    _assert_bug_accessible(accessible_project_ids(db, user), bug)

    # Bounded queries instead of the unbounded relationship collections.
    # Newest first, matching the Bug.comments / Bug.activities ordering.
    recent_comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(_DETAIL_COMMENTS_MAX)
    ).all())
    recent_activities = list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(_DETAIL_ACTIVITIES_MAX)
    ).all())

    # Load all attachments (bug-level and comment-level), grouped by comment.
    all_atts = list(db.scalars(
        select(Attachment).where(Attachment.bug_id == bug_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        .limit(_DETAIL_ATTACHMENTS_MAX)
    ).all())
    by_comment: dict[int, list[Attachment]] = {}
    bug_level: list[Attachment] = []
    for a in all_atts:
        if a.comment_id is None:
            bug_level.append(a)
        else:
            by_comment.setdefault(a.comment_id, []).append(a)

    payload = _bug_to_out_dict(
        bug,
        # Use an aggregate count, not len(all_atts): that list is capped at
        # _DETAIL_ATTACHMENTS_MAX and would under-report on busy items.
        _attachment_count(db, bug_id),
        can_edit_bug(user, bug.reporter_id, [a.id for a in bug.assignees],
                     item_type=_item_type(bug)),
    )
    payload["attachments"] = [_attachment_brief(a) for a in bug_level]
    payload["links"] = _bug_links(db, bug_id)
    payload["comments"] = []
    for c in recent_comments:
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
        for a in recent_activities
    ]
    return BugDetail.model_validate(payload)


@router.post("", status_code=status.HTTP_201_CREATED, responses=BAD_REQUEST_FORBIDDEN_422)
def create_bug(
    payload: BugCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BugOut:
    accessible = accessible_project_ids(db, actor)
    _assert_project_accessible(db, accessible, payload.project_id)
    # Agile hierarchy types (Epic/Story/Sub-task) are only creatable through
    # /api/agile/work-items, which runs hierarchy validation and rank
    # assignment; this keeps the legacy Bug/Requirement/Task path unchanged.
    if payload.item_type not in LEGACY_ITEM_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"{payload.item_type} items must be created via /api/agile/work-items",
        )
    # Only admins/managers may create Tasks/Requirements. This mirrors the
    # can_edit_bug rule so a user can't create an item they couldn't edit.
    if not can_edit_bug(actor, actor.id, [], item_type=payload.item_type):
        raise HTTPException(
            status_code=403,
            detail=f"Only admins or managers can create a {payload.item_type.lower()}.",
        )

    # Only admins/managers can set an explicit reporter; everyone else files as
    # themselves.
    if payload.reporter_id is not None and payload.reporter_id != actor.id:
        if actor.role not in ("admin", "manager"):
            raise HTTPException(
                status_code=403,
                detail="You can only file items as yourself",
            )
        reporter = _resolve_user(db, payload.reporter_id, actor.org_id)
    else:
        reporter = actor

    assignees = _resolve_users(db, payload.assignee_ids, actor.org_id)
    _reject_inactive([reporter, *assignees])

    if payload.event_id is not None:
        _assert_event_accessible(db, actor, accessible, payload.event_id)

    bug = Bug(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        reporter_id=reporter.id,
        item_type=payload.item_type,
        status=payload.status,
        priority=payload.priority,
        environment=payload.environment,
        due_date=payload.due_date,
        event_id=payload.event_id,
    )
    # The flush ranks it at the bottom of its project's backlog
    # (app/agile/integrity.py), so it is in the Agile backlog straight away.
    bug.assignees = list(assignees)
    db.add(bug)
    db.flush()
    _log(
        db, bug.id, actor, "bug_created",
        f"{bug.item_type} #{bug.id} '{bug.title}' created with status '{bug.status}'.",
    )
    if assignees:
        names = ", ".join(a.name for a in assignees)
        _log(
            db, bug.id, actor, "assignees_added",
            f"Bug #{bug.id} '{bug.title}' assigned to: {names}",
        )
    # Write in-app notifications on this session so they commit with the bug.
    _itype = _item_type(bug).lower()
    assignee_ids = [a.id for a in assignees]
    # No exclude here: a self-assignment still notifies the actor.
    notification_service.notify(
        db, assignee_ids, kind="assigned", background=background,
        title=f"Assigned to {_itype} #{bug.id}",
        body=f"{actor.name} assigned you to “{bug.title}”.",
        bug_id=bug.id, actor_name=actor.name,
    )
    if reporter.id != actor.id and reporter.id not in assignee_ids:
        notification_service.notify(
            db, [reporter.id], kind="reported", background=background,
            title=f"You're the reporter on {_itype} #{bug.id}",
            body=f"{actor.name} filed “{bug.title}” with you as reporter.",
            bug_id=bug.id, actor_name=actor.name,
        )
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
    background.add_task(
        deliver_event, actor.org_id, "bug.created",
        {"bug": _bug_to_out_dict(fresh), "actor": _user_brief(actor)},
    )

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, 0,
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees],
                     item_type=fresh.item_type),
    ))


_UPDATE_TRACKED_FIELDS = [
    "item_type", "status", "priority", "environment", "project_id",
    "due_date", "title", "description", "event_id", "sprint_id",
]


def _validate_update_authorization(bug: Bug, actor: User) -> None:
    if not can_edit_bug(actor, bug.reporter_id, [a.id for a in bug.assignees],
                        item_type=_item_type(bug)):
        # Regular users cannot edit Tasks or Requirements.
        noun = _item_type(bug).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to edit this {noun}.",
        )


# Link types where the reverse would contradict the forward ("A blocks B" and
# "B blocks A" can't both be true). `relates` is symmetric so it's excluded.
_DIRECTIONAL_LINK_TYPES = {"blocks", "duplicate"}


def _require_can_edit_link_endpoint(item: Bug, actor: User) -> None:
    """Require edit rights on both link endpoints. Without this, a user who can
    edit a Bug could attach or detach links on a Task/Requirement they can't
    otherwise touch."""
    if not can_edit_bug(actor, item.reporter_id, [a.id for a in item.assignees],
                        item_type=_item_type(item)):
        noun = _item_type(item).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to change links on this {noun}.",
        )


# Bound the cycle-detection BFS so a pathological graph can't make a single
# link insert run forever.
_LINK_CYCLE_MAX_NODES = 10_000


def _directional_link_reaches(db: Session, start_id: int, goal_id: int, link_type: str) -> bool:
    """True if ``goal_id`` is reachable from ``start_id`` by following forward
    edges of ``link_type`` (bounded BFS). Used to detect cycles before insert."""
    seen = {start_id}
    frontier = [start_id]
    while frontier and len(seen) < _LINK_CYCLE_MAX_NODES:
        rows = db.execute(
            select(BugLink.target_bug_id).where(
                BugLink.source_bug_id.in_(frontier),
                BugLink.link_type == link_type,
            )
        ).all()
        nxt = []
        for (tid,) in rows:
            if tid == goal_id:
                return True
            if tid not in seen:
                seen.add(tid)
                nxt.append(tid)
        frontier = nxt
    return False


def _reject_inverse_directional_link(
    db: Session, source_id: int, target: Bug, link_type: str,
) -> None:
    """Block a directional link A->B that would create a cycle.

    If the target can already reach the source by forward edges of the same
    type, adding source->target closes a loop (A transitively blocked by
    itself). This catches both the direct inverse B->A and transitive cycles."""
    if link_type not in _DIRECTIONAL_LINK_TYPES:
        return
    if _directional_link_reaches(db, target.id, source_id, link_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"This would create a {link_type} cycle — #{target.id} already "
                f"{_link_phrase(link_type, 'outgoing')} #{source_id} "
                f"(directly or transitively)."
            ),
        )


def _authorize_item_type_change(fields: dict, bug: Bug, actor: User) -> None:
    """Guard against over-posting an item_type change. The earlier auth check
    only saw the current type, so without this a regular user could PUT
    {"item_type": "Task"} to convert a Bug into a type they can't edit.
    Same-table conversions among Bug/Requirement/Task/Story keep the row id and
    common fields, validate the status against the target type, and never delete
    a remote branch. A Story with an Active feature branch cannot convert away
    from Story (Deleted history does not block); converting a non-Story into a
    Story never touches existing branch rows.
    """
    new_type = fields.get("item_type")
    if new_type is None or new_type == _item_type(bug):
        return
    if new_type not in _CONVERTIBLE_ITEM_TYPES or _item_type(bug) not in _CONVERTIBLE_ITEM_TYPES:
        raise _conversion_refused(
            422,
            _CONVERSION_NOT_SUPPORTED_CODE,
            f"Cannot convert {_item_type(bug)} to {new_type} via /api/bugs; use /api/agile/work-items",
        )
    if not can_edit_bug(actor, bug.reporter_id,
                        [a.id for a in bug.assignees], item_type=new_type):
        raise HTTPException(
            status_code=403,
            detail=(
                "You don't have permission to convert this item to a "
                f"{new_type.lower()}."
            ),
        )


def _assert_no_active_branch_for_conversion(db: Session, bug: Bug) -> None:
    """An Active feature branch blocks moving a Story to another type.

    Deleted branch history never blocks. Nothing is deleted remotely: the
    operator removes the branch through the branch endpoint first.
    """
    row = db.scalar(
        select(WorkItemBranch.id)
        .where(
            WorkItemBranch.work_item_type == _item_type(bug),
            WorkItemBranch.work_item_id == bug.id,
            WorkItemBranch.status == "Active",
        )
        .limit(1)
    )
    if row is not None:
        raise _conversion_refused(
            409,
            _CONVERSION_BLOCKED_CODE,
            "This User Story has an Active feature branch and cannot be converted "
            "to another type. Delete the branch first.",
        )


def _conversion_change(fields: dict, bug: Bug) -> tuple[str, str] | None:
    """(from_type, to_type) when this payload reclassifies the item, else None."""
    new_type = fields.get("item_type")
    if new_type is None or new_type == _item_type(bug):
        return None
    return _item_type(bug), new_type


def _audit_conversion(db: Session, bug: Bug, actor: User, conversion: tuple[str, str], action: str, detail: str = "") -> None:
    """Append one conversion audit row on the caller's session (caller commits)."""
    old_type, new_type = conversion
    suffix = f" — {detail}" if detail else ""
    _log(
        db, bug.id, actor, action,
        f"#{bug.id} '{bug.title}' — item_type: '{old_type}' → '{new_type}'{suffix}",
    )


def _audit_conversion_failure(db: Session, bug: Bug, actor: User, conversion: tuple[str, str], exc: Exception) -> None:
    """Record a refused/failed conversion on a fresh transaction.

    The request's own work has been rolled back, so this is committed
    separately; a failure to audit must never mask the original error. Only the
    human-readable refusal message is stored — never a credential or payload.
    """
    detail = str(getattr(exc, "detail", "") or exc)[:300]
    try:
        db.rollback()
        _audit_conversion(db, bug, actor, conversion, "conversion_failed", detail)
        db.commit()
    except Exception:  # pragma: no cover - audit must never break the response
        db.rollback()


def _preserve_display_id_on_type_change(fields: dict, bug: Bug) -> None:
    """Freeze the current user-facing id before a reclassification.

    The display id is assigned once. Converting a Story into a Task keeps
    "USRSTR-<id>", so existing references and the branch rows keyed to the
    original type + numeric id stay valid.
    """
    new_type = fields.get("item_type")
    if new_type is None or new_type == _item_type(bug):
        return
    if not (getattr(bug, "display_id", None) or "").strip():
        bug.display_id = display_id_for(_item_type(bug), bug.id)


def _normalize_update_event_id(fields: dict, db: Session, actor: User, accessible) -> None:
    if "event_id" in fields and fields["event_id"]:
        _assert_event_accessible(db, actor, accessible, fields["event_id"])
    if "event_id" in fields and fields["event_id"] == 0:
        # Treat 0 as a "clear event" signal for clients that can't send null.
        fields["event_id"] = None


def _validate_update_status(fields: dict, bug: Bug) -> None:
    """Per-type status validation. Pydantic only checks the union of all
    allowed statuses; this enforces the per-type subset.

    When item_type changes, the effective status is re-validated against the
    new type, so converting a Resolved Bug into a Task can't leave it stuck on
    a Bug-only status. An unchanged status is tolerated only when the type
    isn't also changing."""
    new_type = fields.get("item_type")
    type_changing = new_type is not None and new_type != _item_type(bug)
    has_new_status = "status" in fields and fields["status"] is not None
    if not has_new_status and not type_changing:
        return
    effective_type = new_type or _item_type(bug)
    effective_status = fields["status"] if has_new_status else bug.status
    allowed_for_type = statuses_for_type(effective_type)
    if effective_status in allowed_for_type:
        return
    # Tolerate an unchanged status only when the type isn't changing; a type
    # conversion must land on a valid status for the target type.
    if not type_changing and has_new_status and effective_status == bug.status:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            f"Status '{effective_status}' is not valid for "
            f"{effective_type}. Allowed: {', '.join(allowed_for_type)}"
        ),
    )


def _validate_update_payload(
    fields: dict, bug: Bug, db: Session, actor: User, accessible,
) -> None:
    if "project_id" in fields and fields["project_id"] is not None:
        _assert_project_accessible(db, accessible, fields["project_id"])
    _normalize_update_event_id(fields, db, actor, accessible)
    _validate_update_status(fields, bug)
    if "sprint_id" in fields and fields["sprint_id"] is not None:
        # Validate against the project the item will end up in.
        target_project_id = fields.get("project_id") or bug.project_id
        sprint = db.get(Sprint, fields["sprint_id"])
        if sprint is None or sprint.project_id != target_project_id or sprint.state in ("closed", "cancelled"):
            raise HTTPException(status_code=422, detail="sprint_id must reference an open Sprint in this project")


def _is_project_move(fields: dict, bug: Bug) -> bool:
    new_pid = fields.get("project_id")
    return new_pid is not None and new_pid != bug.project_id


def _detach_for_project_move(db: Session, bug: Bug, fields: dict, actor: User) -> None:
    """Sprints, backlog ranks, epic links, components and labels are
    project-scoped, so an issue moving to another project leaves them behind;
    its Sub-tasks move with it
    (app/agile/integrity.py). An Epic that still has issues, or a Sub-task
    (which belongs to its parent's project), cannot move on its own."""
    if not _is_project_move(fields, bug):
        return
    if is_subtask(bug.item_type):
        raise HTTPException(
            status_code=409,
            detail="A Sub-task stays in its parent's project; move the parent issue instead.",
        )
    if is_epic(bug.item_type) and db.scalar(select(Bug.id).where(Bug.epic_id == bug.id).limit(1)):
        raise HTTPException(
            status_code=409,
            detail="This Epic still has issues; move or unlink them before changing its project.",
        )
    if bug.sprint_id is not None:
        sprint = db.get(Sprint, bug.sprint_id)
        if sprint is not None and sprint.state in ("future", "active"):
            # Taking an issue out of a planned or running sprint is backlog
            # management, whichever field the request changes.
            require_permission(actor, MANAGE_BACKLOG)
            sprints_svc.remove_item_from_sprint(db, sprint, bug.id, actor)
        bug.sprint_id = None
    bug.parent_id = bug.epic_id = None
    # Components and labels are per project: they stay behind too.
    from app.agile.taxonomy import detach_project_taxonomy

    subtask_ids = list(db.scalars(select(Bug.id).where(Bug.parent_id == bug.id)).all())
    detach_project_taxonomy(db, [bug.id, *subtask_ids])


def _apply_sprint_change(db: Session, bug: Bug, new_sprint_id, actor: User) -> None:
    """Move the issue between sprints through the sprint service, so its rank
    follows it into the right list and an active sprint records the change.
    Sub-tasks follow their parent and Epics are never planned: both refused."""
    if new_sprint_id == bug.sprint_id:
        return
    target = db.get(Sprint, new_sprint_id) if new_sprint_id is not None else None
    try:
        sprints_svc.move_items(db, bug.project_id, [bug.id], target, actor)
    except sprints_svc.SprintLifecycleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _stale_edit_conflict() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=("This item was changed by someone else since you opened it. "
                "Reload to see the latest, then reapply your change."),
        headers={"X-Error-Code": _VERSION_CONFLICT_CODE},
    )


def _reject_if_updated_at_drifted(
    expected_updated_at: str, current_updated: datetime,
) -> None:
    """Timestamp path of the optimistic-concurrency check. Unparseable input
    returns 400 so a client that opts in gets a clear signal. current_updated
    comes from a NOT NULL column, so it's always present."""
    try:
        seen = datetime.fromisoformat(str(expected_updated_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="expected_updated_at is not a valid ISO-8601 timestamp.",
        ) from exc
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    current = current_updated
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    # updated_at is whole-second resolution, so compare at second granularity.
    if int(seen.timestamp()) != int(current.timestamp()):
        raise _stale_edit_conflict()


def _enforce_optimistic_concurrency(
    db: Session, bug_id: int,
    expected_version: Optional[int],
    expected_updated_at: Optional[str],
) -> None:
    """Opt-in optimistic concurrency check.

    Locks the row (FOR UPDATE) so concurrent writers serialize, then rejects
    with 409 when the client's last-seen version (preferred, sub-second safe)
    or updated_at (whole-second) no longer matches. Does nothing when neither
    field is sent, preserving last-write-wins behavior.
    """
    if expected_version is None and not expected_updated_at:
        return
    locked = db.execute(
        select(Bug.version, Bug.updated_at)
        .where(Bug.id == bug_id)
        .with_for_update()
    ).first()
    if locked is None:
        # Row vanished under a concurrent delete; the caller's load will 404.
        return
    current_version, current_updated = locked
    if expected_version is not None:
        if int(expected_version) != int(current_version):
            raise _stale_edit_conflict()
        return
    _reject_if_updated_at_drifted(expected_updated_at, current_updated)


def _compute_tracked_changes(bug: Bug, fields: dict) -> list[tuple[str, str, str]]:
    """Return (field, old, new) for each tracked field that changed."""
    changes: list[tuple[str, str, str]] = []
    for f in _UPDATE_TRACKED_FIELDS:
        if f in fields and getattr(bug, f) != fields[f]:
            changes.append((f, str(getattr(bug, f) or ""), str(fields[f] or "")))
    return changes


def _apply_reporter_change(bug: Bug, db: Session, org_id: int, new_reporter_id: Optional[int],
                           changes: list[tuple[str, str, str]]) -> None:
    """Swap the reporter and record the change.
    Only called when the reporter id actually changed (see
    reporter_actually_changes), so this must always append to `changes` — even
    when two different users share a display name — or _persist_update treats
    an empty `changes` list as a no-op and rolls back the FK write along with it.
    Permission is checked by the caller.
    """
    old_reporter = bug.reporter
    old_reporter_label = old_reporter.name if old_reporter else "—"
    new_reporter = None
    if new_reporter_id is None:
        bug.reporter_id = None
        new_reporter_label = "—"
    else:
        new_reporter = _resolve_user(db, new_reporter_id, org_id)
        _reject_inactive([new_reporter], "reporter")
        bug.reporter_id = new_reporter.id
        new_reporter_label = new_reporter.name if new_reporter else "—"
    if old_reporter_label == new_reporter_label and old_reporter_label != "—":
        # Same display name, different person: disambiguate with email so the
        # audit entry still shows a real change.
        old_reporter_label += f" ({old_reporter.email})" if old_reporter else ""
        new_reporter_label += f" ({new_reporter.email})" if new_reporter else ""
    changes.append(("reporter", old_reporter_label, new_reporter_label))


def _apply_assignee_diff(bug: Bug, db: Session, org_id: int, assignee_ids: Optional[list[int]],
                        changes: list[tuple[str, str, str]]) -> tuple[list[User], list[User]]:
    """Diff and re-bind assignees. Returns (added, removed) so the caller
    can notify both sides (removal is as worth notifying as a new assignment)."""
    if assignee_ids is None:
        return [], []
    new_users = _resolve_users(db, assignee_ids, org_id)
    old_users = list(bug.assignees)
    old_ids = {a.id for a in old_users}
    new_ids = {u.id for u in new_users}
    added_ids = new_ids - old_ids
    removed_ids = old_ids - new_ids
    if not (added_ids or removed_ids):
        return [], []
    added = [u for u in new_users if u.id in added_ids]
    removed = [u for u in old_users if u.id in removed_ids]
    _reject_inactive(added)
    old_names = sorted(a.name for a in old_users)
    new_names = sorted(u.name for u in new_users)
    changes.append((
        "assignees",
        ", ".join(old_names) or "(none)",
        ", ".join(new_names) or "(none)",
    ))
    bug.assignees = new_users
    return added, removed


def _persist_update(db: Session, bug: Bug, actor: User,
                    changes: list[tuple[str, str, str]]) -> None:
    """Commit if there are tracked changes; roll back otherwise so a no-op
    PUT doesn't bump updated_at."""
    if not changes:
        db.rollback()
        return
    prefix = f"#{bug.id} '{bug.title}' — "
    for field, old, new in changes:
        _log(
            db, bug.id, actor, f"{field}_changed",
            f"{prefix}{field}: '{old}' → '{new}'",
        )
    # Bump the version so a concurrent writer with a stale copy gets a 409 at
    # sub-second resolution. version is NOT NULL (server_default 1).
    bug.version = bug.version + 1
    db.commit()


def _stage_update_notifications(
    db: Session, bug: Bug,
    changes: list[tuple[str, str, str]],
    newly_assigned: list[User], newly_removed: list[User],
    actor: User, background: BackgroundTasks,
) -> None:
    """Stage in-app notification rows on the current session (no commit).
    They commit atomically with the change in _persist_update, so a crash
    between two commits cannot persist the change without its notifications."""
    new_ids = {u.id for u in newly_assigned}
    itype = _item_type(bug).lower()
    # _apply_reporter_change and _apply_assignee_diff always append to `changes`
    # whenever they return a non-empty diff, so given the caller's guard (`if
    # changes or newly_assigned or newly_removed`), changes is never empty here.
    if changes:  # pragma: no branch
        recipients = [
            uid for uid in [bug.reporter_id, *[a.id for a in bug.assignees]]
            if uid not in new_ids
        ]
        notification_service.notify(
            db, recipients, kind="updated", background=background,
            title=f"{itype.capitalize()} #{bug.id} updated",
            body=f"{actor.name} changed " + ", ".join(f for f, _, _ in changes) + ".",
            bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
        )
    if newly_assigned:
        # No exclude: a self-assignment still notifies the actor.
        notification_service.notify(
            db, list(new_ids), kind="assigned", background=background,
            title=f"Assigned to {itype} #{bug.id}",
            body=f"{actor.name} assigned you to “{bug.title}”.",
            bug_id=bug.id, actor_name=actor.name,
        )
    if newly_removed:
        notification_service.notify(
            db, [u.id for u in newly_removed], kind="updated", background=background,
            title=f"Unassigned from {itype} #{bug.id}",
            body=f"{actor.name} removed you from “{bug.title}”.",
            bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
        )


def _schedule_update_notifications(background: BackgroundTasks, snap: BugSnapshot,
                                   changes: list[tuple[str, str, str]],
                                   newly_assigned: list[User], actor: User,
                                   fresh: Bug) -> None:
    if changes:
        background.add_task(
            notify_bug_updated, snap, list(changes), actor.name, actor.id,
        )
        background.add_task(
            deliver_event, actor.org_id, "bug.updated",
            {"bug": _bug_to_out_dict(fresh),
             "changes": [{"field": f, "old": o, "new": n} for f, o, n in changes],
             "actor": _user_brief(actor)},
        )
    if newly_assigned:
        background.add_task(
            notify_assignment, snap,
            tuple(UserSnapshot(id=u.id, name=u.name, email=u.email) for u in newly_assigned),
            actor.name,
        )


@router.put("/{bug_id}", responses=BAD_REQUEST_FORBIDDEN_NOT_FOUND_422)
def update_bug(
    bug_id: int,
    payload: BugUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BugOut:
    fields = payload.model_dump(exclude_unset=True)
    expected_version = fields.pop("expected_version", None)
    expected_updated_at = fields.pop("expected_updated_at", None)

    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Out-of-scope items return 404, not 403, so the restriction doesn't leak
    # which bugs or projects exist.
    accessible = accessible_project_ids(db, actor)
    _assert_bug_accessible(accessible, bug)
    # Authorize before the optimistic-concurrency check. Checking the version
    # first would let an unauthorized caller probe a protected item's version
    # via 409-vs-403 differential responses.
    _validate_update_authorization(bug, actor)
    _enforce_optimistic_concurrency(db, bug_id, expected_version, expected_updated_at)
    _validate_update_payload(fields, bug, db, actor, accessible)
    # Reclassification: same table, same row id, common fields and relations
    # preserved. The guards run before any attribute is applied so a refused
    # conversion leaves the item completely untouched.
    conversion = _conversion_change(fields, bug)
    if conversion is not None:
        try:
            _assert_no_active_branch_for_conversion(db, bug)
            _authorize_item_type_change(fields, bug, actor)
        except HTTPException as exc:
            _audit_conversion_failure(db, bug, actor, conversion, exc)
            raise
    else:
        _authorize_item_type_change(fields, bug, actor)
    if conversion is not None:
        _audit_conversion(db, bug, actor, conversion, "conversion_requested")
    assignee_ids = fields.pop("assignee_ids", None)
    has_reporter_in_payload = "reporter_id" in fields
    new_reporter_id = fields.pop("reporter_id", None)

    reporter_actually_changes = (
        has_reporter_in_payload and new_reporter_id != bug.reporter_id
    )
    if reporter_actually_changes and actor.role not in ("admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Only admins or managers can change the reporter",
        )
    # A reclassification must not rename an item people already reference, so
    # freeze the pre-conversion id before the new item_type is applied.
    _preserve_display_id_on_type_change(fields, bug)
    changes = _compute_tracked_changes(bug, fields)
    old_status = bug.status
    if "sprint_id" in fields and fields["sprint_id"] != bug.sprint_id:
        # Planning work into or out of a sprint is backlog management, the
        # same permission the board's move endpoint requires.
        require_permission(actor, MANAGE_BACKLOG)
    _detach_for_project_move(db, bug, fields, actor)
    sprint_requested = "sprint_id" in fields
    new_sprint_id = fields.pop("sprint_id", None)
    for key, value in fields.items():
        setattr(bug, key, value)
    # A project move re-ranks the issue at the bottom of its new project's
    # backlog at flush (app/agile/integrity.py).
    if sprint_requested:
        _apply_sprint_change(db, bug, new_sprint_id, actor)
    record_status_change(db, bug, actor, old_status)

    if reporter_actually_changes:
        _apply_reporter_change(bug, db, actor.org_id, new_reporter_id, changes)
    newly_assigned, newly_removed = _apply_assignee_diff(
        bug, db, actor.org_id, assignee_ids, changes,
    )

    if changes or newly_assigned or newly_removed:
        _stage_update_notifications(
            db, bug, changes, newly_assigned, newly_removed, actor, background,
        )
    if conversion is not None:
        _audit_conversion(db, bug, actor, conversion, "conversion_succeeded")
    try:
        _persist_update(db, bug, actor, changes)
    except Exception as exc:
        # Transaction rollback: the item keeps its original type and the
        # refusal is audited on its own transaction.
        if conversion is not None:
            _audit_conversion_failure(db, bug, actor, conversion, exc)
        raise
    fresh = db.scalar(_eager_bug().where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)
    _schedule_update_notifications(background, snap, changes, newly_assigned, actor, fresh)

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, _attachment_count(db, bug_id),
        can_edit_bug(actor, fresh.reporter_id, [a.id for a in fresh.assignees],
                     item_type=fresh.item_type),
    ))


# Shared stakeholder notification for delete, attachments, comment edits, and
# links. The create/update paths build their own tailored notifications; these
# secondary operations all go through one helper so every mutation reaches the
# reporter + assignees and feeds the email digest.
def _notify_item_stakeholders(
    db: Session, bug: Bug, actor: User, *, kind: str, title: str, body: str,
    background: "BackgroundTasks | None" = None, link_bug: bool = True,
) -> None:
    """Notify a work item's reporter and assignees, excluding the actor.

    Rows are written on the caller's session; the caller commits.

    Pass link_bug=False when deleting: the bug_id FK cascades on delete, so a
    notification that deep-links to the bug would vanish before the digest
    could send it.
    """
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind=kind, background=background, title=title, body=body,
        bug_id=bug.id if link_bug else None,
        actor_name=actor.name, exclude=actor.id,
    )


@router.delete("/{bug_id}", responses=NOT_FOUND_FORBIDDEN_403)
def delete_bug(
    bug_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _assert_bug_accessible(accessible_project_ids(db, actor), bug)
    # Deletion is admin-only; managers can edit but not delete.
    if not can_delete_bug(actor, item_type=_item_type(bug)):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete items.",
        )
    title = bug.title
    itype = _item_type(bug)
    # Notify before deleting; link_bug=False because the FK cascade would
    # remove a notification pointing at the now-deleted bug.
    _notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background, link_bug=False,
        title=f"{itype} #{bug_id} deleted",
        body=f"{actor.name} deleted “{title}”.",
    )
    subtasks = _delete_item(db, bug, actor)
    db.commit()
    background.add_task(
        deliver_event, actor.org_id, "bug.deleted",
        {"bug": {"id": bug_id, "title": title, "item_type": itype}, "actor": _user_brief(actor)},
    )
    suffix = f" with {subtasks} sub-task(s)" if subtasks else ""
    return {"message": f"{itype} deleted{suffix}"}


@router.get("/{bug_id}/comments", response_model=list[CommentOut], responses=NOT_FOUND_404)
def list_comments(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    _load_accessible_bug(db, bug_id, accessible_project_ids(db, _user))
    # Newest first, matching the detail endpoint's ordering.
    comments = list(db.scalars(
        select(Comment).where(Comment.bug_id == bug_id)
        .order_by(Comment.created_at.desc(), Comment.id.desc())
        .limit(_DETAIL_COMMENTS_MAX)
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


@router.post("/{bug_id}/comments", response_model=CommentOut, status_code=status.HTTP_201_CREATED, responses=FORBIDDEN_NOT_FOUND_429)
def add_comment(
    bug_id: int,
    payload: CommentIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    author: User = Depends(get_current_user),
) -> dict:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _assert_bug_accessible(accessible_project_ids(db, author), bug)
    # Same per-type policy as edit/link: regular users can comment on Bugs but
    # not on Tasks/Requirements.
    _validate_update_authorization(bug, author)
    # Rate-limit after auth so probing/forbidden requests don't burn the budget.
    _check_comment_rate(author.id)

    c = Comment(
        bug_id=bug_id,
        author_user_id=author.id,
        author_name=author.name,
        body=payload.body,
    )
    db.add(c)
    db.flush()
    _log(db, bug_id, author, "comment_added",
         f"#{bug.id} '{bug.title}' — comment by {author.name}: {payload.body[:80]}")
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind="comment", background=background,
        title=f"New comment on #{bug.id}",
        body=f"{author.name} commented on “{bug.title}”.",
        bug_id=bug.id, actor_name=author.name, exclude=author.id,
    )
    db.commit()
    db.refresh(c)

    snap = _bug_snapshot(bug)
    background.add_task(
        notify_comment_added, snap, author.name, author.id, payload.body,
    )
    background.add_task(
        deliver_event, author.org_id, "comment.added",
        {"bug_id": bug_id, "comment_id": c.id, "body": c.body, "author": _user_brief(author)},
    )
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at, "attachments": [],
    }


async def _read_upload_with_limit(file: UploadFile, limit: int) -> bytes:
    """Stream the upload in chunks, aborting early if the size limit is
    exceeded so the body is never fully buffered."""
    buf = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                status_code=413,
                detail=(
                    "This file is too large. The largest file you can attach "
                    f"is {limit // (1024 * 1024)} MB."
                ),
            )
    return bytes(buf)


@router.post("/{bug_id}/attachments", response_model=AttachmentBrief, status_code=status.HTTP_201_CREATED, responses=BAD_REQUEST_FORBIDDEN_NOT_FOUND_PAYLOAD_TOO_LARGE_429)
async def upload_attachment(
    bug_id: int,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    comment_id: Optional[int] = Form(default=None),
    db: Session = Depends(get_db),
    uploader: User = Depends(get_current_user),
) -> dict:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    _assert_bug_accessible(accessible_project_ids(db, uploader), bug)
    # Same per-type policy as edit/link: regular users can attach to Bugs but
    # not to Tasks/Requirements they can't edit.
    _validate_update_authorization(bug, uploader)
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    # Check rate limit after auth/404 so probing requests don't burn the budget,
    # but before reading the body.
    _check_upload_rate(uploader.id)

    # Refuse dangerous extensions before streaming the body.
    bad_ext = _dangerous_upload_ext(file.filename or "")
    if bad_ext is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Files of type .{bad_ext} can't be attached for security reasons.",
        )

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="This file is empty, so there's nothing to attach.")

    # Strip EXIF/GPS/XMP/ICC metadata from raster images. No-op for non-images;
    # fails open on errors so an exotic format doesn't block the upload.
    data = strip_image_metadata(data, file.content_type)
    # Re-check the cap: stripping and re-encoding could grow the file slightly.
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "This file is too large. The largest file you can attach "
                f"is {MAX_FILE_BYTES // (1024 * 1024)} MB."
            ),
        )

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
        db, bug_id, uploader, "attachment_added",
        f"{uploader.name} uploaded '{att.filename}' ({len(data)} bytes)"
        + (f" on comment #{comment_id}" if comment_id else ""),
        entity_type="attachment", entity_id=att.id,
    )
    _notify_item_stakeholders(
        db, bug, uploader, kind="updated", background=background,
        title=f"Attachment added on #{bug_id}",
        body=f"{uploader.name} attached “{att.filename}” to “{bug.title}”.",
    )
    db.commit()
    db.refresh(att)
    return _attachment_brief(att)


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int) -> Optional[tuple[int, int]]:
    """Parse a single-range ``Range: bytes=start-end`` header.

    Returns an inclusive (start, end) span clamped to [0, size-1], or None
    when the header is absent, malformed, unsatisfiable, or multi-range (the
    caller then serves the full body). Browsers need 206 replies to seek
    <video>: without them, setting currentTime snaps back to 0.
    """
    if not header or size <= 0:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None  # malformed or multi-range; serve whole body
    start_s, end_s = m.group(1), m.group(2)
    if not start_s and not end_s:
        return None
    if not start_s:
        # suffix range: last N bytes
        length = min(int(end_s), size)
        start, end = size - length, size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return None  # unsatisfiable; fall back to a full 200
    return start, end


@router.get("/{bug_id}/attachments/{att_id}/download", response_class=Response,
            responses={**ATTACHMENT_FILE_200, **NOT_FOUND_404})
def download_attachment(
    bug_id: int, att_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    # Fetch metadata without the BLOB column so a video seek (many small Range
    # requests) doesn't repeatedly load up to 50 MB just to slice a few bytes.
    meta = db.execute(
        select(
            Attachment.bug_id, Attachment.filename,
            Attachment.content_type, Attachment.size_bytes,
        ).where(Attachment.id == att_id)
    ).first()
    if meta is None or meta[0] != bug_id:
        raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)
    _assert_attachment_in_scope(db, accessible_project_ids(db, _user), bug_id)
    _att_bug_id, att_filename, att_content_type, att_size = meta

    # Active types (HTML, SVG, JS) can carry executable script; rendering them
    # inline would run it in our origin. Downgrade to octet-stream and force
    # Content-Disposition: attachment.
    ct_lower = (att_content_type or "").lower().split(";")[0].strip()
    is_active = ct_lower in _ACTIVE_CONTENT_TYPES
    safe_ct = _DEFAULT_MIME if is_active else (att_content_type or _DEFAULT_MIME)
    inline_ok = not is_active and (
        ct_lower in _INLINE_SAFE_TYPES
        or ct_lower.startswith(_INLINE_SAFE_PREFIXES)
    )
    disposition = "inline" if inline_ok else "attachment"

    safe_fname = _safe_filename_for_header(att_filename)
    # filename*= carries the Unicode form (RFC 5987); filename= is the ASCII fallback.
    cd = (
        f'{disposition}; filename="{safe_fname}"; '
        f"filename*=UTF-8''{quote(att_filename, safe='')}"
    )

    headers = {
        "Content-Disposition": cd,
        # Without Accept-Ranges, browsers don't attempt to seek <video>/<audio>
        # and currentTime resets to 0.
        "Accept-Ranges": "bytes",
        # Explicit Content-Encoding opts out of GZipMiddleware. Gzipping a 206
        # partial body corrupts the byte offsets the player uses to seek.
        "Content-Encoding": "identity",
        # Defense-in-depth for any future code path that might serve HTML.
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Frame-Options": "DENY",
        # Explicit Cache-Control so global middleware doesn't override it.
        "Cache-Control": "private, max-age=0, no-cache",
    }

    span = _parse_range(request.headers.get("range"), att_size)
    if span is not None:
        start, end = span
        length = end - start + 1
        # Slice in the DB (substr is 1-indexed) so only the requested bytes
        # cross the wire. Works on both Postgres (bytea) and SQLite (blob).
        raw = db.scalar(
            select(func.substr(Attachment.data, start + 1, length))
            .where(Attachment.id == att_id)
        )
        if raw is None:
            # Row was deleted between the metadata read and this slice.
            raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)
        chunk = bytes(raw)
        headers["Content-Range"] = f"bytes {start}-{end}/{att_size}"
        headers["Content-Length"] = str(len(chunk))
        return Response(
            content=chunk,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=safe_ct,
            headers=headers,
        )

    # Full download: fetch the whole column. Response sets Content-Length
    # accurately; StreamingResponse would drop an explicit value.
    data = db.scalar(select(Attachment.data).where(Attachment.id == att_id))
    return Response(content=bytes(data) if data is not None else b"",
                    media_type=safe_ct, headers=headers)


@router.delete("/{bug_id}/attachments/{att_id}", responses=NOT_FOUND_FORBIDDEN_403)
def delete_attachment(
    bug_id: int, att_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    a = db.get(Attachment, att_id)
    if a is None or a.bug_id != bug_id:
        raise HTTPException(status_code=404, detail=_DETAIL_ATTACHMENT_NOT_FOUND)
    # Admin-only: uploaders and managers cannot remove their own files.
    if not can_delete_attachment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete attachments.",
        )
    _assert_attachment_in_scope(db, accessible_project_ids(db, actor), bug_id)
    fname = a.filename
    db.delete(a)
    _log(
        db, bug_id, actor, "attachment_deleted",
        f"Deleted attachment '{fname}'",
        entity_type="attachment", entity_id=att_id,
    )
    # The FK from Attachment.bug_id guarantees the bug still exists here.
    bug = db.get(Bug, bug_id)
    _notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background,
        title=f"Attachment removed on #{bug_id}",
        body=f"{actor.name} deleted attachment “{fname}” from “{bug.title}”.",
    )
    db.commit()
    return {"message": "Attachment deleted"}


@router.put("/{bug_id}/comments/{comment_id}", response_model=CommentOut, responses=NOT_FOUND_FORBIDDEN_403)
def update_comment(
    bug_id: int, comment_id: int,
    payload: CommentIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Edit a comment's body. Admin only. The audit trail records who edited
    and the new body so an accidental rewrite is traceable."""
    if not can_edit_comment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can edit comments.",
        )
    c = db.get(Comment, comment_id)
    if c is None or c.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    _assert_bug_accessible(accessible_project_ids(db, actor), db.get(Bug, bug_id))
    c.body = payload.body
    db.flush()
    _log(
        db, bug_id, actor, "comment_edited",
        f"Comment #{c.id} edited by {actor.name}: {payload.body[:80]}",
        entity_type="comment", entity_id=c.id,
    )
    bug = db.get(Bug, bug_id)
    _notify_item_stakeholders(
        db, bug, actor, kind="comment", background=background,
        title=f"Comment edited on #{bug_id}",
        body=f"{actor.name} edited a comment on “{bug.title}”.",
    )
    db.commit()
    db.refresh(c)
    atts = list(db.scalars(
        select(Attachment).where(Attachment.comment_id == c.id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        .limit(_DETAIL_ATTACHMENTS_MAX)
    ).all())
    return {
        "id": c.id, "bug_id": c.bug_id,
        "author_user_id": c.author_user_id, "author_name": c.author_name,
        "body": c.body, "created_at": c.created_at,
        "attachments": [_attachment_brief(a) for a in atts],
    }


@router.delete("/{bug_id}/comments/{comment_id}", responses=NOT_FOUND_FORBIDDEN_403)
def delete_comment(
    bug_id: int, comment_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Delete a comment and its attachments (the FK on attachments cascades
    from comment_id). Admin only. The audit row keeps a preview of the deleted
    body for context."""
    if not can_delete_comment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete comments.",
        )
    c = db.get(Comment, comment_id)
    if c is None or c.bug_id != bug_id:
        raise HTTPException(status_code=404, detail="Comment not found")
    _assert_bug_accessible(accessible_project_ids(db, actor), db.get(Bug, bug_id))
    preview = (c.body or "")[:80]
    author_name = c.author_name
    db.delete(c)
    _log(
        db, bug_id, actor, "comment_deleted",
        f"Comment #{comment_id} by {author_name} deleted: {preview}",
        entity_type="comment", entity_id=comment_id,
    )
    bug = db.get(Bug, bug_id)
    _notify_item_stakeholders(
        db, bug, actor, kind="comment", background=background,
        title=f"Comment deleted on #{bug_id}",
        body=f"{actor.name} deleted a comment by {author_name} on “{bug.title}”.",
    )
    db.commit()
    return {"message": "Comment deleted"}


@router.get("/{bug_id}/activity", response_model=list[ActivityOut], responses=NOT_FOUND_404)
def list_activity(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[Activity]:
    _load_accessible_bug(db, bug_id, accessible_project_ids(db, _user))
    return list(db.scalars(
        select(Activity).where(Activity.bug_id == bug_id)
        .order_by(Activity.created_at.desc(), Activity.id.desc())
        .limit(_DETAIL_ACTIVITIES_MAX)
    ).all())


@router.get("/{bug_id}/links", response_model=list[BugLinkOut], responses=NOT_FOUND_404)
def list_links(
    bug_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[dict]:
    _load_accessible_bug(db, bug_id, accessible_project_ids(db, _user))
    return _bug_links(db, bug_id)


def _reload_link(db: Session, link_id: int) -> BugLink:
    link = db.scalar(
        select(BugLink)
        .options(selectinload(BugLink.source), selectinload(BugLink.target))
        .where(BugLink.id == link_id)
    )
    if link is None:
        # Deleted between the insert/find and this reload; 409 beats
        # AttributeError in _serialize_link.
        raise HTTPException(status_code=409,
                            detail="Link was modified concurrently. Please retry.")
    return link


def _insert_link_or_existing(db: Session, link: BugLink, refetch) -> tuple[BugLink, bool]:
    """Flush a new BugLink; on a unique-index race roll back and return the
    existing edge. Returns (edge, created).

    A concurrent identical request that wins the race raises IntegrityError
    here, which is converted to the existing edge rather than a 500. refetch
    re-runs the existence lookup after the rollback.
    """
    db.add(link)
    try:
        db.flush()
        return link, True
    except IntegrityError:
        db.rollback()
        existing = refetch()
        if existing is None:
            raise
        return existing, False


def _link_stakeholders(bug: Bug) -> list[int | None]:
    """Reporter + assignee ids for one end of a link."""
    return [bug.reporter_id, *[a.id for a in bug.assignees]]


def _notify_link_endpoints(
    db: Session, recipients: list[int | None], primary_id: int, actor: User, *,
    background: "BackgroundTasks | None", title: str, body: str,
) -> None:
    """Notify the reporters + assignees of both link endpoints. notify()
    deduplicates ids and drops the actor, so a user on both ends gets one
    notification. Deep-links to primary_id (the item the request was on)."""
    notification_service.notify(
        db, recipients, kind="updated", background=background,
        title=title, body=body, bug_id=primary_id,
        actor_name=actor.name, exclude=actor.id,
    )


@router.post("/{bug_id}/links", response_model=BugLinkOut, status_code=status.HTTP_201_CREATED, responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def add_link(
    bug_id: int,
    payload: BugLinkIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    """Create a directed link from this bug to another. Requires edit rights on
    the source item. Idempotent on (source, target, type): re-linking returns
    the existing edge."""
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Both ends must be in the actor's projects. The source returns 404 if
    # out-of-scope; the target returns "does not exist" for the same reason.
    accessible = accessible_project_ids(db, actor)
    _assert_bug_accessible(accessible, bug)
    _validate_update_authorization(bug, actor)
    if payload.target_bug_id == bug_id:
        raise HTTPException(status_code=400, detail="An item can't be linked to itself")
    target = db.scalar(_eager_bug().where(Bug.id == payload.target_bug_id))
    if target is None or not can_access_project(accessible, target.project_id):
        raise HTTPException(status_code=400, detail="Target item does not exist")
    _require_can_edit_link_endpoint(target, actor)
    _reject_inverse_directional_link(db, bug_id, target, payload.link_type)

    # 'relates' is symmetric: A->B and B->A are the same edge. The unique index
    # is direction-specific, so a reverse-relates would create a duplicate row
    # without this check. Return the existing edge idempotently instead.
    if payload.link_type == "relates":
        reverse = db.scalar(
            select(BugLink).where(
                BugLink.source_bug_id == target.id,
                BugLink.target_bug_id == bug_id,
                BugLink.link_type == "relates",
            )
        )
        if reverse is not None:
            return _serialize_link(_reload_link(db, reverse.id), bug_id)

    def _find_existing() -> Optional[BugLink]:
        return db.scalar(
            select(BugLink).where(
                BugLink.source_bug_id == bug_id,
                BugLink.target_bug_id == target.id,
                BugLink.link_type == payload.link_type,
            )
        )

    existing = _find_existing()
    if existing is not None:
        return _serialize_link(_reload_link(db, existing.id), bug_id)

    link, created = _insert_link_or_existing(
        db,
        BugLink(
            source_bug_id=bug_id, target_bug_id=target.id,
            link_type=payload.link_type, created_by_user_id=actor.id,
        ),
        _find_existing,
    )
    if not created:
        # A concurrent identical request won the race; return the existing edge.
        return _serialize_link(_reload_link(db, link.id), bug_id)
    _log(
        db, bug_id, actor, "link_added",
        f"#{bug_id} '{bug.title}' {_link_phrase(payload.link_type, 'outgoing')} "
        f"#{target.id} '{target.title}'",
    )
    _notify_link_endpoints(
        db, _link_stakeholders(bug) + _link_stakeholders(target), bug.id, actor,
        background=background,
        title=f"Link added on #{bug_id}",
        body=(
            f"{actor.name} linked “{bug.title}” "
            f"{_link_phrase(payload.link_type, 'outgoing')} "
            f"#{target.id} “{target.title}”."
        ),
    )
    db.commit()
    return _serialize_link(_reload_link(db, link.id), bug_id)


@router.delete("/{bug_id}/links/{link_id}", responses=NOT_FOUND_FORBIDDEN_403)
def remove_link(
    bug_id: int, link_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    """Remove a link. The path bug must be one of the two endpoints, and the
    caller must be able to edit it."""
    link = db.get(BugLink, link_id)
    if link is None or bug_id not in (link.source_bug_id, link.target_bug_id):
        raise HTTPException(status_code=404, detail="Link not found")
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    # Project scope: the path bug must be in the actor's projects.
    _assert_bug_accessible(accessible_project_ids(db, actor), bug)
    _validate_update_authorization(bug, actor)
    other_id = link.target_bug_id if link.source_bug_id == bug_id else link.source_bug_id
    other = db.scalar(_eager_bug().where(Bug.id == other_id))
    recipients = _link_stakeholders(bug)
    if other is not None:
        _require_can_edit_link_endpoint(other, actor)
        recipients += _link_stakeholders(other)
    db.delete(link)
    _log(db, bug_id, actor, "link_removed", f"Unlinked #{bug_id} ↔ #{other_id}")
    _notify_link_endpoints(
        db, recipients, bug.id, actor, background=background,
        title=f"Link removed on #{bug_id}",
        body=f"{actor.name} unlinked “{bug.title}” from #{other_id}.",
    )
    db.commit()
    return {"message": "Link removed"}


# Bulk actions. Each item goes through the same permission, audit, and
# notification path as its single-item endpoint. Items the caller can't touch
# are skipped so a mixed selection partially succeeds.
def _norm_or_400(value: Optional[str], allowed: list[str], label: str) -> str:
    try:
        return normalize_choice(value or "", allowed, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bulk_resolve_value(payload: BulkActionIn):
    """Validate and canonicalize the action's value once, so a bad value gives
    a single 400 for the whole request. Returns None for delete."""
    action = payload.action
    if action == "set_status":
        return _norm_or_400(payload.value, ALLOWED_STATUSES, "status")
    if action == "set_priority":
        return _norm_or_400(payload.value, ALLOWED_PRIORITIES, "priority")
    if action == "set_environment":
        return _norm_or_400(payload.value, ALLOWED_ENVIRONMENTS, "environment")
    return None  # delete


def _bulk_notify_update(db: Session, bug: Bug, actor: User, what: str,
                        background: BackgroundTasks) -> None:
    _itype = _item_type(bug).lower()
    notification_service.notify(
        db, [bug.reporter_id, *[a.id for a in bug.assignees]],
        kind="updated", background=background,
        title=f"{_itype.capitalize()} #{bug.id} updated",
        body=f"{actor.name} changed {what} (bulk).",
        bug_id=bug.id, actor_name=actor.name, exclude=actor.id,
    )


def _bulk_set_status(db: Session, bug: Bug, actor: User, value: str,
                     background: BackgroundTasks) -> str:
    # A status valid for Bug may be invalid for Task; skip if so.
    if value not in statuses_for_type(_item_type(bug)):
        return "skipped"
    if bug.status == value:
        return "skipped"
    old = bug.status
    bug.status = value
    record_status_change(db, bug, actor, old)
    # Bump the version so a later expected_version PUT can detect this change.
    bug.version = (bug.version or 1) + 1
    _log(db, bug.id, actor, "status_changed",
         f"#{bug.id} '{bug.title}' — status: '{old}' → '{value}' (bulk)")
    _bulk_notify_update(db, bug, actor, "status", background)
    return "updated"


def _bulk_set_field(db: Session, bug: Bug, actor: User, field_name: str,
                    value: str, background: BackgroundTasks) -> str:
    old = getattr(bug, field_name)
    if old == value:
        return "skipped"
    setattr(bug, field_name, value)
    bug.version = (bug.version or 1) + 1
    _log(db, bug.id, actor, f"{field_name}_changed",
         f"#{bug.id} '{bug.title}' — {field_name}: '{old}' → '{value}' (bulk)")
    _bulk_notify_update(db, bug, actor, field_name, background)
    return "updated"


def _delete_item(db: Session, bug: Bug, actor: User, note: str = "") -> int:
    """Delete ``bug`` the way Jira does and record it in the audit trail.

    Its Sub-tasks go with it (a Sub-task cannot exist without its parent; the
    FK alone would leave them parentless), and an Epic's issues are unlinked
    through the ORM so the change log records it. Audit history is detached
    first so the trail survives: entity_id and detail still carry the id and
    title. Returns how many Sub-tasks were deleted with it.
    """
    bug_id = bug.id
    subtasks = list(db.scalars(
        select(Bug).where(Bug.parent_id == bug_id, Bug.item_type == "Sub-task")
    ).all())
    if bug.item_type == "Epic":
        for issue in db.scalars(select(Bug).where(Bug.epic_id == bug_id, Bug.item_type != "Sub-task")).all():
            issue.epic_id = None
    doomed = [*subtasks, bug]
    db.execute(
        update(Activity)
        .where(Activity.bug_id.in_([d.id for d in doomed]))
        .values(bug_id=None)
    )
    db.flush()
    for item in doomed:
        db.delete(item)
        db.add(Activity(
            org_id=actor.org_id, bug_id=None, entity_type="bug", entity_id=item.id,
            actor_user_id=actor.id, actor_name=actor.name,
            action="bug_deleted",
            detail=(
                f"Deleted {_item_type(item).lower()} #{item.id}: {item.title}"
                + (f" (sub-task of #{bug_id})" if item is not bug else "")
                + note
            ),
        ))
    return len(subtasks)


def _bulk_delete(db: Session, bug: Bug, actor: User, background: BackgroundTasks) -> None:
    # Same pattern as delete_bug: notify first (link_bug=False, since the FK
    # would cascade the notification away), detach audit history, delete, then
    # record the delete in the trail.
    title, itype, bug_id = bug.title, _item_type(bug), bug.id
    if bug in db.deleted or sa_inspect(bug).was_deleted:
        # Already removed with its parent earlier in this selection.
        return
    _notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background, link_bug=False,
        title=f"{itype} #{bug_id} deleted",
        body=f"{actor.name} deleted “{title}”.",
    )
    _delete_item(db, bug, actor, note=" (bulk)")


def _bulk_version_conflict(bug: Bug, payload: BulkActionIn) -> bool:
    """Return True if the caller sent an expected version for this bug and it
    no longer matches, meaning a concurrent edit landed in between."""
    if payload.expected_versions is None:
        return False
    expected = payload.expected_versions.get(bug.id)
    return expected is not None and bug.version != expected


def _apply_bulk_to_bug(db: Session, bug: Bug, actor: User, payload: BulkActionIn,
                       resolved, background: BackgroundTasks) -> str:
    """Apply one bulk action to one bug. Returns 'updated', 'skipped', or
    'conflict'. Permission failures become 'skipped' so a mixed selection
    partially succeeds."""
    if _bulk_version_conflict(bug, payload):
        return "conflict"
    action = payload.action
    if action == "delete":
        if not can_delete_bug(actor, item_type=_item_type(bug)):
            return "skipped"
        _bulk_delete(db, bug, actor, background)
        return "updated"
    if not can_edit_bug(actor, bug.reporter_id, [a.id for a in bug.assignees],
                        item_type=_item_type(bug)):
        return "skipped"
    if action == "set_status":
        return _bulk_set_status(db, bug, actor, resolved, background)
    if action == "set_priority":
        return _bulk_set_field(db, bug, actor, "priority", resolved, background)
    return _bulk_set_field(db, bug, actor, "environment", resolved, background)


@router.post("/bulk", responses=BAD_REQUEST_400)
def bulk_action(
    payload: BulkActionIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> BulkActionResult:
    """Apply one action to many selected items at once. Each item is gated by
    the same rules as its single-item endpoint; items the caller can't touch are
    skipped. Returns the updated/skipped/not-found tally."""
    resolved = _bulk_resolve_value(payload)
    updated = skipped = failed = conflicts = 0
    updated_ids: list[int] = []
    # Batch-load all selected items in one eager query. Out-of-scope ids simply
    # don't appear in the result and fall through to the not-found tally.
    by_id = {
        b.id: b
        for b in db.scalars(
            scope_bug_query(
                _eager_bug().where(Bug.id.in_(payload.ids)),
                accessible_project_ids(db, actor),
            )
        ).all()
    }
    for bug_id in payload.ids:
        bug = by_id.get(bug_id)
        if bug is None:
            failed += 1
            continue
        outcome = _apply_bulk_to_bug(db, bug, actor, payload, resolved, background)
        if outcome == "updated":
            updated += 1
            updated_ids.append(bug_id)
        elif outcome == "conflict":
            conflicts += 1
        else:
            skipped += 1
    db.commit()
    if updated_ids:
        background.add_task(
            deliver_event, actor.org_id,
            "bugs.bulk_deleted" if payload.action == "delete" else "bugs.bulk_updated",
            {"action": payload.action, "bug_ids": updated_ids, "actor": _user_brief(actor)},
        )
    parts = [f"{updated} updated"]
    if conflicts:
        parts.append(f"{conflicts} changed since you loaded them")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} not found")
    return BulkActionResult(
        updated=updated, skipped=skipped, failed=failed, conflicts=conflicts,
        message=", ".join(parts),
    )
