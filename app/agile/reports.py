"""Agile reports, computed the way Jira Software computes them: by replaying
each issue's change history (written for every write path by
app.agile.integrity) against a sprint's exact start and completion instants.

Conventions, all as in Jira:

* the statistic is the board's estimation: story points, original time
  estimate (in hours) or issue count; an unestimated issue counts 0;
* only standard issues (Story, Task, Bug, Requirement) count; Sub-tasks are
  part of their parent;
* an issue is done when its status is in the board's right-most column;
* calendar days are the board's timezone; the guideline stays flat on the
  board's non-working weekdays.

Sprints started before 4.0 recorded their full history have only their
start/finish summary rows (sprint_item_history); their reports are built
from those and flagged ``history_complete = False``.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.agile.boards import done_status_values, ordered_columns, status_to_column
from app.agile.integrity import format_value, parse_number
from app.agile.itemtypes import EPIC, STANDARD_TYPES, SUBTASK
from app.agile.taxonomy import category_of, epic_progress, status_categories
from app.models import (
    Board,
    Bug,
    Sprint,
    SprintCapacity,
    SprintItemHistory,
    User,
    WorkItemChange,
)
from app.schemas import display_id_for

UTC = timezone.utc
STATISTIC_LABELS = {
    "story_points": "Story points",
    "time": "Original time estimate (hours)",
    "item_count": "Issue count",
}
_CURRENT_ATTR = {
    "status": "status", "sprint": "sprint_id", "story_points": "story_points",
    "original_estimate": "original_estimate_minutes", "epic": "epic_id",
    "parent": "parent_id", "item_type": "item_type", "project": "project_id",
}
# Event labels of the burndown/sprint report timelines (shown to users).
EVENT_ADDED = "Issue added to sprint"
EVENT_REMOVED = "Issue removed from sprint"
EVENT_COMPLETED = "Issue completed"
EVENT_REOPENED = "Issue reopened"
EVENT_ESTIMATE = "Estimate changed"
EVENT_CREATED = "Issue created in sprint"
_SIDE_EFFECT_REASONS = ("sprint_completed", "sprint_deleted")
_CHUNK = 500


def statistic_label(mode: str) -> str:
    return STATISTIC_LABELS.get(mode, STATISTIC_LABELS["story_points"])


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def board_zone(board: Board | None) -> tzinfo:
    try:
        return ZoneInfo((board.timezone if board else None) or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _midnight(day: date, zone: tzinfo) -> datetime:
    return datetime.combine(day, time.min, zone).astimezone(UTC)


def _display(item: Bug) -> str:
    return (item.display_id or "").strip() or display_id_for(item.item_type or "Bug", item.id)


def _chunks(values: list, size: int = _CHUNK):
    for n in range(0, len(values), size):
        yield values[n:n + size]


def statistic_value(mode: str, points_text: str | None, minutes_text: str | None) -> float:
    if mode == "item_count":
        return 1.0
    if mode == "time":
        minutes = parse_number(minutes_text)
        return round(minutes / 60, 2) if minutes else 0.0
    return parse_number(points_text) or 0.0


def item_statistic(mode: str, item: Bug) -> float:
    return statistic_value(mode, format_value(item.story_points), format_value(item.original_estimate_minutes))


# --- Change-history replay -----------------------------------------------------

@dataclass
class Change:
    at: datetime
    id: int
    item_id: int
    field: str
    old: str | None
    new: str | None
    reason: str | None
    actor_id: int | None


class History:
    """The recorded changes of a set of issues, and their values at any instant."""

    def __init__(self, db: Session, items: list[Bug]) -> None:
        self.items = {i.id: i for i in items}
        self.by_item: dict[int, dict[str, list[Change]]] = defaultdict(lambda: defaultdict(list))
        self.all: list[Change] = []
        created: dict[int, datetime] = {}
        for chunk in _chunks(list(self.items)):
            for row in db.scalars(select(WorkItemChange).where(WorkItemChange.work_item_id.in_(chunk))).all():
                change = Change(
                    at=_aware(row.changed_at), id=row.id, item_id=row.work_item_id, field=row.field,
                    old=row.old_value, new=row.new_value, reason=row.reason, actor_id=row.actor_id,
                )
                self.all.append(change)
                if row.field == "created":
                    created[row.work_item_id] = min(created.get(row.work_item_id, change.at), change.at)
        self.all.sort(key=lambda c: (c.at, c.id))
        for change in self.all:
            self.by_item[change.item_id][change.field].append(change)
        self.created = {
            item_id: created.get(item_id) or _aware(item.created_at)
            for item_id, item in self.items.items()
        }

    def current(self, item_id: int, field_name: str) -> str | None:
        return format_value(getattr(self.items[item_id], _CURRENT_ATTR[field_name]))

    def value(self, item_id: int, field_name: str, at: datetime) -> str | None:
        """The field's value at ``at``: the old value of the first later change,
        else the current value."""
        for change in self.by_item[item_id].get(field_name, ()):
            if change.at > at:
                return change.old
        return self.current(item_id, field_name)

    def exists(self, item_id: int, at: datetime) -> bool:
        return self.created[item_id] <= at

    def between(self, start: datetime, end: datetime) -> list[Change]:
        return [c for c in self.all if start < c.at <= end]


@dataclass
class IssueState:
    exists: bool
    sprint: str | None
    status: str | None
    points: str | None
    minutes: str | None

    @classmethod
    def at(cls, history: History, item_id: int, instant: datetime) -> "IssueState":
        return cls(
            exists=history.exists(item_id, instant),
            sprint=history.value(item_id, "sprint", instant),
            status=history.value(item_id, "status", instant),
            points=history.value(item_id, "story_points", instant),
            minutes=history.value(item_id, "original_estimate", instant),
        )

    def apply(self, change: Change) -> None:
        if change.field == "sprint":
            self.sprint = change.new
        elif change.field == "status":
            self.status = change.new
        elif change.field == "story_points":
            self.points = change.new
        elif change.field == "original_estimate":
            self.minutes = change.new


# --- Sprint analysis (shared by sprint report, burndown/burnup, velocity) ------

@dataclass
class SprintAnalysis:
    sprint: Sprint
    board: Board
    mode: str
    done: set[str]
    start: datetime | None
    planned_end: datetime | None
    end: datetime | None
    history_complete: bool = True
    items: dict[int, Bug] = field(default_factory=dict)
    committed: dict[int, float] = field(default_factory=dict)
    added: set[int] = field(default_factory=set)
    removed: dict[int, float] = field(default_factory=dict)
    completed: set[int] = field(default_factory=set)
    incomplete: set[int] = field(default_factory=set)
    completed_outside: set[int] = field(default_factory=set)
    estimate_at_end: dict[int, float] = field(default_factory=dict)
    estimate_change_count: int = 0
    series: list[tuple[datetime, float, float, float]] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    @property
    def completed_inside(self) -> set[int]:
        """Completed in this sprint: Jira's "Completed issues" and velocity.
        Issues already done when they joined the sprint are listed apart."""
        return self.completed - self.completed_outside

    def total(self, ids) -> float:
        return sum(self.estimate_at_end.get(i, 0.0) for i in ids)


def sprint_window(sprint: Sprint, board: Board) -> tuple[datetime | None, datetime | None, datetime | None]:
    """(start, planned end, end of the reported period) of ``sprint``."""
    zone = board_zone(board)
    start = _aware(sprint.started_at)
    if start is None and sprint.start_date:
        start = _midnight(date.fromisoformat(sprint.start_date), zone)
    planned_end = (
        _midnight(date.fromisoformat(sprint.end_date) + timedelta(days=1), zone) if sprint.end_date else None
    )
    if sprint.state == "future" or start is None:
        return None, planned_end, None
    if sprint.state == "closed":
        end = _aware(sprint.completed_at) or planned_end or _now()
    elif sprint.state == "cancelled":
        end = _aware(sprint.cancelled_at) or _now()
    else:
        end = _now()
    return start, planned_end, max(end, start)


def sprint_candidates(db: Session, sprint: Sprint) -> list[Bug]:
    """Issues that were in ``sprint`` at any time (Sub-tasks and issues
    converted since included; analyze_sprint keeps the standard issues)."""
    sid = str(sprint.id)
    ids = set(db.scalars(select(Bug.id).where(Bug.sprint_id == sprint.id)).all())
    ids |= set(db.scalars(select(WorkItemChange.work_item_id).where(
        WorkItemChange.field == "sprint",
        or_(WorkItemChange.old_value == sid, WorkItemChange.new_value == sid),
    )).all())
    ids |= set(db.scalars(
        select(SprintItemHistory.work_item_id).where(SprintItemHistory.sprint_id == sprint.id)
    ).all())
    items: list[Bug] = []
    for chunk in _chunks(sorted(ids)):
        items.extend(db.scalars(
            select(Bug).options(selectinload(Bug.assignees))
            .where(Bug.id.in_(chunk), Bug.item_type != EPIC)
        ).all())
    return items


def _event_label(field_name: str, was_member: bool, is_member: bool, was_done: bool, is_done: bool) -> str | None:
    if not was_member and is_member:
        return EVENT_ADDED
    if was_member and not is_member:
        return EVENT_REMOVED
    if not is_member:
        return None
    if field_name == "status":
        if not was_done and is_done:
            return EVENT_COMPLETED
        if was_done and not is_done:
            return EVENT_REOPENED
        return None
    if field_name in ("story_points", "original_estimate"):
        return EVENT_ESTIMATE
    if field_name == "created":
        return EVENT_CREATED
    return None


def _completion_from_records(db: Session, sprint: Sprint) -> tuple[set[int], set[int]] | None:
    rows = db.scalars(select(SprintItemHistory).where(
        SprintItemHistory.sprint_id == sprint.id,
        SprintItemHistory.event_type.in_(("completed", "carried_over")),
    )).all()
    if not rows:
        return None
    completed = {r.work_item_id for r in rows if r.event_type == "completed"}
    incomplete = {r.work_item_id for r in rows if r.event_type == "carried_over"}
    return completed, incomplete


def _live_completion(db: Session, members: set[int], done: set[str], history: History,
                     at: datetime) -> tuple[set[int], set[int]]:
    """Jira's completion rule for a running sprint: the issue and all of its
    Sub-tasks are done."""
    open_parents: set[int] = set()
    if members:
        for chunk in _chunks(sorted(members)):
            for parent_id, status in db.execute(
                select(Bug.parent_id, Bug.status).where(Bug.parent_id.in_(chunk), Bug.item_type == SUBTASK)
            ).all():
                if status not in done:
                    open_parents.add(parent_id)
    completed = {
        i for i in members
        if history.value(i, "status", at) in done and i not in open_parents
    }
    return completed, members - completed


def analyze_sprint(db: Session, sprint: Sprint, board: Board) -> SprintAnalysis:
    mode = board.estimation_mode
    done = done_status_values(db, board)
    start, planned_end, end = sprint_window(sprint, board)
    analysis = SprintAnalysis(sprint=sprint, board=board, mode=mode, done=done,
                              start=start, planned_end=planned_end, end=end)
    items = sprint_candidates(db, sprint)
    analysis.items = {i.id: i for i in items}
    if start is None or end is None:
        analysis.items = {i: item for i, item in analysis.items.items() if item.item_type in STANDARD_TYPES}
        return analysis
    if sprint.started_at is None:
        analysis.items = {i: item for i, item in analysis.items.items() if item.item_type in STANDARD_TYPES}
        return _analyze_legacy(db, analysis)

    history = History(db, items)
    # A closed sprint keeps the issues it had: an issue converted to another
    # type afterwards still counts as what it was when the sprint ended.
    analysis.items = {
        i: item for i, item in analysis.items.items() if history.value(i, "item_type", end) in STANDARD_TYPES
    }
    sid = str(sprint.id)
    states = {i: IssueState.at(history, i, start) for i in analysis.items}
    outside = analysis.completed_outside

    def member(st: IssueState) -> bool:
        return st.exists and st.sprint == sid

    def estimate(st: IssueState) -> float:
        return statistic_value(mode, st.points, st.minutes)

    def contribution(item_id: int, st: IssueState) -> tuple[float, float, float]:
        # Work completed outside the sprint is listed apart and never burns.
        if not member(st) or item_id in outside:
            return 0.0, 0.0, 0.0
        value = estimate(st)
        finished = st.status in done
        return (0.0 if finished else value), value, (value if finished else 0.0)

    # Issues already done when the sprint started were completed outside it:
    # they are not part of the commitment.
    outside.update(i for i, st in states.items() if member(st) and st.status in done)
    analysis.committed = {i: estimate(st) for i, st in states.items() if member(st) and i not in outside}
    remaining = scope = completed = 0.0
    for i, st in states.items():
        r, sc, c = contribution(i, st)
        remaining, scope, completed = remaining + r, scope + sc, completed + c
    analysis.series.append((start, remaining, scope, completed))

    for change in history.between(start, end):
        if change.item_id not in states:
            continue
        if change.field not in ("created", "sprint", "status", "story_points", "original_estimate"):
            continue
        if change.reason in _SIDE_EFFECT_REASONS and change.field == "sprint":
            continue
        item_id = change.item_id
        st = states[item_id]
        before = contribution(item_id, st)
        was_member, was_done, estimate_before = member(st), st.status in done, estimate(st)
        if change.field == "created":
            states[item_id] = st = IssueState.at(history, item_id, change.at)
        else:
            st.apply(change)
        is_member, is_done = member(st), st.status in done
        label = _event_label(change.field, was_member, is_member, was_done, is_done)
        if label in (EVENT_ADDED, EVENT_CREATED):
            if item_id not in analysis.committed:
                analysis.added.add(item_id)
            if is_done:
                outside.add(item_id)
            analysis.removed.pop(item_id, None)
        elif label == EVENT_REMOVED:
            if item_id in outside:
                outside.discard(item_id)
            else:
                analysis.removed[item_id] = estimate_before
        elif label == EVENT_ESTIMATE:
            if estimate(st) == estimate_before:
                label = None
            else:
                analysis.estimate_change_count += 1
        if change.field == "status" and is_member and was_done and not is_done and item_id in outside:
            # Reopened during the sprint: it is now work of this sprint.
            outside.discard(item_id)
            if item_id not in analysis.committed:
                analysis.added.add(item_id)
        after = contribution(item_id, st)
        delta_remaining = after[0] - before[0]
        remaining, scope, completed = (
            remaining + delta_remaining, scope + (after[1] - before[1]), completed + (after[2] - before[2]),
        )
        if label is None or (delta_remaining == 0 and after[1] == before[1] and after[2] == before[2]):
            continue
        item = analysis.items[item_id]
        analysis.series.append((change.at, remaining, scope, completed))
        analysis.events.append({
            "at": change.at, "work_item_id": item.id, "display_id": _display(item),
            "title": item.title, "event": label, "change": round(delta_remaining, 2),
            "actor_id": change.actor_id,
            "remaining": round(remaining, 2),
            "scope_change": label in (EVENT_ADDED, EVENT_REMOVED,
                                      EVENT_ESTIMATE, EVENT_CREATED),
        })

    members = {i for i, st in states.items() if member(st)}
    recorded = _completion_from_records(db, sprint) if sprint.state == "closed" else None
    if recorded is not None:
        analysis.completed, analysis.incomplete = recorded
    else:
        analysis.completed, analysis.incomplete = _live_completion(db, members, done, history, end)
    analysis.completed &= set(analysis.items)
    analysis.incomplete &= set(analysis.items)
    analysis.removed = {i: v for i, v in analysis.removed.items()
                        if i not in analysis.completed and i not in analysis.incomplete}
    analysis.estimate_at_end = {i: estimate(states[i]) for i in analysis.items}
    # The last point uses the sprint report's completion rule (an issue with
    # an open Sub-task is not done), so the charts and the report agree.
    end_completed = analysis.total(analysis.completed_inside)
    end_remaining = analysis.total(analysis.incomplete - outside)
    analysis.series.append((end, end_remaining, end_completed + end_remaining, end_completed))
    return analysis


def _analyze_legacy(db: Session, analysis: SprintAnalysis) -> SprintAnalysis:
    """Sprints from before full history: use the sprint's own event rows."""
    analysis.history_complete = False
    mode, sprint = analysis.mode, analysis.sprint
    rows = db.scalars(
        select(SprintItemHistory).where(SprintItemHistory.sprint_id == sprint.id)
        .order_by(SprintItemHistory.occurred_at, SprintItemHistory.id)
    ).all()

    def est(item_id: int, recorded) -> float:
        if mode == "story_points" and recorded is not None:
            return float(recorded)
        item = analysis.items.get(item_id)
        return item_statistic(mode, item) if item else 0.0

    for row in rows:
        if row.work_item_id not in analysis.items:
            continue
        if row.event_type == "committed":
            analysis.committed[row.work_item_id] = est(row.work_item_id, row.new_estimate)
        elif row.event_type == "added":
            analysis.added.add(row.work_item_id)
        elif row.event_type == "removed":
            analysis.removed[row.work_item_id] = est(row.work_item_id, row.old_estimate)
    recorded = _completion_from_records(db, sprint)
    if recorded is not None:
        analysis.completed, analysis.incomplete = recorded
    else:
        members = {i for i, item in analysis.items.items() if item.sprint_id == sprint.id}
        analysis.completed = {i for i in members if analysis.items[i].status in analysis.done}
        analysis.incomplete = members - analysis.completed
    analysis.removed = {i: v for i, v in analysis.removed.items()
                        if i not in analysis.completed and i not in analysis.incomplete}
    analysis.estimate_at_end = {i: item_statistic(mode, item) for i, item in analysis.items.items()}
    scope = sum(analysis.committed.values()) + sum(analysis.estimate_at_end[i] for i in analysis.added) \
        - sum(analysis.removed.values())
    done_value = sum(analysis.estimate_at_end[i] for i in analysis.completed)
    analysis.series = [
        (analysis.start, sum(analysis.committed.values()), sum(analysis.committed.values()), 0.0),
        (analysis.end, max(scope - done_value, 0.0), scope, done_value),
    ]
    return analysis


