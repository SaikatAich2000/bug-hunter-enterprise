"""Saved views — named filter snapshots above the bug list.

Per-user by default. Admins/managers can flip `shared_with_org=true`
to make a view visible to the whole org (team queues).
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import can_invite, get_current_user
from app.database import get_db
from app.models import SavedView, User

router = APIRouter(prefix="/api/saved-views", tags=["saved-views"])


class SavedViewOut(BaseModel):
    id: int
    name: str
    filters: dict
    shared_with_org: bool
    owner_user_id: int
    is_mine: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, v: SavedView, viewer: User) -> "SavedViewOut":
        try:
            filters = json.loads(v.filters_json or "{}")
        except json.JSONDecodeError:
            filters = {}
        return cls(
            id=v.id, name=v.name, filters=filters,
            shared_with_org=bool(v.shared_with_org),
            owner_user_id=v.owner_user_id,
            is_mine=v.owner_user_id == viewer.id,
            created_at=v.created_at.isoformat(),
            updated_at=v.updated_at.isoformat(),
        )


class SavedViewIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    filters: dict = Field(default_factory=dict)
    shared_with_org: bool = False


class SavedViewUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    filters: Optional[dict] = None
    shared_with_org: Optional[bool] = None


@router.get("", response_model=list[SavedViewOut])
def list_views(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SavedViewOut]:
    rows = list(db.scalars(
        select(SavedView)
        .where(
            SavedView.org_id == user.org_id,
            or_(
                SavedView.owner_user_id == user.id,
                SavedView.shared_with_org.is_(True),
            ),
        )
        .order_by(SavedView.shared_with_org.desc(), SavedView.name.asc())
    ).all())
    return [SavedViewOut.from_row(v, user) for v in rows]


@router.post("", response_model=SavedViewOut, status_code=201)
def create_view(
    payload: SavedViewIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedViewOut:
    # Only managers/admins can publish org-wide views.
    shared = payload.shared_with_org and can_invite(user)
    v = SavedView(
        org_id=user.org_id,
        owner_user_id=user.id,
        name=payload.name.strip(),
        filters_json=json.dumps(payload.filters)[:8000],
        shared_with_org=shared,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return SavedViewOut.from_row(v, user)


def _get_view_or_404(db: Session, view_id: int, user: User) -> SavedView:
    v = db.get(SavedView, view_id)
    if v is None or v.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="View not found")
    # Visibility: own view OR org-shared view.
    if v.owner_user_id != user.id and not v.shared_with_org:
        raise HTTPException(status_code=404, detail="View not found")
    return v


@router.get("/{view_id}", response_model=SavedViewOut)
def get_view(
    view_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedViewOut:
    return SavedViewOut.from_row(_get_view_or_404(db, view_id, user), user)


@router.put("/{view_id}", response_model=SavedViewOut)
def update_view(
    view_id: int,
    payload: SavedViewUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedViewOut:
    v = _get_view_or_404(db, view_id, user)
    # Only the owner can edit. Admins/managers shouldn't silently
    # overwrite someone else's saved filter — they can create their own.
    if v.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the view's creator can edit it.")
    fields = payload.model_dump(exclude_unset=True)
    if "name" in fields:
        v.name = fields["name"].strip()
    if "filters" in fields:
        v.filters_json = json.dumps(fields["filters"])[:8000]
    if "shared_with_org" in fields:
        v.shared_with_org = bool(fields["shared_with_org"]) and can_invite(user)
    db.commit()
    db.refresh(v)
    return SavedViewOut.from_row(v, user)


@router.delete("/{view_id}", status_code=204)
def delete_view(
    view_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    v = _get_view_or_404(db, view_id, user)
    if v.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the view's creator can delete it.")
    db.delete(v)
    db.commit()
