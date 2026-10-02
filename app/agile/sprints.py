"""Sprint lifecycle (Jira Software Scrum rules).

* Only standard issues (Story, Task, Bug, Requirement) are planned. Sub-tasks
  follow their parent (app.agile.integrity); Epics are never in a sprint.
* A sprint starts with at least one issue and gets dates; one sprint is
  active per board unless parallel sprints are enabled.
* Completing a sprint: an issue is complete when its status is in the
  board's right-most column and so are all of its Sub-tasks. Completed issues
  stay in the closed sprint; the rest move, with their Sub-tasks, to the
  backlog, a future sprint or a new sprint.

Every mutation flushes within the caller's transaction; the route commits.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.agile.backlog import backlog_scope, sprint_scope
from app.agile.boards import done_status_values, normalize_name
from app.agile.integrity import change_reason
from app.agile.itemtypes import STANDARD_TYPES, SUBTASK, is_epic, is_standard, is_subtask
from app.agile.ranking import RankExhausted, ranks_between, spaced_ranks
from app.models import Board, Bug, Sprint, SprintItemHistory, User

DEFAULT_SPRINT_DAYS = 14
CLOSED_STATES = ("closed", "cancelled")


class SprintLifecycleError(ValueError):
    """Raised for any sprint-lifecycle rule violation; routes map to 409/422."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now_precise() -> datetime:
    return datetime.now(timezone.utc)


def board_today(board: Board | None) -> date:
    """Today's date in the board's timezone (sprint dates are board-local)."""
    tz_name = (board.timezone if board is not None else None) or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    return datetime.now(tz).date()


def list_sprints(db: Session, board_id: int, state: str | None = None) -> list[Sprint]:
    stmt = select(Sprint).options(selectinload(Sprint.assignees)).where(Sprint.board_id == board_id)
    if state:
        stmt = stmt.where(Sprint.state == state)
    return list(db.scalars(stmt.order_by(Sprint.sequence_number, Sprint.id)).all())


def sprint_item_counts(db: Session, sprint_ids: list[int]) -> dict[int, int]:
    """sprint_item_count for many sprints in one query."""
    if not sprint_ids:
        return {}
    rows = db.execute(
        select(Bug.sprint_id, func.count(Bug.id))
        .where(Bug.sprint_id.in_(sprint_ids), Bug.item_type.in_(STANDARD_TYPES))
        .group_by(Bug.sprint_id)
    ).all()
    return {sprint_id: count for sprint_id, count in rows}


def sprint_item_count(db: Session, sprint_id: int) -> int:
    """Issues planned in the sprint (standard issues; Sub-tasks ride along)."""
    return db.scalar(
        select(func.count(Bug.id)).where(
            Bug.sprint_id == sprint_id, Bug.item_type.in_(STANDARD_TYPES),
        )
    ) or 0


def sprint_issues(db: Session, sprint_id: int) -> list[Bug]:
    return list(db.scalars(
        select(Bug).where(Bug.sprint_id == sprint_id, Bug.item_type.in_(STANDARD_TYPES))
        .order_by(Bug.rank, Bug.id)
    ).all())


def _check_dates(start_date: str | None, end_date: str | None) -> None:
    if start_date and end_date and end_date < start_date:
        raise SprintLifecycleError("The sprint's end date cannot be before its start date")


def next_sprint_name(db: Session, board: Board) -> str:
    """Jira-style default name: "<board> Sprint <n>" with the next free number."""
    count = db.scalar(select(func.count(Sprint.id)).where(Sprint.board_id == board.id)) or 0
    base = (board.name or "Board").removesuffix(" Board").strip() or "Board"
    taken = set(db.scalars(select(Sprint.name_normalized).where(Sprint.board_id == board.id)).all())
    n = count + 1
    while normalize_name(f"{base} Sprint {n}") in taken:
        n += 1
    return f"{base} Sprint {n}"


def create_sprint(
    db: Session, board: Board, actor: User,
    name: str, goal: str, start_date: str | None, end_date: str | None,
    cadence: str | None = None,
) -> Sprint:
    _check_dates(start_date, end_date)
    next_seq = (db.scalar(
        select(Sprint.sequence_number)
        .where(Sprint.board_id == board.id)
        .order_by(Sprint.sequence_number.desc())
        .limit(1)
    ) or 0) + 1
    sprint = Sprint(
        board_id=board.id,
        project_id=board.project_id,
        name=name,
        name_normalized=normalize_name(name),
        goal=goal or "",
        state="future",
        start_date=start_date,
        end_date=end_date,
        cadence=cadence,
        sequence_number=next_seq,
        created_by_id=actor.id,
    )
    db.add(sprint)
    db.flush()
    return sprint


