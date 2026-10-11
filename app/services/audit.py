"""One writer for Activity (audit-trail) rows."""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Activity


def audit(
    db: Session,
    actor: Any,
    entity_type: str,
    entity_id: Optional[int],
    action: str,
    detail: str,
    *,
    bug_id: Optional[int] = None,
    org_id: Optional[int] = None,
) -> None:
    """Queue an Activity row in the actor's organization (``org_id`` when there is no actor).
    actor may be None (recorded as "system")."""
    db.add(Activity(
        org_id=getattr(actor, "org_id", None) if org_id is None else org_id,
        bug_id=bug_id, entity_type=entity_type, entity_id=entity_id,
        actor_user_id=getattr(actor, "id", None),
        actor_name=getattr(actor, "name", "") or "system",
        action=action, detail=detail,
    ))
