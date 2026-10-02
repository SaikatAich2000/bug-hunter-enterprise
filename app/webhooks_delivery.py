"""Outbound webhook delivery.

Deliveries run in a FastAPI BackgroundTask right after the request commits: best-effort,
no queue, and lost if the worker restarts mid-flight. Each payload carries a unique
``delivery_id`` so listeners can deduplicate. Ten consecutive failures suspend a hook so a
broken listener cannot generate noise forever; an admin re-enables it once it is fixed.

Destinations on private networks (loopback, RFC 1918, link-local, cloud metadata hosts) are
refused unless WEBHOOK_ALLOW_PRIVATE_NETWORKS is set: the hostname is checked when the hook
is saved and every address it resolves to is checked again at delivery time. Redirects are
never followed.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.metrics import record_event
from app.models import Webhook
from app.secrets_box import SecretUnreadable, unseal

logger = logging.getLogger("bug_hunter.webhooks")

MAX_CONSECUTIVE_FAILURES = 10

# Names that resolve inside private infrastructure without being IP literals.
_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal"})
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")

PRIVATE_TARGET_MESSAGE = "Webhook URLs must point at a public host (no localhost or private ranges)."


class WebhookTargetError(ValueError):
    """The destination is not allowed (private network) or cannot be resolved."""


def _is_private(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def _literal_ip(host: str) -> str | None:
    """The host as an IP address string when it is an IP literal (including the plain-integer
    form ``2130706433``), else None."""
    candidate = str(ipaddress.ip_address(int(host))) if host.isdigit() and int(host) < 2**32 else host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def check_hostname(url: str) -> str:
    """Static check of the URL's host (no DNS); returns the host. Raises WebhookTargetError."""
    try:
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    except ValueError as exc:  # malformed IPv6 brackets
        raise WebhookTargetError(PRIVATE_TARGET_MESSAGE) from exc
    if not host:
        raise WebhookTargetError(PRIVATE_TARGET_MESSAGE)
    if get_settings().WEBHOOK_ALLOW_PRIVATE_NETWORKS:
        return host
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise WebhookTargetError(PRIVATE_TARGET_MESSAGE)
    address = _literal_ip(host)
    if address is not None and _is_private(address):
        raise WebhookTargetError(PRIVATE_TARGET_MESSAGE)
    return host


def check_destination(url: str) -> None:
    """Delivery-time check: the host passes ``check_hostname`` and every address it resolves
    to is public."""
    host = check_hostname(url)
    if get_settings().WEBHOOK_ALLOW_PRIVATE_NETWORKS or _literal_ip(host) is not None:
        return
    port = urlparse(url).port or (443 if url.lower().startswith("https") else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebhookTargetError(f"Could not resolve {host}") from exc
    if any(_is_private(info[4][0]) for info in infos):
        raise WebhookTargetError(PRIVATE_TARGET_MESSAGE)


def matches_event(subscriptions: str, event: str) -> bool:
    """``subscriptions`` is comma-separated: "*", an exact name, or a family such as "bug.*"."""
    for raw in subscriptions.split(","):
        sub = raw.strip()
        if sub == "*" or sub == event:
            return True
        if sub.endswith(".*") and event.startswith(sub[:-1]):
            return True
    return False


def sign(secret: str, body: bytes) -> str:
    """``sha256=<hex>`` HMAC of the body, for listeners to verify in constant time."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(client: httpx.Client, hook: Webhook, body: bytes, event: str, delivery_id: str) -> None:
    """Send one delivery and record its outcome on ``hook``."""
    now = datetime.now(timezone.utc)
    hook.last_delivered_at = now
    try:
        check_destination(hook.url)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"BugHunter-Webhook/{get_settings().APP_VERSION or 'dev'}",
            "X-BugHunter-Event": event,
            "X-BugHunter-Delivery": delivery_id,
            "X-BugHunter-Signature": sign(unseal(hook.secret), body),
        }
        started = time.monotonic()
        response = client.post(hook.url, content=body, headers=headers)
        hook.last_status_code = response.status_code
        ok = 200 <= response.status_code < 300
        error = None if ok else f"HTTP {response.status_code}"
        logger.info("webhook hook=%s event=%s status=%d (%.0f ms)", hook.id, event,
                    response.status_code, (time.monotonic() - started) * 1000)
    except (WebhookTargetError, SecretUnreadable) as exc:
        hook.last_status_code, ok, error = None, False, str(exc)[:500]
    except httpx.HTTPError as exc:
        hook.last_status_code, ok, error = None, False, f"{type(exc).__name__}: {exc}"[:500]
    if ok:
        hook.consecutive_failures, hook.last_error = 0, None
        record_event("webhook_ok")
        return
    hook.consecutive_failures = (hook.consecutive_failures or 0) + 1
    hook.last_error = error
    record_event("webhook_failure")
    logger.warning("webhook failed hook=%s event=%s error=%s failures=%d",
                   hook.id, event, error, hook.consecutive_failures)
    if hook.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        hook.is_active = False
        logger.warning("webhook suspended after %d failures: hook_id=%s",
                       hook.consecutive_failures, hook.id)


def deliver_event(
    org_id: int, event: str, payload: dict[str, Any], hook_id: int | None = None,
) -> None:
    """Send ``event`` to every active hook of the organization that subscribes to it, or only
    to ``hook_id`` (a test ping) whatever its subscriptions and state.

    Runs as a BackgroundTask, so it opens its own session: the request's is already closed."""
    db = SessionLocal()
    try:
        stmt = select(Webhook).where(Webhook.org_id == org_id)
        if hook_id is not None:
            hooks = list(db.scalars(stmt.where(Webhook.id == hook_id)).all())
        else:
            hooks = [
                h for h in db.scalars(stmt.where(Webhook.is_active.is_(True))).all()
                if matches_event(h.events, event)
            ]
        if not hooks:
            return
        delivery_id = secrets.token_hex(12)
        body = json.dumps({
            "delivery_id": delivery_id, "event": event, "org_id": org_id,
            "delivered_at": datetime.now(timezone.utc).isoformat(), "payload": payload,
        }, default=str).encode("utf-8")
        timeout = httpx.Timeout(get_settings().WEBHOOK_TIMEOUT_SECONDS)
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for hook in hooks:
                _post(client, hook, body, event, delivery_id)
        db.commit()
    except Exception:  # noqa: BLE001 - a background task must never raise into the worker
        logger.exception("webhook delivery failed for org_id=%s event=%s", org_id, event)
        db.rollback()
    finally:
        db.close()
