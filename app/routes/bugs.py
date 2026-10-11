"""Bugs API + comments + attachments + activity + links + bulk actions (per-bug)."""

from __future__ import annotations

import re
import threading
import time
from collections import deque
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
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app import bulk_import, notification_service
from app import custom_values as cv
from app.access import (
    accessible_project_ids,
    can_access_project,
    scope_bug_query,
)
from app.agile.permissions import MANAGE_BACKLOG, require_permission
from app.agile.workflow import record_status_change
from app.api_docs import (
    ATTACHMENT_FILE_200,
    BAD_REQUEST_400,
    BAD_REQUEST_413,
    BAD_REQUEST_422,
    BAD_REQUEST_FORBIDDEN_422,
    BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404,
    BAD_REQUEST_FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_422,
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
    notify_comment_added,
)
from app.image_strip import ImageTooLarge, strip_image_metadata
from app.models import (
    Activity,
    Attachment,
    Bug,
    BugLink,
    Comment,
    User,
)
from app.people import reject_inactive, require_project_members, resolve_user, resolve_users
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
)
from app.services.bug_bulk import (
    apply_bulk_to_bug,
    bulk_resolve_value,
    delete_item,
)
from app.services.bug_links import (
    bug_links,
    insert_link_or_existing,
    link_phrase,
    reject_inverse_directional_link,
    reload_link,
    require_can_edit_link_endpoint,
    serialize_link,
)
from app.services.bug_notifications import (
    link_stakeholders,
    notify_item_stakeholders,
    notify_link_endpoints,
    schedule_update_notifications,
    stage_update_notifications,
)
from app.services.bug_query import (
    apply_list_filters,
    normalize_choice_list,
    reject_overflow_ids,
)
from app.services.bug_update import (
    apply_assignee_diff,
    apply_reporter_change,
    apply_sprint_change,
    assert_no_active_branch_for_conversion,
    audit_conversion,
    audit_conversion_failure,
    authorize_item_type_change,
    compute_tracked_changes,
    conversion_change,
    detach_for_project_move,
    enforce_optimistic_concurrency,
    persist_update,
    preserve_display_id_on_type_change,
    require_people_on_project,
    validate_update_authorization,
    validate_update_payload,
)
from app.services.work_items import (
    DETAIL_ATTACHMENT_NOT_FOUND,
    DETAIL_BUG_NOT_FOUND,
    assert_attachment_in_scope,
    assert_bug_accessible,
    assert_event_accessible,
    assert_project_accessible,
    load_accessible_bug,
    log_activity,
    work_item_display_id,
    work_item_type,
)
from app.upload_policy import check_extension, verify_upload
from app.webhooks_delivery import deliver_event

router = APIRouter(prefix="/api/bugs", tags=["bugs"])

_DEFAULT_MIME = "application/octet-stream"


MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB per attachment

# int4 max; larger numeric search tokens are treated as free text (avoids DataError).

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
        "display_id": work_item_display_id(bug),
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "title": bug.title,
        "description": bug.description or "",
        "reporter": _user_brief(bug.reporter) if bug.reporter else None,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "item_type": work_item_type(bug),
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
        description=bug.description or "",
        reporter=(UserSnapshot(id=bug.reporter.id, name=bug.reporter.name, email=bug.reporter.email)
                  if bug.reporter else None),
        assignees=tuple(UserSnapshot(id=a.id, name=a.name, email=a.email) for a in bug.assignees),
        # work_item_type() handles old rows with no item_type set.
        item_type=work_item_type(bug),
        event_name=bug.event.name if getattr(bug, "event", None) else None,
        from_address=bug.project.organization.email_from_override if bug.project else None,
    )


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
    reject_overflow_ids(
        reporter_id=reporter_id, event_id=event_id,
        project_id=project_id, assignee_id=assignee_id,
    )

    statuses = normalize_choice_list(status_filter, ALLOWED_STATUSES, "status")
    priorities = normalize_choice_list(priority, ALLOWED_PRIORITIES, "priority")
    environments = normalize_choice_list(environment, ALLOWED_ENVIRONMENTS, "environment")
    item_types = normalize_choice_list(item_type, ALLOWED_ITEM_TYPES, "item_type")

    # Drop None/0 from the int lists so callers can send blanks safely.
    project_ids = [p for p in (project_id or []) if p]
    assignee_ids = [a for a in (assignee_id or []) if a]

    stmt, count_stmt = apply_list_filters(
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
            can_edit_bug(_user, item_type=work_item_type(b)),
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
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    # A bug outside the actor's projects reads as not-found.
    assert_bug_accessible(accessible_project_ids(db, user), bug)

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
        can_edit_bug(user, item_type=work_item_type(bug)),
    )
    payload["attachments"] = [_attachment_brief(a) for a in bug_level]
    payload["links"] = bug_links(db, bug_id)
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
    assert_project_accessible(db, accessible, payload.project_id)
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
    if not can_edit_bug(actor, item_type=payload.item_type):
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
        reporter = resolve_user(db, payload.reporter_id, actor.org_id)
    else:
        reporter = actor

    assignees = resolve_users(db, payload.assignee_ids, actor.org_id)
    reject_inactive([reporter, *assignees])
    require_project_members(db, assignees, payload.project_id)
    require_project_members(db, [reporter], payload.project_id, noun="reporter")

    if payload.event_id is not None:
        assert_event_accessible(db, actor, accessible, payload.event_id)

    cfields = cv.project_fields(db, payload.project_id)
    try:
        cvalues = cv.clean_incoming(cfields, payload.custom_values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    missing = cv.missing_required(cfields, cvalues)
    if missing:
        raise HTTPException(status_code=422, detail=cv.required_message(missing))

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
    cv.write_values(db, bug.id, cfields, cvalues)
    log_activity(
        db, bug.id, actor, "bug_created",
        f"{bug.item_type} #{bug.id} '{bug.title}' created with status '{bug.status}'.",
    )
    if assignees:
        names = ", ".join(a.name for a in assignees)
        log_activity(
            db, bug.id, actor, "assignees_added",
            f"Bug #{bug.id} '{bug.title}' assigned to: {names}",
        )
    # Write in-app notifications on this session so they commit with the bug.
    _itype = work_item_type(bug).lower()
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
        can_edit_bug(actor, item_type=fresh.item_type),
    ))