# --- Report builders -------------------------------------------------------------

def _issue_row(analysis: SprintAnalysis, item_id: int, categories: dict, estimate: float | None) -> dict:
    item = analysis.items[item_id]
    return {
        "id": item.id, "display_id": _display(item), "title": item.title,
        "item_type": item.item_type or "Bug", "status": item.status,
        "status_category": "done" if item.status in analysis.done else category_of(categories, item),
        "estimate": round(estimate, 2) if estimate is not None else None,
        "estimate_at_start": (round(analysis.committed[item_id], 2) if item_id in analysis.committed else None),
        "added_during_sprint": item_id in analysis.added,
        "assignees": [a.name for a in item.assignees],
    }


def sprint_report(db: Session, sprint: Sprint, board: Board) -> dict:
    a = analyze_sprint(db, sprint, board)
    categories = status_categories(db)

    def rows(ids, estimates) -> list[dict]:
        return sorted(
            (_issue_row(a, i, categories, estimates.get(i)) for i in ids if i in a.items),
            key=lambda r: r["id"],
        )

    completed_est = {i: a.estimate_at_end.get(i, 0.0) for i in a.completed}
    incomplete_est = {i: a.estimate_at_end.get(i, 0.0) for i in a.incomplete}
    return {
        "sprint_id": sprint.id, "sprint_name": sprint.name, "state": sprint.state,
        "goal": sprint.goal or "", "estimation_mode": a.mode, "statistic_label": statistic_label(a.mode),
        "start_date": sprint.start_date, "end_date": sprint.end_date,
        "started_at": sprint.started_at, "completed_at": sprint.completed_at,
        "committed_count": len(a.committed),
        "committed_estimate": round(sum(a.committed.values()), 2),
        "completed_count": len(a.completed_inside),
        "completed_estimate": round(a.total(a.completed_inside), 2),
        "completed_committed_count": len(a.completed_inside & set(a.committed)),
        "completed_outside_count": len(a.completed & a.completed_outside),
        "completed_outside_estimate": round(a.total(a.completed & a.completed_outside), 2),
        "incomplete_count": len(a.incomplete),
        "incomplete_estimate": round(sum(incomplete_est.values()), 2),
        "added_count": len(a.added),
        "added_estimate": round(sum(a.estimate_at_end.get(i, 0.0) for i in a.added), 2),
        "removed_count": len(a.removed),
        "removed_estimate": round(sum(a.removed.values()), 2),
        "estimate_change_count": a.estimate_change_count,
        "completed": rows(a.completed - a.completed_outside, completed_est),
        "not_completed": rows(a.incomplete, incomplete_est),
        "removed": rows(set(a.removed), a.removed),
        "completed_outside": rows(a.completed & a.completed_outside, completed_est),
        "history_complete": a.history_complete,
    }


