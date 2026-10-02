"""Idempotency-Key support for Start/Complete/Cancel Sprint and bulk mutations.

Same key + same payload -> replay the cached response. Same key + different
payload -> 409 (IdempotencyConflict). Expired rows are lazily reaped when the
same key is looked up again; a broader periodic sweep is a later-slice
addition.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IdempotencyKey

EXPIRY_HOURS = 24


class IdempotencyConflict(Exception):
    """Same Idempotency-Key reused with a different request payload."""


def fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_cached_response(
    db: Session, user_id: int, project_id: int, operation_key: str,
    idempotency_key: str, payload: dict,
) -> tuple[int, str] | None:
    """Return (status, body) to replay, or None if this is a fresh request.

    Raises IdempotencyConflict if the key is reused with a different payload.
    """
    row = db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.project_id == project_id,
            IdempotencyKey.operation_key == operation_key,
            IdempotencyKey.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        # SQLite round-trips DateTime(timezone=True) as naive; treat as UTC
        # (all Agile timestamps are written in UTC — see _utcnow()).
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < _utcnow():
        db.execute(delete(IdempotencyKey).where(IdempotencyKey.id == row.id))
        db.flush()
        return None
    if row.request_fingerprint != fingerprint(payload):
        raise IdempotencyConflict(
            f"Idempotency-Key '{idempotency_key}' was already used with a different request"
        )
    return row.response_status, row.response_body


def store_result(
    db: Session, user_id: int, project_id: int, operation_key: str,
    idempotency_key: str, payload: dict, response_status: int, response_body: str,
) -> None:
    db.add(IdempotencyKey(
        user_id=user_id,
        project_id=project_id,
        operation_key=operation_key,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint(payload),
        response_status=response_status,
        response_body=response_body,
        created_at=_utcnow(),
        expires_at=_utcnow() + timedelta(hours=EXPIRY_HOURS),
    ))
    db.flush()
