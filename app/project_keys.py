"""Short project keys ("WEB", "MS") derived from project names, unique per organization."""
from __future__ import annotations

import re

from sqlalchemy import select

_KEY_BAD = re.compile(r"[^A-Z0-9]+")


def derive_key(name: str) -> str:
    """A default project key from the name: "Marketing Site" -> "MS", "Web" -> "WEB"."""
    words = name.split()
    if len(words) > 1:
        letters = _KEY_BAD.sub("", "".join(w[0] for w in words[:4]).upper())
    else:
        letters = _KEY_BAD.sub("", name.upper())[:6]
    if not letters:
        return "P"
    return letters if letters[0].isalpha() else f"P{letters}"[:6]


def unique_key(db, org_id: int, base: str) -> str:
    """``base``, or ``base`` plus a number, free within the organization. ``db`` is a Session or
    a Core connection (used from the flush-time hook in app/models.py)."""
    from app.models import Project

    taken = set(db.execute(
        select(Project.key).where(Project.org_id == org_id, Project.key.like(f"{base}%"))
    ).scalars().all())
    candidate, n = base, 2
    while candidate in taken or len(candidate) < 2:
        candidate = f"{base}{n}"
        n += 1
    return candidate[:16]