def guideline(start: datetime, planned_end: datetime | None, total: float, board: Board) -> list[dict]:
    """Jira's guideline: from the committed total at the start to zero at the
    planned end, flat over non-working days."""
    if planned_end is None or planned_end <= start:
        return []
    zone = board_zone(board)
    working = set(board.working_weekdays or [1, 2, 3, 4, 5])
    boundaries = [start]
    day = start.astimezone(zone).date() + timedelta(days=1)
    while _midnight(day, zone) < planned_end:
        boundaries.append(_midnight(day, zone))
        day += timedelta(days=1)
    boundaries.append(planned_end)
    segments = [
        (a, b, a.astimezone(zone).isoweekday() in working) for a, b in zip(boundaries, boundaries[1:])
    ]
    work_seconds = sum((b - a).total_seconds() for a, b, w in segments if w)
    if work_seconds <= 0:
        return [{"at": start, "value": total}, {"at": planned_end, "value": 0.0}]
    value = total
    points = [{"at": start, "value": round(total, 2)}]
    for a, b, is_working in segments:
        if is_working:
            value -= total * (b - a).total_seconds() / work_seconds
        points.append({"at": b, "value": round(max(value, 0.0), 2)})
    points[-1]["value"] = 0.0
    return points


def non_working_days(start: datetime | None, planned_end: datetime | None, board: Board) -> list[str]:
    if start is None or planned_end is None:
        return []
    zone = board_zone(board)
    working = set(board.working_weekdays or [1, 2, 3, 4, 5])
    day, last = start.astimezone(zone).date(), (planned_end - timedelta(seconds=1)).astimezone(zone).date()
    out = []
    while day <= last:
        if day.isoweekday() not in working:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def board_days(start: datetime | None, end: datetime | None, board: Board) -> list[dict]:
    """Every board-local calendar day the chart spans, as exact instants: the
    browser draws day ticks and non-working bands from these without doing
    any timezone arithmetic of its own (a viewer in another timezone sees the
    board's days, not their own)."""
    if start is None or end is None:
        return []
    zone = board_zone(board)
    working = set(board.working_weekdays or [1, 2, 3, 4, 5])
    day, last = start.astimezone(zone).date(), (end - timedelta(seconds=1)).astimezone(zone).date()
    out = []
    while day <= last:
        out.append({
            "date": day.isoformat(),
            "start": _midnight(day, zone).astimezone(timezone.utc),
            "end": _midnight(day + timedelta(days=1), zone).astimezone(timezone.utc),
            "working": day.isoweekday() in working,
        })
        day += timedelta(days=1)
    return out


