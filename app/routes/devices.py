"""Device-token + notification-preferences endpoints (v2.10).

The Android client POSTs its current FCM registration token here so the
push service can fan out notifications. Endpoints:

  POST   /api/devices/register     — upsert (user_id, token) row
  DELETE /api/devices/{token}      — remove a single token (logout / etc.)
  GET    /api/notifications/preferences
  PUT    /api/notifications/preferences

Cross-org isolation: every operation is keyed by the authenticated user.
A token row belongs to whoever owns the user row; there is no way for a
caller to operate on another user's token, even within the same org.

Tokens are NOT shared across users — Firebase generates one token per
(device, app-install) pair, and reinstalling the app rotates it. If the
same token shows up under a different user (because user A signed out
and user B signed in on the same device), we move the row to user B —
the previous owner stops receiving pushes immediately, which is the
behaviour we want for a shared phone.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import DeviceToken, NotificationPreference, User
from app.schemas import (
    DeviceTokenIn,
    DeviceTokenOut,
    NotificationPreferencesIn,
    NotificationPreferencesOut,
)

router = APIRouter(tags=["devices"])


# ---------------------------------------------------------------------------
# Device tokens
# ---------------------------------------------------------------------------
@router.post(
    "/api/devices/register",
    response_model=DeviceTokenOut,
    status_code=status.HTTP_200_OK,
)
def register_device(
    payload: DeviceTokenIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upsert a (user, token) pair. Idempotent: re-registering the same
    token just refreshes `last_seen_at`."""
    existing = db.scalar(select(DeviceToken).where(DeviceToken.token == payload.token))
    now = datetime.now(timezone.utc)
    if existing is not None:
        # If the token belonged to a different user (shared device, then
        # second user logs in), move ownership over. The previous owner
        # stops getting pushes — that's the intended behaviour.
        existing.user_id = actor.id
        existing.platform = payload.platform
        existing.last_seen_at = now
        db.commit()
        db.refresh(existing)
        return existing

    row = DeviceToken(
        user_id=actor.id,
        token=payload.token,
        platform=payload.platform,
        last_seen_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/api/devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(
    token: str,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a token registered to the current user. Returns 204
    whether the token existed or not — the goal is "the server no longer
    has this token for me", which is true in both cases."""
    db.execute(
        delete(DeviceToken)
        .where(DeviceToken.token == token)
        .where(DeviceToken.user_id == actor.id)
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Notification preferences
# ---------------------------------------------------------------------------
def _read_or_default(db: Session, user_id: int) -> NotificationPreference | None:
    return db.scalar(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )


def _to_response(row: NotificationPreference | None) -> NotificationPreferencesOut:
    """A missing row means "all channels on" — we surface that derived
    default directly instead of 404-ing the client. Simpler for the SPA
    and the Android client; both can treat the response as authoritative."""
    if row is None:
        return NotificationPreferencesOut(mentions=True, assignments=True, activity=True)
    return NotificationPreferencesOut(
        mentions=row.mentions,
        assignments=row.assignments,
        activity=row.activity,
    )


@router.get("/api/notifications/preferences", response_model=NotificationPreferencesOut)
def get_preferences(
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _to_response(_read_or_default(db, actor.id))


@router.put("/api/notifications/preferences", response_model=NotificationPreferencesOut)
def update_preferences(
    payload: NotificationPreferencesIn,
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _read_or_default(db, actor.id)
    if row is None:
        row = NotificationPreference(user_id=actor.id)
        db.add(row)
    if payload.mentions is not None:
        row.mentions = payload.mentions
    if payload.assignments is not None:
        row.assignments = payload.assignments
    if payload.activity is not None:
        row.activity = payload.activity
    db.commit()
    db.refresh(row)
    return _to_response(row)
