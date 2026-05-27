"""Sleuth actions — the write-side of the assistant.

Where executor.py is read-only (queries, exports, summaries), this module
performs MUTATIONS the user requested in natural language. Every action:

  1. Re-validates the actor's permission against the same rules used by
     the REST API. The chat path is not a back door.
  2. Applies the change in a single short transaction.
  3. Writes an Activity row so the audit log knows who did what — exactly
     like the REST routes do.
  4. Returns a Response of blocks the router can serialise back to the
     user.

The supported ActionKind list is deliberately conservative. We ship the
operations that map cleanly to single-bug edits and comments. Anything
destructive (delete bug, delete user, password reset, role change) is NOT
exposed via chat — those stay UI-only for the foreseeable future, where
the user clicks through an explicit confirm dialog.

Confirmation flow:
  - Risky writes (status change to Closed, assignee changes, due-date
    changes) are STAGED, not executed, on the first turn. The handler
    returns a Response containing a "confirm" block. The frontend renders
    Yes/No buttons. On Yes, the same plan is executed via a
    follow-up message ("yes" / "confirm") which the router resolves by
    calling memory.take_pending() and dispatching to apply_pending().
  - Safe writes (add comment, set priority on a low-priority bug) can be
    configured to skip the confirm. For v1 we keep them all confirmable —
    accidental writes from a misparse are far more annoying than one
    extra click.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    can_access_project, can_create_project, can_edit_bug, can_manage_users,
)
from app.models import Activity, Bug, Comment, Project, User

from app.chatbot.executor import Block, Response


# ---------------------------------------------------------------------------
# ActionPlan — a fully-resolved write request awaiting execution
# ---------------------------------------------------------------------------
@dataclass
class ActionPlan:
    """A concrete change to make. Built once during parse, executed once
    on confirm. Storing IDs (not objects) means we can serialise it into
    memory.store between turns without holding ORM instances across a
    session boundary."""
    kind: str  # "assign", "unassign", "set_status", "set_priority",
               # "set_environment", "set_due_date", "add_comment",
               # "create_bug", "create_project"
    actor_user_id: int
    bug_id: Optional[int] = None
    target_user_ids: list[int] = field(default_factory=list)
    target_user_names: list[str] = field(default_factory=list)
    new_value: Optional[str] = None      # status / priority / env / due_date
    comment_body: Optional[str] = None
    new_title: Optional[str] = None
    new_description: Optional[str] = None
    new_project_id: Optional[int] = None
    new_project_name: Optional[str] = None
    # Human-readable summary, shown in the confirm prompt and the
    # post-execution success message.
    summary_human: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for memory.store."""
        return {
            "kind": self.kind,
            "actor_user_id": self.actor_user_id,
            "bug_id": self.bug_id,
            "target_user_ids": list(self.target_user_ids),
            "target_user_names": list(self.target_user_names),
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
            target_user_ids=list(d.get("target_user_ids") or []),
            target_user_names=list(d.get("target_user_names") or []),
            new_value=d.get("new_value"),
            comment_body=d.get("comment_body"),
            new_title=d.get("new_title"),
            new_description=d.get("new_description"),
            new_project_id=d.get("new_project_id"),
            new_project_name=d.get("new_project_name"),
            summary_human=d.get("summary_human", ""),
        )


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------
def _check_can_edit_bug(db: Session, actor: User, bug: Bug) -> Optional[str]:
    """Returns None if OK, otherwise an error string. Cross-org and
    project-membership both enforced — chatbot is not a back door."""
    if bug.project is None or bug.project.org_id != actor.org_id:
        return "Bug not found"
    if not can_edit_bug(db, actor, bug.project):
        return "You don't have permission to edit that bug"
    return None


def _check_can_create_project(actor: User) -> Optional[str]:
    if not can_create_project(actor):
        return ("Only admins or managers can create projects. "
                "Ask one of them to do it for you")
    return None


def _check_can_create_bug(actor: User) -> Optional[str]:
    # Per current bug routes: any authenticated user can file a bug.
    # This stays consistent with the REST endpoint POST /api/bugs.
    if not actor.is_active:
        return "Your account is inactive"
    return None


# ---------------------------------------------------------------------------
# Audit helper — mirrors routes/bugs.py::_log
# ---------------------------------------------------------------------------
def _audit(db: Session, bug_id: Optional[int], actor: User,
           action: str, detail: str,
           entity_type: str = "bug", entity_id: Optional[int] = None) -> None:
    # The Activity table requires org_id post-multi-tenant refactor.
    # The actor's org_id is always correct here because every action
    # path resolves the target (bug/project/user) within actor.org_id.
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


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------
def _confirm_response(plan: ActionPlan, prompt: str) -> Response:
    """Build a 'please confirm' response. The frontend renders the
    payload's `prompt` plus Yes / No buttons. On Yes, the user sends
    the literal word 'yes' (or 'confirm') as the next message, which
    the router resolves by calling apply_pending()."""
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