def burndown_report(db: Session, sprint: Sprint, board: Board) -> dict:
    a = analyze_sprint(db, sprint, board)
    last = a.series[-1] if a.series else None
    start_remaining = a.series[0][1] if a.series else 0.0
    return {
        "sprint_id": sprint.id, "sprint_name": sprint.name, "state": sprint.state,
        "estimation_mode": a.mode, "statistic_label": statistic_label(a.mode),
        "start": a.start, "planned_end": a.planned_end, "end": a.end,
        "committed_estimate": round(sum(a.committed.values()), 2),
        "remaining_estimate": round(last[1], 2) if last else 0.0,
        "scope_estimate": round(last[2], 2) if last else 0.0,
        "completed_estimate": round(last[3], 2) if last else 0.0,
        "points": [
            {"at": at, "remaining": round(r, 2), "scope": round(s, 2), "completed": round(c, 2)}
            for at, r, s, c in a.series
        ],
        "guideline": guideline(a.start, a.planned_end, start_remaining, board) if a.start else [],
        "events": a.events,
        "non_working_days": non_working_days(a.start, a.planned_end, board),
        "timezone": str(board.timezone or "UTC"),
        "days": board_days(a.start, max(filter(None, (a.end, a.planned_end)), default=None), board),
        "history_complete": a.history_complete,
    }