def update_sprint(
    db: Session, sprint: Sprint, name: str | None, goal: str | None,
    start_date: str | None, end_date: str | None, cadence: str | None = None,
    *, clear_start: bool = False, clear_end: bool = False,
) -> Sprint:
    """Edit a sprint; ``None`` leaves a field as it is, ``clear_*`` empties a
    date (a future sprint may have none, as in Jira; a running one keeps them)."""
    if sprint.state in CLOSED_STATES:
        raise SprintLifecycleError("A closed or deleted sprint cannot be edited")
    if (clear_start or clear_end) and sprint.state == "active":
        raise SprintLifecycleError("An active sprint keeps its start and end dates")
    new_start = None if clear_start else (start_date if start_date is not None else sprint.start_date)
    new_end = None if clear_end else (end_date if end_date is not None else sprint.end_date)
    _check_dates(new_start, new_end)
    if name is not None:
        sprint.name = name
        sprint.name_normalized = normalize_name(name)
    if goal is not None:
        sprint.goal = goal
    sprint.start_date = new_start
    sprint.end_date = new_end
    if cadence is not None:
        sprint.cadence = cadence or None
    db.flush()
    return sprint


# --- Planning: which issues are in which sprint, in what order ---------------

def _plannable_items(db: Session, project_id: int, item_ids: list[int]) -> list[Bug]:
    items: list[Bug] = []
    seen: set[int] = set()
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        item = db.get(Bug, item_id)
        if item is None or item.project_id != project_id:
            raise SprintLifecycleError(f"Issue #{item_id} was not found in this project")
        if is_subtask(item.item_type):
            raise SprintLifecycleError(
                f"Sub-task #{item_id} moves with its parent issue #{item.parent_id}; plan the parent instead"
            )
        if is_epic(item.item_type):
            raise SprintLifecycleError(
                f"#{item_id} is an Epic. Epics are not planned into sprints; plan their issues instead"
            )
        if not is_standard(item.item_type):
            raise SprintLifecycleError(f"#{item_id} ({item.item_type}) cannot be planned into a sprint")
        items.append(item)
    return items


def _neighbour(db: Session, project_id: int, scope: str, item_id: int | None, moving: set[int]) -> Bug | None:
    if item_id is None:
        return None
    other = db.get(Bug, item_id)
    if other is None or other.project_id != project_id or other.rank_scope != scope:
        raise SprintLifecycleError(f"Issue #{item_id} is not in the list you dropped onto")
    if other.id in moving:
        raise SprintLifecycleError("An issue cannot be positioned relative to itself")
    return other


def _place(
    db: Session, items: list[Bug], scope: str, project_id: int,
    before_id: int | None, after_id: int | None, default: str,
) -> None:
    """Rank ``items`` (in the given order) into ``scope``: right after
    ``before_id`` and/or right before ``after_id``, else at the top or bottom.

    When the gap between the neighbours is used up (or legacy data left two
    issues with the same rank) the whole list is re-spaced, keeping its order.
    """
    moving = {i.id for i in items}
    before = _neighbour(db, project_id, scope, before_id, moving)
    after = _neighbour(db, project_id, scope, after_id, moving)
    if any(n is not None and n.rank is None for n in (before, after)):
        _respace(db, items, scope, moving, before, after, default)
        return
    others = select(Bug.rank).where(
        Bug.rank_scope == scope, Bug.rank.is_not(None), Bug.id.not_in(moving), Bug.item_type.in_(STANDARD_TYPES),
    )
    if before is not None or after is not None:
        lo = before.rank if before else None
        hi = after.rank if after else None
        if before is not None and after is None:
            hi = db.scalar(others.where(Bug.rank > before.rank).order_by(Bug.rank).limit(1))
        if after is not None and before is None:
            lo = db.scalar(others.where(Bug.rank < after.rank).order_by(Bug.rank.desc()).limit(1))
        if before is not None and after is not None and before.rank > after.rank:
            raise SprintLifecycleError("Those two issues are not next to each other")
    elif default == "top":
        lo, hi = None, db.scalar(others.order_by(Bug.rank).limit(1))
    else:
        lo, hi = db.scalar(others.order_by(Bug.rank.desc()).limit(1)), None
    try:
        tokens = ranks_between(lo, hi, len(items))
    except RankExhausted:
        _respace(db, items, scope, moving, before, after, default)
        return
    for item, token in zip(items, tokens):
        item.rank_scope = scope
        item.rank = token


