"""Custom-field values: validation, the required-field rule and the write that every path
(item create, update, import and PUT /custom-values) shares, so none can skip a rule."""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BugCustomValue, CustomField

_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def check_value(field: CustomField, value: str) -> str:
    """The value normalized for storage; raises ValueError naming the field when invalid.
    An empty value is always valid here (required fields are checked separately)."""
    value = value.strip()
    if not value:
        return ""
    if field.field_type == "number" and not _NUMBER_RE.match(value):
        raise ValueError(f"'{field.name}' must be a number")
    if field.field_type == "date":
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"'{field.name}' must be a date (YYYY-MM-DD)") from exc
    if field.field_type == "select" and value not in (field.options or "").split("|"):
        raise ValueError(f"'{field.name}' must be one of its options")
    return value


def project_fields(db: Session, project_id: int) -> dict[int, CustomField]:
    return {f.id: f for f in db.scalars(
        select(CustomField).where(CustomField.project_id == project_id)
    ).all()}


def clean_incoming(fields: dict[int, CustomField], pairs) -> dict[int, str]:
    """field id -> checked value. Fields of other projects are ignored (never written)."""
    return {
        p.field_id: check_value(fields[p.field_id], p.value) for p in pairs or [] if p.field_id in fields
    }


def missing_required(fields: dict[int, CustomField], values: dict[int, str]) -> list[str]:
    return sorted(f.name for fid, f in fields.items() if f.is_required and not values.get(fid))


def required_message(names: list[str]) -> str:
    return "Required custom fields are missing: " + ", ".join(names)


def existing_values(db: Session, bug_id: int) -> dict[int, BugCustomValue]:
    return {r.field_id: r for r in db.scalars(
        select(BugCustomValue).where(BugCustomValue.bug_id == bug_id)
    ).all()}


def write_values(
    db: Session, bug_id: int, fields: dict[int, CustomField], incoming: dict[int, str],
) -> list[tuple[str, str, str]]:
    """Make the item's values equal ``incoming`` (blank clears). Returns (field name, old, new)
    for each value that actually changed; the caller audits and commits."""
    existing = existing_values(db, bug_id)
    changes: list[tuple[str, str, str]] = []
    for fid, row in existing.items():
        if not incoming.get(fid):
            changes.append((fields[fid].name if fid in fields else f"#{fid}", row.value, ""))
            db.delete(row)
    for fid, value in incoming.items():
        if not value:
            continue
        if fid in existing:
            if existing[fid].value != value:
                changes.append((fields[fid].name, existing[fid].value, value))
                existing[fid].value = value
        else:
            changes.append((fields[fid].name, "", value))
            db.add(BugCustomValue(bug_id=bug_id, field_id=fid, value=value))
    return changes
