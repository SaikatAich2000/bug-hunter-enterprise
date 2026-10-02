"""Hierarchy integrity and the work-item change log, enforced at flush time.

Every write path (item form, board, backlog, bulk actions, import, Sleuth)
ends in a session flush, so doing this here keeps the rules true everywhere
without each route having to remember them:

* a Sub-task always carries its parent's epic, sprint and project;
* moving a standard issue to another sprint or epic moves its Sub-tasks too;
* an Epic is never in a sprint and never has a parent;
* a standard issue is always ranked in the list it sits in (its sprint or
  its project's backlog), at the bottom when it arrives there by any path
  other than an explicit drag-and-drop position;
* every change to a tracked field is appended to ``work_item_changes`` (the
  history the agile reports replay), tagged with the acting user and, for side
  effects such as Complete Sprint, the reason.

Validation with user-facing errors stays in app.agile.hierarchy and the
routes; this hook only derives values and records history, so it never
raises into the flush.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session

from app.agile.itemtypes import is_epic, is_standard, is_subtask

logger = logging.getLogger("bug_hunter.agile.integrity")

# Bug column -> change-log field name.
TRACKED_FIELDS: dict[str, str] = {
    "status": "status",
    "sprint_id": "sprint",
    "story_points": "story_points",
    "original_estimate_minutes": "original_estimate",
    "epic_id": "epic",
    "parent_id": "parent",
    "item_type": "item_type",
    "project_id": "project",
}

ACTOR_KEY = "actor_id"
REASON_KEY = "change_reason"


def set_actor(session: Session, user_id: int | None) -> None:
    """Attribute the session's upcoming changes to ``user_id``."""
    session.info[ACTOR_KEY] = user_id


class change_reason:  # noqa: N801 - used as a context manager, reads like a statement
    """Tag the changes flushed inside the block, e.g. ``with change_reason(db, "sprint_completed"):``."""

    def __init__(self, session: Session, reason: str) -> None:
        self.session = session
        self.reason = reason
        self._previous = None

    def __enter__(self):
        self._previous = self.session.info.get(REASON_KEY)
        self.session.info[REASON_KEY] = self.reason
        return self

    def __exit__(self, *exc):
        # Flush while the reason is still set so the tagged rows are written now.
        if exc[0] is None:
            self.session.flush()
        self.session.info[REASON_KEY] = self._previous
        return False


def format_value(value) -> str | None:
    """Canonical text form of a tracked value (3.00 -> "3", 2.50 -> "2.5")."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Decimal, float)):
        number = Decimal(str(value)).normalize()
        text = format(number, "f")
        return text[:64]
    return str(value)[:64]


def parse_number(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _children_of(session: Session, item) -> list:
    from app.models import Bug

    if item.id is None:
        return []
    return list(session.scalars(select(Bug).where(Bug.parent_id == item.id)).all())


def _derive_from_parent(session: Session, item) -> None:
    """A Sub-task mirrors its parent's epic, sprint and project."""
    from app.models import Bug

    if item.parent_id is None:
        return
    parent = session.get(Bug, item.parent_id)
    if parent is None:
        return
    for attr in ("epic_id", "sprint_id", "project_id"):
        value = getattr(parent, attr)
        if getattr(item, attr) != value:
            setattr(item, attr, value)


def _changed(item, attr: str) -> bool:
    return inspect(item).attrs[attr].history.has_changes()


def expected_rank_scope(item) -> str:
    """The ordered list an issue belongs to: an Epic ranks among its project's
    Epics; a standard issue in its sprint, else its project's backlog."""
    if is_epic(item.item_type):
        return f"epics:{item.project_id}"
    if item.sprint_id is not None:
        return f"sprint:{item.sprint_id}"
    return f"backlog:{item.project_id}"


def _ensure_ranked(session: Session, item, last_rank: dict) -> None:
    """Give a standard issue a rank in the list it sits in (bottom of it), so
    every issue can be dragged and positioned relative to its neighbours."""
    from app.agile.ranking import RankExhausted, rank_between, spaced_ranks
    from app.models import Bug

    scope = expected_rank_scope(item)
    if item.rank_scope == scope and item.rank is not None:
        return
    if scope not in last_rank:
        last_rank[scope] = session.scalar(select(func.max(Bug.rank)).where(Bug.rank_scope == scope))
    try:
        token = rank_between(last_rank[scope], None)
    except RankExhausted:
        # No room after the last issue (damaged legacy ranks): re-space the
        # list in its current order and append after it.
        members = list(session.scalars(
            select(Bug).where(Bug.rank_scope == scope, Bug.id != item.id)
            .order_by(Bug.rank.is_(None), Bug.rank, Bug.id)
        ).all())
        for member, spaced in zip(members, spaced_ranks(len(members))):
            member.rank = spaced
        last_rank[scope] = members[-1].rank if members else None
        token = rank_between(last_rank[scope], None)
    last_rank[scope] = token
    item.rank_scope = scope
    item.rank = token


