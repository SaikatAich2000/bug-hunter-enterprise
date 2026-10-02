"""Per-user in-app notifications API.

Every endpoint is scoped to the authenticated user; there is no admin path
to another user's notifications.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api_docs import NOT_FOUND_404
from app.auth import get_current_user
from app.database import get_db
from app.models import Notification, User
from app.schemas import NotificationOut, UnreadCountOut

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_DETAIL_NOT_FOUND = "Notification not found"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Notification]:
    """The current user's notifications, newest first."""
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    return list(db.scalars(stmt).all())


@router.get("/unread_count", response_model=UnreadCountOut)
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    """How many unread notifications the current user has (drives the badge)."""
    n = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    return {"unread": int(n or 0)}


def _owned_or_404(db: Session, notif_id: int, user: User) -> Notification:
    """Fetch a caller-owned notification; 404 even for others' rows so IDs don't leak."""
    notif = db.get(Notification, notif_id)
    if notif is None or notif.user_id != user.id:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    return notif


@router.post("/{notif_id}/read", responses=NOT_FOUND_404)
def mark_read(
    notif_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    notif = _owned_or_404(db, notif_id, user)
    if notif.read_at is None:
        notif.read_at = _utcnow()
        db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    db.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=_utcnow())
    )
    db.commit()
    return {"ok": True}


@router.delete("/{notif_id}", responses=NOT_FOUND_404)
def delete_notification(
    notif_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    notif = _owned_or_404(db, notif_id, user)
    db.delete(notif)
    db.commit()
    return {"ok": True}