burnup_report = burndown_report


def velocity_report(db: Session, board: Board, sprint_count: int) -> dict:
    sprints = list(db.scalars(
        select(Sprint).where(Sprint.board_id == board.id, Sprint.state == "closed")
        .order_by(Sprint.completed_at.desc().nulls_last(), Sprint.sequence_number.desc()).limit(sprint_count)
    ).all())
    sprints.reverse()
    points = []
    for sprint in sprints:
        a = analyze_sprint(db, sprint, board)
        points.append({
            "sprint_id": sprint.id, "sprint_name": sprint.name,
            "start_date": sprint.start_date, "end_date": sprint.end_date,
            "committed_estimate": round(sum(a.committed.values()), 2),
            "completed_estimate": round(a.total(a.completed_inside), 2),
        })
    n = len(points) or 1
    return {
        "board_id": board.id, "estimation_mode": board.estimation_mode,
        "statistic_label": statistic_label(board.estimation_mode),
        "average_committed": round(sum(p["committed_estimate"] for p in points) / n, 2) if points else 0.0,
        "average_completed": round(sum(p["completed_estimate"] for p in points) / n, 2) if points else 0.0,
        "points": points,
    }


def _status_timelines(db: Session, project_id: int, types: tuple[str, ...]) -> dict[int, list[tuple[datetime, str | None]]]:
    """item id -> [(instant, status from then on)], starting at creation."""
    items = db.execute(
        select(Bug.id, Bug.status, Bug.created_at).where(Bug.project_id == project_id, Bug.item_type.in_(types))
    ).all()
    by_item: dict[int, list] = defaultdict(list)
    created: dict[int, datetime] = {}
    ids = [i.id for i in items]
    for chunk in _chunks(ids):
        for row in db.execute(
            select(WorkItemChange.work_item_id, WorkItemChange.field, WorkItemChange.old_value,
                   WorkItemChange.new_value, WorkItemChange.changed_at, WorkItemChange.id)
            .where(WorkItemChange.work_item_id.in_(chunk), WorkItemChange.field.in_(("status", "created")))
        ).all():
            if row.field == "created":
                created[row.work_item_id] = _aware(row.changed_at)
            else:
                by_item[row.work_item_id].append((_aware(row.changed_at), row.id, row.old_value, row.new_value))
    out: dict[int, list[tuple[datetime, str | None]]] = {}
    for item_id, current, created_at in items:
        changes = sorted(by_item.get(item_id, []))
        initial = changes[0][2] if changes else current
        timeline = [(created.get(item_id) or _aware(created_at), initial)]
        timeline.extend((at, new) for at, _id, _old, new in changes)
        out[item_id] = timeline
    return out


