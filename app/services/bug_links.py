"""Work-item link rules and persistence."""

from __future__ import annotations

from fastapi import (
    HTTPException,
)
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    can_edit_bug,
)
from app.models import (
    Bug,
    BugLink,
    User,
)
from app.services.work_items import work_item_type

# Human-readable labels for each link type, as (outgoing, incoming) pairs.
LINK_LABELS: dict[str, tuple[str, str]] = {
    "relates": ("relates to", "relates to"),
    "blocks": ("blocks", "is blocked by"),
    "duplicate": ("duplicates", "is duplicated by"),
}


# Link types where the reverse would contradict the forward ("A blocks B" and
# "B blocks A" can't both be true). `relates` is symmetric so it's excluded.
DIRECTIONAL_LINK_TYPES = {"blocks", "duplicate"}


# Bound the cycle-detection BFS so a pathological graph can't make a single
# link insert run forever.
LINK_CYCLE_MAX_NODES = 10_000


def link_phrase(link_type: str, direction: str) -> str:
    out_label, in_label = LINK_LABELS.get(link_type, (link_type, link_type))
    return out_label if direction == "outgoing" else in_label


def serialize_link(link: BugLink, from_bug_id: int) -> dict:
    """Render a BugLink from the perspective of from_bug_id.

    Both FKs are NOT NULL with cascade-delete, so the other end is always
    present.
    """
    outgoing = link.source_bug_id == from_bug_id
    other = link.target if outgoing else link.source
    direction = "outgoing" if outgoing else "incoming"
    return {
        "id": link.id,
        "link_type": link.link_type,
        "direction": direction,
        "label": link_phrase(link.link_type, direction),
        "other_bug_id": other.id,
        "other_bug_title": other.title,
        "other_bug_status": other.status,
        "other_bug_item_type": work_item_type(other),
        "created_at": link.created_at,
    }


def bug_links(db: Session, bug_id: int) -> list[dict]:
    """Return all links touching this bug (either direction), newest first."""
    rows = list(db.scalars(
        select(BugLink)
        .options(selectinload(BugLink.source), selectinload(BugLink.target))
        .where(or_(BugLink.source_bug_id == bug_id, BugLink.target_bug_id == bug_id))
        .order_by(BugLink.created_at.desc(), BugLink.id.desc())
    ).all())
    return [serialize_link(link, bug_id) for link in rows]


def require_can_edit_link_endpoint(item: Bug, actor: User) -> None:
    """Require edit rights on both link endpoints. Without this, a user who can
    edit a Bug could attach or detach links on a Task/Requirement they can't
    otherwise touch."""
    if not can_edit_bug(actor, item_type=work_item_type(item)):
        noun = work_item_type(item).lower()
        raise HTTPException(
            status_code=403,
            detail=f"You don't have permission to change links on this {noun}.",
        )


def directional_link_reaches(db: Session, start_id: int, goal_id: int, link_type: str) -> bool:
    """True if ``goal_id`` is reachable from ``start_id`` by following forward
    edges of ``link_type`` (bounded BFS). Used to detect cycles before insert."""
    seen = {start_id}
    frontier = [start_id]
    while frontier and len(seen) < LINK_CYCLE_MAX_NODES:
        rows = db.execute(
            select(BugLink.target_bug_id).where(
                BugLink.source_bug_id.in_(frontier),
                BugLink.link_type == link_type,
            )
        ).all()
        nxt = []
        for (tid,) in rows:
            if tid == goal_id:
                return True
            if tid not in seen:
                seen.add(tid)
                nxt.append(tid)
        frontier = nxt
    return False


def reject_inverse_directional_link(
    db: Session, source_id: int, target: Bug, link_type: str,
) -> None:
    """Block a directional link A->B that would create a cycle.

    If the target can already reach the source by forward edges of the same
    type, adding source->target closes a loop (A transitively blocked by
    itself). This catches both the direct inverse B->A and transitive cycles."""
    if link_type not in DIRECTIONAL_LINK_TYPES:
        return
    if directional_link_reaches(db, target.id, source_id, link_type):
        raise HTTPException(
            status_code=400,
            detail=(
                f"This would create a {link_type} cycle — #{target.id} already "
                f"{link_phrase(link_type, 'outgoing')} #{source_id} "
                f"(directly or transitively)."
            ),
        )


def reload_link(db: Session, link_id: int) -> BugLink:
    link = db.scalar(
        select(BugLink)
        .options(selectinload(BugLink.source), selectinload(BugLink.target))
        .where(BugLink.id == link_id)
    )
    if link is None:
        # Deleted between the insert/find and this reload; 409 beats
        # AttributeError in serialize_link.
        raise HTTPException(status_code=409,
                            detail="Link was modified concurrently. Please retry.")
    return link


def insert_link_or_existing(db: Session, link: BugLink, refetch) -> tuple[BugLink, bool]:
    """Flush a new BugLink; on a unique-index race roll back and return the
    existing edge. Returns (edge, created).

    A concurrent identical request that wins the race raises IntegrityError
    here, which is converted to the existing edge rather than a 500. refetch
    re-runs the existence lookup after the rollback.
    """
    db.add(link)
    try:
        db.flush()
        return link, True
    except IntegrityError:
        db.rollback()
        existing = refetch()
        if existing is None:
            raise
        return existing, False
