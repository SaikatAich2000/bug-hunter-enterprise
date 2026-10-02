"""Sleuth allow-listed tools for the LLM-driven agent
(app/chatbot/llm_tools_agent.py). Every tool function re-checks the calling
user's permissions and project scope exactly as the equivalent REST endpoint
would and mutates data only through the same
domain services/models the REST routes use — never a shortcut, never a raw
write the REST API wouldn't also allow.
"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import notification_service
from app.access import accessible_project_ids, can_access_project
from app.auth import can_edit_bug
from app.models import Board, Bug, Comment, Project, User
from app.schemas import (
    ALLOWED_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    LEGACY_ITEM_TYPES,
    normalize_choice,
    rich_text_to_plain,
    sanitize_html,
    statuses_for_type,
)


class ToolError(ValueError):
    """Raised for any tool failure (not found, permission, validation). The
    agent surfaces the message to the user; it never crashes the chat."""


_SPRINT_ID_REQUIRED = "sprint_id is required"


# --- Shared resolution helpers ---

def _resolve_project(db: Session, actor: User, name: str) -> Project:
    project = db.scalar(select(Project).where(
        Project.org_id == actor.org_id, func.lower(Project.name) == name.strip().lower(),
    ))
    if project is None:
        raise ToolError(f"No project named '{name}'")
    if not can_access_project(accessible_project_ids(db, actor), project.id):
        raise ToolError(f"You don't have access to project '{name}'")
    return project


def _resolve_user_by_name(db: Session, actor: User, name: str) -> User:
    name_norm = name.strip().lower()
    in_org = User.org_id == actor.org_id
    user = db.scalar(select(User).where(in_org, func.lower(User.name) == name_norm))
    if user is None:
        user = db.scalar(select(User).where(in_org, func.lower(User.email).like(f"{name_norm}@%")))
    if user is None:
        raise ToolError(f"No user found matching '{name}'")
    return user


def _get_item(db: Session, actor: User, item_id: int) -> Bug:
    item = db.get(Bug, item_id)
    if item is None or not can_access_project(accessible_project_ids(db, actor), item.project_id):
        raise ToolError(f"Work item #{item_id} not found")
    return item


def _limit_arg(args: dict, default: int = 20, cap: int = 50) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), cap))
    except (TypeError, ValueError):
        return default


def _item_brief(item: Bug) -> dict[str, Any]:
    return {
        "id": item.id, "title": item.title, "item_type": item.item_type,
        "status": item.status, "priority": item.priority,
        "project": item.project.name if item.project else None,
        "assignees": [a.name for a in item.assignees],
        "epic_id": item.epic_id, "sprint_id": item.sprint_id,
        "story_points": float(item.story_points) if item.story_points is not None else None,
    }


# --- Read tools ---

def search_work_items(db: Session, actor: User, args: dict) -> dict:
    stmt = select(Bug).where(Bug.project_id.in_(accessible_project_ids(db, actor)))
    if args.get("project_name"):
        project = _resolve_project(db, actor, args["project_name"])
        stmt = stmt.where(Bug.project_id == project.id)
    if args.get("status"):
        stmt = stmt.where(Bug.status == args["status"])
    if args.get("item_type"):
        stmt = stmt.where(Bug.item_type == args["item_type"])
    if args.get("query"):
        needle = f"%{args['query'].strip().lower()}%"
        stmt = stmt.where(func.lower(Bug.title).like(needle))
    if args.get("assignee_name"):
        user = _resolve_user_by_name(db, actor, args["assignee_name"])
        stmt = stmt.where(Bug.assignees.any(User.id == user.id))
    limit = _limit_arg(args)
    rows = list(db.scalars(stmt.order_by(Bug.updated_at.desc()).limit(limit)).all())
    return {"items": [_item_brief(r) for r in rows], "count": len(rows)}


def get_work_item(db: Session, actor: User, args: dict) -> dict:
    item_id = args.get("item_id")
    if not item_id:
        raise ToolError("item_id is required")
    return _item_brief(_get_item(db, actor, _int_arg(args, "item_id")))


def get_backlog(db: Session, actor: User, args: dict) -> dict:
    from app.agile import backlog as backlog_svc

    project = _resolve_project(db, actor, args.get("project_name", ""))
    limit = _limit_arg(args)
    items = backlog_svc.list_backlog(db, project.id, limit=limit)
    return {"items": [_item_brief(i) for i in items], "count": len(items)}


def list_sprints_tool(db: Session, actor: User, args: dict) -> dict:
    from app.agile import boards as boards_svc
    from app.agile import sprints as sprints_svc

    project = _resolve_project(db, actor, args.get("project_name", ""))
    board = boards_svc.get_default_board(db, project.id)
    if board is None:
        raise ToolError(f"Agile is not enabled for project '{project.name}'")
    sprints = sprints_svc.list_sprints(db, board.id, args.get("state"))
    return {"sprints": [
        {"id": s.id, "name": s.name, "state": s.state, "goal": s.goal,
         "start_date": s.start_date, "end_date": s.end_date}
        for s in sprints
    ]}


def get_sprint_planning_tool(db: Session, actor: User, args: dict) -> dict:
    from app.agile import boards as boards_svc
    from app.agile import planning as planning_svc
    from app.models import Sprint

    sprint_id = args.get("sprint_id")
    if not sprint_id:
        raise ToolError(_SPRINT_ID_REQUIRED)
    sprint = db.get(Sprint, _int_arg(args, "sprint_id"))
    if sprint is None or not can_access_project(accessible_project_ids(db, actor), sprint.project_id):
        raise ToolError(f"Sprint #{sprint_id} not found")
    board = boards_svc.get_board_or_none(db, sprint.board_id)
    if board is None:
        raise ToolError("Sprint Board not found")
    summary = planning_svc.build_planning_summary(db, sprint, board)
    return {
        "item_count": summary["item_count"], "total_estimate": summary["total_estimate"],
        "ready_to_start": summary["ready_to_start"],
        "readiness": [
            {"key": c["key"], "passed": c["passed"], "message": c["message"]}
            for c in summary["readiness"]
        ],
    }


def get_epic_progress_tool(db: Session, actor: User, args: dict) -> dict:
    from app.agile import taxonomy as taxonomy_svc

    epic_id = args.get("epic_id")
    if not epic_id:
        raise ToolError("epic_id is required")
    epic = _get_item(db, actor, _int_arg(args, "epic_id"))
    if epic.item_type != "Epic":
        raise ToolError(f"#{epic_id} is not an Epic")
    return {"epic_id": epic.id, "title": epic.title, "progress": taxonomy_svc.epic_progress(db, epic)}


# --- Write tools (never executed directly by the agent loop; always staged
# for explicit user confirmation first — see llm_tools_agent.py) ---

def _choice(value: str, allowed: list[str], label: str) -> str:
    try:
        return normalize_choice(value, allowed, label)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _int_arg(args: dict, name: str) -> int:
    try:
        return int(args.get(name))
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number") from None


def create_work_item(db: Session, actor: User, args: dict) -> dict:
    from app.agile.hierarchy import HierarchyError, assign_hierarchy

    title = (args.get("title") or "").strip()
    if len(title) < 3:
        raise ToolError("title must be at least 3 characters")
    if len(title) > 200:
        raise ToolError("title must be at most 200 characters")
    project = _resolve_project(db, actor, args.get("project_name", ""))
    item_type = _choice(args.get("item_type") or "Bug", ALLOWED_ITEM_TYPES, "item_type")
    priority = _choice(args.get("priority") or "Medium", ALLOWED_PRIORITIES, "priority")
    # Plain-text descriptions, same as the REST create validators.
    description = rich_text_to_plain(sanitize_html((args.get("description") or "").strip()))

    if item_type in LEGACY_ITEM_TYPES:
        if not can_edit_bug(actor, actor.id, [], item_type=item_type):
            raise ToolError(f"You don't have permission to create a {item_type}")
    elif not project.agile_enabled:
        # Same rule as POST /api/agile/work-items.
        raise ToolError(f"Agile is not enabled for project '{project.name}', so a {item_type} can't be created")
    parent_id = _int_arg(args, "parent_id") if args.get("parent_id") else None

    item = Bug(
        project_id=project.id, reporter_id=actor.id, title=title,
        description=description, item_type=item_type, status="New", priority=priority,
    )
    db.add(item)
    db.flush()
    if item_type not in LEGACY_ITEM_TYPES:
        try:
            assign_hierarchy(db, item, parent_id, None)
        except HierarchyError as exc:
            raise ToolError(str(exc)) from exc
    # Ranking (bottom of the backlog for standard issues, the Epic list for
    # Epics, none for Sub-tasks) is applied at flush by app/agile/integrity.py,
    # exactly as for the REST create paths.
    db.flush()
    return {"id": item.id, "title": item.title, "item_type": item.item_type}


def transition_work_item_tool(db: Session, actor: User, args: dict) -> dict:
    from app.agile.workflow import record_status_change

    to_status = (args.get("to_status") or "").strip()
    if not args.get("item_id") or not to_status:
        raise ToolError("item_id and to_status are required")
    item_id = _int_arg(args, "item_id")
    item = _get_item(db, actor, item_id)
    if not can_edit_bug(actor, item.reporter_id, [a.id for a in item.assignees], item_type=item.item_type):
        raise ToolError(f"You don't have permission to edit #{item_id}")
    allowed = statuses_for_type(item.item_type)
    try:
        to_status = normalize_choice(to_status, allowed, "status")
    except ValueError:
        raise ToolError(
            f"'{to_status}' is not valid for {item.item_type}. Allowed: {', '.join(allowed)}"
        ) from None
    old_status = item.status
    item.status = to_status
    record_status_change(db, item, actor, old_status)
    item.version += 1
    db.flush()
    return {"id": item.id, "status": item.status, "previous_status": old_status}


def assign_work_item_tool(db: Session, actor: User, args: dict) -> dict:
    item_id = args.get("item_id")
    assignee_name = (args.get("assignee_name") or "").strip()
    if not item_id or not assignee_name:
        raise ToolError("item_id and assignee_name are required")
    item = _get_item(db, actor, _int_arg(args, "item_id"))
    if not can_edit_bug(actor, item.reporter_id, [a.id for a in item.assignees], item_type=item.item_type):
        raise ToolError(f"You don't have permission to edit #{item_id}")
    user = _resolve_user_by_name(db, actor, assignee_name)
    if not user.is_active:
        raise ToolError(f"{user.name} is not an active user")
    if user in item.assignees:
        return {"id": item.id, "assignees": [a.name for a in item.assignees], "unchanged": True}
    item.assignees.append(user)
    item.version += 1
    notification_service.notify(
        db, [user.id], kind="assigned",
        title=f"Assigned to {(item.item_type or 'item').lower()} #{item.id}",
        body=f"{actor.name} assigned you to “{item.title}”.",
        bug_id=item.id, actor_name=actor.name,
    )
    db.flush()
    return {"id": item.id, "assignees": [a.name for a in item.assignees]}


def add_comment_tool(db: Session, actor: User, args: dict) -> dict:
    item_id = args.get("item_id")
    body = (args.get("body") or "").strip()
    if not item_id or not body:
        raise ToolError("item_id and body are required")
    item = _get_item(db, actor, _int_arg(args, "item_id"))
    if not can_edit_bug(actor, item.reporter_id, [a.id for a in item.assignees], item_type=item.item_type):
        raise ToolError(f"You don't have permission to comment on #{item_id}")
    comment = Comment(bug_id=item.id, author_user_id=actor.id, author_name=actor.name, body=sanitize_html(body))
    db.add(comment)
    notification_service.notify(
        db, [item.reporter_id, *[a.id for a in item.assignees]], kind="comment",
        title=f"New comment on #{item.id}",
        body=f"{actor.name} commented on “{item.title}”.",
        bug_id=item.id, actor_name=actor.name, exclude=actor.id,
    )
    db.flush()
    return {"id": item.id, "comment_id": comment.id}


def start_sprint_tool(db: Session, actor: User, args: dict) -> dict:
    from app.agile import sprints as sprints_svc
    from app.agile.permissions import START_SPRINT, has_permission
    from app.models import Sprint

    sprint_id = args.get("sprint_id")
    if not sprint_id:
        raise ToolError(_SPRINT_ID_REQUIRED)
    sprint = db.get(Sprint, _int_arg(args, "sprint_id"))
    if sprint is None or not can_access_project(accessible_project_ids(db, actor), sprint.project_id):
        raise ToolError(f"Sprint #{sprint_id} not found")
    if not has_permission(actor, START_SPRINT):
        raise ToolError("You don't have permission to start sprints")
    project = db.get(Project, sprint.project_id)
    allow_parallel = bool((project.agile_feature_flags or {}).get("parallel_sprints")) if project else False
    try:
        # The board's timezone decides "today" for the start date.
        sprints_svc.start_sprint(db, sprint, actor, allow_parallel, board=db.get(Board, sprint.board_id))
    except sprints_svc.SprintLifecycleError as exc:
        raise ToolError(str(exc)) from exc
    sprint.version += 1
    db.flush()
    return {"id": sprint.id, "state": sprint.state}


def complete_sprint_tool(db: Session, actor: User, args: dict) -> dict:
    from app.agile import sprints as sprints_svc
    from app.agile.permissions import COMPLETE_SPRINT, has_permission
    from app.models import Sprint

    sprint_id = args.get("sprint_id")
    closing_note = (args.get("closing_note") or "Completed via Sleuth").strip()
    if not sprint_id:
        raise ToolError(_SPRINT_ID_REQUIRED)
    sprint = db.get(Sprint, _int_arg(args, "sprint_id"))
    if sprint is None or not can_access_project(accessible_project_ids(db, actor), sprint.project_id):
        raise ToolError(f"Sprint #{sprint_id} not found")
    if not has_permission(actor, COMPLETE_SPRINT):
        raise ToolError("You don't have permission to complete sprints")
    try:
        sprints_svc.complete_sprint(
            db, sprint, actor, closing_note, {}, "backlog", None, board=db.get(Board, sprint.board_id),
        )
    except sprints_svc.SprintLifecycleError as exc:
        raise ToolError(str(exc)) from exc
    sprint.version += 1
    db.flush()
    return {"id": sprint.id, "state": sprint.state}


# --- Registry ---

READ_TOOL_NAMES = frozenset({
    "search_work_items", "get_work_item", "get_backlog",
    "list_sprints", "get_sprint_planning", "get_epic_progress",
})
WRITE_TOOL_NAMES = frozenset({
    "create_work_item", "transition_work_item", "assign_work_item",
    "add_comment", "start_sprint", "complete_sprint",
})
ALL_TOOL_NAMES = READ_TOOL_NAMES | WRITE_TOOL_NAMES

TOOL_REGISTRY: dict[str, Callable[[Session, User, dict], dict]] = {
    "search_work_items": search_work_items,
    "get_work_item": get_work_item,
    "get_backlog": get_backlog,
    "list_sprints": list_sprints_tool,
    "get_sprint_planning": get_sprint_planning_tool,
    "get_epic_progress": get_epic_progress_tool,
    "create_work_item": create_work_item,
    "transition_work_item": transition_work_item_tool,
    "assign_work_item": assign_work_item_tool,
    "add_comment": add_comment_tool,
    "start_sprint": start_sprint_tool,
    "complete_sprint": complete_sprint_tool,
}

TOOL_SPECS: list[dict] = [
    {"type": "function", "function": {
        "name": "search_work_items",
        "description": "Search work items (bugs/requirements/tasks/epics/stories/sub-tasks) the caller can access.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Text to search in the title"},
            "project_name": {"type": "string"},
            "status": {"type": "string"},
            "item_type": {"type": "string"},
            "assignee_name": {"type": "string"},
            "limit": {"type": "integer"},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_work_item",
        "description": "Get full details of one work item by id.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"},
        }, "required": ["item_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_backlog",
        "description": "List the ranked, unsprinted Sprint Items for a project (requires Agile enabled).",
        "parameters": {"type": "object", "properties": {
            "project_name": {"type": "string"}, "limit": {"type": "integer"},
        }, "required": ["project_name"]},
    }},
    {"type": "function", "function": {
        "name": "list_sprints",
        "description": "List sprints for a project's Sprint Board, optionally filtered by state.",
        "parameters": {"type": "object", "properties": {
            "project_name": {"type": "string"},
            "state": {"type": "string", "enum": ["future", "active", "closed", "cancelled"]},
        }, "required": ["project_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_sprint_planning",
        "description": "Get the planning readiness summary for a sprint.",
        "parameters": {"type": "object", "properties": {
            "sprint_id": {"type": "integer"},
        }, "required": ["sprint_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_epic_progress",
        "description": "Get progress rollup (completed vs total children/estimate) for an Epic.",
        "parameters": {"type": "object", "properties": {
            "epic_id": {"type": "integer"},
        }, "required": ["epic_id"]},
    }},
    {"type": "function", "function": {
        "name": "create_work_item",
        "description": "Create a new work item. A Sub-task needs parent_id (a Story, Requirement, Task or Bug in the same project). Mutating — requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "project_name": {"type": "string"}, "title": {"type": "string"},
            "item_type": {"type": "string"}, "priority": {"type": "string"},
            "description": {"type": "string"}, "parent_id": {"type": "integer"},
        }, "required": ["project_name", "title"]},
    }},
    {"type": "function", "function": {
        "name": "transition_work_item",
        "description": "Change a work item's status. Mutating — requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"}, "to_status": {"type": "string"},
        }, "required": ["item_id", "to_status"]},
    }},
    {"type": "function", "function": {
        "name": "assign_work_item",
        "description": "Assign a user to a work item. Mutating — requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"}, "assignee_name": {"type": "string"},
        }, "required": ["item_id", "assignee_name"]},
    }},
    {"type": "function", "function": {
        "name": "add_comment",
        "description": "Add a comment to a work item. Mutating — requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "integer"}, "body": {"type": "string"},
        }, "required": ["item_id", "body"]},
    }},
    {"type": "function", "function": {
        "name": "start_sprint",
        "description": "Start a future sprint (locks in the commitment baseline). Mutating — requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "sprint_id": {"type": "integer"},
        }, "required": ["sprint_id"]},
    }},
    {"type": "function", "function": {
        "name": "complete_sprint",
        "description": "Complete an active sprint, moving incomplete items to Sprint Items. Mutating — requires user confirmation.",
        "parameters": {"type": "object", "properties": {
            "sprint_id": {"type": "integer"}, "closing_note": {"type": "string"},
        }, "required": ["sprint_id"]},
    }},
]