# ---------------------------------------------------------------------------
# Plan execution — the actual writes
# ---------------------------------------------------------------------------
def _load_bug(db: Session, bug_id: int, actor: User) -> Optional[Bug]:
    """Load a bug, but only if it lives in the actor's org. Returns None
    for both 'does not exist' and 'belongs to another org' so we don't
    leak existence across tenants via timing or error text."""
    bug = db.scalar(
        select(Bug)
        .options(selectinload(Bug.project),
                 selectinload(Bug.reporter),
                 selectinload(Bug.assignees))
        .where(Bug.id == bug_id)
    )
    if bug is None or bug.project is None or bug.project.org_id != actor.org_id:
        return None
    return bug


def _apply_assign(db: Session, plan: ActionPlan, actor: User) -> Response:
    bug = _load_bug(db, plan.bug_id, actor) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found.")
    err = _check_can_edit_bug(db, actor, bug)
    if err:
        return _error_response(err)
    # Tenant scope: target users must belong to the same org. Without
    # this guard, a malicious operator who learned a foreign user_id
    # could quietly assign work into another tenant.
    targets = list(db.scalars(
        select(User).where(
            User.id.in_(plan.target_user_ids),
            User.org_id == actor.org_id,
        )
    ).all()) if plan.target_user_ids else []
    if not targets:
        return _error_response("Couldn't find the user(s) to assign")

    before = sorted(a.name for a in bug.assignees)
    new_set = list(bug.assignees) + [t for t in targets
                                     if t.id not in {a.id for a in bug.assignees}]
    bug.assignees = new_set
    after = sorted(a.name for a in bug.assignees)
    detail = f"Assignees: {before} -> {after}"
    _audit(db, bug.id, actor, "bug_update", detail)
    db.commit()
    names = ", ".join(t.name for t in targets)
    return _success_response(
        f"Done — assigned **{names}** to bug #{bug.id} (*{bug.title[:60]}*).",
        bug_id=bug.id,
    )


def _apply_unassign(db: Session, plan: ActionPlan, actor: User) -> Response:
    bug = _load_bug(db, plan.bug_id, actor) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found.")
    err = _check_can_edit_bug(db, actor, bug)
    if err:
        return _error_response(err)
    drop_ids = set(plan.target_user_ids)
    before = sorted(a.name for a in bug.assignees)
    bug.assignees = [a for a in bug.assignees if a.id not in drop_ids]
    after = sorted(a.name for a in bug.assignees)
    if before == after:
        return _error_response(
            "Nothing changed — those users weren't assigned to this bug."
        )
    detail = f"Assignees: {before} -> {after}"
    _audit(db, bug.id, actor, "bug_update", detail)
    db.commit()
    names = ", ".join(plan.target_user_names) or "user(s)"
    return _success_response(
        f"Done — removed **{names}** from bug #{bug.id}.",
        bug_id=bug.id,
    )


def _apply_set_field(db: Session, plan: ActionPlan, actor: User,
                     field_name: str, label: str) -> Response:
    bug = _load_bug(db, plan.bug_id, actor) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found.")
    err = _check_can_edit_bug(db, actor, bug)
    if err:
        return _error_response(err)
    old = getattr(bug, field_name)
    new = plan.new_value
    if old == new:
        return _success_response(
            f"Bug #{bug.id} {label} is already **{old}** — nothing to do.",
            bug_id=bug.id,
        )
    setattr(bug, field_name, new)
    detail = f"{field_name}: {old!r} -> {new!r}"
    _audit(db, bug.id, actor, "bug_update", detail)
    db.commit()
    return _success_response(
        f"Done — bug #{bug.id} {label} changed from **{old}** to **{new}**.",
        bug_id=bug.id,
    )


