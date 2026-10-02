"""Saved views: named filter snapshots shown above the item list.

Private to their owner; managers and admins can share one with the whole organization.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api_docs import NOT_FOUND_FORBIDDEN_403
from app.auth import can_invite, get_current_user
from app.database import get_db
from app.models import SavedView, User

router = APIRouter(prefix="/api/saved-views", tags=["saved-views"])

_MAX_FILTERS_JSON = 8000


def _filters_json(filters: dict) -> str:
    text = json.dumps(filters, separators=(",", ":"))
    if len(text) > _MAX_FILTERS_JSON:
        raise ValueError("The view's filters are too large")
    return text


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
    def from_row(cls, view: SavedView, viewer: User) -> "SavedViewOut":
        try:
            filters = json.loads(view.filters_json or "{}")
        except json.JSONDecodeError:
            filters = {}
        return cls(
            id=view.id, name=view.name, filters=filters if isinstance(filters, dict) else {},
            shared_with_org=bool(view.shared_with_org), owner_user_id=view.owner_user_id,
            is_mine=view.owner_user_id == viewer.id,
            created_at=view.created_at.isoformat(), updated_at=view.updated_at.isoformat(),
        )


class SavedViewIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    filters: dict = Field(default_factory=dict)
    shared_with_org: bool = False

    @field_validator("filters")
    @classmethod
    def _check_filters(cls, v: dict) -> dict:
        _filters_json(v)
        return v


class SavedViewUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    filters: Optional[dict] = None
    shared_with_org: Optional[bool] = None

    @field_validator("filters")
    @classmethod
    def _check_filters(cls, v: Optional[dict]) -> Optional[dict]:
        if v is not None:
            _filters_json(v)
        return v


def _view_or_404(db: Session, view_id: int, user: User) -> SavedView:
    view = db.get(SavedView, view_id)
    if view is None or view.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="View not found")
    if view.owner_user_id != user.id and not view.shared_with_org:
        raise HTTPException(status_code=404, detail="View not found")
    return view


def _owned_view(db: Session, view_id: int, user: User) -> SavedView:
    view = _view_or_404(db, view_id, user)
    if view.owner_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only the view's creator can change it.")
    return view


@router.get("")
def list_views(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[SavedViewOut]:
    rows = db.scalars(
        select(SavedView)
        .where(
            SavedView.org_id == user.org_id,
            or_(SavedView.owner_user_id == user.id, SavedView.shared_with_org.is_(True)),
        )
        .order_by(SavedView.shared_with_org.desc(), SavedView.name, SavedView.id)
    ).all()
    return [SavedViewOut.from_row(v, user) for v in rows]


@router.post("", status_code=201)
def create_view(
    payload: SavedViewIn, user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> SavedViewOut:
    view = SavedView(
        org_id=user.org_id, owner_user_id=user.id, name=payload.name.strip(),
        filters_json=_filters_json(payload.filters),
        shared_with_org=payload.shared_with_org and can_invite(user),
    )
    db.add(view)
    db.commit()
    db.refresh(view)
    return SavedViewOut.from_row(view, user)


@router.get("/{view_id}", responses=NOT_FOUND_FORBIDDEN_403)
def get_view(view_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> SavedViewOut:
    return SavedViewOut.from_row(_view_or_404(db, view_id, user), user)


@router.put("/{view_id}", responses=NOT_FOUND_FORBIDDEN_403)
def update_view(
    view_id: int, payload: SavedViewUpdateIn, user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SavedViewOut:
    view = _owned_view(db, view_id, user)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("name") is not None:
        view.name = fields["name"].strip()
    if fields.get("filters") is not None:
        view.filters_json = _filters_json(fields["filters"])
    if fields.get("shared_with_org") is not None:
        view.shared_with_org = bool(fields["shared_with_org"]) and can_invite(user)
    db.commit()
    db.refresh(view)
    return SavedViewOut.from_row(view, user)


@router.delete("/{view_id}", status_code=204, responses=NOT_FOUND_FORBIDDEN_403)
def delete_view(view_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    db.delete(_owned_view(db, view_id, user))
    db.commit()