def _respace(
    db: Session, items: list[Bug], scope: str, moving: set[int],
    before: Bug | None, after: Bug | None, default: str,
) -> None:
    """Give the whole list fresh, evenly spaced ranks with ``items`` inserted
    at the requested place."""
    rest = list(db.scalars(
        select(Bug).where(Bug.rank_scope == scope, Bug.id.not_in(moving), Bug.item_type.in_(STANDARD_TYPES))
        .order_by(Bug.rank.is_(None), Bug.rank, Bug.id)
    ).all())
    if before is not None:
        at = rest.index(before) + 1
    elif after is not None:
        at = rest.index(after)
    else:
        at = 0 if default == "top" else len(rest)
    ordered = rest[:at] + items + rest[at:]
    for item, token in zip(ordered, spaced_ranks(len(ordered))):
        item.rank_scope = scope
        item.rank = token


def _record_membership(
    db: Session, item: Bug, old_sprint_id: int | None, new_sprint_id: int | None, actor: User,
) -> None:
    """Scope-change history for active sprints (sprint report / daily report)."""
    if old_sprint_id == new_sprint_id:
        return
    if old_sprint_id is not None:
        old = db.get(Sprint, old_sprint_id)
        if old is not None and old.state == "active":
            db.add(SprintItemHistory(
                sprint_id=old.id, work_item_id=item.id, event_type="removed",
                from_sprint_id=old.id, to_sprint_id=new_sprint_id, actor_id=actor.id,
                old_estimate=item.story_points,
            ))
    if new_sprint_id is not None:
        new = db.get(Sprint, new_sprint_id)
        if new is not None and new.state == "active":
            db.add(SprintItemHistory(
                sprint_id=new.id, work_item_id=item.id, event_type="added",
                from_sprint_id=old_sprint_id, to_sprint_id=new.id, actor_id=actor.id,
                new_estimate=item.story_points,
            ))


def lock_sprints(db: Session, sprint_ids) -> None:
    """Row-lock the sprints for the rest of the transaction (SELECT ... FOR
    UPDATE on PostgreSQL; a no-op on SQLite) and refresh them."""
    ids = sorted(i for i in sprint_ids if i is not None)
    if ids:
        db.scalars(
            select(Sprint).where(Sprint.id.in_(ids)).order_by(Sprint.id).with_for_update()
            .execution_options(populate_existing=True)
        ).all()


def move_items(
    db: Session, project_id: int, item_ids: list[int], target: Sprint | None, actor: User,
    before_id: int | None = None, after_id: int | None = None,
) -> list[Bug]:
    """Move issues into ``target`` (a sprint, or the backlog when None) at a
    position: the backlog/sprint drag-and-drop of Jira's Backlog view."""
    if target is not None and target.project_id != project_id:
        raise SprintLifecycleError("That sprint belongs to another project")
    items = _plannable_items(db, project_id, item_ids)
    # Lock every sprint involved (in id order, so concurrent moves never
    # deadlock) and read their states fresh: a sprint completed or started by
    # a concurrent request is seen as it is now, not as this request read it.
    lock_sprints(db, ({target.id} if target is not None else set()) | {i.sprint_id for i in items if i.sprint_id})
    if target is not None and target.state in CLOSED_STATES:
        raise SprintLifecycleError("Issues cannot be added to a closed or deleted sprint")
    scope = sprint_scope(target.id) if target is not None else backlog_scope(project_id)
    new_sprint_id = target.id if target is not None else None
    for item in items:
        old = item.sprint_id
        if old is not None and old != new_sprint_id:
            old_sprint = db.get(Sprint, old)
            if old_sprint is not None and old_sprint.state in CLOSED_STATES:
                # A finished issue stays in its closed sprint for the reports;
                # only unfinished work can be re-planned from there.
                raise SprintLifecycleError(
                    f"Issue #{item.id} belongs to the closed sprint '{old_sprint.name}'"
                )
        _record_membership(db, item, old, new_sprint_id, actor)
        item.sprint_id = new_sprint_id
    default = "bottom" if target is not None else "top"
    _place(db, items, scope, project_id, before_id, after_id, default)
    db.flush()
    return items


def add_items_to_sprint(
    db: Session, sprint: Sprint, item_ids: list[int], actor: User,
    before_id: int | None = None, after_id: int | None = None,
) -> list[Bug]:
    return move_items(db, sprint.project_id, item_ids, sprint, actor, before_id, after_id)


def remove_item_from_sprint(db: Session, sprint: Sprint, item_id: int, actor: User) -> Bug:
    item = db.get(Bug, item_id)
    if item is None or item.sprint_id != sprint.id:
        raise SprintLifecycleError(f"Issue #{item_id} is not in this sprint")
    if sprint.state in CLOSED_STATES:
        raise SprintLifecycleError("Issues cannot be removed from a closed sprint")
    return move_items(db, sprint.project_id, [item_id], None, actor)[0]


