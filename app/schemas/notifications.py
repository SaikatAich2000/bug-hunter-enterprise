"""Pydantic schemas: notifications."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# --- Notification — per-user in-app notification row. ---
class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    title: str
    body: str
    bug_id: Optional[int] = None
    event_id: Optional[int] = None
    actor_name: str
    read_at: Optional[datetime] = None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


# --- Web push (Firebase Cloud Messaging) ---
class PushSubscribeIn(BaseModel):
    """A browser/device registering its FCM token for push."""
    token: str = Field(min_length=1, max_length=512)
    platform: str = Field(default="web", max_length=20)
    user_agent: str = Field(default="", max_length=400)


class PushUnsubscribeIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class PushConfigOut(BaseModel):
    """Public Firebase web config for the browser messaging SDK. All values are
    publishable; the service-account secret stays on the backend. `enabled` is
    False when push isn't configured."""
    enabled: bool
    api_key: str = ""
    auth_domain: str = ""
    project_id: str = ""
    messaging_sender_id: str = ""
    app_id: str = ""
    vapid_key: str = ""


class DeviceTokenIn(BaseModel):
    """A mobile client's FCM registration token."""
    token: str = Field(min_length=10, max_length=512)
    platform: str = Field(default="android", max_length=16)

    @field_validator("platform")
    @classmethod
    def _platform_lower(cls, v: str) -> str:
        return (v or "android").strip().lower() or "android"


class DeviceTokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform: str
    last_seen_at: Optional[datetime] = None
    created_at: datetime


class NotificationPreferencesOut(BaseModel):
    """Push channel toggles; a user without a stored row has every channel on."""
    mentions: bool = True
    assignments: bool = True
    activity: bool = True


class NotificationPreferencesIn(BaseModel):
    """Only the channels being changed need to be present."""
    mentions: Optional[bool] = None
    assignments: Optional[bool] = None
    activity: Optional[bool] = None
