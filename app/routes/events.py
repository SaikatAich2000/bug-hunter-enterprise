"""Events API — containers for work items (standups, sprint meetings).

v2.4 — enterprise edition. Same shape as the OSS edition but every
read and write is org-scoped: an Event belongs to exactly one
organization (Event.org_id) and is invisible to every other org.

An event groups any number of items (Bug / Requirement / Task) so the
morning standup can be tracked as a first-class entity. Items are
linked via `Bug.event_id` (nullable). The link is fully editable:

  - Create an item directly under an event:  POST /api/bugs with event_id
  - Move an existing item into an event:     PUT  /api/bugs/{id} {event_id: N}
  - Take an item back out:                   PUT  /api/bugs/{id} {event_id: null}

Deleting an event preserves its items — the FK is declared
``ondelete="SET NULL"`` on Bug.event_id; we also explicitly null the
items in the delete handler for SQLite parity. Audit history of the
event is preserved by the same activity-detach pattern as bugs.

Permissions (v2.4):
  - create / edit: admin or manager (members are read-only)
  - delete:        admin only

Managers (event_managers M2M): admin/manager users who own the event.
They receive event-level notification emails (create / update /
delete). Per-item assignment emails fan out only to the item's own
assignees — they do NOT cc event managers.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import can_delete_event, can_edit_event, get_current_user
from app.database import get_db
from app.email_service import (
    EventSnapshot, UserSnapshot,
    notify_event_created, notify_event_deleted, notify_event_updated,
)
from app.models import (
    ROLE_ADMIN, ROLE_MANAGER,
    Activity, Attachment, Bug, Event, User,
)
from app.schemas import EventCreate, EventDetailOut, EventOut, EventUpdate

router = APIRouter(prefix="/api/events", tags=["events"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _log(
    db: Session, org_id: int, event_id: int | None, actor: User | None,
    action: str, detail: str,
) -> None:
    db.add(Activity(
        org_id=org_id,
        bug_id=None,
        entity_type="event",
        entity_id=event_id,
        actor_user_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        detail=detail,
    ))


def _user_brief(u: User) -> dict:
    return {
        "id": u.id, "name": u.name, "email": u.email, "role": u.role,
        "is_active": getattr(u, "is_active", True),
    }


def _event_brief(db: Session, ev: Event, actor: User) -> dict:
    item_count = db.scalar(
        select(func.count(Bug.id)).where(Bug.event_id == ev.id)
    ) or 0
    return {
        "id": ev.id,
        "name": ev.name,
        "description": ev.description,
        "scheduled_for": ev.scheduled_for,
        "managers": [_user_brief(m) for m in (ev.managers or [])],
        "item_count": int(item_count),
        "created_at": ev.created_at,
        "updated_at": ev.updated_at,
        "can_edit": can_edit_event(actor),
        "can_delete": can_delete_event(actor),
    }


def _bug_to_event_item(bug: Bug, attachment_count: int = 0) -> dict:
    """Project the bug into the EventItemBrief shape used by the
    event-detail items list. Mirrors the work-items table columns so
    the UI can render the same component."""
    return {
        "id": bug.id,
        "item_type": getattr(bug, "item_type", None) or "Bug",
        "title": bug.title,
        "project_id": bug.project_id,
        "project_name": bug.project.name if bug.project else None,
        "project_key": bug.project.key if bug.project else None,
        "status": bug.status,
        "priority": bug.priority,
        "environment": bug.environment,
        "due_date": bug.due_date,
        "assignees": [_user_brief(a) for a in bug.assignees],
        "attachment_count": attachment_count,
    }


def _resolve_managers(db: Session, ids: list[int], org_id: int) -> list[User]:
    """Validate manager_ids: every id must point at a user in the SAME
    org with role admin or manager. The org check is the load-bearing
    security gate — without it, an attacker who guessed a user id from
    another tenant could assign that user as a manager and trigger
    cross-tenant notification emails."""
    if not ids:
        return []
    # Dedupe while preserving order so the returned list reflects
    # what the caller asked for.
    deduped: list[int] = []
    seen: set[int] = set()
    for i in ids:
        if i not in seen:
            seen.add(i)
            deduped.append(i)
    rows = db.scalars(select(User).where(User.id.in_(deduped))).all()
    found = {u.id: u for u in rows}
    missing = [i for i in deduped if i not in found]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown user ids: {missing}")
    cross_org = [u for u in rows if u.org_id != org_id]
    if cross_org:
        # Don't leak the cross-tenant existence — same 400 shape as
        # "unknown user ids" so probing returns no new information.
        raise HTTPException(
            status_code=400,
            detail=f"Unknown user ids: {[u.id for u in cross_org]}",
        )
    bad_roles = [u for u in rows if u.role not in (ROLE_ADMIN, ROLE_MANAGER)]
    if bad_roles:
        names = ", ".join(u.name for u in bad_roles)
        raise HTTPException(
            status_code=400,
            detail=f"Only admin or manager users can be event managers ({names} is not)",
        )
    return [found[i] for i in deduped]


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
    )


def _require_edit(actor: User) -> None:
    if not can_edit_event(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins and managers can manage events.",
        )


def _get_event_or_404(db: Session, event_id: int, actor: User) -> Event:
    """Resolve an event by id but 404 if it belongs to a different org —
    same response as a genuinely-missing event so we don't leak
    cross-tenant existence."""
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers))
        .where(Event.id == event_id)
    )
    if ev is None or ev.org_id != actor.org_id:
        raise HTTPException(status_code=404, detail="Event not found")
    return ev


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("", response_model=list[EventOut])
def list_events(
    scheduled_for: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> list[dict]:
    stmt = (
        select(Event)
        .options(selectinload(Event.managers))
        .where(Event.org_id == actor.org_id)
        .order_by(Event.scheduled_for.desc(), Event.id.desc())
    )
    if scheduled_for:
        stmt = stmt.where(Event.scheduled_for == scheduled_for)
    rows = list(db.scalars(stmt).all())
    return [_event_brief(db, ev, actor) for ev in rows]


# ---------------------------------------------------------------------------
# Detail (event + its items)
# ---------------------------------------------------------------------------
@router.get("/{event_id}", response_model=EventDetailOut)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    ev = _get_event_or_404(db, event_id, actor)
    items_stmt = (
        select(Bug)
        .options(
            selectinload(Bug.project),
            selectinload(Bug.reporter),
            selectinload(Bug.assignees),
        )
        .where(Bug.event_id == event_id)
        .order_by(Bug.id.asc())
    )
    items = list(db.scalars(items_stmt).all())
    # One aggregate query for attachment counts — no N+1.
    bug_ids = [b.id for b in items]
    if bug_ids:
        att_counts = dict(db.execute(
            select(Attachment.bug_id, func.count(Attachment.id))
            .where(Attachment.bug_id.in_(bug_ids))
            .group_by(Attachment.bug_id)
        ).all())
    else:
        att_counts = {}
    payload = _event_brief(db, ev, actor)
    payload["items"] = [
        _bug_to_event_item(b, int(att_counts.get(b.id, 0))) for b in items
    ]
    return payload


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    _require_edit(actor)
    managers = _resolve_managers(db, payload.manager_ids or [], actor.org_id)
    ev = Event(
        org_id=actor.org_id,
        name=payload.name,
        description=payload.description,
        scheduled_for=payload.scheduled_for,
        created_by_user_id=actor.id,
    )
    if managers:
        ev.managers = managers
    db.add(ev)
    db.flush()
    _log(
        db, actor.org_id, ev.id, actor, "event_created",
        f"Event #{ev.id} '{ev.name}' created"
        + (f" (scheduled for {ev.scheduled_for})" if ev.scheduled_for else ""),
    )
    db.commit()
    # Re-fetch with managers loaded for the response.
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers))
        .where(Event.id == ev.id)
    )
    if ev and ev.managers:
        snap = _event_snapshot(ev)
        background.add_task(notify_event_created, snap, actor.name, actor.id)
    return _event_brief(db, ev, actor)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
@router.put("/{event_id}", response_model=EventOut)
def update_event(
    event_id: int,
    payload: EventUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict:
    _require_edit(actor)
    ev = _get_event_or_404(db, event_id, actor)

    fields = payload.model_dump(exclude_unset=True)
    tracked = ["name", "description", "scheduled_for"]
    changes: list[tuple[str, str, str]] = []
    for f in tracked:
        if f in fields and getattr(ev, f) != fields[f]:
            changes.append((f, str(getattr(ev, f) or ""), str(fields[f] or "")))
    new_manager_ids = fields.pop("manager_ids", None)
    for k, v in fields.items():
        setattr(ev, k, v)
    # Manager diff. Set-equality so re-sending the same list isn't a change.
    if new_manager_ids is not None:
        old_ids = sorted({m.id for m in (ev.managers or [])})
        new_ids = sorted(set(new_manager_ids))
        if old_ids != new_ids:
            new_managers = _resolve_managers(db, new_manager_ids, actor.org_id)
            old_names = sorted(m.name for m in (ev.managers or []))
            new_names = sorted(m.name for m in new_managers)
            changes.append((
                "managers",
                ", ".join(old_names) or "(none)",
                ", ".join(new_names) or "(none)",
            ))
            ev.managers = new_managers
    if changes:
        prefix = f"#{ev.id} '{ev.name}' — "
        for field, old, new in changes:
            _log(
                db, actor.org_id, ev.id, actor, f"event_{field}_changed",
                f"{prefix}{field}: '{old}' → '{new}'",
            )
        db.commit()
    else:
        db.rollback()
    ev = db.scalar(
        select(Event).options(selectinload(Event.managers))
        .where(Event.id == event_id)
    )
    if changes and ev and ev.managers:
        snap = _event_snapshot(ev)
        background.add_task(
            notify_event_updated, snap, list(changes), actor.name, actor.id,
        )
    return _event_brief(db, ev, actor)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
) -> dict[str, str]:
    ev = _get_event_or_404(db, event_id, actor)
    if not can_delete_event(actor):
        raise HTTPException(
            status_code=403,
            detail="Only admins can delete events.",
        )
    name = ev.name
    org_id = ev.org_id
    snap = _event_snapshot(ev) if ev.managers else None
    # Items keep existing. The FK is ondelete=SET NULL but we null
    # them explicitly so SQLite's per-session relationship cache
    # stays consistent.
    db.query(Bug).filter(Bug.event_id == event_id).update(
        {Bug.event_id: None}, synchronize_session=False,
    )
    db.delete(ev)
    _log(
        db, org_id, None, actor, "event_deleted",
        f"Deleted event #{event_id} '{name}'",
    )
    db.commit()
    if snap is not None:
        background.add_task(notify_event_deleted, snap, actor.name, actor.id)
    return {"message": "Event deleted"}
