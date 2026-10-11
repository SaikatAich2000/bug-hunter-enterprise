"""Audit-log retention: delete audit rows older than AUDIT_RETENTION_DAYS (0 keeps everything).

Run daily by the in-app scheduler (app/scheduler.py). History-based reports (status changes,
time to resolution) only cover the retention window.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.jobs.retention import _purge
from app.models import Activity

logger = logging.getLogger("bug_hunter.audit_retention")


def purge_expired(db: Session, retention_days: int) -> int:
    """Delete audit rows older than ``retention_days`` and return how many went."""
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    # Id-chunked, one commit per chunk: a year of backlog never holds one long lock or
    # builds one huge transaction.
    deleted = _purge(db, Activity, Activity.created_at < cutoff)
    if deleted:
        logger.info("Audit retention: purged %d row(s) older than %d days", deleted, retention_days)
    return deleted
