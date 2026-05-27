"""Custom fields per project — admins/leads define them, anyone with
access to the project can fill in values when editing a bug.

Endpoints:
  GET    /api/projects/{project_id}/custom-fields            — list
  POST   /api/projects/{project_id}/custom-fields            — create
  PUT    /api/projects/{project_id}/custom-fields/{field_id} — edit
  DELETE /api/projects/{project_id}/custom-fields/{field_id} — remove
  GET    /api/bugs/{bug_id}/custom-values                    — read
  PUT    /api/bugs/{bug_id}/custom-values                    — bulk-set
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    can_access_project, can_manage_project, get_current_user,
    get_org_project_or_404,
)
from app.database import get_db
from app.models import Activity, Bug, BugCustomValue, CustomField, User

router = APIRouter(tags=["custom-fields"])

_VALID_TYPES = {"text", "number", "date", "select"}


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
        opts = [o for o in (f.options or "").split("|") if o]
        return cls(
            id=f.id, project_id=f.project_id, name=f.name,
            field_type=f.field_type, options=opts,
            is_required=bool(f.is_required), position=int(f.position or 0),
        )


class CustomFieldIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    field_type: str = Field("text", max_length=20)
    options: list[str] = Field(default_factory=list)
    is_required: bool = False
    position: int = 0

    @field_validator("field_type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _VALID_TYPES:
            raise ValueError(f"field_type must be one of {sorted(_VALID_TYPES)}")
        return v


class CustomFieldUpdateIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    field_type: Optional[str] = None
    options: Optional[list[str]] = None
    is_required: Optional[bool] = None
    position: Optional[int] = None


class CustomValueOut(BaseModel):
    field_id: int
    value: str


@router.get("/api/projects/{project_id}/custom-fields", response_model=list[CustomFieldOut])
def list_fields(
    project_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomFieldOut]:
    project = get_org_project_or_404(db, project_id, user)
    if not can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="No access to this project")
    rows = list(db.scalars(
        select(CustomField).where(CustomField.project_id == project_id)
        .order_by(CustomField.position.asc(), CustomField.id.asc())
    ).all())
    return [CustomFieldOut.from_row(r) for r in rows]


@router.post("/api/projects/{project_id}/custom-fields",
             response_model=CustomFieldOut, status_code=201)
def create_field(
    project_id: int,
    payload: CustomFieldIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomFieldOut:
    project = get_org_project_or_404(db, project_id, user)
    if not can_manage_project(db, user, project):
        raise HTTPException(status_code=403, detail="Only admins / project leads can add custom fields")
    f = CustomField(
        project_id=project_id,
        name=payload.name.strip(),
        field_type=payload.field_type,
        options="|".join(o.strip() for o in payload.options if o.strip())[:500],
        is_required=payload.is_required,
        position=payload.position,
    )
    db.add(f)
    db.flush()
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="custom_field", entity_id=f.id,
        actor_user_id=user.id, actor_name=user.name,
        action="custom_field_created",
        detail=f"Added field '{f.name}' ({f.field_type}) to project {project.name}",
    ))
    db.commit()
    db.refresh(f)
    return CustomFieldOut.from_row(f)


@router.put("/api/projects/{project_id}/custom-fields/{field_id}",
            response_model=CustomFieldOut)
def update_field(
    project_id: int,
    field_id: int,
    payload: CustomFieldUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomFieldOut:
    project = get_org_project_or_404(db, project_id, user)
    if not can_manage_project(db, user, project):
        raise HTTPException(status_code=403, detail="Only admins / project leads can manage custom fields")
    f = db.get(CustomField, field_id)
    if f is None or f.project_id != project_id:
        raise HTTPException(status_code=404, detail="Field not found")
    fields = payload.model_dump(exclude_unset=True)
    if "options" in fields:
        opts = "|".join(o.strip() for o in (fields["options"] or []) if o.strip())[:500]
        f.options = opts
    for k in ("name", "field_type", "is_required", "position"):
        if k in fields:
            if k == "field_type" and fields[k] not in _VALID_TYPES:
                raise HTTPException(status_code=400, detail="invalid field_type")
            setattr(f, k, fields[k].strip() if isinstance(fields[k], str) else fields[k])
    db.commit()
    db.refresh(f)
    return CustomFieldOut.from_row(f)


@router.delete("/api/projects/{project_id}/custom-fields/{field_id}", status_code=204)
def delete_field(
    project_id: int,
    field_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = get_org_project_or_404(db, project_id, user)
    if not can_manage_project(db, user, project):
        raise HTTPException(status_code=403, detail="Only admins / project leads can manage custom fields")
    f = db.get(CustomField, field_id)
    if f is None or f.project_id != project_id:
        raise HTTPException(status_code=404, detail="Field not found")
    name = f.name
    db.delete(f)
    db.add(Activity(
        org_id=user.org_id, bug_id=None, entity_type="custom_field", entity_id=field_id,
        actor_user_id=user.id, actor_name=user.name,
        action="custom_field_deleted",
        detail=f"Removed field '{name}' from project {project.name}",
    ))
    db.commit()


@router.get("/api/bugs/{bug_id}/custom-values", response_model=list[CustomValueOut])
def list_values(
    bug_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomValueOut]:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    project = bug.project
    if project is None or project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Bug not found")
    if not can_access_project(db, user, project):
        raise HTTPException(status_code=404, detail="Bug not found")
    rows = list(db.scalars(
        select(BugCustomValue).where(BugCustomValue.bug_id == bug_id)
    ).all())
    return [CustomValueOut(field_id=r.field_id, value=r.value) for r in rows]


@router.put("/api/bugs/{bug_id}/custom-values", response_model=list[CustomValueOut])
def set_values(
    bug_id: int,
    payload: list[CustomValueOut],
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CustomValueOut]:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status_code=404, detail="Bug not found")
    project = bug.project
    if project is None or project.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="Bug not found")
    if not can_access_project(db, user, project):
        raise HTTPException(status_code=404, detail="Bug not found")
    # Validate every field_id belongs to this bug's project
    project_field_ids = set(db.scalars(
        select(CustomField.id).where(CustomField.project_id == bug.project_id)
    ).all())
    incoming_by_field = {v.field_id: v.value for v in payload if v.field_id in project_field_ids}
    # Existing rows
    existing = {
        r.field_id: r for r in db.scalars(
            select(BugCustomValue).where(BugCustomValue.bug_id == bug_id)
        ).all()
    }
    for fid, value in incoming_by_field.items():
        if fid in existing:
            existing[fid].value = value
        else:
            db.add(BugCustomValue(bug_id=bug_id, field_id=fid, value=value))
    # Remove values for fields no longer in the payload but still in DB.
    for fid, row in existing.items():
        if fid not in incoming_by_field:
            db.delete(row)
    db.commit()
    rows = list(db.scalars(
        select(BugCustomValue).where(BugCustomValue.bug_id == bug_id)
    ).all())
    return [CustomValueOut(field_id=r.field_id, value=r.value) for r in rows]