def _reopened_in_closed_sprint(session: Session, item, done_cache: dict) -> bool:
    """True when a status change takes an issue that finished in a closed
    sprint out of the board's done column. Such an issue is open work again
    and belongs in the backlog (the closed sprint's reports replay history up
    to its completion, so they are unaffected)."""
    from app.agile.boards import done_status_values, get_default_board
    from app.models import Sprint

    if item.id is None or item.sprint_id is None or not _changed(item, "status"):
        return False
    sprint = session.get(Sprint, item.sprint_id)
    if sprint is None or sprint.state not in ("closed", "cancelled"):
        return False
    if item.project_id not in done_cache:
        board = get_default_board(session, item.project_id)
        done_cache[item.project_id] = done_status_values(session, board) if board is not None else set()
    done = done_cache[item.project_id]
    return bool(done) and item.status not in done


def _enforce(session: Session, items: list) -> list:
    """Apply the derivation rules; returns every item touched (inputs plus
    cascaded Sub-tasks) so their changes are logged in the same flush."""
    touched = []
    seen: set[int] = set()
    last_rank: dict[str, str | None] = {}
    done_cache: dict[int, set] = {}
    queue = list(items)
    while queue:
        item = queue.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        touched.append(item)
        item_type = item.item_type or "Bug"
        if is_epic(item_type):
            if item.sprint_id is not None:
                item.sprint_id = None
            if item.parent_id is not None:
                item.parent_id = None
            if item.epic_id is not None:
                item.epic_id = None
            _ensure_ranked(session, item, last_rank)
        elif is_subtask(item_type):
            # Sub-tasks are ordered under their parent, never in a list.
            if item.rank is not None or item.rank_scope is not None:
                item.rank = item.rank_scope = None
            _derive_from_parent(session, item)
        elif is_standard(item_type):
            if item.parent_id is not None:
                logger.warning("Clearing parent_id on %s #%s: only Sub-tasks have a parent",
                               item_type, item.id)
                item.parent_id = None
            if _reopened_in_closed_sprint(session, item, done_cache):
                item.sprint_id = None
            _ensure_ranked(session, item, last_rank)
            if item.id is not None and any(
                _changed(item, attr) for attr in ("sprint_id", "epic_id", "project_id", "item_type")
            ):
                for child in _children_of(session, item):
                    if is_subtask(child.item_type):
                        _derive_from_parent(session, child)
                        queue.append(child)
    return touched


def _log_changes(session: Session, new_items: list, dirty_items: list) -> None:
    from app.models import WorkItemChange

    actor_id = session.info.get(ACTOR_KEY)
    reason = session.info.get(REASON_KEY)
    for item in new_items:
        session.add(WorkItemChange(
            work_item=item, project_id=item.project_id, field="created",
            old_value=None, new_value=item.item_type or "Bug",
            actor_id=actor_id, reason=reason,
        ))
    for item in dirty_items:
        state = inspect(item)
        for attr, field in TRACKED_FIELDS.items():
            history = state.attrs[attr].history
            if not history.has_changes():
                continue
            old = history.deleted[0] if history.deleted else None
            new = history.added[0] if history.added else None
            old_text, new_text = format_value(old), format_value(new)
            if old_text == new_text:
                continue
            session.add(WorkItemChange(
                work_item=item, project_id=item.project_id, field=field,
                old_value=old_text, new_value=new_text,
                actor_id=actor_id, reason=reason,
            ))


def register(session_factory) -> None:
    """Attach the hook to ``session_factory`` (app.database.SessionLocal).

    Registered on the factory rather than the Session class so the test
    suite's per-test re-imports never stack duplicate listeners.
    """
    if not event.contains(session_factory, "before_flush", _before_flush):
        event.listen(session_factory, "before_flush", _before_flush)


def _before_flush(session: Session, flush_context, instances) -> None:
    from app.models import Bug

    new_items = [o for o in session.new if isinstance(o, Bug)]
    dirty_items = [
        o for o in session.dirty
        if isinstance(o, Bug) and o not in session.deleted and session.is_modified(o)
    ]
    if not new_items and not dirty_items:
        return
    with session.no_autoflush:
        touched = _enforce(session, new_items + dirty_items)
        new_ids = {id(o) for o in new_items}
        changed = [o for o in touched if id(o) not in new_ids and session.is_modified(o)]
        _log_changes(session, new_items, changed)