def cumulative_flow_report(db: Session, board: Board, date_from: str, date_to: str,
                           include_subtasks: bool = True) -> dict:
    zone = board_zone(board)
    columns = ordered_columns(board)
    placement = {status: col.id for status, col in status_to_column(board).items()}
    types = (*STANDARD_TYPES, SUBTASK) if include_subtasks else STANDARD_TYPES
    timelines = _status_timelines(db, board.project_id, types)
    first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
    events: list[tuple[datetime, int | None, int | None]] = []
    for timeline in timelines.values():
        previous_column = None
        for at, status in timeline:
            column = placement.get(status)
            if column != previous_column:
                events.append((at, previous_column, column))
            previous_column = column
    events.sort(key=lambda e: e[0])
    counts = {col.id: 0 for col in columns}
    points = []
    cursor = 0
    day = first
    while day <= last:
        day_end = _midnight(day + timedelta(days=1), zone)
        while cursor < len(events) and events[cursor][0] < day_end:
            _at, old, new = events[cursor]
            if old is not None:
                counts[old] -= 1
            if new is not None:
                counts[new] += 1
            cursor += 1
        points.append({"date": day.isoformat(), "counts": {str(k): v for k, v in counts.items()}})
        day += timedelta(days=1)
    return {
        "board_id": board.id, "project_id": board.project_id,
        "columns": [{"id": c.id, "name": c.name, "category": c.category} for c in columns],
        "points": points,
    }


def _work_column_statuses(board: Board) -> tuple[set[str], list[str]]:
    columns = ordered_columns(board)
    work = columns[1:-1] if len(columns) >= 3 else columns[:-1]
    placement = status_to_column(board)
    statuses = {s for s, col in placement.items() if col in work}
    return statuses, [c.name for c in work]


