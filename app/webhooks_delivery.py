"""Outbound webhook delivery.

We don't have a queue (no Redis on the deployment target by default),
so we fire each webhook from a FastAPI BackgroundTask and accept the
trade-off: deliveries are best-effort and may be lost if the worker
restarts mid-flight. For high-stakes integrations we recommend listeners
implement idempotency (we include a unique `delivery_id` in the payload)
and operators ack receipt before treating it as durable.

Auto-suspension: ten consecutive failures (timeout, connect error,
non-2xx) flips `is_active=false` so a misconfigured listener can't keep
generating retry noise indefinitely. Operators re-enable from the
settings UI once they've fixed their endpoint.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import Webhook
from app.observability import record_event

logger = logging.getLogger("bug_hunter.webhooks")

_MAX_CONSECUTIVE_FAILURES = 10


def _matches_event(subscriptions: str, event: str) -> bool:
    """`subscriptions` is comma-separated. Either "*" or a glob like
    "bug.*" or an exact event matches. We keep this dumb-simple so
    operators can write what they expect without quoting rules."""
    if not subscriptions:
        return False
    for s in subscriptions.split(","):
        s = s.strip()
        if not s:
            continue
        if s == "*" or s == event:
            return True
        if s.endswith(".*") and event.startswith(s[:-1]):
            return True
    return False


def _sign_payload(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the JSON body. Listeners verify by computing the
    same and using constant-time compare."""
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def deliver_event(org_id: int, event: str, payload: dict[str, Any]) -> None:
    """Synchronous delivery of `event` to all active hooks in `org_id`
    that subscribe to it. Intended to be called from BackgroundTasks
    so it doesn't block the request's response.

    We open a fresh DB session here (the request's session may already
    be closed by the time the background task runs).
    """
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        hooks = list(db.scalars(
            select(Webhook).where(Webhook.org_id == org_id, Webhook.is_active.is_(True))
        ).all())
    except Exception:
        logger.exception("failed to load webhooks for org_id=%s", org_id)
        db.close()
        return

    matching = [h for h in hooks if _matches_event(h.events, event)]
    if not matching:
        db.close()
        return

    delivery_id = secrets.token_hex(12)
    body_dict = {
        "delivery_id": delivery_id,
        "event": event,
        "org_id": org_id,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    body = json.dumps(body_dict, default=str).encode("utf-8")

    timeout = httpx.Timeout(settings.WEBHOOK_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for hook in matching:
            sig = _sign_payload(hook.secret, body)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "BugHunter-Webhook/2.2",
                "X-BugHunter-Event": event,
                "X-BugHunter-Delivery": delivery_id,
                "X-BugHunter-Signature": sig,
            }
            start = time.monotonic()
            try:
                resp = client.post(hook.url, content=body, headers=headers)
                latency_ms = (time.monotonic() - start) * 1000
                hook.last_delivered_at = datetime.now(timezone.utc)
                hook.last_status_code = resp.status_code
                if 200 <= resp.status_code < 300:
                    hook.consecutive_failures = 0
                    hook.last_error = None
                    record_event(f"webhook_ok_{event}")
                    logger.info(
                        "webhook delivered hook=%s event=%s status=%d (%.0f ms)",
                        hook.id, event, resp.status_code, latency_ms,
                    )
                else:
                    hook.consecutive_failures = (hook.consecutive_failures or 0) + 1
                    hook.last_error = f"HTTP {resp.status_code}"
                    record_event(f"webhook_fail_{event}")
                    logger.warning(
                        "webhook non-2xx hook=%s event=%s status=%d failures=%d",
                        hook.id, event, resp.status_code, hook.consecutive_failures,
                    )
            except Exception as exc:
                hook.consecutive_failures = (hook.consecutive_failures or 0) + 1
                hook.last_error = str(exc)[:500]
                hook.last_status_code = None
                hook.last_delivered_at = datetime.now(timezone.utc)
                record_event(f"webhook_fail_{event}")
                logger.warning(
                    "webhook error hook=%s event=%s err=%s failures=%d",
                    hook.id, event, exc, hook.consecutive_failures,
                )
            if hook.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                hook.is_active = False
                logger.warning(
                    "webhook auto-suspended after %d failures: hook_id=%s url=%s",
                    hook.consecutive_failures, hook.id, hook.url,
                )

    try:
        db.commit()
    except Exception:
        logger.exception("failed to persist webhook delivery state")
        db.rollback()
    finally:
        db.close()