# --- Start / complete / delete ----------------------------------------------

def start_sprint(
    db: Session, sprint: Sprint, actor: User, allow_parallel: bool,
    *, name: str | None = None, goal: str | None = None,
    start_date: str | None = None, end_date: str | None = None, board: Board | None = None,
) -> Sprint:
    lock_sprints(db, {sprint.id})  # every caller, not only the REST route
    if sprint.state != "future":
        raise SprintLifecycleError("Only a future sprint can be started")
    items = sprint_issues(db, sprint.id)
    if not items:
        raise SprintLifecycleError("A sprint cannot start without issues")
    if not allow_parallel:
        # Serialize starts on one board (row lock on PostgreSQL), so two
        # concurrent starts cannot both see "no active sprint".
        db.execute(select(Board.id).where(Board.id == sprint.board_id).with_for_update())
        other_active = db.scalar(
            select(Sprint.id).where(
                Sprint.board_id == sprint.board_id, Sprint.state == "active",
                Sprint.id != sprint.id,
            ).limit(1)
        )
        if other_active is not None:
            raise SprintLifecycleError(
                "Another sprint is already active on this board. Complete it first, "
                "or enable parallel sprints for the project"
            )
    if name is not None and name.strip():
        sprint.name = name.strip()
        sprint.name_normalized = normalize_name(sprint.name)
    if goal is not None:
        sprint.goal = goal
    start = start_date or sprint.start_date or board_today(board).isoformat()
    end = end_date or sprint.end_date
    if not end:
        end = (date.fromisoformat(start) + timedelta(days=DEFAULT_SPRINT_DAYS - 1)).isoformat()
    _check_dates(start, end)
    sprint.start_date, sprint.end_date = start, end
    sprint.state = "active"
    sprint.started_by_id = actor.id
    sprint.started_at = _now_precise()
    for item in items:
        db.add(SprintItemHistory(
            sprint_id=sprint.id, work_item_id=item.id, event_type="committed",
            to_sprint_id=sprint.id, new_estimate=item.story_points, actor_id=actor.id,
            occurred_at=sprint.started_at,
        ))
    db.flush()
    return sprint


def _subtasks_by_parent(db: Session, parent_ids: list[int]) -> dict[int, list[Bug]]:
    out: dict[int, list[Bug]] = {pid: [] for pid in parent_ids}
    if not parent_ids:
        return out
    for sub in db.scalars(select(Bug).where(Bug.parent_id.in_(parent_ids), Bug.item_type == SUBTASK)).all():
        out.setdefault(sub.parent_id, []).append(sub)
    return out


def completion_split(db: Session, sprint: Sprint, board: Board) -> tuple[list[Bug], list[Bug]]:
    """(complete, incomplete) issues of ``sprint`` by Jira's rule: the issue and
    all of its Sub-tasks are in the board's right-most column."""
    done = done_status_values(db, board)
    issues = sprint_issues(db, sprint.id)
    subs = _subtasks_by_parent(db, [i.id for i in issues])
    complete, incomplete = [], []
    for issue in issues:
        finished = issue.status in done and all(s.status in done for s in subs.get(issue.id, []))
        (complete if finished else incomplete).append(issue)
    return complete, incomplete


def _destination_sprint(db: Session, sprint: Sprint, target_id: int | None) -> Sprint:
    target = db.get(Sprint, target_id) if target_id else None
    if target is None or target.board_id != sprint.board_id or target.state != "future":
        raise SprintLifecycleError("Unfinished issues can only move to a future sprint on the same board")
    return target


