"""Webhooks API: outbound HTTP integrations for the caller's organization (admins only).

Webhooks fire organization-wide, so only admins manage them. The signing secret is shown
once, when a hook is created or its secret rotated, and never again.

  GET    /api/webhooks                     list
  POST   /api/webhooks                     create (returns the secret)
  GET    /api/webhooks/{id}                detail
  PUT    /api/webhooks/{id}                edit name, url, events, is_active
  POST   /api/webhooks/{id}/rotate-secret  new signing secret (returned once)
  DELETE /api/webhooks/{id}                remove
  POST   /api/webhooks/{id}/test           queue a synthetic webhook.ping
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api_docs import BAD_REQUEST_NOT_FOUND_404, NOT_FOUND_404
from app.auth import require_admin
from app.database import get_db
from app.models import Activity, User, Webhook
from app.secrets_box import seal
from app.webhooks_delivery import MAX_CONSECUTIVE_FAILURES, WebhookTargetError, check_hostname, deliver_event

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_URL_RE = re.compile(r"^https?://[\w\-.:/%?&=#~+,;@!$'()*\[\]]+$", re.IGNORECASE)
_EVENT_RE = re.compile(r"^(\*|[a-z_]+(\.[a-z_*]+)*)$")


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str
    events: str
    is_active: bool
    consecutive_failures: int
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    last_delivered_at: Optional[str] = None
    created_at: str
    # Only set in the response that creates the hook or rotates its secret.
    secret: Optional[str] = None

    @classmethod
    def from_row(cls, hook: Webhook, secret: str | None = None) -> "WebhookOut":
        return cls(
            id=hook.id, name=hook.name, url=hook.url, events=hook.events,
            is_active=bool(hook.is_active),
            consecutive_failures=int(hook.consecutive_failures or 0),
            last_status_code=hook.last_status_code, last_error=hook.last_error,
            last_delivered_at=hook.last_delivered_at.isoformat() if hook.last_delivered_at else None,
            created_at=hook.created_at.isoformat(), secret=secret,
        )


def _check_url(v: str) -> str:
    v = v.strip()
    if len(v) > 500:
        raise ValueError("URL too long")
    if not _URL_RE.match(v):
        raise ValueError("URL must start with http:// or https://")
    try:
        check_hostname(v)
    except WebhookTargetError as exc:
        raise ValueError(str(exc)) from exc
    return v


def _check_events(v: str) -> str:
    names = [e.strip() for e in v.split(",") if e.strip()]
    if not names or any(not _EVENT_RE.match(n) for n in names):
        raise ValueError('events must be "*" or comma-separated names such as bug.created or bug.*')
    return ",".join(names)


class WebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1)
    events: str = Field(default="*", min_length=1, max_length=500)
    is_active: bool = True

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _check_url(v)

    @field_validator("events")
    @classmethod
    def _events(cls, v: str) -> str:
        return _check_events(v)


class WebhookUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    url: Optional[str] = None
    events: Optional[str] = Field(default=None, min_length=1, max_length=500)
    is_active: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _check_url(v)

    @field_validator("events")
    @classmethod
    def _events(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _check_events(v)


def _audit(db: Session, user: User, hook_id: int, action: str, detail: str) -> None:
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="webhook", entity_id=hook_id,
        actor_user_id=user.id, actor_name=user.name, action=action, detail=detail,
    ))


def _hook_or_404(db: Session, hook_id: int, user: User) -> Webhook:
    hook = db.get(Webhook, hook_id)
    if hook is None or hook.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return hook


@router.get("")
def list_webhooks(user: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[WebhookOut]:
    rows = db.scalars(
        select(Webhook).where(Webhook.org_id == user.org_id)
        .order_by(Webhook.created_at.desc(), Webhook.id.desc())
    ).all()
    return [WebhookOut.from_row(h) for h in rows]


@router.post("", status_code=201)
def create_webhook(
    payload: WebhookIn, user: User = Depends(require_admin), db: Session = Depends(get_db),
) -> WebhookOut:
    secret = secrets.token_urlsafe(24)
    hook = Webhook(
        org_id=user.org_id, name=payload.name.strip(), url=payload.url, events=payload.events,
        is_active=payload.is_active, secret=seal(secret), created_by_user_id=user.id,
    )
    db.add(hook)
    db.flush()
    _audit(db, user, hook.id, "webhook_created", f"Created webhook '{hook.name}' → {hook.url}")
    db.commit()
    db.refresh(hook)
    return WebhookOut.from_row(hook, secret=secret)


@router.get("/{hook_id}", responses=NOT_FOUND_404)
def get_webhook(hook_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> WebhookOut:
    return WebhookOut.from_row(_hook_or_404(db, hook_id, user))


@router.put("/{hook_id}", responses=NOT_FOUND_404)
def update_webhook(
    hook_id: int, payload: WebhookUpdateIn, user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> WebhookOut:
    hook = _hook_or_404(db, hook_id, user)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("is_active") and hook.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        # An operator re-enabling a suspended hook gets a clean run before it can suspend again.
        hook.consecutive_failures, hook.last_error = 0, None
    for key, value in fields.items():
        if value is not None:
            setattr(hook, key, value.strip() if isinstance(value, str) else value)
    _audit(db, user, hook.id, "webhook_updated", f"Updated webhook '{hook.name}'")
    db.commit()
    db.refresh(hook)
    return WebhookOut.from_row(hook)


@router.post("/{hook_id}/rotate-secret", responses=NOT_FOUND_404)
def rotate_secret(hook_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> WebhookOut:
    hook = _hook_or_404(db, hook_id, user)
    secret = secrets.token_urlsafe(24)
    hook.secret = seal(secret)
    _audit(db, user, hook.id, "webhook_secret_rotated", f"Rotated the signing secret of '{hook.name}'")
    db.commit()
    db.refresh(hook)
    return WebhookOut.from_row(hook, secret=secret)


@router.delete("/{hook_id}", status_code=204, responses=NOT_FOUND_404)
def delete_webhook(hook_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)) -> None:
    hook = _hook_or_404(db, hook_id, user)
    name = hook.name
    db.delete(hook)
    _audit(db, user, hook_id, "webhook_deleted", f"Deleted webhook '{name}'")
    db.commit()


@router.post("/{hook_id}/test", status_code=202, responses=BAD_REQUEST_NOT_FOUND_404)
def test_webhook(
    hook_id: int, background: BackgroundTasks, user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    hook = _hook_or_404(db, hook_id, user)
    background.add_task(
        deliver_event, hook.org_id, "webhook.ping",
        {"hook_id": hook.id, "name": hook.name, "sent_by": user.email}, hook.id,
    )
    return {"message": "Test ping queued"}
