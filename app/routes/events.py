"""Events API: containers for work items, linked via nullable Bug.event_id.

Deleting an event preserves its items. Create/edit: admin or manager;
delete: admin only. Event managers must be admin/manager and get event emails.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import notification_service
from app.access import (
    accessible_project_ids,
    can_access_project,
    event_visible,
    scope_bug_query,
    scope_event_query,
)
from app.api_docs import (
    BAD_REQUEST_FORBIDDEN_403,
    BAD_REQUEST_FORBIDDEN_NOT_FOUND_404,
    NOT_FOUND_404,
    NOT_FOUND_FORBIDDEN_403,
)
from app.auth import can_delete_event, can_edit_bug, can_edit_event, get_current_user
from app.database import get_db
from app.email_service import (
    EventSnapshot,
    UserSnapshot,
    notify_event_created,
    notify_event_deleted,
    notify_event_updated,
)
from app.models import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    Activity,
    Attachment,
    Bug,
    Event,
    Project,
    User,
    bug_assignees,
)
from app.schemas import (
    EventCreate,
    EventDetail,
    EventOut,
    EventUpdate,
)

router = APIRouter(prefix="/api/events", tags=["events"])


_DETAIL_EVENT_NOT_FOUND = "Event not found"

def _log(
    db: Session, event_id: int | None, actor: User,
    action: str, detail: str,
) -> None:
    db.add(Activity(
        org_id=actor.org_id,
        bug_id=None,
        entity_type="event",
        entity_id=event_id,
        actor_user_id=actor.id,
        actor_name=actor.name,
        action=action,
        detail=detail,
    ))


def _user_brief(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "role": u.role}


def _event_list_aggregates(
    db: Session, event_ids: list[int],
) -> tuple[dict[int, int], dict[int, int]]:
    """Return (item_count, assignee_count) maps in two queries per batch."""
    if not event_ids:
        return {}, {}
    item_counts = {
        eid: int(n) for eid, n in db.execute(
            select(Bug.event_id, func.count(Bug.id))
            .where(Bug.event_id.in_(event_ids))
            .group_by(Bug.event_id)
        ).all()
    }
    assignee_counts = {
        eid: int(n) for eid, n in db.execute(
            select(Bug.event_id, func.count(func.distinct(bug_assignees.c.user_id)))
            .select_from(bug_assignees)
            .join(Bug, Bug.id == bug_assignees.c.bug_id)
            .where(Bug.event_id.in_(event_ids))
            .group_by(Bug.event_id)
        ).all()
    }
    return item_counts, assignee_counts


def _event_brief(
    db: Session, ev: Event,
    item_count: Optional[int] = None,
    assignee_count: Optional[int] = None,
    creator_names: Optional[dict[int, str]] = None,
) -> dict:
    if item_count is None:
        item_count = db.scalar(
            select(func.count(Bug.id)).where(Bug.event_id == ev.id)
        ) or 0
    if assignee_count is None:
        assignee_count = db.scalar(
            select(func.count(func.distinct(bug_assignees.c.user_id)))
            .select_from(bug_assignees)
            .join(Bug, Bug.id == bug_assignees.c.bug_id)
            .where(Bug.event_id == ev.id)
        ) or 0
    creator_name = None
    if ev.created_by_user_id:
        if creator_names is not None:
            creator_name = creator_names.get(ev.created_by_user_id)
        else:
            u = db.get(User, ev.created_by_user_id)
            creator_name = u.name if u else None
    return {
        "id": ev.id,
        "name": ev.name,
        "description": ev.description,
        "scheduled_for": ev.scheduled_for,
        "project_id": ev.project_id,
        "project_name": ev.project.name if ev.project else None,
        "created_by_user_id": ev.created_by_user_id,
        "created_by_name": creator_name,
        "item_count": int(item_count),
        "assignee_count": int(assignee_count),
        "managers": [_user_brief(m) for m in (ev.managers or [])],
        "created_at": ev.created_at,
        "updated_at": ev.updated_at,
    }


def _bug_to_brief(bug: Bug, actor: User, attachment_count: int = 0) -> dict:
    item_type = getattr(bug, "item_type", None) or "Bug"
    return {
        "id": bug.id,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "title": bug.title,
        "description": bug.description,
        "reporter": _user_brief(bug.reporter) if bug.reporter else None,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "item_type": item_type,
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "event_id": bug.event_id,
        "event_name": bug.event.name if bug.event else None,
        "created_at": bug.created_at,
        "updated_at": bug.updated_at,
        "attachment_count": attachment_count,
        # per-item so the SPA can hide edit where the actor lacks rights
        "can_edit": can_edit_bug(
            actor, bug.reporter_id, [a.id for a in bug.assignees],
            item_type=item_type,
        ),
    }


def _resolve_managers(db: Session, ids: list[int], org_id: int) -> list[User]:
    """Resolve ids to Users of the organization; each must exist and be admin or manager."""
    if not ids:
        return []
    rows = db.scalars(select(User).where(User.id.in_(ids), User.org_id == org_id)).all()
    found = {u.id: u for u in rows}
    missing = set(ids) - set(found)
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {sorted(missing)}")
    bad_roles = [u for u in rows if u.role not in (ROLE_ADMIN, ROLE_MANAGER)]
    if bad_roles:
        names = ", ".join(u.name for u in bad_roles)
        raise HTTPException(
            status_code=400,
            detail=f"Only admin or manager users can be event managers ({names} is not)",
        )
    return [found[i] for i in ids if i in found]


def _event_snapshot(ev: Event) -> EventSnapshot:
    return EventSnapshot(
        id=ev.id,
        name=ev.name,
        description=ev.description,
        scheduled_for=ev.scheduled_for,
        managers=tuple(
            UserSnapshot(id=m.id, name=m.name, email=m.email)
            for m in (ev.managers or [])
        ),
        from_address=ev.organization.email_from_override,
    )


def _require_edit(actor: User) -> None:
    if not can_edit_event(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can manage events.",
        )


def _validate_event_project(db: Session, accessible, project_id: int) -> None:
    """400 for missing or out-of-scope project — must not leak existence."""
    if db.get(Project, project_id) is None or not can_access_project(accessible, project_id):
        raise HTTPException(status_code=400, detail="Project does not exist")


@router.get("", response_model=list[EventOut])
def list_events(
    scheduled_for: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=500, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    stmt = select(Event).options(
        selectinload(Event.managers),
        selectinload(Event.project),
    ).order_by(
        # unscheduled events sink to the bottom
        Event.scheduled_for.desc().nullslast(),
        Event.id.desc(),
    )
    # restricted users see only their projects' events; project-less stay admin-only
    stmt = scope_event_query(stmt, user, accessible_project_ids(db, user))
    if scheduled_for:
        stmt = stmt.where(Event.scheduled_for == scheduled_for)
    stmt = stmt.limit(page_size).offset((page - 1) * page_size)
    rows = list(db.scalars(stmt).all())
    item_counts, assignee_counts = _event_list_aggregates(
        db, [ev.id for ev in rows])
    creator_ids = {ev.created_by_user_id for ev in rows if ev.created_by_user_id}
    creator_names: dict[int, str] = {}
    if creator_ids:
        creator_names = dict(db.execute(
            select(User.id, User.name).where(User.id.in_(creator_ids), User.org_id == user.org_id)
        ).all())
    return [
        _event_brief(
            db, ev,
            item_count=item_counts.get(ev.id, 0),
            assignee_count=assignee_counts.get(ev.id, 0),
            creator_names=creator_names,
        )
        for ev in rows
    ]


# Caps a detail payload; the true count is still returned for "N of M".
_EVENT_ITEMS_MAX = 1000


@router.get("/{event_id}", response_model=EventDetail, responses=NOT_FOUND_404)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    accessible = accessible_project_ids(db, user)
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers), selectinload(Event.project))
        .where(Event.id == event_id)
    )
    # 404 not 403 — scoping must not leak existence
    if ev is None or not event_visible(user, accessible, ev):
        raise HTTPException(status_code=404, detail=_DETAIL_EVENT_NOT_FOUND)
    items_stmt = select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    ).where(Bug.event_id == event_id).order_by(
        Bug.updated_at.desc(), Bug.id.desc(),
    ).limit(_EVENT_ITEMS_MAX)
    # items can span projects, so scope them too
    items_stmt = scope_bug_query(items_stmt, accessible)
    items = list(db.scalars(items_stmt).all())
    total_items = db.scalar(
        scope_bug_query(
            select(func.count(Bug.id)).where(Bug.event_id == event_id), accessible,
        )
    ) or 0
    # one aggregate query for attachment counts — avoids N+1
    bug_ids = [b.id for b in items]
    if bug_ids:
        att_counts = dict(db.execute(
            select(Attachment.bug_id, func.count(Attachment.id))
            .where(Attachment.bug_id.in_(bug_ids))
            .group_by(Attachment.bug_id)
        ).all())
    else:
        att_counts = {}
    payload = _event_brief(db, ev, item_count=total_items)
    payload["items"] = [_bug_to_brief(b, user, int(att_counts.get(b.id, 0))) for b in items]
    payload["items_truncated"] = total_items > len(items)
    return payload


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED, responses=BAD_REQUEST_FORBIDDEN_403)
def create_event(
    payload: EventCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    _require_edit(actor)
    # project-less events are allowed and become admin-only
    if payload.project_id is not None:
        _validate_event_project(db, accessible_project_ids(db, actor), payload.project_id)
    managers = _resolve_managers(db, payload.manager_ids or [], actor.org_id)
    ev = Event(
        org_id=actor.org_id,
        name=payload.name,
        description=payload.description,
        scheduled_for=payload.scheduled_for,
        project_id=payload.project_id,
        created_by_user_id=actor.id,
    )
    if managers:
        ev.managers = managers
    db.add(ev)
    db.flush()
    _log(db, ev.id, actor, "event_created",
         f"Event created: {ev.name}"
         + (f" (scheduled for {ev.scheduled_for})" if ev.scheduled_for else ""))
    # notify all managers, including a self-added creator
    if managers:
        notification_service.notify(
            db, [m.id for m in managers], kind="event", background=background,
            title=f"Added as manager on “{ev.name}”",
            body=f"You're a manager on the event “{ev.name}” created by {actor.name}.",
            event_id=ev.id, actor_name=actor.name,
        )
    db.commit()
    db.refresh(ev)

    if managers:
        snap = _event_snapshot(ev)
        background.add_task(notify_event_created, snap, actor.name, actor.id)
    return _event_brief(db, ev)


_EVENT_TRACKED_FIELDS = ["name", "description", "scheduled_for"]


def _compute_event_changes(ev: Event, fields: dict) -> list[tuple[str, str, str]]:
    changes: list[tuple[str, str, str]] = []
    for f in _EVENT_TRACKED_FIELDS:
        if f in fields and getattr(ev, f) != fields[f]:
            changes.append((f, str(getattr(ev, f) or ""), str(fields[f] or "")))
    return changes


def _apply_event_manager_diff(ev: Event, db: Session, new_manager_ids: Optional[list[int]],
                              changes: list[tuple[str, str, str]]) -> None:
    """Order-insensitive diff: re-sending the same list produces no change."""
    if new_manager_ids is None:
        return
    old_ids = sorted({m.id for m in (ev.managers or [])})
    new_ids = sorted(set(new_manager_ids))
    if old_ids == new_ids:
        return
    new_managers = _resolve_managers(db, new_manager_ids, ev.org_id)
    old_names = sorted(m.name for m in (ev.managers or []))
    new_names = sorted(m.name for m in new_managers)
    changes.append((
        "managers",
        ", ".join(old_names) or "(none)",
        ", ".join(new_names) or "(none)",
    ))
    ev.managers = new_managers


def _persist_event_update(db: Session, ev: Event, actor: User,
                          changes: list[tuple[str, str, str]]) -> None:
    if not changes:
        db.rollback()
        return
    for field, old, new in changes:
        _log(db, ev.id, actor, f"event_{field}_changed",
             f"{field}: '{old}' → '{new}'")
    db.commit()


@router.put("/{event_id}", response_model=EventOut, responses=BAD_REQUEST_FORBIDDEN_NOT_FOUND_404)
def update_event(
    event_id: int,
    payload: EventUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    _require_edit(actor)
    accessible = accessible_project_ids(db, actor)
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers), selectinload(Event.project))
        .where(Event.id == event_id)
    )
    # 404 not 403 — scoping must not leak existence
    if ev is None or not event_visible(actor, accessible, ev):
        raise HTTPException(status_code=404, detail=_DETAIL_EVENT_NOT_FOUND)

    fields = payload.model_dump(exclude_unset=True)
    # project_id has its own validation; pop before the generic setattr loop
    new_project_id = fields.pop("project_id", None)
    if new_project_id is not None:
        _validate_event_project(db, accessible, new_project_id)
    changes = _compute_event_changes(ev, fields)
    new_manager_ids = fields.pop("manager_ids", None)
    # snapshot first so a removed manager is still notified of their removal
    old_manager_ids = [m.id for m in ev.managers]
    for k, v in fields.items():
        setattr(ev, k, v)
    if new_project_id is not None and ev.project_id != new_project_id:
        changes.append(("project", str(ev.project_id or ""), str(new_project_id)))
        ev.project_id = new_project_id
    _apply_event_manager_diff(ev, db, new_manager_ids, changes)
    recipient_ids = sorted({*old_manager_ids, *(m.id for m in ev.managers)})
    if changes and recipient_ids:
        _summary = ", ".join(f for f, _, _ in changes)
        notification_service.notify(
            db, recipient_ids, kind="event", background=background,
            title=f"Event “{ev.name}” updated",
            body=f"{actor.name} changed {_summary}.",
            event_id=ev.id, actor_name=actor.name, exclude=actor.id,
        )
    _persist_event_update(db, ev, actor, changes)

    ev = db.scalar(
        select(Event).options(selectinload(Event.managers), selectinload(Event.project))
        .where(Event.id == event_id)
    )
    if changes and ev.managers:
        snap = _event_snapshot(ev)
        background.add_task(
            notify_event_updated, snap, list(changes), actor.name, actor.id,
        )
    return _event_brief(db, ev)


@router.delete("/{event_id}", responses=NOT_FOUND_FORBIDDEN_403)
def delete_event(
    event_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers)).where(Event.id == event_id)
    )
    if ev is None or ev.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail=_DETAIL_EVENT_NOT_FOUND)
    if not can_delete_event(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete events.",
        )
    name = ev.name
    manager_ids = [m.id for m in (ev.managers or [])]
    snap = _event_snapshot(ev) if ev.managers else None
    # explicit NULL — SQLite can skip ondelete='SET NULL' for in-session objects
    db.query(Bug).filter(Bug.event_id == event_id).update(
        {Bug.event_id: None}, synchronize_session=False,
    )
    db.delete(ev)
    # keep the real event_id so the audit screen can find it after deletion
    _log(db, event_id, actor, "event_deleted",
         f"Deleted event #{event_id}: {name}")
    # omit event_id — the FK cascade would delete the notification rows
    if manager_ids:
        notification_service.notify(
            db, manager_ids, kind="event", background=background,
            title=f"Event “{name}” deleted",
            body=f"{actor.name} deleted this event.",
            actor_name=actor.name, exclude=actor.id,
        )
    db.commit()

    if snap is not None:
        background.add_task(notify_event_deleted, snap, actor.name, actor.id)
    return {"message": "Event deleted"}
