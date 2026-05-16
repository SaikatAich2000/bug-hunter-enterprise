"""Organization API.

Only the caller's own organization is reachable through these endpoints.
We never accept an org_id parameter from the client — it's always
inferred from the authenticated user. This keeps tenant isolation an
invariant of the URL space, not a per-handler check.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Activity, Organization, User
from app.schemas import OrganizationOut, OrganizationUpdate

router = APIRouter(prefix="/api/organization", tags=["organization"])


def _audit(db: Session, org_id: int, actor: User, action: str, detail: str) -> None:
    db.add(Activity(
        org_id=org_id, bug_id=None, entity_type="organization", entity_id=org_id,
        actor_user_id=actor.id, actor_name=actor.name,
        action=action, detail=detail,
    ))


@router.get("", response_model=OrganizationOut)
def get_my_org(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Organization:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.put("", response_model=OrganizationOut)
def update_my_org(
    payload: OrganizationUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Organization:
    org = db.get(Organization, user.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    fields = payload.model_dump(exclude_unset=True)
    changes = []
    for k, v in fields.items():
        old = getattr(org, k)
        if old != v:
            changes.append(f"{k}: {old!r} → {v!r}")
            setattr(org, k, v)
    if changes:
        _audit(db, org.id, user,
               "organization_updated",
               "Updated organization: " + "; ".join(changes))
    db.commit()
    db.refresh(org)
    return org