@router.put("/{bug_id}", responses=BAD_REQUEST_FORBIDDEN_NOT_FOUND_CONFLICT_VALIDATION_422)
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
    fields.pop("custom_values", None)
    custom_in = payload.custom_values

    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    # Out-of-scope items return 404, not 403, so the restriction doesn't leak
    # which bugs or projects exist.
    accessible = accessible_project_ids(db, actor)
    assert_bug_accessible(accessible, bug)
    # Authorize before the optimistic-concurrency check. Checking the version
    # first would let an unauthorized caller probe a protected item's version
    # via 409-vs-403 differential responses.
    validate_update_authorization(bug, actor)
    enforce_optimistic_concurrency(db, bug_id, expected_version, expected_updated_at)
    validate_update_payload(fields, bug, db, actor, accessible)
    cfields = cv.project_fields(db, fields.get("project_id") or bug.project_id)
    try:
        cvalues = cv.clean_incoming(cfields, custom_in) if custom_in is not None else {
            fid: row.value for fid, row in cv.existing_values(db, bug_id).items() if fid in cfields
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    missing = cv.missing_required(cfields, cvalues)
    if missing:
        raise HTTPException(status_code=422, detail=cv.required_message(missing))
    # Reclassification: same table, same row id, common fields and relations
    # preserved. The guards run before any attribute is applied so a refused
    # conversion leaves the item completely untouched.
    conversion = conversion_change(fields, bug)
    if conversion is not None:
        try:
            assert_no_active_branch_for_conversion(db, bug)
            authorize_item_type_change(fields, bug, actor)
        except HTTPException as exc:
            audit_conversion_failure(db, bug, actor, conversion, exc)
            raise
    else:
        authorize_item_type_change(fields, bug, actor)
    if conversion is not None:
        audit_conversion(db, bug, actor, conversion, "conversion_requested")
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
    preserve_display_id_on_type_change(fields, bug)
    changes = compute_tracked_changes(bug, fields)
    if custom_in is not None:
        changes += [(f"custom_{n[:40]}", o, v) for n, o, v in cv.write_values(db, bug_id, cfields, cvalues)]
    old_status = bug.status
    if "sprint_id" in fields and fields["sprint_id"] != bug.sprint_id:
        # Planning work into or out of a sprint is backlog management, the
        # same permission the board's move endpoint requires.
        require_permission(actor, MANAGE_BACKLOG)
    project_moved = "project_id" in fields and fields["project_id"] != bug.project_id
    detach_for_project_move(db, bug, fields, actor)
    sprint_requested = "sprint_id" in fields
    new_sprint_id = fields.pop("sprint_id", None)
    for key, value in fields.items():
        setattr(bug, key, value)
    # A project move re-ranks the issue at the bottom of its new project's
    # backlog at flush (app/agile/integrity.py).
    if sprint_requested:
        apply_sprint_change(db, bug, new_sprint_id, actor)
    record_status_change(db, bug, actor, old_status)

    if reporter_actually_changes:
        apply_reporter_change(bug, db, actor.org_id, new_reporter_id, changes)
    newly_assigned, newly_removed = apply_assignee_diff(
        bug, db, actor.org_id, assignee_ids, changes,
    )
    require_people_on_project(db, bug, project_moved, newly_assigned, reporter_actually_changes)

    if changes or newly_assigned or newly_removed:
        stage_update_notifications(
            db, bug, changes, newly_assigned, newly_removed, actor, background,
        )
    if conversion is not None:
        audit_conversion(db, bug, actor, conversion, "conversion_succeeded")
    try:
        persist_update(db, bug, actor, changes)
    except Exception as exc:
        # Transaction rollback: the item keeps its original type and the
        # refusal is audited on its own transaction.
        if conversion is not None:
            audit_conversion_failure(db, bug, actor, conversion, exc)
        raise
    fresh = db.scalar(_eager_bug().where(Bug.id == bug_id))
    snap = _bug_snapshot(fresh)
    schedule_update_notifications(background, snap, changes, newly_assigned, actor)
    if changes:
        background.add_task(
            deliver_event, actor.org_id, "bug.updated",
            {"bug": _bug_to_out_dict(fresh),
             "changes": [{"field": f, "old": o, "new": n} for f, o, n in changes],
             "actor": _user_brief(actor)},
        )

    return BugOut.model_validate(_bug_to_out_dict(
        fresh, _attachment_count(db, bug_id),
        can_edit_bug(actor, item_type=fresh.item_type),
    ))


@router.delete("/{bug_id}", responses=NOT_FOUND_FORBIDDEN_403)
def delete_bug(
    bug_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    bug = db.scalar(_eager_bug().where(Bug.id == bug_id))
    if bug is None:
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    assert_bug_accessible(accessible_project_ids(db, actor), bug)
    # Deletion is admin-only; managers can edit but not delete.
    if not can_delete_bug(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete items.",
        )
    title = bug.title
    itype = work_item_type(bug)
    # Notify before deleting; link_bug=False because the FK cascade would
    # remove a notification pointing at the now-deleted bug.
    notify_item_stakeholders(
        db, bug, actor, kind="updated", background=background, link_bug=False,
        title=f"{itype} #{bug_id} deleted",
        body=f"{actor.name} deleted “{title}”.",
    )
    subtasks = delete_item(db, bug, actor)
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
    load_accessible_bug(db, bug_id, accessible_project_ids(db, _user))
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
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    assert_bug_accessible(accessible_project_ids(db, author), bug)
    # Same per-type policy as edit/link: regular users can comment on Bugs but
    # not on Tasks/Requirements.
    validate_update_authorization(bug, author)
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
    log_activity(db, bug_id, author, "comment_added",
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
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    assert_bug_accessible(accessible_project_ids(db, uploader), bug)
    # Same per-type policy as edit/link: regular users can attach to Bugs but
    # not to Tasks/Requirements they can't edit.
    validate_update_authorization(bug, uploader)
    if comment_id is not None:
        c = db.get(Comment, comment_id)
        if c is None or c.bug_id != bug_id:
            raise HTTPException(status_code=400, detail="Invalid comment_id for this bug")

    # Check rate limit after auth/404 so probing requests don't burn the budget,
    # but before reading the body.
    _check_upload_rate(uploader.id)

    # Refuse unlisted extensions before streaming the body.
    refusal = check_extension(file.filename or "")
    if refusal is not None:
        raise HTTPException(status_code=400, detail=refusal)

    data = await _read_upload_with_limit(file, MAX_FILE_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="This file is empty, so there's nothing to attach.")
    try:
        content_type = verify_upload(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # Strip EXIF/GPS/XMP metadata from raster images (off the event loop: a re-encode can
    # take a while). No-op for non-images and for bytes that are not a readable image.
    try:
        data = await run_in_threadpool(strip_image_metadata, data, content_type)
    except ImageTooLarge:
        raise HTTPException(
            status_code=413,
            detail=("This image is too large to process. Save it as JPEG or PNG, or resize "
                    "it below 16 megapixels, and try again."),
        ) from None
    # Re-check the cap: re-encoding a GIF, BMP or TIFF could grow the file slightly.
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
        content_type=content_type,
        size_bytes=len(data),
        data=data,
    )
    db.add(att)
    db.flush()
    log_activity(
        db, bug_id, uploader, "attachment_added",
        f"{uploader.name} uploaded '{att.filename}' ({len(data)} bytes)"
        + (f" on comment #{comment_id}" if comment_id else ""),
        entity_type="attachment", entity_id=att.id,
    )
    notify_item_stakeholders(
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
        raise HTTPException(status_code=404, detail=DETAIL_ATTACHMENT_NOT_FOUND)
    assert_attachment_in_scope(db, accessible_project_ids(db, _user), bug_id)
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
            raise HTTPException(status_code=404, detail=DETAIL_ATTACHMENT_NOT_FOUND)
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
        raise HTTPException(status_code=404, detail=DETAIL_ATTACHMENT_NOT_FOUND)
    # Admin-only: uploaders and managers cannot remove their own files.
    if not can_delete_attachment(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete attachments.",
        )
    assert_attachment_in_scope(db, accessible_project_ids(db, actor), bug_id)
    fname = a.filename
    db.delete(a)
    log_activity(
        db, bug_id, actor, "attachment_deleted",
        f"Deleted attachment '{fname}'",
        entity_type="attachment", entity_id=att_id,
    )
    # The FK from Attachment.bug_id guarantees the bug still exists here.
    bug = db.get(Bug, bug_id)
    notify_item_stakeholders(
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
    assert_bug_accessible(accessible_project_ids(db, actor), db.get(Bug, bug_id))
    c.body = payload.body
    db.flush()
    log_activity(
        db, bug_id, actor, "comment_edited",
        f"Comment #{c.id} edited by {actor.name}: {payload.body[:80]}",
        entity_type="comment", entity_id=c.id,
    )
    bug = db.get(Bug, bug_id)
    notify_item_stakeholders(
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
    assert_bug_accessible(accessible_project_ids(db, actor), db.get(Bug, bug_id))
    preview = (c.body or "")[:80]
    author_name = c.author_name
    db.delete(c)
    log_activity(
        db, bug_id, actor, "comment_deleted",
        f"Comment #{comment_id} by {author_name} deleted: {preview}",
        entity_type="comment", entity_id=comment_id,
    )
    bug = db.get(Bug, bug_id)
    notify_item_stakeholders(
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
    load_accessible_bug(db, bug_id, accessible_project_ids(db, _user))
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
    load_accessible_bug(db, bug_id, accessible_project_ids(db, _user))
    return bug_links(db, bug_id)


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
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    # Both ends must be in the actor's projects. The source returns 404 if
    # out-of-scope; the target returns "does not exist" for the same reason.
    accessible = accessible_project_ids(db, actor)
    assert_bug_accessible(accessible, bug)
    validate_update_authorization(bug, actor)
    if payload.target_bug_id == bug_id:
        raise HTTPException(status_code=400, detail="An item can't be linked to itself")
    target = db.scalar(_eager_bug().where(Bug.id == payload.target_bug_id))
    if target is None or not can_access_project(accessible, target.project_id):
        raise HTTPException(status_code=400, detail="Target item does not exist")
    require_can_edit_link_endpoint(target, actor)
    reject_inverse_directional_link(db, bug_id, target, payload.link_type)

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
            return serialize_link(reload_link(db, reverse.id), bug_id)

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
        return serialize_link(reload_link(db, existing.id), bug_id)

    link, created = insert_link_or_existing(
        db,
        BugLink(
            source_bug_id=bug_id, target_bug_id=target.id,
            link_type=payload.link_type, created_by_user_id=actor.id,
        ),
        _find_existing,
    )
    if not created:
        # A concurrent identical request won the race; return the existing edge.
        return serialize_link(reload_link(db, link.id), bug_id)
    log_activity(
        db, bug_id, actor, "link_added",
        f"#{bug_id} '{bug.title}' {link_phrase(payload.link_type, 'outgoing')} "
        f"#{target.id} '{target.title}'",
    )
    notify_link_endpoints(
        db, link_stakeholders(bug) + link_stakeholders(target), bug.id, actor,
        background=background,
        title=f"Link added on #{bug_id}",
        body=(
            f"{actor.name} linked “{bug.title}” "
            f"{link_phrase(payload.link_type, 'outgoing')} "
            f"#{target.id} “{target.title}”."
        ),
    )
    db.commit()
    return serialize_link(reload_link(db, link.id), bug_id)


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
        raise HTTPException(status_code=404, detail=DETAIL_BUG_NOT_FOUND)
    # Project scope: the path bug must be in the actor's projects.
    assert_bug_accessible(accessible_project_ids(db, actor), bug)
    validate_update_authorization(bug, actor)
    other_id = link.target_bug_id if link.source_bug_id == bug_id else link.source_bug_id
    other = db.scalar(_eager_bug().where(Bug.id == other_id))
    recipients = link_stakeholders(bug)
    if other is not None:
        require_can_edit_link_endpoint(other, actor)
        recipients += link_stakeholders(other)
    db.delete(link)
    log_activity(db, bug_id, actor, "link_removed", f"Unlinked #{bug_id} ↔ #{other_id}")
    notify_link_endpoints(
        db, recipients, bug.id, actor, background=background,
        title=f"Link removed on #{bug_id}",
        body=f"{actor.name} unlinked “{bug.title}” from #{other_id}.",
    )
    db.commit()
    return {"message": "Link removed"}


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
    resolved = bulk_resolve_value(payload)
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
        outcome = apply_bulk_to_bug(db, bug, actor, payload, resolved, background)
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
