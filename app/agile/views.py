"""Read models for the Board (active sprint) and Backlog views.

Each view is assembled with a fixed number of batched queries, whatever the
number of issues, so a 500-issue sprint costs the same round trips as a
5-issue one.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.agile.boards import column_status_values, done_status_values, ordered_columns, status_to_column
from app.agile.itemtypes import STANDARD_TYPES, SUBTASK
from app.agile.taxonomy import category_of, epic_progress_many, status_categories
from app.auth import can_edit_bug
from app.models import Board, Bug, EpicDetail, Label, Project, Sprint, User, WorkItemLabel
from app.schemas import CANONICAL_STATUSES, display_id_for

BACKLOG_DEFAULT_LIMIT = 1000
_STATUS_ORDER = {value: n for n, value in enumerate(CANONICAL_STATUSES)}


def ordered_statuses(values) -> list[str]:
    """Status values in the workflow's canonical order (New ... Completed), so
    the first status of a column is its natural drop target."""
    return sorted(values, key=lambda v: (_STATUS_ORDER.get(v, len(_STATUS_ORDER)), v))


def display_ref(item: Bug) -> str:
    return (item.display_id or "").strip() or display_id_for(item.item_type or "Bug", item.id)


def _labels_for(db: Session, item_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if not item_ids:
        return out
    rows = db.execute(
        select(WorkItemLabel.work_item_id, Label.id, Label.display_name, Label.color)
        .join(Label, Label.id == WorkItemLabel.label_id)
        .where(WorkItemLabel.work_item_id.in_(item_ids))
        .order_by(Label.display_name)
    ).all()
    for item_id, label_id, name, color in rows:
        out.setdefault(item_id, []).append({"id": label_id, "name": name, "color": color or ""})
    return out


def _subtask_stats(db: Session, parent_ids: list[int], done: set[str]) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    if not parent_ids:
        return out
    rows = db.execute(
        select(Bug.parent_id, Bug.status).where(Bug.parent_id.in_(parent_ids), Bug.item_type == SUBTASK)
    ).all()
    for parent_id, status in rows:
        total, finished = out.get(parent_id, (0, 0))
        out[parent_id] = (total + 1, finished + (1 if status in done else 0))
    return out


def _parents(db: Session, items: list[Bug]) -> dict[int, Bug]:
    ids = {i.parent_id for i in items if i.parent_id is not None}
    if not ids:
        return {}
    return {p.id: p for p in db.scalars(select(Bug).where(Bug.id.in_(ids))).all()}


def issue_payloads(
    db: Session, items: list[Bug], viewer: User, *, done: set[str],
    column_of: dict[str, int] | None = None,
) -> list[dict]:
    categories = status_categories(db)
    ids = [i.id for i in items]
    labels = _labels_for(db, ids)
    stats = _subtask_stats(db, [i.id for i in items if i.item_type in STANDARD_TYPES], done)
    parents = _parents(db, items)
    out = []
    for item in items:
        parent = parents.get(item.parent_id) if item.parent_id is not None else None
        total, finished = stats.get(item.id, (0, 0))
        out.append({
            "id": item.id,
            "display_id": display_ref(item),
            "title": item.title,
            "item_type": item.item_type or "Bug",
            "status": item.status,
            # The board's last column decides "done" (Jira), whatever the category.
            "status_category": "done" if item.status in done else category_of(categories, item),
            "priority": item.priority,
            "story_points": float(item.story_points) if item.story_points is not None else None,
            "original_estimate_minutes": item.original_estimate_minutes,
            "assignees": item.assignees,
            "epic_id": item.epic_id,
            "parent_id": item.parent_id,
            "parent_display_id": display_ref(parent) if parent else None,
            "parent_title": parent.title if parent else None,
            "subtask_count": total,
            "subtasks_done": finished,
            "flagged": bool(item.flagged),
            "blocked": bool(item.blocked),
            "labels": labels.get(item.id, []),
            "sprint_id": item.sprint_id,
            "rank": item.rank,
            "due_date": item.due_date,
            "updated_at": item.updated_at,
            "version": item.version,
            "can_edit": can_edit_bug(viewer, item.reporter_id, [], item_type=item.item_type or "Bug"),
            "column_id": column_of.get(item.status) if column_of is not None else None,
        })
    return out


def epic_briefs(db: Session, project_id: int) -> list[dict]:
    epics = list(db.scalars(
        select(Bug).where(Bug.project_id == project_id, Bug.item_type == "Epic").order_by(Bug.rank.is_(None), Bug.rank, Bug.id)
    ).all())
    ids = [e.id for e in epics]
    details = {
        d.epic_id: d for d in db.scalars(select(EpicDetail).where(EpicDetail.epic_id.in_(ids))).all()
    } if ids else {}
    categories = status_categories(db)
    out = []
    for epic in epics:
        detail = details.get(epic.id)
        out.append({
            "id": epic.id, "display_id": display_ref(epic), "title": epic.title,
            "color": detail.color if detail else "", "status": epic.status,
            "status_category": category_of(categories, epic),
            "archived": bool(detail.archived) if detail else False,
        })
    return out


def sprint_brief(sprint: Sprint) -> dict:
    return {
        "id": sprint.id, "name": sprint.name, "goal": sprint.goal or "", "state": sprint.state,
        "start_date": sprint.start_date, "end_date": sprint.end_date,
        "started_at": sprint.started_at, "completed_at": sprint.completed_at,
        "version": sprint.version, "sequence_number": sprint.sequence_number,
    }


def _issues_query():
    return select(Bug).options(selectinload(Bug.assignees))


def board_view(db: Session, board: Board, sprint: Sprint | None, viewer: User) -> dict:
    active = list(db.scalars(
        select(Sprint).where(Sprint.board_id == board.id, Sprint.state == "active")
        .order_by(Sprint.sequence_number, Sprint.id)
    ).all())
    if sprint is None and active:
        sprint = active[0]
    columns = ordered_columns(board)
    placement = status_to_column(board)
    column_of = {status: col.id for status, col in placement.items()}
    done = done_status_values(db, board)
    items: list[Bug] = []
    if sprint is not None:
        items = list(db.scalars(
            _issues_query().where(
                Bug.sprint_id == sprint.id, Bug.item_type.in_((*STANDARD_TYPES, SUBTASK)),
            ).order_by(Bug.rank, Bug.id)
        ).all())
        # Sub-tasks have no rank of their own; keep them in their parent's order.
        order = {i.id: n for n, i in enumerate(items) if i.item_type in STANDARD_TYPES}
        items.sort(key=lambda i: (order.get(i.parent_id if i.item_type == SUBTASK else i.id, 1 << 30),
                                  i.item_type == SUBTASK, i.id))
    payloads = issue_payloads(db, items, viewer, done=done, column_of=column_of)
    by_column: dict[int, list[dict]] = {c.id: [] for c in columns}
    unmapped = []
    for card in payloads:
        if card["column_id"] is None:
            unmapped.append(card)
        else:
            by_column[card["column_id"]].append(card)
    return {
        "board_id": board.id,
        "board_name": board.name,
        "estimation_mode": board.estimation_mode,
        "swimlane_mode": board.swimlane_mode,
        "card_color_scheme": board.card_color_scheme,
        "sprint_id": sprint.id if sprint else None,
        "sprint_name": sprint.name if sprint else None,
        "sprint": sprint_brief(sprint) if sprint else None,
        "active_sprints": [sprint_brief(s) for s in active],
        "columns": [
            {
                "id": col.id, "name": col.name, "position": col.position, "category": col.category,
                "wip_limit": col.wip_limit, "min_cards": col.min_cards,
                "wip_enforcement": col.wip_enforcement or "off",
                "statuses": ordered_statuses(column_status_values(col)),
                "card_count": len(by_column[col.id]), "cards": by_column[col.id],
            }
            for col in columns
        ],
        "unmapped": unmapped,
        "epics": epic_briefs(db, board.project_id),
    }


def planning_view(db: Session, board: Board, viewer: User, backlog_limit: int = BACKLOG_DEFAULT_LIMIT) -> dict:
    project = db.get(Project, board.project_id)
    done = done_status_values(db, board)
    sprints = list(db.scalars(
        select(Sprint).where(Sprint.board_id == board.id, Sprint.state.in_(("active", "future")))
    ).all())
    sprints.sort(key=lambda s: (s.state != "active", s.sequence_number, s.id))
    sprint_ids = [s.id for s in sprints]
    sprint_items = list(db.scalars(
        _issues_query().where(Bug.sprint_id.in_(sprint_ids), Bug.item_type.in_(STANDARD_TYPES))
        .order_by(Bug.rank, Bug.id)
    ).all()) if sprint_ids else []
    # Jira's backlog: issues in no open sprint whose status is not in the
    # board's last column.
    backlog_filter = (
        Bug.project_id == board.project_id, Bug.sprint_id.is_(None),
        Bug.item_type.in_(STANDARD_TYPES),
    )
    if done:
        backlog_filter = (*backlog_filter, Bug.status.not_in(done))
    backlog_total = db.scalar(select(func.count(Bug.id)).where(*backlog_filter)) or 0
    backlog_items = list(db.scalars(
        _issues_query().where(*backlog_filter)
        .order_by(Bug.rank.is_(None), Bug.rank, Bug.id).limit(backlog_limit)
    ).all())
    payloads = issue_payloads(db, sprint_items + backlog_items, viewer, done=done)
    by_id = {p["id"]: p for p in payloads}
    sprints_out = []
    for sprint in sprints:
        brief = sprint_brief(sprint)
        brief["issues"] = [by_id[i.id] for i in sprint_items if i.sprint_id == sprint.id]
        sprints_out.append(brief)
    flags = (project.agile_feature_flags or {}) if project else {}
    return {
        "board_id": board.id,
        "estimation_mode": board.estimation_mode,
        "parallel_sprints": bool(flags.get("parallel_sprints")),
        "sprints": sprints_out,
        "backlog": [by_id[i.id] for i in backlog_items],
        "backlog_total": backlog_total,
        "epics": epic_briefs(db, board.project_id),
    }


def epic_rows(db: Session, epics: list[Bug]) -> list[dict]:
    """Epics with their detail fields and progress (EpicOut shape)."""
    ids = [e.id for e in epics]
    details = {
        d.epic_id: d for d in db.scalars(select(EpicDetail).where(EpicDetail.epic_id.in_(ids))).all()
    } if ids else {}
    progress = epic_progress_many(db, ids)
    categories = status_categories(db)
    out = []
    for epic in epics:
        detail = details.get(epic.id)
        out.append({
            "id": epic.id, "display_id": display_ref(epic), "title": epic.title,
            "description": epic.description or "", "status": epic.status,
            "status_category": category_of(categories, epic), "project_id": epic.project_id,
            "color": detail.color if detail else "",
            "owner_id": detail.owner_id if detail else None,
            "start_date": detail.start_date if detail else None,
            "target_date": detail.target_date if detail else None,
            "health": detail.health if detail else "unknown",
            "summary_note": detail.summary_note if detail else "",
            "archived": bool(detail.archived) if detail else False,
            "version": detail.version if detail else 1,
            "progress": progress[epic.id],
        })
    return out


HIERARCHY_UNPARENTED_LIMIT = 500


def hierarchy_view(
    db: Session, project_id: int, viewer: User, board: Board | None,
    *, q: str = "", include_done: bool = True,
) -> dict:
    """Epic > issue > Sub-task tree of a project. Issues without an Epic are
    capped (with their total) and searchable, since legacy projects can hold
    thousands of them."""
    done = done_status_values(db, board) if board is not None else set()
    epics = list(db.scalars(
        select(Bug).where(Bug.project_id == project_id, Bug.item_type == "Epic").order_by(Bug.rank.is_(None), Bug.rank, Bug.id)
    ).all())
    epic_ids = [e.id for e in epics]
    needle = f"%{q.strip().lower()}%" if q.strip() else None

    def scoped(stmt):
        if needle:
            stmt = stmt.where(func.lower(Bug.title).like(needle) | func.lower(func.coalesce(Bug.display_id, "")).like(needle))
        if not include_done and done:
            stmt = stmt.where(Bug.status.not_in(done))
        return stmt

    in_epics = list(db.scalars(scoped(
        _issues_query().where(Bug.epic_id.in_(epic_ids), Bug.item_type.in_(STANDARD_TYPES))
    ).order_by(Bug.rank, Bug.id)).all()) if epic_ids else []
    unparented_filter = (
        Bug.project_id == project_id, Bug.epic_id.is_(None), Bug.item_type.in_(STANDARD_TYPES),
    )
    unparented_total = db.scalar(scoped(select(func.count(Bug.id)).where(*unparented_filter))) or 0
    unparented = list(db.scalars(scoped(
        _issues_query().where(*unparented_filter)
    ).order_by(Bug.rank.is_(None), Bug.rank, Bug.id).limit(HIERARCHY_UNPARENTED_LIMIT)).all())
    issues = in_epics + unparented
    parent_ids = [i.id for i in issues]
    subtasks = []
    for n in range(0, len(parent_ids), 500):
        subtasks.extend(db.scalars(
            _issues_query().where(Bug.parent_id.in_(parent_ids[n:n + 500]), Bug.item_type == SUBTASK)
            .order_by(Bug.id)
        ).all())
    payloads = issue_payloads(db, issues + subtasks, viewer, done=done)
    by_id = {p["id"]: p for p in payloads}
    sprints = list(db.scalars(
        select(Sprint).where(Sprint.board_id == board.id).order_by(Sprint.sequence_number, Sprint.id)
    ).all()) if board is not None else []
    return {
        "project_id": project_id,
        "epics": epic_rows(db, epics),
        "issues": [by_id[i.id] for i in issues],
        "subtasks": [by_id[i.id] for i in subtasks],
        "unparented_total": unparented_total,
        "unparented_limit": HIERARCHY_UNPARENTED_LIMIT,
        "sprints": [sprint_brief(s) for s in sprints],
    }
