"""Webhooks CRUD — outbound HTTP integrations for an organization.

Endpoints (admin-only — webhooks fire org-wide, so creators must have
org-admin rights to avoid privilege escalation via 3rd-party listeners).

  GET    /api/webhooks                     — list this org's hooks
  POST   /api/webhooks                     — create one
  GET    /api/webhooks/{id}                — detail
  PUT    /api/webhooks/{id}                — edit name/url/events/is_active
  DELETE /api/webhooks/{id}                — remove
  POST   /api/webhooks/{id}/test           — fire a synthetic ping event
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.config import get_settings
from app.database import get_db
from app.models import Activity, Webhook, User
from app.webhooks_delivery import deliver_event

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_URL_RE = re.compile(r"^https?://[\w\-.:/%?&=#~+,;@!$'()*]+$", re.IGNORECASE)


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

    @classmethod
    def from_row(cls, w: Webhook) -> "WebhookOut":
        return cls(
            id=w.id, name=w.name, url=w.url, events=w.events,
            is_active=bool(w.is_active),
            consecutive_failures=int(w.consecutive_failures or 0),
            last_status_code=w.last_status_code,
            last_error=w.last_error,
            last_delivered_at=w.last_delivered_at.isoformat() if w.last_delivered_at else None,
            created_at=w.created_at.isoformat(),
        )


class WebhookIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    url: str = Field(..., min_length=1)
    events: str = Field("*", min_length=1, max_length=500)
    is_active: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        settings = get_settings()
        if len(v) > settings.WEBHOOK_MAX_URL_LENGTH:
            raise ValueError("URL too long")
        if not _URL_RE.match(v):
            raise ValueError("URL must be http:// or https://")
        # Block obvious SSRF vectors. We disallow private IPs in the
        # hostname so a malicious admin can't probe internal services.
        lower = v.lower()
        for blocked in ("://localhost", "://127.", "://0.", "://10.", "://192.168.", "://169.254."):
            if blocked in lower:
                raise ValueError("Webhook URLs must point at a public host (no localhost / private ranges).")
        return v


class WebhookUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    url: Optional[str] = None
    events: Optional[str] = Field(None, min_length=1, max_length=500)
    is_active: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return WebhookIn._validate_url(v)


@router.get("", response_model=list[WebhookOut])
def list_webhooks(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[WebhookOut]:
    rows = list(db.scalars(
        select(Webhook).where(Webhook.org_id == user.org_id).order_by(Webhook.created_at.desc())
    ).all())
    return [WebhookOut.from_row(w) for w in rows]


@router.post("", response_model=WebhookOut, status_code=201)
def create_webhook(
    payload: WebhookIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> WebhookOut:
    w = Webhook(
        org_id=user.org_id,
        name=payload.name.strip(),
        url=payload.url.strip(),
        events=payload.events.strip(),
        is_active=payload.is_active,
        secret=secrets.token_urlsafe(24),
        created_by_user_id=user.id,
    )
    db.add(w)
    db.flush()
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="webhook", entity_id=w.id,
        actor_user_id=user.id, actor_name=user.name,
        action="webhook_created",
        detail=f"Created webhook '{w.name}' → {w.url}",
    ))
    db.commit()
    db.refresh(w)
    return WebhookOut.from_row(w)


def _get_webhook_or_404(db: Session, hook_id: int, user: User) -> Webhook:
    w = db.get(Webhook, hook_id)
    if w is None or w.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return w


@router.get("/{hook_id}", response_model=WebhookOut)
def get_webhook(
    hook_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> WebhookOut:
    return WebhookOut.from_row(_get_webhook_or_404(db, hook_id, user))


@router.put("/{hook_id}", response_model=WebhookOut)
def update_webhook(
    hook_id: int,
    payload: WebhookUpdateIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> WebhookOut:
    w = _get_webhook_or_404(db, hook_id, user)
    fields = payload.model_dump(exclude_unset=True)
    if "is_active" in fields and fields["is_active"] and (w.consecutive_failures or 0) >= 10:
        # Operator re-enabling — reset failure counter so the hook gets
        # a clean run before auto-suspending again.
        w.consecutive_failures = 0
        w.last_error = None
    for k, v in fields.items():
        setattr(w, k, v.strip() if isinstance(v, str) else v)
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="webhook", entity_id=w.id,
        actor_user_id=user.id, actor_name=user.name,
        action="webhook_updated",
        detail=f"Updated webhook '{w.name}'",
    ))
    db.commit()
    db.refresh(w)
    return WebhookOut.from_row(w)


@router.delete("/{hook_id}", status_code=204)
def delete_webhook(
    hook_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    w = _get_webhook_or_404(db, hook_id, user)
    name = w.name
    db.delete(w)
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="webhook", entity_id=hook_id,
        actor_user_id=user.id, actor_name=user.name,
        action="webhook_deleted",
        detail=f"Deleted webhook '{name}'",
    ))
    db.commit()


@router.post("/{hook_id}/test", status_code=202)
def test_webhook(
    hook_id: int,
    background: BackgroundTasks,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    w = _get_webhook_or_404(db, hook_id, user)
    background.add_task(
        deliver_event, w.org_id, "webhook.ping",
        {"hook_id": w.id, "name": w.name, "sent_by": user.email},
    )
    return {"message": "Test ping queued"}