def complete_sprint(
    db: Session, sprint: Sprint, actor: User, closing_note: str,
    dispositions: dict[int, tuple[str, int | None]],
    default_destination: str, default_target_sprint_id: int | None,
    *, board: Board | None = None, new_sprint_name: str | None = None,
) -> Sprint:
    """Close ``sprint``. Unfinished issues go to ``default_destination``
    ("backlog", "sprint" with default_target_sprint_id, or "new_sprint"),
    or per issue via ``dispositions`` {item_id: (destination, sprint_id)}."""
    lock_sprints(db, {sprint.id})  # every caller, not only the REST route
    del closing_note  # audited by the route, not stored on the sprint
    if sprint.state != "active":
        raise SprintLifecycleError("Only an active sprint can be completed")
    board = board or db.get(Board, sprint.board_id)
    complete, incomplete = completion_split(db, sprint, board)
    incomplete_ids = {i.id for i in incomplete}
    extraneous = set(dispositions) - incomplete_ids
    if extraneous:
        raise SprintLifecycleError(
            f"Destinations were given for issues that are finished or not in this sprint: {sorted(extraneous)}"
        )

    new_sprint: Sprint | None = None

    def resolve(item: Bug) -> Sprint | None:
        nonlocal new_sprint
        destination, target_id = dispositions.get(item.id, (default_destination, default_target_sprint_id))
        if destination == "backlog":
            return None
        if destination == "new_sprint":
            if new_sprint is None:
                name = (new_sprint_name or "").strip() or next_sprint_name(db, board)
                new_sprint = create_sprint(db, board, actor, name, "", None, None)
            return new_sprint
        if destination == "sprint":
            return _destination_sprint(db, sprint, target_id)
        raise SprintLifecycleError(f"Unknown destination '{destination}'")

    # Resolve every destination before changing anything, so a bad request
    # leaves the sprint untouched.
    plan = [(item, resolve(item)) for item in incomplete]

    sprint.completed_at = _now_precise()
    for item in complete:
        db.add(SprintItemHistory(
            sprint_id=sprint.id, work_item_id=item.id, event_type="completed",
            from_sprint_id=sprint.id, actor_id=actor.id, new_estimate=item.story_points,
            occurred_at=sprint.completed_at,
        ))
    with change_reason(db, "sprint_completed"):
        by_target: dict[int | None, list[Bug]] = {}
        for item, target in plan:
            db.add(SprintItemHistory(
                sprint_id=sprint.id, work_item_id=item.id, event_type="carried_over",
                from_sprint_id=sprint.id, to_sprint_id=target.id if target else None,
                actor_id=actor.id, new_estimate=item.story_points,
                occurred_at=sprint.completed_at,
            ))
            by_target.setdefault(target.id if target else None, []).append(item)
        for target_id, items in by_target.items():
            target = db.get(Sprint, target_id) if target_id else None
            scope = sprint_scope(target.id) if target else backlog_scope(sprint.project_id)
            for item in items:
                item.sprint_id = target.id if target else None
            # Carried-over work goes to the top of where it lands, in its old order.
            _place(db, items, scope, sprint.project_id, None, None, "top")
    sprint.state = "closed"
    sprint.completed_by_id = actor.id
    db.flush()
    return sprint


def cancel_sprint(
    db: Session, sprint: Sprint, actor: User, cancellation_note: str,
    dispositions: dict[int, tuple[str, int | None]],
    default_destination: str, default_target_sprint_id: int | None,
) -> Sprint:
    """Delete a sprint (kept as 'cancelled' for the audit trail): its issues
    return to the backlog or move to a future sprint."""
    del cancellation_note  # audited by the route, not stored on the sprint
    if sprint.state not in ("active", "future"):
        raise SprintLifecycleError("Only an active or future sprint can be deleted")
    issues = sprint_issues(db, sprint.id)
    extraneous = set(dispositions) - {i.id for i in issues}
    if extraneous:
        raise SprintLifecycleError(f"Destinations were given for issues not in this sprint: {sorted(extraneous)}")
    plan = []
    for item in issues:
        destination, target_id = dispositions.get(item.id, (default_destination, default_target_sprint_id))
        target = _destination_sprint(db, sprint, target_id) if destination == "sprint" else None
        if destination not in ("backlog", "sprint"):
            raise SprintLifecycleError(f"Unknown destination '{destination}'")
        plan.append((item, target))
    was_active = sprint.state == "active"
    with change_reason(db, "sprint_deleted"):
        for item, target in plan:
            if was_active:
                db.add(SprintItemHistory(
                    sprint_id=sprint.id, work_item_id=item.id, event_type="removed",
                    from_sprint_id=sprint.id, to_sprint_id=target.id if target else None,
                    actor_id=actor.id, old_estimate=item.story_points,
                ))
            item.sprint_id = target.id if target else None
        for target_id in {t.id if t else None for _, t in plan}:
            items = [i for i, t in plan if (t.id if t else None) == target_id]
            scope = sprint_scope(target_id) if target_id else backlog_scope(sprint.project_id)
            _place(db, items, scope, sprint.project_id, None, None, "top")
    sprint.state = "cancelled"
    sprint.cancelled_at = _utcnow()
    db.flush()
    return sprint


def get_history(db: Session, sprint_id: int) -> list[SprintItemHistory]:
    return list(db.scalars(
        select(SprintItemHistory)
        .where(SprintItemHistory.sprint_id == sprint_id)
        .order_by(SprintItemHistory.occurred_at, SprintItemHistory.id)
    ).all())
