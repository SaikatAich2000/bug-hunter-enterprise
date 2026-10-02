"""Creating organizations: the shared path of public sign-up and the bootstrap admin."""
from __future__ import annotations

import re
import secrets

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.database import DEFAULT_PROJECT_COLOR, DEFAULT_PROJECT_DESCRIPTION, DEFAULT_PROJECT_NAME
from app.models import Organization, Project

_SLUG_BAD = re.compile(r"[^a-z0-9]+")
DEFAULT_PROJECT_KEY = "GEN"


def unique_slug(db: Session | Connection, name: str) -> str:
    """A free slug derived from the name (random suffix on collision); ``db`` may be a Session
    or a Core connection."""
    base = _SLUG_BAD.sub("-", name.lower()).strip("-")[:60] or "org"
    candidate = base
    for _ in range(8):
        taken = db.execute(
            select(Organization.__table__.c.id).where(Organization.__table__.c.slug == candidate)
        ).first()
        if taken is None:
            return candidate
        candidate = f"{base}-{secrets.token_hex(3)}"
    return f"{base}-{secrets.token_hex(6)}"


def create_organization(db: Session, name: str) -> Organization:
    """Add an organization with its default project and flush; the caller adds its first admin
    and commits."""
    org = Organization(name=name, slug=unique_slug(db, name))
    db.add(org)
    db.flush()
    db.add(Project(
        org_id=org.id, name=DEFAULT_PROJECT_NAME, key=DEFAULT_PROJECT_KEY,
        description=DEFAULT_PROJECT_DESCRIPTION, color=DEFAULT_PROJECT_COLOR,
    ))
    return org
