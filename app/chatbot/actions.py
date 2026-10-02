"""Sleuth actions — the write-side of the assistant (executor.py is read-only).

Every action re-validates permissions, runs one short transaction, and audits.
All writes are staged behind a Yes/Cancel confirm; destructive ops stay UI-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Literal, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app import notification_service
from app.access import accessible_project_ids, add_user_project, can_access_project
from app.auth import (
    ROLE_ADMIN,
    can_edit_bug,
    can_manage_projects,
)
from app.chatbot.executor import Block, Response
from app.models import PROJECT_ROLE_LEAD, Activity, Bug, Comment, Project, User
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PRIORITIES,
    normalize_choice,
    rich_text_to_plain,
    sanitize_html,
    statuses_for_type,
)

# Fallback label when an assign/unassign plan carries ids but no display names.
_UNKNOWN_USERS = "user(s)"

# Distinct from "action_done" so _apply_bulk counts it as "skipped".
_INTENT_NOOP = "action_noop"

# Only these kinds may fan across bug_ids.
_BULK_KINDS = frozenset({
    "assign", "unassign", "set_status", "set_priority", "set_environment",
})


# --- ActionPlan — a fully-resolved write request awaiting execution ---
@dataclass
class ActionPlan:
    """A concrete change built at parse time and executed on confirm.
    Stores IDs, not ORM objects, so it serializes into memory.store between turns."""
    kind: Literal[
        "assign", "unassign", "set_status", "set_priority",
        "set_environment", "set_due_date", "add_comment",
        "create_bug", "create_project",
    ]
    actor_user_id: int
    bug_id: Optional[int] = None
    # When non-empty, the action applies to every id here. bug_id stays None.
    bug_ids: list[int] = field(default_factory=list)
    target_user_ids: list[int] = field(default_factory=list)
    target_user_names: list[str] = field(default_factory=list)
    # unassign only: drop EVERY assignee, ignoring target_user_ids.
    unassign_all: bool = False
    new_value: Optional[str] = None      # status / priority / env / due_date
    comment_body: Optional[str] = None
    new_title: Optional[str] = None
    new_description: Optional[str] = None
    new_project_id: Optional[int] = None
    new_project_name: Optional[str] = None
    # Shown in the confirm prompt and the post-execution message.
    summary_human: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for memory.store."""
        return {
            "kind": self.kind,
            "actor_user_id": self.actor_user_id,
            "bug_id": self.bug_id,
            "bug_ids": list(self.bug_ids),
            "target_user_ids": list(self.target_user_ids),
            "target_user_names": list(self.target_user_names),
            "unassign_all": self.unassign_all,
            "new_value": self.new_value,
            "comment_body": self.comment_body,
            "new_title": self.new_title,
            "new_description": self.new_description,
            "new_project_id": self.new_project_id,
            "new_project_name": self.new_project_name,
            "summary_human": self.summary_human,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ActionPlan":
        return cls(
            kind=d.get("kind", ""),
            actor_user_id=d.get("actor_user_id", 0),
            bug_id=d.get("bug_id"),
            bug_ids=list(d.get("bug_ids") or []),
            target_user_ids=list(d.get("target_user_ids") or []),
            target_user_names=list(d.get("target_user_names") or []),
            unassign_all=bool(d.get("unassign_all", False)),
            new_value=d.get("new_value"),
            comment_body=d.get("comment_body"),
            new_title=d.get("new_title"),
            new_description=d.get("new_description"),
            new_project_id=d.get("new_project_id"),
            new_project_name=d.get("new_project_name"),
            summary_human=d.get("summary_human", ""),
        )


# --- Permission helpers ---
def _check_can_edit_bug(actor: User, bug: Bug) -> Optional[str]:
    """Return None if the actor may edit this item, otherwise an error string.
    Passes the item's real type so per-type rules match PUT /api/bugs/{id}."""
    itype = getattr(bug, "item_type", None) or "Bug"
    if not can_edit_bug(actor,
                        bug.reporter_id,
                        [a.id for a in bug.assignees],
                        item_type=itype):
        return f"You don't have permission to edit that {itype.lower()}"
    return None


def _check_can_create_project(actor: User) -> Optional[str]:
    if not can_manage_projects(actor):
        return ("Only admins or managers can create projects. "
                "Ask one of them to do it for you")
    return None


def _check_can_create_bug(actor: User) -> Optional[str]:
    # Any authenticated user can file a bug (mirrors POST /api/bugs).
    if not actor.is_active:
        return "Your account is inactive"
    return None


# --- Audit helper ---
def _audit(db: Session, bug_id: Optional[int], actor: User,
           action: str, detail: str,
           entity_type: str = "bug", entity_id: Optional[int] = None) -> None:
    db.add(Activity(
        org_id=actor.org_id,
        bug_id=bug_id,
        entity_type=entity_type,
        entity_id=entity_id if entity_id is not None else bug_id,
        actor_user_id=actor.id,
        actor_name=actor.name,
        action=action,
        detail=detail,
    ))


# --- Block helpers ---
def _confirm_response(plan: ActionPlan, prompt: str) -> Response:
    """Confirm response: frontend renders Yes/No buttons; Yes dispatches the plan."""
    return Response(
        blocks=[
            Block("text", {"text": prompt}),
            Block("confirm", {
                "summary": plan.summary_human,
                "yes_label": "Yes, do it",
                "no_label": "Cancel",
            }),
        ],
        summary=f"Awaiting confirmation: {plan.summary_human}",
        intent="confirm_action",
    )


def _success_response(message: str, intent: str = "action_done",
                      bug_id: Optional[int] = None) -> Response:
    blocks: list[Block] = [Block("text", {"text": message})]
    if bug_id is not None:
        blocks.append(Block("suggestions", {
            "items": [
                {"label": f"Show bug #{bug_id}",
                 "send":  f"bug #{bug_id}"},
                {"label": f"Comment on #{bug_id}",
                 "send":  f"comment on #{bug_id}: "},
                {"label": "Recent activity",
                 "send":  "recent activity"},
            ]
        }))
    return Response(blocks=blocks, summary=message[:80], intent=intent)


def _error_response(message: str, intent: str = "action_error") -> Response:
    return Response(
        blocks=[Block("text", {"text": message})],
        summary=message[:80],
        intent=intent,
    )


# --- Plan execution — the actual writes ---
def _load_bug(db: Session, actor: User, bug_id: int) -> Optional[Bug]:
    """The item, or None when it is missing or outside the actor's organization/projects."""
    bug = db.scalar(
        select(Bug)
        .options(selectinload(Bug.project),
                 selectinload(Bug.reporter),
                 selectinload(Bug.assignees))
        .where(Bug.id == bug_id)
    )
    if bug is None or not can_access_project(accessible_project_ids(db, actor), bug.project_id):
        return None
    return bug


def _itype_of(bug: Bug) -> str:
    return (getattr(bug, "item_type", None) or "bug").lower()


def _notify_chat_op(db: Session, bug: Bug, actor: User, *, kind: str,
                    title: str, body: str, extra_user_ids: Iterable[int] = ()) -> None:
    """In-app notification to stakeholders (reporter + assignees + actor + extras).
    In-app only — no immediate email/push is wired through the chat path."""
    recipients: set[int] = {actor.id}
    if bug.reporter_id is not None:
        recipients.add(bug.reporter_id)
    recipients.update(a.id for a in bug.assignees)
    recipients.update(extra_user_ids)
    notification_service.notify(
        db, list(recipients), kind=kind, title=title, body=body,
        bug_id=bug.id, actor_name=actor.name,
    )


def _resolve_targets(
    db: Session, actor: User, ids: list[int],
) -> tuple[list[User], Optional[str]]:
    """Resolve user ids to active User objects of the actor's organization, or return an
    error string. Unknown ids are a hard error; deactivated users can't be assignees."""
    if not ids:
        return [], "Couldn't find the user(s) to assign"
    users = list(db.scalars(
        select(User).where(User.id.in_(ids), User.org_id == actor.org_id)
    ).all())
    found = {u.id for u in users}
    missing = [i for i in ids if i not in found]
    if missing:
        return [], f"Couldn't find user(s) with id(s): {', '.join(map(str, missing))}"
    inactive = [u.name for u in users if not u.is_active]
    if inactive:
        return [], f"Cannot assign a deactivated user: {', '.join(inactive)}"
    return users, None


def _apply_assign(db: Session, plan: ActionPlan, actor: User,
                  notify: bool = True, commit: bool = True) -> Response:
    bug = _load_bug(db, actor, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    err = _check_can_edit_bug(actor, bug)
    if err:
        return _error_response(err)
    targets, terr = _resolve_targets(db, actor, plan.target_user_ids)
    if terr:
        return _error_response(terr)

    before = sorted(a.name for a in bug.assignees)
    already = {a.id for a in bug.assignees}
    added = [t for t in targets if t.id not in already]
    if not added:
        # No-op — signal "skipped" to the bulk caller; no audit/version bump.
        return _success_response(
            f"No change — those user(s) are already assigned to bug #{bug.id}",
            intent=_INTENT_NOOP, bug_id=bug.id,
        )
    bug.assignees = list(bug.assignees) + added
    # Assignee change touches no Bug column — bump version for optimistic concurrency.
    bug.version = (bug.version or 1) + 1
    after = sorted(a.name for a in bug.assignees)
    _audit(db, bug.id, actor, "assignees_changed",
           f"#{bug.id} '{bug.title}' — assignees: {before} -> {after}")
    # Skip per-item notification on bulk; the bulk caller emits one aggregate.
    if notify:
        names = ", ".join(t.name for t in added)
        _notify_chat_op(
            db, bug, actor, kind="assigned",
            title=f"Assigned to {_itype_of(bug)} #{bug.id}",
            body=f"{actor.name} assigned {names} to “{bug.title}”.",
            extra_user_ids=[t.id for t in added])
    if commit:
        db.commit()
    names = ", ".join(t.name for t in added)
    return _success_response(
        f"Done — assigned **{names}** to bug #{bug.id} (*{bug.title[:60]}*)",
        bug_id=bug.id,
    )


def _apply_unassign(db: Session, plan: ActionPlan, actor: User,
                    notify: bool = True, commit: bool = True) -> Response:
    bug = _load_bug(db, actor, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    err = _check_can_edit_bug(actor, bug)
    if err:
        return _error_response(err)
    # unassign_all clears every assignee; otherwise drop just the named targets.
    drop_ids = ({a.id for a in bug.assignees} if plan.unassign_all
                else set(plan.target_user_ids))
    before = sorted(a.name for a in bug.assignees)
    bug.assignees = [a for a in bug.assignees if a.id not in drop_ids]
    after = sorted(a.name for a in bug.assignees)
    if before == after:
        # Nobody to drop — signal "skipped" to the bulk caller.
        msg = ("Nothing changed — that bug had no assignees" if plan.unassign_all
               else "Nothing changed — those users weren't assigned to this bug")
        return _success_response(msg, intent=_INTENT_NOOP, bug_id=bug.id)
    bug.version = (bug.version or 1) + 1
    _audit(db, bug.id, actor, "assignees_changed",
           f"#{bug.id} '{bug.title}' — assignees: {before} -> {after}")
    if notify:
        names = ("all assignees" if plan.unassign_all
                 else ", ".join(plan.target_user_names) or _UNKNOWN_USERS)
        _notify_chat_op(
            db, bug, actor, kind="updated",
            title=f"Unassigned from {_itype_of(bug)} #{bug.id}",
            body=f"{actor.name} unassigned {names} from “{bug.title}”.",
            extra_user_ids=drop_ids)
    if commit:
        db.commit()
    names = ", ".join(plan.target_user_names) or _UNKNOWN_USERS
    return _success_response(
        f"Done — removed **{names}** from bug #{bug.id}",
        bug_id=bug.id,
    )


def _validate_field_value(bug: Bug, field_name: str, value: Any) -> Optional[str]:
    """Return an error string if value is invalid for this field, else None.
    Mirrors REST enum/date validation."""
    if field_name == "status":
        if value not in statuses_for_type(_itype_of(bug)):
            return f"“{value}” isn't a valid status for a {_itype_of(bug).lower()}."
        return None
    if field_name == "priority" and value not in ALLOWED_PRIORITIES:
        return f"“{value}” isn't a valid priority."
    if field_name == "environment" and value not in ALLOWED_ENVIRONMENTS:
        return f"“{value}” isn't a valid environment."
    if field_name == "due_date" and value:
        try:
            date.fromisoformat(str(value))
        except ValueError:
            return f"“{value}” isn't a valid date — use YYYY-MM-DD."
    return None


def _apply_set_field(db: Session, plan: ActionPlan, actor: User,
                     field_name: str, label: str, notify: bool = True,
                     commit: bool = True) -> Response:
    bug = _load_bug(db, actor, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    err = _check_can_edit_bug(actor, bug)
    if err:
        return _error_response(err)
    old = getattr(bug, field_name)
    new = plan.new_value
    invalid = _validate_field_value(bug, field_name, new)
    if invalid:
        return _error_response(invalid)
    if old == new:
        # No-op — skip the audit row and signal "skipped" to the bulk caller.
        return _success_response(
            f"Bug #{bug.id} {label} is already **{old}** — nothing to do",
            intent=_INTENT_NOOP, bug_id=bug.id,
        )
    setattr(bug, field_name, new)
    bug.version = (bug.version or 1) + 1
    if field_name == "status":
        # Same sprint-history / resolved_at bookkeeping as the REST paths.
        from app.agile.workflow import record_status_change

        record_status_change(db, bug, actor, old)
    # REST audit verb/format so resolution reports pick up chat-driven changes.
    _audit(db, bug.id, actor, f"{field_name}_changed",
           f"#{bug.id} '{bug.title}' — {field_name}: {old!r} -> {new!r}")
    if notify:
        _notify_chat_op(
            db, bug, actor, kind="updated",
            title=f"{_itype_of(bug).capitalize()} #{bug.id} updated",
            body=f"{actor.name} changed {label} to {new}.")
    if commit:
        db.commit()
    return _success_response(
        f"Done — bug #{bug.id} {label} changed from **{old}** to **{new}**",
        bug_id=bug.id,
    )


def _apply_add_comment(db: Session, plan: ActionPlan, actor: User,
                       notify: bool = True, commit: bool = True) -> Response:
    bug = _load_bug(db, actor, plan.bug_id) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found")
    raw = (plan.comment_body or "").strip()
    if not raw:
        return _error_response(
            "I don't have any comment text to post. Try: "
            "*comment on #5: this is fixed in commit abc*"
        )
    if len(raw) > 4000:
        return _error_response("Comment too long — keep it under 4000 chars")
    # Comments render as HTML, so sanitize before storing (same as CommentIn).
    body = sanitize_html(raw)
    c = Comment(bug_id=bug.id, author_user_id=actor.id,
                author_name=actor.name, body=body)
    db.add(c)
    db.flush()
    _audit(db, bug.id, actor, "comment_added",
           f"Comment by {actor.name}: {body[:80]}")
    if notify:
        snippet = body if len(body) < 100 else body[:97] + "..."
        _notify_chat_op(
            db, bug, actor, kind="comment",
            title=f"New comment on {_itype_of(bug)} #{bug.id}",
            body=f"{actor.name}: {snippet}")
    if commit:
        db.commit()
    preview = body if len(body) < 120 else body[:117] + "..."
    return _success_response(
        f"Comment posted on bug #{bug.id}: \"{preview}\"",
        bug_id=bug.id,
    )


def _apply_create_bug(db: Session, plan: ActionPlan, actor: User) -> Response:
    err = _check_can_create_bug(actor)
    if err:
        return _error_response(err)
    title = (plan.new_title or "").strip()
    if not title:
        return _error_response(
            "I need a title to create a bug. Try: "
            "*create a bug titled \"Login broken\" in project Apollo*"
        )
    if len(title) > 200:
        return _error_response("Title too long — keep it under 200 chars")
    project_id = plan.new_project_id
    accessible = accessible_project_ids(db, actor)
    if project_id is None:
        # Fall back to the actor's first project, matching the SPA's "General" default.
        if not accessible:
            return _error_response(
                "There are no projects yet. Create one first"
            )
        project_id = min(accessible)
    elif project_id not in accessible:
        return _error_response("That project doesn't exist anymore")
    # Same validator as the REST create payload — no out-of-enum priority.
    try:
        priority = normalize_choice(plan.new_value or "Medium", ALLOWED_PRIORITIES, "priority")
    except ValueError:
        return _error_response(
            f"'{plan.new_value}' isn't a valid priority. Allowed: {', '.join(ALLOWED_PRIORITIES)}"
        )
    assignees: list[User] = []
    if plan.target_user_ids:
        assignees, terr = _resolve_targets(db, actor, plan.target_user_ids)
        if terr:
            return _error_response(terr)
    bug = Bug(
        title=title,
        # Plain-text description, same as the REST BugCreate validator.
        description=rich_text_to_plain(sanitize_html((plan.new_description or "").strip())),
        status="New",
        priority=priority,
        environment="DEV",
        project_id=project_id,
        reporter_id=actor.id,
    )
    # Ranked at the bottom of the project's backlog at flush, like a
    # REST-created item (app/agile/integrity.py).
    db.add(bug)
    db.flush()
    if assignees:
        bug.assignees = assignees
    _audit(db, bug.id, actor, "bug_created",
           f"Created bug #{bug.id}: {title[:80]}")
    _notify_chat_op(
        db, bug, actor, kind="created",
        title=f"New {_itype_of(bug)} #{bug.id} created",
        body=f"{actor.name} created “{title[:80]}”.")
    db.commit()
    return _success_response(
        f"Created bug #{bug.id} — *{title[:80]}* (you're the reporter)",
        bug_id=bug.id,
    )


def _apply_create_project(db: Session, plan: ActionPlan, actor: User) -> Response:
    err = _check_can_create_project(actor)
    if err:
        return _error_response(err)
    name = (plan.new_project_name or "").strip()
    if not name:
        return _error_response("I need a name to create a project")
    if len(name) > 120:
        return _error_response("Project name too long — keep it under 120 chars")
    # Case-insensitive uniqueness check.
    existing = db.scalar(
        select(Project).where(Project.org_id == actor.org_id, Project.name.ilike(name))
    )
    if existing is not None:
        return _error_response(
            f"There's already a project called **{existing.name}**"
        )
    proj = Project(org_id=actor.org_id, name=name, description=(plan.new_description or ""))
    db.add(proj)
    db.flush()
    if actor.role != ROLE_ADMIN:
        add_user_project(db, actor.id, proj.id, PROJECT_ROLE_LEAD)
    _audit(db, None, actor, "project_created",
           f"Created project '{name}'",
           entity_type="project", entity_id=proj.id)
    # Projects have no stakeholders — notify the actor so it surfaces in the bell.
    notification_service.notify(
        db, [actor.id], kind="updated",
        title="Project created",
        body=f"{actor.name} created project “{proj.name}”.",
        actor_name=actor.name)
    db.commit()
    return Response(
        blocks=[Block("text", {"text":
            f"Project **{proj.name}** created — you can now file bugs against it"})],
        summary=f"Created project {proj.name}",
        intent="action_done",
    )


# --- Public dispatch ---
def sleuth_write_denied(actor: User) -> Optional[str]:
    """Return a refusal string if the actor may not write via Sleuth, else None.
    Chat writes are admin-only; everyone else is read-only here."""
    if actor.role == ROLE_ADMIN:
        return None
    return (
        "I can look things up for you — search bugs, run reports, show details, "
        "counts and activity — but making changes through me is limited to "
        "admins. Please use the app (where your role allows) or ask an admin."
    )


def _dispatch_single(plan: ActionPlan, db: Session, actor: User,
                     notify: bool = True, commit: bool = True) -> Response:
    """Apply one single-bug (or no-bug) action and return its Response.
    notify/commit=False let the bulk caller aggregate notifications and commit once."""
    if plan.kind == "assign":
        return _apply_assign(db, plan, actor, notify=notify, commit=commit)
    if plan.kind == "unassign":
        return _apply_unassign(db, plan, actor, notify=notify, commit=commit)
    if plan.kind == "set_status":
        return _apply_set_field(db, plan, actor, "status", "status", notify=notify, commit=commit)
    if plan.kind == "set_priority":
        return _apply_set_field(db, plan, actor, "priority", "priority", notify=notify, commit=commit)
    if plan.kind == "set_environment":
        return _apply_set_field(db, plan, actor, "environment", "environment", notify=notify, commit=commit)
    if plan.kind == "set_due_date":
        return _apply_set_field(db, plan, actor, "due_date", "due date", notify=notify, commit=commit)
    if plan.kind == "add_comment":
        return _apply_add_comment(db, plan, actor, notify=notify, commit=commit)
    if plan.kind == "create_bug":
        return _apply_create_bug(db, plan, actor)
    if plan.kind == "create_project":
        return _apply_create_project(db, plan, actor)
    return _error_response(f"Unknown action: {plan.kind}")


def _notify_bulk(db: Session, plan: ActionPlan, actor: User, updated: int) -> None:
    """One aggregate notification for a bulk operation instead of N per item."""
    if plan.kind in ("assign", "unassign"):
        names = ("all assignees" if plan.unassign_all
                 else ", ".join(plan.target_user_names) or _UNKNOWN_USERS)
        verb = "assigned" if plan.kind == "assign" else "unassigned"
        prep = "to" if plan.kind == "assign" else "from"
        recipients = [actor.id, *plan.target_user_ids]
        title = f"{updated} items — {verb}"
        body = f"{actor.name} {verb} {names} {prep} {updated} items."
        kind = "assigned" if plan.kind == "assign" else "updated"
    else:  # set_status / set_priority
        label = "status" if plan.kind == "set_status" else "priority"
        recipients = [actor.id]
        title = f"{updated} items updated"
        body = f"{actor.name} set {label} to {plan.new_value} on {updated} items."
        kind = "updated"
    notification_service.notify(
        db, recipients, kind=kind, title=title, body=body, actor_name=actor.name)


def _apply_bulk(plan: ActionPlan, db: Session, actor: User) -> Response:
    """Apply a plan's action to every id in plan.bug_ids in one transaction.
    Single commit at the end so a mid-batch failure rolls everything back."""
    updated = skipped = 0
    for bid in plan.bug_ids:
        # Copy all plan fields (bug_ids cleared) so the per-item plan is single-target.
        one = ActionPlan(
            kind=plan.kind, actor_user_id=actor.id, bug_id=bid,
            target_user_ids=list(plan.target_user_ids),
            target_user_names=list(plan.target_user_names),
            unassign_all=plan.unassign_all,
            new_value=plan.new_value, comment_body=plan.comment_body,
            new_title=plan.new_title, new_description=plan.new_description,
            new_project_id=plan.new_project_id, new_project_name=plan.new_project_name,
            summary_human=plan.summary_human,
        )
        resp = _dispatch_single(one, db, actor, notify=False, commit=False)
        if resp.intent == "action_done":
            updated += 1
        else:
            skipped += 1
    if updated:
        _notify_bulk(db, plan, actor, updated)
        db.commit()
    else:
        db.rollback()
    tail = f", {skipped} skipped" if skipped else ""
    return _success_response(
        f"Done — {plan.summary_human}: **{updated}** updated{tail}.",
        intent="action_done",
    )


def execute_plan(plan: ActionPlan, db: Session, actor: User) -> Response:
    """Run a confirmed plan. Caller ensures ownership; actor.id is re-checked
    here as a safety net."""
    if plan.actor_user_id != actor.id:
        return _error_response("That action was staged for a different user")
    # Re-check write policy at execute time (TOCTOU) — actor may have been demoted.
    denied = sleuth_write_denied(actor)
    if denied is not None:
        return _error_response(denied)
    try:
        # Fan across bug_ids only for supported bulk kinds.
        if plan.bug_ids and plan.kind in _BULK_KINDS:
            return _apply_bulk(plan, db, actor)
        return _dispatch_single(plan, db, actor)
    except (SQLAlchemyError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Best-effort rollback so a partial change never sticks.
        try:
            db.rollback()
        except SQLAlchemyError:
            pass
        return _error_response(f"Action failed: {exc}")


# --- Confirmation-prompt builder ---
def stage_with_confirm(plan: ActionPlan) -> Response:
    """Return a confirm Response for a plan the caller already staged in memory.store."""
    prompt = (
        f"Just to confirm: **{plan.summary_human}**\n\n"
        f"Reply **yes** (or click below) to proceed, **no** to cancel"
    )
    return _confirm_response(plan, prompt)


__all__ = [
    "ActionPlan",
    "execute_plan",
    "stage_with_confirm",
]