def control_chart_report(db: Session, board: Board, date_from: str, date_to: str) -> dict:
    zone = board_zone(board)
    done = done_status_values(db, board)
    work_statuses, work_columns = _work_column_statuses(board)
    range_start = _midnight(date.fromisoformat(date_from), zone)
    range_end = _midnight(date.fromisoformat(date_to) + timedelta(days=1), zone)
    timelines = _status_timelines(db, board.project_id, STANDARD_TYPES)
    completions: list[tuple[int, datetime | None, datetime, float, datetime]] = []
    # One dot per issue, at its last move into the done column, as in Jira.
    # Its cycle time is the time spent in the work columns up to then,
    # summed over every pass (an issue reopened and finished again counts all
    # the time worked on it).
    for item_id, timeline in timelines.items():
        created_at = timeline[0][0]
        cycle_seconds = 0.0
        started: datetime | None = None
        last: tuple | None = None
        for (at, status), nxt in zip(timeline, timeline[1:] + [(None, None)]):
            next_at, next_status = nxt
            if status in work_statuses and next_at is not None:
                cycle_seconds += (next_at - at).total_seconds()
                started = started or at
            if next_at is not None and next_status in done and status not in done:
                last = (item_id, started, next_at, cycle_seconds, created_at)
        if last is not None and range_start <= last[2] < range_end and timeline[-1][1] in done:
            completions.append(last)
    ids = [c[0] for c in completions]
    items = {}
    for chunk in _chunks(sorted(set(ids))):
        items.update({b.id: b for b in db.scalars(select(Bug).where(Bug.id.in_(chunk))).all()})
    entries = []
    for item_id, started, finished, seconds, created_at in sorted(completions, key=lambda c: c[2]):
        item = items.get(item_id)
        if item is None:
            continue
        entries.append({
            "work_item_id": item_id, "display_id": _display(item), "title": item.title,
            "item_type": item.item_type or "Bug", "started_at": started, "completed_at": finished,
            "cycle_time_days": round(seconds / 86400, 2),
            "lead_time_days": round((finished - created_at).total_seconds() / 86400, 2),
        })
    values = [e["cycle_time_days"] for e in entries]
    window = max(1, round(len(values) * 0.2))
    rolling = []
    for n, entry in enumerate(entries):
        lo, hi = max(0, n - window // 2), min(len(values), n + window // 2 + 1)
        rolling.append({"completed_at": entry["completed_at"],
                        "value": round(sum(values[lo:hi]) / (hi - lo), 2)})
    return {
        "board_id": board.id, "work_columns": work_columns, "count": len(values),
        "average_days": round(statistics.fmean(values), 2) if values else 0.0,
        "median_days": round(statistics.median(values), 2) if values else 0.0,
        "min_days": min(values) if values else 0.0,
        "max_days": max(values) if values else 0.0,
        "std_dev_days": round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0,
        "entries": entries, "rolling_average": rolling,
    }


def epic_report(db: Session, epic: Bug, board: Board | None) -> dict:
    mode = board.estimation_mode if board else "story_points"
    categories = status_categories(db)
    issues = list(db.scalars(
        select(Bug).options(selectinload(Bug.assignees))
        .where(Bug.epic_id == epic.id, Bug.item_type.in_(STANDARD_TYPES)).order_by(Bug.id)
    ).all())
    buckets: dict[str, list[dict]] = {"done": [], "in_progress": [], "todo": []}
    for issue in issues:
        category = category_of(categories, issue)
        bucket = "done" if category == "done" else ("in_progress" if category in ("in_progress", "testing") else "todo")
        buckets[bucket].append({
            "id": issue.id, "display_id": _display(issue), "title": issue.title,
            "item_type": issue.item_type or "Bug", "status": issue.status, "status_category": category,
            "estimate": item_statistic(mode, issue), "assignees": [a.name for a in issue.assignees],
        })
    sprint_rows = []
    if board is not None:
        epic_text = str(epic.id)
        candidate_ids = {i.id for i in issues} | set(db.scalars(select(WorkItemChange.work_item_id).where(
            WorkItemChange.field == "epic",
            or_(WorkItemChange.old_value == epic_text, WorkItemChange.new_value == epic_text),
        )).all())
        candidates = []
        for chunk in _chunks(sorted(candidate_ids)):
            candidates.extend(db.scalars(select(Bug).where(Bug.id.in_(chunk), Bug.item_type.in_(STANDARD_TYPES))).all())
        history = History(db, candidates)
        done = done_status_values(db, board)
        sprints = db.scalars(
            select(Sprint).where(Sprint.board_id == board.id, Sprint.state.in_(("active", "closed")))
            .order_by(Sprint.sequence_number, Sprint.id)
        ).all()
        for sprint in sprints:
            a = analyze_sprint(db, sprint, board)
            if a.end is None:
                continue
            in_epic = [i for i in history.items if history.exists(i, a.end)
                       and history.value(i, "epic", a.end) == epic_text]
            scope = sum(statistic_value(mode, history.value(i, "story_points", a.end),
                                        history.value(i, "original_estimate", a.end)) for i in in_epic)
            finished = [i for i in in_epic if history.value(i, "status", a.end) in done]
            finished_value = sum(statistic_value(mode, history.value(i, "story_points", a.end),
                                                 history.value(i, "original_estimate", a.end)) for i in finished)
            completed_here = [i for i in a.completed_inside if i in history.items
                              and history.value(i, "epic", a.end) == epic_text]
            sprint_rows.append({
                "sprint_id": sprint.id, "sprint_name": sprint.name, "state": sprint.state,
                "completed_count": len(completed_here),
                "completed_estimate": round(sum(a.estimate_at_end.get(i, 0.0) for i in completed_here), 2),
                "scope_estimate": round(scope, 2),
                "remaining_estimate": round(scope - finished_value, 2),
            })
    return {
        "epic_id": epic.id, "display_id": _display(epic), "title": epic.title, "status": epic.status,
        "estimation_mode": mode, "statistic_label": statistic_label(mode),
        "progress": epic_progress(db, epic), "sprints": sprint_rows,
        "done": buckets["done"], "in_progress": buckets["in_progress"], "todo": buckets["todo"],
    }


def workload_report(db: Session, sprint: Sprint, board: Board) -> dict:
    mode = board.estimation_mode
    if sprint.state == "closed":
        # What the sprint held when it closed, carried-over issues included,
        # at their estimates then.
        a = analyze_sprint(db, sprint, board)
        items = [a.items[i] for i in sorted(a.completed | a.incomplete)]
        estimates = {i: a.estimate_at_end.get(i, 0.0) for i in a.items}
    else:
        items = list(db.scalars(
            select(Bug).options(selectinload(Bug.assignees))
            .where(Bug.sprint_id == sprint.id, Bug.item_type.in_(STANDARD_TYPES))
        ).all())
        estimates = {item.id: item_statistic(mode, item) for item in items}
    capacity = {c.user_id: c for c in db.scalars(select(SprintCapacity).where(SprintCapacity.sprint_id == sprint.id)).all()}
    per_user: dict[int, dict] = {}
    unassigned_count, unassigned_estimate = 0, 0.0
    for item in items:
        value = estimates[item.id]
        if not item.assignees:
            unassigned_count += 1
            unassigned_estimate += value
        for assignee in item.assignees:
            entry = per_user.setdefault(assignee.id, {"count": 0, "estimate": 0.0, "name": assignee.name})
            entry["count"] += 1
            entry["estimate"] += value
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(set(per_user) | set(capacity)))).all()} \
        if (per_user or capacity) else {}
    entries = []
    for user_id in sorted(set(per_user) | set(capacity), key=lambda u: (users[u].name if u in users else "")):
        data = per_user.get(user_id, {"count": 0, "estimate": 0.0, "name": users[user_id].name if user_id in users else "?"})
        cap = capacity.get(user_id)
        cap_value = float(cap.capacity_value) if cap else None
        entries.append({
            "user_id": user_id, "user_name": data["name"],
            "assigned_estimate": round(data["estimate"], 2), "assigned_count": data["count"],
            "capacity_value": cap_value, "capacity_unit": cap.capacity_unit if cap else None,
            "utilization_pct": round(data["estimate"] / cap_value * 100, 1) if cap_value else None,
        })
    return {"sprint_id": sprint.id, "unassigned_count": unassigned_count,
            "unassigned_estimate": round(unassigned_estimate, 2), "entries": entries}