def _apply_add_comment(db: Session, plan: ActionPlan, actor: User) -> Response:
    bug = _load_bug(db, plan.bug_id, actor) if plan.bug_id else None
    if bug is None:
        return _error_response(f"Bug #{plan.bug_id} not found.")
    # Project-access check on top of org scope — non-member shouldn't
    # be able to post a comment on a project they can't see.
    if not can_access_project(db, actor, bug.project):
        return _error_response("You don't have access to that bug's project")
    body = (plan.comment_body or "").strip()
    if not body:
        return _error_response(
            "I don't have any comment text to post. Try: "
            "*comment on #5: this is fixed in commit abc*"
        )
    if len(body) > 4000:
        return _error_response("Comment too long — keep it under 4000 chars")
    c = Comment(bug_id=bug.id, author_user_id=actor.id,
                author_name=actor.name, body=body)
    db.add(c)
    db.flush()
    _audit(db, bug.id, actor, "comment_added",
           f"Comment by {actor.name}: {body[:80]}")
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

    # Resolve the project within the actor's org and access set.
    from app.auth import accessible_project_ids
    pids = accessible_project_ids(db, actor)
    project_id = plan.new_project_id
    if project_id is None:
        # No project given — pick the first project the user can access.
        if not pids:
            return _error_response(
                "You don't have access to any projects yet. "
                "Ask an admin to add you to one."
            )
        first = db.scalar(
            select(Project).where(Project.id.in_(pids)).order_by(Project.id)
        )
        if first is None:
            return _error_response("There are no projects yet. Create one first")
        project_id = first.id
    else:
        proj = db.get(Project, project_id)
        if proj is None or proj.org_id != actor.org_id:
            return _error_response("That project doesn't exist anymore")
        if project_id not in pids:
            return _error_response("You don't have access to that project")

    bug = Bug(
        title=title,
        description=(plan.new_description or ""),
        status="New",
        priority=(plan.new_value or "Medium"),
        environment="DEV",
        project_id=project_id,
        reporter_id=actor.id,
    )
    db.add(bug)
    db.flush()
    if plan.target_user_ids:
        targets = list(db.scalars(
            select(User).where(
                User.id.in_(plan.target_user_ids),
                User.org_id == actor.org_id,
            )
        ).all())
        bug.assignees = targets
    _audit(db, bug.id, actor, "bug_create",
           f"Created bug #{bug.id}: {title[:80]}")
    db.commit()
    return _success_response(
        f"Created bug #{bug.id} — *{title[:80]}*. You're the reporter.",
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

    # Org-scoped uniqueness — two different orgs can each have a
    # "Platform" project. Only collisions inside the same org are blocked.
    existing = db.scalar(
        select(Project).where(
            Project.org_id == actor.org_id,
            Project.name.ilike(name),
        )
    )
    if existing is not None:
        return _error_response(
            f"There's already a project called **{existing.name}** in your organization."
        )

    # Derive a project key — same heuristic as routes/projects.py.
    from app.routes.projects import _derive_key, _unique_key
    from app.models import PROJECT_ROLE_LEAD, ProjectMembership
    key = _unique_key(db, actor.org_id, _derive_key(name))

    proj = Project(
        org_id=actor.org_id,
        name=name,
        key=key,
        description=(plan.new_description or ""),
    )
    db.add(proj)
    db.flush()
    # Creator auto-becomes lead so they can manage members and edit it.
    db.add(ProjectMembership(
        project_id=proj.id, user_id=actor.id, role=PROJECT_ROLE_LEAD,
    ))
    _audit(db, None, actor, "project_create",
           f"Created project '{name}' ({key})",
           entity_type="project", entity_id=proj.id)
    db.commit()
    return Response(
        blocks=[Block("text", {"text":
            f"Project **{proj.name}** created. You can now file bugs against it."})],
        summary=f"Created project {proj.name}",
        intent="action_done",
    )


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------
def execute_plan(plan: ActionPlan, db: Session, actor: User) -> Response:
    """Run a confirmed plan. Caller is responsible for verifying that the
    plan really came from this user (we still re-check actor.id below)."""
    if plan.actor_user_id != actor.id:
        return _error_response("That action was staged for a different user")

    try:
        if plan.kind == "assign":
            return _apply_assign(db, plan, actor)
        if plan.kind == "unassign":
            return _apply_unassign(db, plan, actor)
        if plan.kind == "set_status":
            return _apply_set_field(db, plan, actor, "status", "status")
        if plan.kind == "set_priority":
            return _apply_set_field(db, plan, actor, "priority", "priority")
        if plan.kind == "set_environment":
            return _apply_set_field(db, plan, actor, "environment", "environment")
        if plan.kind == "set_due_date":
            return _apply_set_field(db, plan, actor, "due_date", "due date")
        if plan.kind == "add_comment":
            return _apply_add_comment(db, plan, actor)
        if plan.kind == "create_bug":
            return _apply_create_bug(db, plan, actor)
        if plan.kind == "create_project":
            return _apply_create_project(db, plan, actor)
        return _error_response(f"Unknown action: {plan.kind}")
    except Exception as exc:   # noqa: BLE001
        # Roll back so a partial change never sticks.
        try:
            db.rollback()
        except Exception:   # noqa: BLE001
            pass
        return _error_response(f"Action failed: {exc}")


# ---------------------------------------------------------------------------
# Confirmation-prompt builder — used by the parser layer when staging an
# action that needs user "yes" before it goes ahead.
# ---------------------------------------------------------------------------
def stage_with_confirm(plan: ActionPlan) -> Response:
    """Return a confirm-Response for the staged plan. The caller is
    expected to have already saved the plan into memory.store under the
    actor's user id, so a "yes" follow-up can pop it back out."""
    prompt = (
        f"Just to confirm: **{plan.summary_human}**.\n\n"
        f"Reply **yes** (or click below) to proceed, **no** to cancel."
    )
    return _confirm_response(plan, prompt)


__all__ = [
    "ActionPlan",
    "execute_plan",
    "stage_with_confirm",
]
