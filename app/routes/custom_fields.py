"""Custom fields: project-level definitions that admins and the project's leads manage, and
the per-item values anyone with access to the project fills in.

  GET    /api/projects/{project_id}/custom-fields             list
  POST   /api/projects/{project_id}/custom-fields             create
  PUT    /api/projects/{project_id}/custom-fields/{field_id}  edit
  DELETE /api/projects/{project_id}/custom-fields/{field_id}  remove
  GET    /api/bugs/{bug_id}/custom-values                     read
  PUT    /api/bugs/{bug_id}/custom-values                     replace the item's values
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import custom_values as cv
from app.access import (
    accessible_project_ids,
    can_access_project,
    can_manage_project,
    get_org_project_or_404,
)
from app.api_docs import BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404, BAD_REQUEST_NOT_FOUND_404
from app.auth import can_edit_bug, get_current_user
from app.database import get_db
from app.models import Activity, Bug, BugCustomValue, CustomField, User
from app.schemas import CustomValueIn
from app.webhooks_delivery import deliver_event

router = APIRouter(tags=["custom-fields"])

FIELD_TYPES = ("text", "number", "date", "select")
_MAX_OPTIONS_LENGTH = 500

_DETAIL_BUG_NOT_FOUND = "Bug not found"
_DETAIL_FIELD_NOT_FOUND = "Field not found"
_DETAIL_FORBIDDEN = "Only admins and project leads can manage custom fields"


class CustomFieldOut(BaseModel):
    id: int
    project_id: int
    name: str
    field_type: str
    options: list[str]
    is_required: bool
    position: int

    @classmethod
    def from_row(cls, f: CustomField) -> "CustomFieldOut":
        return cls(
            id=f.id, project_id=f.project_id, name=f.name, field_type=f.field_type,
            options=[o for o in (f.options or "").split("|") if o],
            is_required=bool(f.is_required), position=int(f.position or 0),
        )


def _clean_options(options: list[str]) -> str:
    cleaned = [o.strip() for o in options if o.strip()]
    if any("|" in o for o in cleaned):
        raise ValueError('Options cannot contain "|"')
    joined = "|".join(cleaned)
    if len(joined) > _MAX_OPTIONS_LENGTH:
        raise ValueError(f"Options are too long ({_MAX_OPTIONS_LENGTH} characters at most in total)")
    return joined


class CustomFieldIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    field_type: str = Field(default="text", max_length=20)
    options: list[str] = Field(default_factory=list, max_length=100)
    is_required: bool = False
    position: int = Field(default=0, ge=0, le=10_000)

    @field_validator("field_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in FIELD_TYPES:
            raise ValueError(f"field_type must be one of {list(FIELD_TYPES)}")
        return v

    @field_validator("options")
    @classmethod
    def _check_options(cls, v: list[str]) -> list[str]:
        _clean_options(v)
        return v


class CustomFieldUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    field_type: Optional[str] = Field(default=None, max_length=20)
    options: Optional[list[str]] = Field(default=None, max_length=100)
    is_required: Optional[bool] = None
    position: Optional[int] = Field(default=None, ge=0, le=10_000)

    @field_validator("field_type")
    @classmethod
    def _check_type(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else CustomFieldIn._check_type(v)

    @field_validator("options")
    @classmethod
    def _check_options(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            _clean_options(v)
        return v


class CustomValueOut(BaseModel):
    field_id: int
    value: str


def _audit(db: Session, user: User, field_id: int, action: str, detail: str) -> None:
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="custom_field", entity_id=field_id,
        actor_user_id=user.id, actor_name=user.name, action=action, detail=detail,
    ))


def _managed_project(db: Session, project_id: int, user: User):
    project = get_org_project_or_404(db, project_id, user)
    if not can_manage_project(db, user, project):
        raise HTTPException(status_code=403, detail=_DETAIL_FORBIDDEN)
    return project


def _field_or_404(db: Session, project_id: int, field_id: int) -> CustomField:
    field = db.get(CustomField, field_id)
    if field is None or field.project_id != project_id:
        raise HTTPException(status_code=404, detail=_DETAIL_FIELD_NOT_FOUND)
    return field


@router.get("/api/projects/{project_id}/custom-fields",
            responses=BAD_REQUEST_NOT_FOUND_404)
def list_fields(
    project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[CustomFieldOut]:
    project = get_org_project_or_404(db, project_id, user)
    if not can_access_project(accessible_project_ids(db, user), project.id):
        raise HTTPException(status_code=404, detail="Project not found")
    rows = db.scalars(
        select(CustomField).where(CustomField.project_id == project_id)
        .order_by(CustomField.position, CustomField.id)
    ).all()
    return [CustomFieldOut.from_row(r) for r in rows]


@router.post("/api/projects/{project_id}/custom-fields",
             status_code=201, responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def create_field(
    project_id: int, payload: CustomFieldIn, user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomFieldOut:
    project = _managed_project(db, project_id, user)
    field = CustomField(
        project_id=project_id, name=payload.name.strip(), field_type=payload.field_type,
        options=_clean_options(payload.options), is_required=payload.is_required,
        position=payload.position,
    )
    db.add(field)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A field with that name already exists") from exc
    _audit(db, user, field.id, "custom_field_created",
           f"Added field '{field.name}' ({field.field_type}) to project {project.name}")
    db.commit()
    db.refresh(field)
    return CustomFieldOut.from_row(field)


@router.put("/api/projects/{project_id}/custom-fields/{field_id}",
            responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def update_field(
    project_id: int, field_id: int, payload: CustomFieldUpdateIn,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> CustomFieldOut:
    project = _managed_project(db, project_id, user)
    field = _field_or_404(db, project_id, field_id)
    changes = payload.model_dump(exclude_unset=True)
    if "options" in changes:
        field.options = _clean_options(changes.pop("options") or [])
    for key, value in changes.items():
        if value is not None:
            setattr(field, key, value.strip() if isinstance(value, str) else value)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="A field with that name already exists") from exc
    _audit(db, user, field.id, "custom_field_updated",
           f"Updated field '{field.name}' of project {project.name}")
    db.commit()
    db.refresh(field)
    return CustomFieldOut.from_row(field)


@router.delete("/api/projects/{project_id}/custom-fields/{field_id}", status_code=204,
               responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def delete_field(
    project_id: int, field_id: int, user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    project = _managed_project(db, project_id, user)
    field = _field_or_404(db, project_id, field_id)
    name = field.name
    db.delete(field)
    _audit(db, user, field_id, "custom_field_deleted",
           f"Removed field '{name}' from project {project.name}")
    db.commit()


def _accessible_bug(db: Session, bug_id: int, user: User) -> Bug:
    bug = db.get(Bug, bug_id)
    if bug is None or not can_access_project(accessible_project_ids(db, user), bug.project_id):
        raise HTTPException(status_code=404, detail=_DETAIL_BUG_NOT_FOUND)
    return bug


@router.get("/api/bugs/{bug_id}/custom-values",
            responses=BAD_REQUEST_NOT_FOUND_404)
def list_values(
    bug_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[CustomValueOut]:
    _accessible_bug(db, bug_id, user)
    rows = db.scalars(select(BugCustomValue).where(BugCustomValue.bug_id == bug_id)).all()
    return [CustomValueOut(field_id=r.field_id, value=r.value) for r in rows]


@router.put("/api/bugs/{bug_id}/custom-values",
            responses=BAD_REQUEST_FORBIDDEN_CONFLICT_NOT_FOUND_404)
def set_values(
    bug_id: int, payload: list[CustomValueIn], background: BackgroundTasks,
    user: User = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[CustomValueOut]:
    """Replace the item's values with ``payload``. Values for fields that do not belong to the
    item's project are ignored; blank values clear a field; required fields must be filled.
    Only someone who may edit the item may do this (a plain user cannot touch a Task); each
    change is audited, bumps the item's version and fires ``bug.updated``."""
    bug = _accessible_bug(db, bug_id, user)
    if not can_edit_bug(user, item_type=bug.item_type):
        raise HTTPException(status_code=403, detail=f"You don't have permission to edit this {bug.item_type.lower()}.")
    fields = cv.project_fields(db, bug.project_id)
    try:
        incoming = cv.clean_incoming(fields, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    missing = cv.missing_required(fields, incoming)
    if missing:
        raise HTTPException(status_code=422, detail=cv.required_message(missing))
    changes = cv.write_values(db, bug_id, fields, incoming)
    if changes:
        record_value_changes(db, bug, user, changes)
        db.commit()
        db.refresh(bug)
        from app.routes.bugs import _bug_to_out_dict, _user_brief  # late: bugs imports this module's peers
        background.add_task(
            deliver_event, user.org_id, "bug.updated",
            {"bug": _bug_to_out_dict(bug), "changes": changes_payload(changes), "actor": _user_brief(user)},
        )
    rows = db.scalars(select(BugCustomValue).where(BugCustomValue.bug_id == bug_id)).all()
    return [CustomValueOut(field_id=r.field_id, value=r.value) for r in rows]


def changes_payload(changes: list[tuple[str, str, str]]) -> list[dict]:
    return [{"field": f"custom_{n}", "old": o, "new": v} for n, o, v in changes]


def record_value_changes(db: Session, bug: Bug, user: User, changes: list[tuple[str, str, str]]) -> None:
    """Audit rows and the version bump for custom-value changes (caller commits)."""
    for name, old, new in changes:
        db.add(Activity(
            org_id=user.org_id, bug_id=bug.id, entity_type="bug", entity_id=bug.id,
            actor_user_id=user.id, actor_name=user.name, action=f"custom_{name[:40]}_changed",
            detail=f"#{bug.id} '{bug.title}' - {name}: '{old}' -> '{new}'",
        ))
    bug.version = (bug.version or 1) + 1