def scope_change_report(db: Session, sprint: Sprint, board: Board) -> dict:
    a = analyze_sprint(db, sprint, board)
    actor_ids = {e["actor_id"] for e in a.events if e.get("actor_id")}
    names = {u.id: u.name for u in db.scalars(select(User).where(User.id.in_(actor_ids))).all()} if actor_ids else {}
    entries = []
    kinds = {EVENT_ADDED: "added", EVENT_CREATED: "added",
             EVENT_REMOVED: "removed", EVENT_ESTIMATE: "estimate_changed"}
    for event in a.events:
        if event["event"] in kinds:
            entries.append({
                "work_item_id": event["work_item_id"], "display_id": event["display_id"],
                "title": event["title"], "event_type": kinds[event["event"]],
                "occurred_at": event["at"], "change": event["change"],
                "actor_name": names.get(event.get("actor_id"), ""),
            })
    if not a.history_complete:
        for row in db.scalars(select(SprintItemHistory).where(
            SprintItemHistory.sprint_id == sprint.id,
            SprintItemHistory.event_type.in_(("added", "removed")),
        ).order_by(SprintItemHistory.occurred_at)).all():
            item = a.items.get(row.work_item_id)
            entries.append({
                "work_item_id": row.work_item_id, "display_id": _display(item) if item else f"#{row.work_item_id}",
                "title": item.title if item else f"#{row.work_item_id}", "event_type": row.event_type,
                "occurred_at": _aware(row.occurred_at), "change": 0.0, "actor_name": "",
            })
    return {"sprint_id": sprint.id, "entries": entries}


def daily_report(db: Session, sprint: Sprint, board: Board, report_date: str, generated_by_id: int | None) -> dict:
    """One calendar day (board timezone) of a sprint: where every issue stood
    at the end of the day, what moved during it, and the current blockers."""
    zone = board_zone(board)
    day = date.fromisoformat(report_date)
    day_start, day_end = _midnight(day, zone), _midnight(day + timedelta(days=1), zone)
    a = analyze_sprint(db, sprint, board)
    mode = a.mode
    placement = status_to_column(board)
    columns = ordered_columns(board)
    last_column = columns[-1] if columns else None
    history = History(db, list(a.items.values()))
    sid = str(sprint.id)
    at = min(day_end, a.end) if a.end else day_end
    members = [i for i in a.items if history.exists(i, at) and history.value(i, "sprint", at) == sid]
    counts = {"todo": 0, "in_progress": 0, "testing": 0, "done": 0}
    item_rows = []
    completed_value = remaining_value = 0.0
    for item_id in sorted(members):
        item = a.items[item_id]
        status = history.value(item_id, "status", at)
        column = placement.get(status)
        category = "done" if status in a.done else (column.category if column else "todo")
        if column is not None and column is last_column:
            category = "done"
        counts[category] = counts.get(category, 0) + 1
        value = statistic_value(mode, history.value(item_id, "story_points", at),
                                history.value(item_id, "original_estimate", at))
        if category == "done":
            completed_value += value
        else:
            remaining_value += value
        item_rows.append({
            "work_item_id": item_id, "display_id": _display(item), "title": item.title,
            "item_type": item.item_type or "Bug", "status": status or "", "category": category,
            "column": column.name if column else "(not on the board)", "story_points": value,
        })
    todays = [e for e in a.events if day_start <= e["at"] < day_end]
    transitions = []
    for change in history.between(day_start - timedelta(microseconds=1), day_end - timedelta(microseconds=1)):
        if change.field != "status" or change.item_id not in a.items:
            continue
        if history.value(change.item_id, "sprint", change.at) != sid:
            continue
        item = a.items[change.item_id]
        transitions.append({
            "work_item_id": item.id, "title": item.title, "event_type": "status_changed",
            "occurred_at": change.at, "detail": f"{change.old or '?'} → {change.new or '?'}",
        })
    for event in todays:
        if event["event"] != EVENT_COMPLETED and event["event"] != EVENT_REOPENED:
            transitions.append({
                "work_item_id": event["work_item_id"], "title": event["title"],
                "event_type": {EVENT_ADDED: "added", EVENT_CREATED: "added",
                               EVENT_REMOVED: "removed",
                               EVENT_ESTIMATE: "estimate_changed"}.get(event["event"], event["event"]),
                "occurred_at": event["at"], "detail": f"{event['change']:+g}",
            })
    transitions.sort(key=lambda t: t["occurred_at"])
    blockers = [a.items[i] for i in members if a.items[i].blocked or a.items[i].flagged]
    return {
        "sprint_id": sprint.id, "sprint_name": sprint.name, "goal": sprint.goal or "",
        "report_date": report_date, "generated_at": _now().replace(microsecond=0),
        "generated_by_id": generated_by_id,
        "committed_count": len(a.committed),
        "story_count": sum(1 for i in members if a.items[i].item_type == "Story"),
        "bug_count": sum(1 for i in members if a.items[i].item_type == "Bug"),
        "todo_count": counts.get("todo", 0), "in_progress_count": counts.get("in_progress", 0),
        "testing_count": counts.get("testing", 0), "done_count": counts.get("done", 0),
        "blocked_count": len(blockers),
        "committed_estimate": round(sum(a.committed.values()), 2),
        "completed_estimate": round(completed_value, 2),
        "remaining_estimate": round(remaining_value, 2),
        # Net of reopens: work finished and reopened the same day is not done.
        "completed_estimate_today": round(
            -sum(e["change"] for e in todays if e["event"] in (EVENT_COMPLETED, EVENT_REOPENED)), 2,
        ),
        "scope_added_today": sum(1 for e in todays if e["event"] in (EVENT_ADDED, EVENT_CREATED)),
        "scope_removed_today": sum(1 for e in todays if e["event"] == EVENT_REMOVED),
        "transitions": transitions,
        "blockers": [
            {"work_item_id": b.id, "title": b.title, "item_type": b.item_type or "Bug",
             "blocked_reason": b.blocked_reason or ("Flagged" if b.flagged else ""), "status": b.status}
            for b in blockers
        ],
        "items": item_rows,
    }


def release_report(db: Session, version) -> dict:
    from app.agile.releases import release_360

    return release_360(db, version)
