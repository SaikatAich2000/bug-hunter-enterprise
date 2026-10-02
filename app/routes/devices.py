"""Mobile-client endpoints: device (push) registration and notification preferences.

  POST   /api/devices/register              register or refresh this device's FCM token
  DELETE /api/devices/{token}               forget a token (sign-out)
  GET    /api/notifications/preferences     push channel toggles
  PUT    /api/notifications/preferences     change them

Devices are stored with the web push subscriptions (see app/push_service.py). Everything is
keyed by the signed-in user; nobody can touch another user's tokens.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import push_service
from app.api_docs import CONFLICT_409
from app.auth import get_current_user
from app.database import get_db
from app.models import NotificationPreference, PushSubscription, User
from app.push_service import PushTokenConflict
from app.schemas import (
    DeviceTokenIn,
    DeviceTokenOut,
    NotificationPreferencesIn,
    NotificationPreferencesOut,
)

router = APIRouter(tags=["devices"])


@router.post("/api/devices/register", response_model=DeviceTokenOut, responses=CONFLICT_409)
def register_device(
    payload: DeviceTokenIn, actor: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> PushSubscription:
    """Idempotent: registering the same token again just refreshes it. A token already bound to
    another account is refused, so a replayed token cannot redirect that user's pushes."""
    try:
        push_service.register(
            db, user_id=actor.id, token=payload.token, platform=payload.platform, user_agent="",
        )
    except PushTokenConflict as exc:
        raise HTTPException(
            status_code=409, detail="This device token is already registered to another account.",
        ) from exc
    db.commit()
    return db.scalar(select(PushSubscription).where(PushSubscription.token == payload.token))


@router.delete("/api/devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(
    token: str, actor: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> Response:
    """204 whether or not the token existed: the goal is that the server no longer has it."""
    db.execute(delete(PushSubscription).where(
        PushSubscription.token == token, PushSubscription.user_id == actor.id,
    ))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _preferences(db: Session, user_id: int) -> NotificationPreference | None:
    return db.get(NotificationPreference, user_id)


def _to_out(row: NotificationPreference | None) -> NotificationPreferencesOut:
    if row is None:
        return NotificationPreferencesOut()
    return NotificationPreferencesOut(
        mentions=row.mentions, assignments=row.assignments, activity=row.activity,
    )


@router.get("/api/notifications/preferences")
def get_preferences(
    actor: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> NotificationPreferencesOut:
    return _to_out(_preferences(db, actor.id))


@router.put("/api/notifications/preferences")
def update_preferences(
    payload: NotificationPreferencesIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationPreferencesOut:
    row = _preferences(db, actor.id)
    if row is None:
        row = NotificationPreference(user_id=actor.id)
        db.add(row)
    for channel, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, channel, value)
    db.commit()
    db.refresh(row)
    return _to_out(row)
