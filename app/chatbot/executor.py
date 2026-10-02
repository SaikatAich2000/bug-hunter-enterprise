"""Sleuth executor: turns a ParsedQuery into a structured Response.

Dispatches on parsed intent and returns a Response of typed blocks (text /
table / file) so the frontend can escape and render every reply uniformly
(no raw HTML → no stored-XSS via bug titles), and rule-engine and LLM
responses share one shape.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.access import (
    accessible_project_ids,
    can_access_project,
    scope_bug_query,
)
from app.config import get_settings
from app.models import (
    Activity,
    Attachment,
    Bug,
    Project,
    User,
    bug_assignees,
)

from .nlu import (
    OPEN_STATUSES,
    Context,
    ParsedQuery,
    describe_filters,
    parse,
    pick_report_key,
)

_LOGGER = logging.getLogger("bug_hunter.sleuth")


@dataclass
class Block:
    """One renderable chunk of the assistant's reply."""
    kind: str   # "text" | "table" | "file" | "suggestions" | "confirm"
    payload: dict


@dataclass
class Response:
    blocks: list[Block] = field(default_factory=list)
    # Drives a one-line aria-live announcement in the frontend.
    summary: str = ""
    # Surfaces in the network log; helps debug missed intents.
    intent: str = ""
    # When the rule engine isn't confident, the router can hand off to the
    # optional LLM. Not shown to the user.
    fallback_eligible: bool = False


# No caching: user/project tables are small, and stale name resolution would
# be a confusing bug. Caps mirror the REST list caps.
_CHAT_LIST_CAP = 500
_CHAT_CONTEXT_CAP = 2000
# Above this, bulk actions are refused rather than staged behind one "yes".
_BULK_ACTION_CAP = 500
_WRITE_DENIED_SUMMARY = "Write not permitted"


def build_context(db: Session, actor: User) -> Context:
    """Name-resolution context: the people and projects of the actor's organization."""
    users = list(db.scalars(
        select(User).where(User.org_id == actor.org_id).limit(_CHAT_CONTEXT_CAP)
    ).all())
    projects = list(db.scalars(
        select(Project).where(Project.org_id == actor.org_id).limit(_CHAT_CONTEXT_CAP)
    ).all())

    user_tuples: list[tuple[int, str, str, str]] = []
    role_map: dict[int, str] = {}
    for u in users:
        norm_name = (u.name or "").strip().lower()
        # Email local-part so "alice" resolves even when her name is "Alice Wong".
        local = ""
        if u.email and "@" in u.email:
            local = u.email.split("@", 1)[0].strip().lower()
        user_tuples.append((u.id, norm_name, local, u.name))
        role_map[u.id] = u.role
    proj_tuples = [
        (p.id, (p.name or "").strip().lower(), p.name) for p in projects
    ]
    return Context(users=user_tuples, projects=proj_tuples, user_role_map=role_map)


def _bug_row(b: Bug) -> dict[str, Any]:
    """Flat row dict for the chat table or Excel export. Minimal by design;
    heavy fields are available via the bug-detail intent."""
    return {
        "id": b.id,
        "title": b.title,
        "project": b.project.name if b.project else "",
        "status": b.status,
        "priority": b.priority,
        "environment": b.environment,
        "reporter": b.reporter.name if b.reporter else "",
        "assignees": ", ".join(a.name for a in b.assignees),
        "due_date": b.due_date or "",
        "updated_at": b.updated_at.isoformat() if b.updated_at else "",
        "created_at": b.created_at.isoformat() if b.created_at else "",
    }


def _eager_bug_query():
    """Bug select with all required relationships eager-loaded to avoid N+1."""
    return select(Bug).options(
        selectinload(Bug.project),
        selectinload(Bug.reporter),
        selectinload(Bug.assignees),
        selectinload(Bug.event),
    )


_CREATED_AT_KEYWORDS = (
    "created", "filed", "reported", "opened", "raised",
    "logged", "submitted", "registered",
)


def _apply_both(stmt, count_stmt, clause):
    """Apply the same WHERE clause to both the select and the count statement."""
    return stmt.where(clause), count_stmt.where(clause)


def _apply_text_search(stmt, count_stmt, needle_raw: str):
    """Build the title/description LIKE clause for the text-search filter."""
    needle = needle_raw.lower().replace("\\", "\\\\")
    needle = needle.replace("%", "\\%").replace("_", "\\_")
    like = f"%{needle}%"
    clause = or_(
        func.lower(Bug.title).like(like, escape="\\"),
        func.lower(Bug.description).like(like, escape="\\"),
    )
    return _apply_both(stmt, count_stmt, clause)


def _apply_time_window(stmt, count_stmt, pq: ParsedQuery):
    """Layer the time-window filter onto the statement pair, picking the
    right timestamp column based on the verbs the user used."""
    msg_l = (pq.raw_message or "").lower()
    use_created = any(k in msg_l for k in _CREATED_AT_KEYWORDS)
    col = Bug.created_at if use_created else Bug.updated_at
    if pq.time_window.start:
        stmt, count_stmt = _apply_both(stmt, count_stmt, col >= pq.time_window.start)
    if pq.time_window.end:
        stmt, count_stmt = _apply_both(stmt, count_stmt, col <= pq.time_window.end)
    return stmt, count_stmt


def _apply_bug_filters(stmt, count_stmt, pq: ParsedQuery):
    """Apply parsed filters to the select+count statement pair, so the caller
    keeps the full total even when paginating to a slice."""
    if pq.item_types:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.item_type.in_(pq.item_types))
    if pq.statuses:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.status.in_(pq.statuses))
    if pq.priorities:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.priority.in_(pq.priorities))
    if pq.environments:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.environment.in_(pq.environments))
    if pq.project_ids:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.project_id.in_(pq.project_ids))
    if pq.reporter_ids:
        stmt, count_stmt = _apply_both(stmt, count_stmt, Bug.reporter_id.in_(pq.reporter_ids))
    if pq.assignee_ids:
        stmt, count_stmt = _apply_both(
            stmt, count_stmt, Bug.assignees.any(User.id.in_(pq.assignee_ids)),
        )
    # NOT EXISTS over the bug_assignees composite-PK index — a clean index seek.
    if pq.unassigned:
        stmt, count_stmt = _apply_both(stmt, count_stmt, ~Bug.assignees.any())
    if pq.text_search:
        stmt, count_stmt = _apply_text_search(stmt, count_stmt, pq.text_search)
    if pq.time_window and (pq.time_window.start or pq.time_window.end):
        stmt, count_stmt = _apply_time_window(stmt, count_stmt, pq)
    return stmt, count_stmt


def _handle_greeting(actor: User) -> Response:
    name_part = f", {actor.name.split()[0]}" if actor and actor.name else ""
    text = (
        f"Hi{name_part}! I'm **Sleuth** 🔍, your {get_settings().APP_NAME} assistant.\n\n"
        "Ask me anything about bugs, projects, users or activity. A few "
        "examples:\n"
        "- *show all open bugs assigned to John*\n"
        "- *export critical bugs in PROD to excel*\n"
        "- *how many bugs were filed this week?*\n"
        "- *bug 42*\n\n"
        "Type **help** for the full list"
    )
    return Response(
        blocks=[Block("text", {"text": text})],
        summary="Sleuth ready",
        intent="greeting",
    )


def _handle_thanks() -> Response:
    return Response(
        blocks=[Block("text", {"text": "Anytime — happy hunting 🐞"})],
        summary="Thanks acknowledged",
        intent="thanks",
    )


def _handle_help() -> Response:
    text = (
        "**Sleuth — what I can do**\n\n"
        "**Find bugs**\n"
        "- *show open bugs assigned to <name>*\n"
        "- *list critical bugs in PROD*\n"
        "- *bugs reported by Alice this week*\n"
        "- *bugs in project Web Portal with status closed*\n"
        "- *bugs about \"login crash\"*\n\n"
        "**Counts & stats**\n"
        "- *how many open bugs?*\n"
        "- *total bugs in UAT*\n"
        "- *summary* / *dashboard stats*\n\n"
        "**Lookups**\n"
        "- *bug 42* — open the full details\n"
        "- *list all users* / *list managers* / *list admins*\n"
        "- *list projects*\n\n"
        "**Activity**\n"
        "- *recent activity* / *what happened today?*\n\n"
        "**Export**\n"
        "- Add *to excel* / *as xlsx* / *download* to any list query and "
        "I'll generate a spreadsheet you can save.\n\n"
        "**Reports** *(managers / admins)*\n"
        "- *report of who solved how many bugs last week*\n"
        "- *pending bugs report*\n"
        "- *throughput last 7 days by user*\n"
        "- *time to resolution this month*\n"
        "- *aging report* / *project breakdown*\n"
        "- *report of all critical bugs in PROD assigned to alice*\n"
        "Every report comes back as an inline preview plus an Excel "
        "download with the full data, drill-down items, and the filters "
        "that produced it.\n\n"
        "**Take actions** *(I'll always ask before changing anything)*\n"
        "- *close bug 5* / *reopen #12* / *mark bug 7 as resolved*\n"
        "- *assign bug 3 to Alice* / *unassign Bob from #5*\n"
        "- *set bug 9 priority to high* / *make bug 3 critical*\n"
        "- *comment on #5: looks fixed in v2.1*\n"
        "- *due bug 8 2026-06-15*\n"
        "- *create a bug titled \"Login broken\" in project Apollo*\n"
        "- *create project Mercury*  (admin / manager only)\n\n"
        "**Pronouns**\n"
        "After viewing or filtering a bug I remember it for the next "
        "30 minutes — so *close it* or *comment on that bug: ...* both "
        "work.\n\n"
        "I match status synonyms (open / fixed / WIP / blocker / urgent / "
        "P0–P3), environment shortcuts (prod / staging / dev) and time "
        "windows (today, yesterday, this week, last 7 days). I'll ask "
        "for clarification when a name matches more than one person"
    )
    return Response(
        blocks=[Block("text", {"text": text})],
        summary="Help",
        intent="help",
    )


def _handle_about(message: str) -> Response:
    """Answer "what is X" questions about the product from a static knowledge
    base in this module. Unrecognized questions fall back to a help nudge."""
    msg = message.lower()
    facts = {
        "status": (
            "**Statuses**: New, In Progress, Reopened, Resolved, Closed, "
            "Resolve Later, Not a Bug. *Open* groups New + In Progress + "
            "Reopened. *Not a Bug* is excluded from the total-bugs KPI"
        ),
        # "priorit" stems both "priority" and "priorities".
        "priorit": (
            "**Priorities**: Low, Medium, High, Critical. P0 → Critical, "
            "P1 → High, P2 → Medium, P3 → Low when you use those aliases"
        ),
        "environment": (
            "**Environments**: DEV, UAT, PROD. *staging*, *qa*, *test* are "
            "treated as UAT; *production*, *live* mean PROD"
        ),
        "role": (
            "**Roles**: admin, manager, user. Admins manage everything. "
            "Managers can edit bugs / projects / non-admin users but can't "
            "delete users or projects. Regular users can create and edit "
            "bugs but can't manage other users"
        ),
        "audit": (
            "The audit trail records every create, update, delete, login, "
            "and session revoke. Visible to admins and managers from the "
            "Audit Trail sidebar item"
        ),
        "session": (
            "Every login creates a server-side session. Admins can see all "
            "active sessions and revoke individual ones from the Sessions "
            "sidebar item. Revoking your own current session isn't allowed "
            "— use Log out instead"
        ),
        "attachment": (
            "Attachments (PDF, image, video — up to 50 MB each) are stored "
            "as BLOBs in PostgreSQL, so a database backup includes every "
            "attachment automatically"
        ),
    }
    for keyword, answer in facts.items():
        if keyword in msg:
            return Response(
                blocks=[Block("text", {"text": answer})],
                summary="Explanation",
                intent="about",
            )
    return Response(
        blocks=[Block("text", {"text":
            "I'm not sure I caught that. Type **help** to see the kinds "
            "of questions I can answer"})],
        summary="Unknown",
        intent="about",
        fallback_eligible=True,
    )


# When both the rule engine and the classifier miss, offer the top-ranked
# intent as a "did you mean", mapped to a canonical example phrasing.
_INTENT_SUGGESTION: dict[str, str] = {
    "list_bugs": "list open bugs",
    "count_bugs": "how many bugs are open?",
    "stats": "show me a summary",
    "list_users": "list users",
    "list_projects": "list projects",
    "bug_detail": "show bug #123",
    "export": "export open bugs to excel",
    "audit": "what changed recently?",
    "report": "throughput report for last week",
}


def _did_you_mean(message: str) -> Optional[str]:
    """Return a near-miss intent suggestion, or None. A classifier failure must
    never break the unknown-fallback path, so exceptions are swallowed."""
    try:
        from app.chatbot import classifier as _clf
        scored = _clf.explain(message, top_k=1)
    except Exception:
        return None
    if not scored:
        return None
    intent, score = scored[0]
    if score >= 0.12 and intent in _INTENT_SUGGESTION:
        return _INTENT_SUGGESTION[intent]
    return None


def _handle_unknown(message: str = "") -> Response:
    """Fallback for unclassified queries: a classifier-ranked "did you mean"
    plus topic-specific hints from recognised tokens."""
    msg = (message or "").lower()
    hints: list[str] = []
    suggestion = _did_you_mean(message)
    if suggestion:
        hints.append(f"*{suggestion}*")
    # Topic-specific hints replace the default when users/projects/stats are named.
    if any(w in msg for w in ("user", "users", "team",
                              "member", "members")):
        hints.append("*list users* or *list admins*")
    elif any(w in msg for w in ("project", "projects")):
        hints.append("*list projects*")
    elif any(w in msg for w in ("stat", "stats", "kpi",
                                "summary", "dashboard")):
        hints.append("*summary* or *stats*")
    else:
        hints.append("*show open bugs assigned to <name>*")
        hints.append("*how many critical bugs in PROD?*")
    bullet_lines = "\n".join(f"- {h}" for h in hints[:3])
    return Response(
        blocks=[Block("text", {"text":
            "Hmm, I didn't quite catch that. A few things you can try:\n\n"
            f"{bullet_lines}\n\n"
            "Or type **help** for the full list"})],
        summary="Unknown",
        intent="unknown",
        fallback_eligible=True,
    )


def _handle_list_users(db: Session, pq: ParsedQuery, actor: User) -> Response:
    stmt = select(User).where(User.org_id == actor.org_id).order_by(func.lower(User.name))
    if pq.role_filter:
        stmt = stmt.where(User.role == pq.role_filter)
    rows = list(db.scalars(stmt.limit(_CHAT_LIST_CAP)).all())
    if not rows:
        return Response(
            blocks=[Block("text", {"text": "No users match that filter"})],
            summary="0 users",
            intent="list_users",
        )
    headers = ["Name", "Email", "Role", "Active"]
    data = [
        [u.name, u.email, u.role, "Yes" if u.is_active else "No"]
        for u in rows
    ]
    label = "users"
    if pq.role_filter:
        label = f"{pq.role_filter}s" if not pq.role_filter.endswith("s") else pq.role_filter
    return Response(
        blocks=[
            Block("text", {"text": f"Found **{len(rows)}** {label}"}),
            Block("table", {"headers": headers, "rows": data}),
        ],
        summary=f"{len(rows)} {label}",
        intent="list_users",
    )


def _handle_list_projects(db: Session, accessible: set[int]) -> Response:
    # Only the actor's own projects (admins: every project of their organization).
    stmt = select(Project).where(Project.id.in_(accessible)).order_by(func.lower(Project.name))
    rows = list(db.scalars(stmt.limit(_CHAT_LIST_CAP)).all())
    if not rows:
        return Response(
            blocks=[Block("text", {"text": "There are no projects yet"})],
            summary="0 projects",
            intent="list_projects",
        )
    # Per-project bug counts in a single grouped query, scoped to match.
    counts = dict(db.execute(
        scope_bug_query(
            select(Bug.project_id, func.count(Bug.id)).group_by(Bug.project_id),
            accessible,
        )
    ).all())
    headers = ["Name", "Bugs", "Description"]
    data = [
        [p.name, str(int(counts.get(p.id, 0))), p.description or "—"]
        for p in rows
    ]
    return Response(
        blocks=[
            Block("text", {"text": f"Found **{len(rows)}** project(s)"}),
            Block("table", {"headers": headers, "rows": data}),
        ],
        summary=f"{len(rows)} projects",
        intent="list_projects",
    )


def _handle_bug_detail(db: Session, pq: ParsedQuery, accessible: set[int]) -> Response:
    bug = db.scalar(_eager_bug_query().where(Bug.id == pq.bug_id))
    # Bugs outside the actor's project scope are treated as not found, so
    # Sleuth can't be used to probe inaccessible projects.
    if bug is None or not can_access_project(accessible, bug.project_id):
        return Response(
            blocks=[Block("text", {"text":
                f"I couldn't find a bug with ID **#{pq.bug_id}**. "
                "Maybe it was deleted, or the number is off?"})],
            summary="Not found",
            intent="bug_detail",
        )
    att_count = db.scalar(
        select(func.count(Attachment.id)).where(Attachment.bug_id == bug.id)
    ) or 0
    descr = (bug.description or "").strip()
    short_descr = (descr[:600] + "…") if len(descr) > 600 else descr
    # Falls back to "Bug" when item_type is absent on a row.
    itype = getattr(bug, "item_type", None) or "Bug"
    event_name = bug.event.name if getattr(bug, "event", None) else None
    body = (
        f"**{itype} #{bug.id} — {bug.title}**\n\n"
        f"**Type:** {itype} · **Status:** {bug.status} · **Priority:** {bug.priority} · "
        f"**Environment:** {bug.environment}\n"
        f"**Project:** {bug.project.name if bug.project else '—'}\n"
    )
    if event_name:
        body += f"**Event:** {event_name}\n"
    body += (
        f"**Reporter:** {bug.reporter.name if bug.reporter else '—'}\n"
        f"**Assignees:** "
        f"{', '.join(a.name for a in bug.assignees) or '—'}\n"
        f"**Due:** {bug.due_date or '—'} · "
        f"**Attachments:** {att_count}\n"
        f"**Created:** {bug.created_at.isoformat() if bug.created_at else '—'}\n"
        f"**Updated:** {bug.updated_at.isoformat() if bug.updated_at else '—'}\n"
    )
    if short_descr:
        body += f"\n**Description:**\n{short_descr}"
    body += (
        f"\n\n[Open in {get_settings().APP_NAME}](#open-bug-{bug.id})"
    )
    return Response(
        blocks=[Block("text", {"text": body, "open_bug_id": bug.id})],
        summary=f"Bug #{bug.id}",
        intent="bug_detail",
    )


def _scope_recent_activity(stmt, actor: User, accessible):
    """Scope a recent-activity query to the actor's organization: regular users see only
    their own actions, managers their projects' bugs, admins everything in the organization."""
    stmt = stmt.where(Activity.org_id == actor.org_id)
    if actor.role not in ("admin", "manager"):
        return stmt.where(Activity.actor_user_id == actor.id)
    if actor.role == "manager":
        return stmt.where(
            Activity.bug_id.in_(select(Bug.id).where(Bug.project_id.in_(accessible)))
        )
    return stmt


def _handle_recent_activity(db: Session, pq: ParsedQuery, actor: User,
                            accessible: set[int]) -> Response:
    # Scoped in _scope_recent_activity so chat can't bypass the audit policy.
    stmt = _scope_recent_activity(
        select(Activity).order_by(Activity.created_at.desc(), Activity.id.desc()),
        actor, accessible,
    )
    if pq.time_window:
        if pq.time_window.start:
            stmt = stmt.where(Activity.created_at >= pq.time_window.start)
        if pq.time_window.end:
            stmt = stmt.where(Activity.created_at <= pq.time_window.end)
    stmt = stmt.limit(25)
    rows = list(db.scalars(stmt).all())
    if not rows:
        return Response(
            blocks=[Block("text", {"text":
                "No recent activity"
                + (f" {pq.time_window.label}" if pq.time_window else "")})],
            summary="0 events",
            intent="recent_activity",
        )
    data = [
        [
            r.created_at.isoformat() if r.created_at else "—",
            r.actor_name,
            r.action,
            (r.detail[:160] + "…") if r.detail and len(r.detail) > 160 else (r.detail or ""),
        ]
        for r in rows
    ]
    return Response(
        blocks=[
            Block("text", {"text":
                f"Showing the **{len(rows)}** most recent activity entr"
                f"{'y' if len(rows)==1 else 'ies'}"
                + (f" {pq.time_window.label}" if pq.time_window else "")}),
            Block("table", {
                "headers": ["When", "Actor", "Action", "Detail"],
                "rows": data,
            }),
        ],
        summary=f"{len(rows)} activity entries",
        intent="recent_activity",
    )


def _handle_stats(db: Session, accessible: set[int]) -> Response:
    """Dashboard KPI snapshot via GROUP BY plus a top-assignees join, all
    project-scoped so restricted users only see their own numbers."""
    excluded = ["Not a Bug"]

    by_status: dict[str, int] = {}
    for s, c in db.execute(
        scope_bug_query(
            select(Bug.status, func.count(Bug.id)).group_by(Bug.status), accessible,
        )
    ).all():
        by_status[s] = int(c)

    total = sum(c for s, c in by_status.items() if s not in excluded)
    open_n = sum(by_status.get(s, 0) for s in OPEN_STATUSES)
    resolved = by_status.get("Resolved", 0)
    closed = by_status.get("Closed", 0)
    later = by_status.get("Resolve Later", 0)

    crit = int(db.scalar(
        scope_bug_query(
            select(func.count(Bug.id)).where(Bug.priority == "Critical"), accessible,
        )
    ) or 0)
    prod = int(db.scalar(
        scope_bug_query(
            select(func.count(Bug.id)).where(Bug.environment == "PROD"), accessible,
        )
    ) or 0)

    text = (
        f"**{get_settings().APP_NAME} — current snapshot**\n\n"
        f"- **Total** (excluding *Not a Bug*): {total}\n"
        f"- **Open** (New + In Progress + Reopened): {open_n}\n"
        f"- **Resolved**: {resolved}\n"
        f"- **Closed**: {closed}\n"
        f"- **Resolve Later**: {later}\n"
        f"- **Critical**: {crit}\n"
        f"- **In PROD**: {prod}\n"
    )
    # Top 5 assignees with open bugs, scoped to the actor's projects.
    top = db.execute(
        scope_bug_query(
            select(User.name, func.count(bug_assignees.c.bug_id))
            .join(bug_assignees, bug_assignees.c.user_id == User.id)
            .join(Bug, Bug.id == bug_assignees.c.bug_id)
            .where(Bug.status.in_(OPEN_STATUSES))
            .group_by(User.id, User.name)
            .order_by(func.count(bug_assignees.c.bug_id).desc())
            .limit(5),
            accessible,
        )
    ).all()
    blocks = [Block("text", {"text": text})]
    if top:
        blocks.append(Block("table", {
            "headers": ["Assignee", "Open bugs"],
            "rows": [[name, str(int(count))] for name, count in top],
        }))
    return Response(
        blocks=blocks,
        summary=f"{total} bugs, {open_n} open",
        intent="stats",
    )


def _build_user_suggest_pool(ctx: Context) -> dict[str, str]:
    """Build a lookup-key → display-name pool for the user-suggestion matcher.
    Keys are normalized names and email local-parts."""
    pool: dict[str, str] = {}
    for _uid, norm_name, email_local, display in ctx.users:
        if norm_name:
            pool.setdefault(norm_name, display)
        if email_local:
            pool.setdefault(email_local, display)
    return pool


def _dedupe_display_names(matches: list[str], pool: dict[str, str]) -> list[str]:
    """Map raw matches back to display names, dropping duplicates while
    preserving order."""
    out: list[str] = []
    seen: set[str] = set()
    for m in matches:
        d = pool.get(m)
        if d and d not in seen:
            out.append(d)
            seen.add(d)
    return out


def _suggest_user(phrase: str, ctx: Optional[Context]) -> str:
    """Return a "did you mean" hint for an unresolved user phrase (difflib over
    names and email local-parts), or "" if nothing is close. Never applied."""
    if not ctx or not phrase:
        return ""
    needle = phrase.strip().lower()
    if not needle:
        return ""
    pool = _build_user_suggest_pool(ctx)
    if not pool:
        return ""
    import difflib
    matches = difflib.get_close_matches(needle, list(pool.keys()), n=2, cutoff=0.6)
    suggestions = _dedupe_display_names(matches, pool) if matches else []
    if not suggestions:
        return ""
    if len(suggestions) == 1:
        return f"Did you mean **{suggestions[0]}**?"
    return f"Did you mean **{suggestions[0]}** or **{suggestions[1]}**?"


_LIST_BUGS_HEADERS = ["#", "Title", "Project", "Status", "Priority", "Env",
                      "Reporter", "Assignees", "Due"]


def _clarify_ambiguous_names(pq: ParsedQuery) -> Optional[Response]:
    """Return a clarification Response if the parser saw an ambiguous name,
    otherwise None."""
    if not pq.ambiguous_names:
        return None
    first = pq.ambiguous_names[0]
    names_str = ", ".join(first[1])
    return Response(
        blocks=[Block("text", {"text":
            f"More than one user matches **{first[0]}**: {names_str}. "
            "Could you give the full name (e.g. *assigned to Alice "
            "Wong*)?"})],
        summary="Ambiguous name",
        intent="clarify",
    )


def _clarify_unresolved_user(pq: ParsedQuery, ctx: Optional[Context]) -> Optional[Response]:
    """Return a clarification Response when a named user couldn't be resolved,
    rather than dropping the filter and returning every bug. None if nothing to ask."""
    if not (pq.unresolved_assignee_names or pq.unresolved_reporter_names):
        return None
    role = "assignee" if pq.unresolved_assignee_names else "reporter"
    phrase = (pq.unresolved_assignee_names
              or pq.unresolved_reporter_names)[0]
    suggestion_msg = _suggest_user(phrase, ctx)
    verb = "assigned to" if role == "assignee" else "reported by"
    body = (f"I couldn't find a user named **{phrase}** — so I'm "
            f"not running this as a *{verb}* query. ")
    body += suggestion_msg or (
        "Try the full name, the email local-part, or *list users* to see who exists"
    )
    return Response(
        blocks=[Block("text", {"text": body})],
        summary=f"Unknown {role}",
        intent="clarify",
    )


def _format_bug_row(b: Bug) -> list[str]:
    """One bug → one row of the rendered table."""
    return [
        f"#{b.id}",
        (b.title or "")[:80],
        (b.project.name if b.project else "")[:40],
        b.status,
        b.priority,
        b.environment,
        (b.reporter.name if b.reporter else "")[:40],
        (", ".join(a.name for a in b.assignees))[:60],
        b.due_date or "",
    ]


def _build_export_suggestion_block(pq: ParsedQuery) -> Block:
    """Build the "Export to Excel" quick-action block, reusing the original
    query so all filters are preserved."""
    base = (pq.raw_message or "").strip().rstrip("?.!,;:")
    export_send = f"{base}, export to excel" if base else "export to excel"
    return Block("suggestions", {
        "items": [{"label": "Export to Excel", "send": export_send}],
    })


def _build_count_response(total: int, descr: str) -> Response:
    verb = "is" if total == 1 else "are"
    s = "" if total == 1 else "s"
    return Response(
        blocks=[Block("text", {"text": f"There {verb} **{total}** bug{s} {descr}"})],
        summary=f"{total} bugs",
        intent="count_bugs",
    )


def _build_no_results_response(descr: str) -> Response:
    empty_text = (
        "There are no bugs in the system yet"
        if descr == "(no filters)"
        else f"No bugs found {descr}"
    )
    return Response(
        blocks=[Block("text", {"text": empty_text})],
        summary="0 bugs",
        intent="list_bugs",
    )


def _build_inline_list_response(rows: list[Bug], total: int, limit: int, pq: ParsedQuery, descr: str) -> Response:
    data = [_format_bug_row(b) for b in rows]
    summary_text = f"Found **{total}** bug{'' if total == 1 else 's'} {descr}"
    if total > limit:
        summary_text += f" — showing the most recent {limit}"
    blocks = [
        Block("text", {"text": summary_text}),
        Block("table", {
            "headers": _LIST_BUGS_HEADERS,
            "rows": data,
            "row_bug_ids": [b.id for b in rows],
        }),
        _build_export_suggestion_block(pq),
    ]
    return Response(blocks=blocks, summary=f"{total} bugs", intent="list_bugs")


def _list_bugs_order(pq: ParsedQuery):
    """Sort hint. "oldest" / "stale" → ASC by updated_at."""
    if pq.sort_oldest:
        return (Bug.updated_at.asc(), Bug.id.asc())
    return (Bug.updated_at.desc(), Bug.id.desc())


def _handle_list_bugs(db: Session, pq: ParsedQuery, ctx: Optional[Context] = None,
                      owner_id: int = 0, *, accessible: set[int]) -> Response:
    """Run the parsed bug filter and render a count, inline list, or file.

    owner_id is stamped on any staged XLSX so only that user can download it.
    accessible is the actor's project scope, bounding both the result set and any export."""
    clarify = _clarify_ambiguous_names(pq) or _clarify_unresolved_user(pq, ctx)
    if clarify is not None:
        return clarify

    stmt, count_stmt = _apply_bug_filters(_eager_bug_query(), select(func.count(Bug.id)), pq)
    # Project scope bounds both the page and the count (and therefore the export).
    stmt = scope_bug_query(stmt, accessible)
    count_stmt = scope_bug_query(count_stmt, accessible)
    total = db.scalar(count_stmt) or 0
    descr = describe_filters(pq) or "(no filters)"

    if pq.wants_count and not pq.wants_export:
        return _build_count_response(total, descr)

    order_cols = _list_bugs_order(pq)

    if pq.wants_export:
        # Cap exports so "give me everything" can't sink a low-resource host.
        export_cap = 5000
        rows = list(db.scalars(stmt.order_by(*order_cols).limit(export_cap)).all())
        return _build_export_response(rows, pq, total, export_cap, owner_id)

    limit = max(5, min(pq.limit or 100, 200))
    rows = list(db.scalars(stmt.order_by(*order_cols).limit(limit)).all())
    if total == 0:
        return _build_no_results_response(descr)
    return _build_inline_list_response(rows, total, limit, pq, descr)


def _build_export_response(rows: list[Bug], pq: ParsedQuery, total: int, cap: int,
                           owner_id: int = 0) -> Response:
    """Generate the Excel file via excel.py and return a file block."""
    # Lazy import so the module loads without openpyxl; failure surfaces as text.
    try:
        from . import excel  # noqa: WPS433
    except ImportError:
        return Response(
            blocks=[Block("text", {"text":
                "Sorry, the Excel exporter isn't available on this server. "
                "Try the CSV export from the sidebar instead"})],
            summary="Excel disabled",
            intent="export_failed",
        )

    descr = describe_filters(pq) or "all bugs"
    file_label = "bugs"
    if pq.assignee_names:
        file_label += "_for_" + "_".join(
            re.sub(r"[^A-Za-z0-9]+", "_", n).strip("_")
            for n in pq.assignee_names
        )
    if pq.statuses:
        file_label += "_" + "_".join(
            re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
            for s in pq.statuses
        )
    file_label = (file_label[:80].strip("_") or "bugs") + ".xlsx"

    try:
        rows_dicts = [_bug_row(b) for b in rows]
        token, byte_size = excel.stage_workbook(rows_dicts, file_label, owner_id, descr)
    except excel.ExcelGenerationError as exc:
        return Response(
            blocks=[Block("text", {"text":
                f"I couldn't build the spreadsheet: {exc}"})],
            summary="Excel error",
            intent="export_failed",
        )

    note = (
        f"I built a spreadsheet of **{len(rows)}** bug{'' if len(rows)==1 else 's'} "
        f"{descr}"
    )
    if total > cap:
        note += (
            f". The full result has **{total}** bugs — I capped the export "
            f"at {cap} most-recently-updated rows to keep things fast"
        )
    return Response(
        blocks=[
            Block("text", {"text": note}),
            Block("file", {
                "filename": file_label,
                "size_bytes": byte_size,
                "download_token": token,
                "row_count": len(rows),
            }),
        ],
        summary=f"Exported {len(rows)} bugs",
        intent="export_bugs",
    )


# Reports delegate to app.reports.engine, so chat numbers match the SPA exactly.
_REPORTS_ROLE_ALLOWED = frozenset({"admin", "manager"})


def _filters_from_parsed(pq: ParsedQuery) -> "Any":
    """Translate a ParsedQuery into a reports-engine Filters object."""
    from app.reports import Filters
    date_from = None
    date_to = None
    if pq.time_window:
        if pq.time_window.start:
            date_from = pq.time_window.start.date()
        if pq.time_window.end:
            date_to = pq.time_window.end.date()
    # item_types is detected once in the NLU and shared so both paths stay consistent.
    return Filters(
        date_from=date_from,
        date_to=date_to,
        item_types=list(pq.item_types),
        statuses=list(pq.statuses),
        priorities=list(pq.priorities),
        environments=list(pq.environments),
        project_ids=list(pq.project_ids),
        assignee_ids=list(pq.assignee_ids),
        reporter_ids=list(pq.reporter_ids),
        text_search=pq.text_search,
        label=f"Sleuth: {pq.raw_message[:80]}" if pq.raw_message else "",
    )


def _report_row_to_table_row(row: dict, columns) -> list[str]:
    out: list[str] = []
    for col in columns:
        v = row.get(col.key, "")
        if v is None:
            out.append("")
        elif isinstance(v, (int, float)):
            out.append(str(v))
        else:
            s = str(v)
            out.append(s if len(s) <= 80 else s[:77] + "…")
    return out


def _stage_report_xlsx(result, report_key: str, owner_id: int) -> tuple[str, int, str]:
    """Build the report XLSX, stage it under a token bound to owner_id, and
    return (token, byte_size, filename)."""
    from app.chatbot import excel as _excel
    from app.reports import build_workbook_bytes
    from app.reports.xlsx import XlsxBuildError
    try:
        payload = build_workbook_bytes(result)
    except XlsxBuildError as exc:
        raise _excel.ExcelGenerationError(str(exc)) from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"report-{report_key}-{stamp}.xlsx"
    token, size = _excel.stage_bytes(payload, filename, owner_id)
    return token, size, filename


def _report_forbidden_response() -> Response:
    return Response(
        blocks=[Block("text", {"text":
            "Reports are limited to managers and admins. Ask one of "
            "them to run this for you, or use **list bugs** / "
            "**show stats** for the lighter views available to you"})],
        summary="Reports forbidden",
        intent="report_forbidden",
    )


def _report_empty_response(result) -> Response:
    return Response(
        blocks=[Block("text", {"text":
            f"**{result.report_label}** — no rows matched your "
            f"filters. Try widening the date range or removing a "
            f"filter and ask again"})],
        summary=f"0 rows · {result.report_label}",
        intent="report",
    )


def _format_summary_extras(s: dict) -> str:
    """Render the inline summary lines for the chat preview."""
    if not s:
        return ""
    parts: list[str] = []
    # Throughput report
    if "total_resolved" in s:
        parts.append(
            f"**Total resolved**: {s['total_resolved']} "
            f"across {s.get('user_count', 0)} user(s)"
        )
    # Item Detail / Pending / Distribution reports
    if "total_items" in s:
        parts.append(f"**Total items**: {s['total_items']}")
    # Timeline pair — skip when total_resolved already appeared above (throughput).
    if "total_created" in s and "total_resolved" in s and "user_count" not in s:
        parts.append(
            f"**Created**: {s['total_created']} · "
            f"**Resolved**: {s['total_resolved']} · "
            f"**Net**: {s.get('net', 0)}"
        )
    # Time-to-resolution report
    if "average_hours" in s:
        parts.append(
            f"**Average**: {s['average_hours']}h · "
            f"**Median**: {s.get('median_hours', 0)}h · "
            f"**P95**: {s.get('p95_hours', 0)}h"
        )
    return "\n\n".join(parts)


def _build_report_preview_text(result, filters, preview_rows_count: int) -> str:
    text = (
        f"**{result.report_label}** — {result.total} row"
        f"{'' if result.total == 1 else 's'}"
    )
    if filters.date_from or filters.date_to:
        text += (
            f" · {filters.date_from or 'earliest'} → "
            f"{filters.date_to or 'now'}"
        )
    extras = _format_summary_extras(result.summary)
    if extras:
        text += "\n\n" + extras
    if result.total > preview_rows_count:
        text += (
            f"\n\nPreview shows the first {preview_rows_count} row"
            f"{'' if preview_rows_count == 1 else 's'} — "
            f"download the spreadsheet for the full set"
        )
    return text


def _try_stage_file_block(result, report_key: str, owner_id: int) -> Optional[Block]:
    """Build and stage the XLSX bound to owner_id; return the file Block or None
    on failure (logged). Never raises — the chat must always reply."""
    try:
        token, size, filename = _stage_report_xlsx(result, report_key, owner_id)
    except Exception as exc:   # noqa: BLE001 — file build must never crash chat
        _LOGGER.exception("Sleuth report XLSX build failed: %s", exc)
        return None
    return Block("file", {
        "filename": filename,
        "size_bytes": size,
        "download_token": token,
        "row_count": result.total,
    })


def _handle_report(db: Session, pq: ParsedQuery, actor: User, accessible: set[int]) -> Response:
    """Run a report and reply with a preview table and an XLSX file block.
    Managers/admins only, matching the REST endpoint's gate."""
    if actor.role not in _REPORTS_ROLE_ALLOWED:
        return _report_forbidden_response()

    report_key = pick_report_key(pq.raw_message or "") or "item_detail"
    from app.reports import REPORT_CATALOG, run_report
    meta = REPORT_CATALOG.get(report_key, {})
    filters = _filters_from_parsed(pq)
    # Mirrors the REST reports route: restricted managers only see their projects.
    filters.restrict_project_ids = accessible
    try:
        result = run_report(report_key, filters, db)
    except (ValueError, KeyError) as exc:
        return Response(
            blocks=[Block("text", {"text": f"I couldn't run that report: {exc}"})],
            summary="Report error",
            intent="report_error",
        )

    if result.total == 0:
        return _report_empty_response(result)

    # Mirror the REST export's 413 guard: refuse to build a huge workbook in RAM.
    from app.config import get_settings as _get_settings
    max_rows = _get_settings().MAX_REPORT_ROWS
    if getattr(result, "truncated", False) or result.total > max_rows:
        return Response(
            blocks=[Block("text", {"text":
                f"That report matches {result.total:,} rows, which is too many "
                f"to export here (limit {max_rows:,}). Please narrow the filters "
                "(a date range, project, status, etc.) and try again."})],
            summary="Report too large",
            intent="report_too_large",
        )

    preview_rows = result.rows[:15]
    blocks: list[Block] = [
        Block("text", {"text": _build_report_preview_text(result, filters, len(preview_rows))}),
        Block("table", {
            "headers": [c.label for c in result.columns],
            "rows": [_report_row_to_table_row(r, result.columns) for r in preview_rows],
        }),
    ]
    file_block = _try_stage_file_block(result, report_key, actor.id)
    if file_block is not None:
        blocks.append(file_block)
    else:
        blocks.append(Block("text", {"text":
            "(Couldn't build the spreadsheet right now — use the "
            "**Reports** sidebar to download it manually)"}))

    label = meta.get("label", result.report_label)
    return Response(
        blocks=blocks,
        summary=f"Report: {label} ({result.total})",
        intent="report",
    )


def _resolve_pronouns(pq, actor: User) -> None:
    """Fill in pq.bug_id from conversation memory when the parser flagged a
    pronoun reference but couldn't resolve the id directly."""
    if pq.bug_id is None and getattr(pq, "used_pronoun_bug", False):
        from app.chatbot.memory import store as _mem
        sess = _mem.get(actor.id)
        if sess is not None and sess.last_bug_id:
            pq.bug_id = sess.last_bug_id


_ACTIONS_NEEDING_BUG = frozenset({
    "assign", "unassign", "set_status", "set_priority",
    "set_environment", "set_due_date", "add_comment",
})


def _plan_assign(plan, pq, kind):
    if not pq.assignee_ids:
        return None, ("I need a name. Try *assign bug 5 to alice* "
                      "or *unassign bob from #5*")
    plan.target_user_ids = list(pq.assignee_ids)
    plan.target_user_names = list(pq.assignee_names)
    verb = "Assign" if kind == "assign" else "Unassign"
    names = ", ".join(plan.target_user_names) or "user(s)"
    direction = "to" if kind == "assign" else "from"
    plan.summary_human = f"{verb} {names} {direction} bug #{pq.bug_id}"
    return plan, None


def _plan_set_status(plan, pq):
    if not pq.action_value:
        return None, ("What status? e.g. *mark bug 5 as resolved* or "
                      "*close bug 5*")
    plan.new_value = pq.action_value
    plan.summary_human = f"Set bug #{pq.bug_id} status to {pq.action_value}"
    return plan, None


def _plan_set_priority(plan, pq):
    if not pq.action_value:
        return None, ("What priority? e.g. *set bug 5 priority to high*")
    plan.new_value = pq.action_value
    plan.summary_human = f"Set bug #{pq.bug_id} priority to {pq.action_value}"
    return plan, None


def _plan_set_environment(plan, pq):
    if not pq.environments:
        return None, ("Which environment? DEV / UAT / PROD")
    plan.new_value = pq.environments[0]
    plan.summary_human = f"Set bug #{pq.bug_id} environment to {plan.new_value}"
    return plan, None


def _plan_set_due_date(plan, pq):
    if not pq.action_value:
        return None, ("What date? Use YYYY-MM-DD format, e.g. "
                      "*due bug 5 2026-06-15*")
    plan.new_value = pq.action_value
    plan.summary_human = f"Set bug #{pq.bug_id} due date to {pq.action_value}"
    return plan, None


def _plan_add_comment(plan, pq):
    if not pq.action_comment:
        return None, ("What should the comment say? Use a colon, e.g. "
                      "*comment on #5: works for me*")
    plan.comment_body = pq.action_comment
    preview = pq.action_comment if len(pq.action_comment) < 60 \
        else pq.action_comment[:57] + "..."
    plan.summary_human = f'Comment on bug #{pq.bug_id}: "{preview}"'
    return plan, None


def _plan_create_bug(plan, pq):
    if not pq.action_title:
        return None, ("I need a title. Try *create a bug titled "
                      '"Login broken" in project Apollo*')
    plan.new_title = pq.action_title
    if pq.priorities:
        plan.new_value = pq.priorities[0]
    if pq.project_ids:
        plan.new_project_id = pq.project_ids[0]
        plan.new_project_name = pq.project_names[0]
    if pq.assignee_ids:
        plan.target_user_ids = list(pq.assignee_ids)
        plan.target_user_names = list(pq.assignee_names)
    proj_part = (f" in project {plan.new_project_name}"
                 if plan.new_project_name else "")
    plan.summary_human = f'Create bug "{pq.action_title[:60]}"{proj_part}'
    return plan, None


def _plan_create_project(plan, pq):
    if not pq.action_title:
        return None, ("I need a project name, e.g. "
                      "*create project Mercury*")
    plan.new_project_name = pq.action_title
    plan.summary_human = f'Create project "{pq.action_title}"'
    return plan, None


def _build_action_plan(pq, actor: User) -> "tuple[Any, Optional[str]]":
    """Translate a parsed write-intent into an ActionPlan, returning
    (plan, error_message); plan is None with a message when it can't be built."""
    from app.chatbot.actions import ActionPlan
    kind = pq.action_kind
    plan = ActionPlan(kind=kind, actor_user_id=actor.id)

    if kind in _ACTIONS_NEEDING_BUG and pq.bug_id is None:
        if pq.used_pronoun_bug:
            return None, ("I don't know which bug you mean. "
                          "Try mentioning the bug id, e.g. *close bug 5*")
        return None, ("Which bug? Tell me the id — for example "
                      "*close bug 5* or *comment on #12: works for me*")
    plan.bug_id = pq.bug_id

    if kind in ("assign", "unassign"):
        return _plan_assign(plan, pq, kind)
    if kind == "set_status":
        return _plan_set_status(plan, pq)
    if kind == "set_priority":
        return _plan_set_priority(plan, pq)
    if kind == "set_environment":
        return _plan_set_environment(plan, pq)
    if kind == "set_due_date":
        return _plan_set_due_date(plan, pq)
    if kind == "add_comment":
        return _plan_add_comment(plan, pq)
    if kind == "create_bug":
        return _plan_create_bug(plan, pq)
    if kind == "create_project":
        return _plan_create_project(plan, pq)
    return None, f"I don't know how to do '{kind}'"


_BULK_SCOPE_RE = re.compile(
    r"\b(all|every|each|everything|the\s+whole|entire)\b"
    r"(?:[\w\s'/-]*?\b"
    r"(bugs?|issues?|tickets?|items?|work\s*items?|requirements?|tasks?)\b)?",
    re.IGNORECASE,
)
# Bulk-capable write kinds. Excludes delete, create, and comment.
_BULK_KINDS = frozenset({"assign", "unassign", "set_status", "set_priority"})


def _bulk_kind_and_value(message: str, pq) -> "tuple[Optional[str], Optional[str]]":
    """Determine the bulk action kind and value, reusing parse() output then
    falling back to direct matching for assign/unassign phrasings it missed."""
    from app.chatbot import nlu as _nlu
    if pq.action_kind in _BULK_KINDS:
        return pq.action_kind, pq.action_value
    if _nlu._UNASSIGN_RE.search(message):
        return "unassign", None
    # Guard against "give me all bugs" — assign verb without a list-request verb.
    if _nlu._ASSIGN_RE.search(message) and not _nlu._LIST_VERB_RE.search(message):
        return "assign", None
    return None, None


def _resolve_trailing_assignee(message: str, ctx) -> "tuple[list[int], list[str]]":
    """Resolve the assignee from a trailing "... to <name>" for bulk assigns,
    which the single-action parser misses. Returns ([], []) if not unambiguous."""
    from app.chatbot import nlu as _nlu
    # IGNORECASE folds case, so [a-z] in the character class is sufficient.
    m = re.search(r"\bto\s+([a-z][\w.'\-]*(?:\s+[a-z][\w.'\-]*)*)\s*$",
                  message.strip(), re.IGNORECASE)
    if not m:
        return [], []
    matches = _nlu._resolve_name(m.group(1).strip(), ctx)
    if len(matches) != 1:
        return [], []
    uid, disp = matches[0]
    return [uid], [disp]


# Bulk-scope noun → item_type it restricts to, so "assign all bugs" doesn't
# touch Requirements/Tasks. Generic nouns map to None to sweep all types.
_BULK_NOUN_TO_TYPE: dict[str, Optional[str]] = {
    "bug": "Bug", "bugs": "Bug",
    "issue": "Bug", "issues": "Bug",
    "ticket": "Bug", "tickets": "Bug",
    "defect": "Bug", "defects": "Bug",
    "requirement": "Requirement", "requirements": "Requirement",
    "task": "Task", "tasks": "Task",
}


def _bulk_item_type(scope_noun: str) -> Optional[str]:
    """The item_type a bulk scope noun restricts to, or None for all types
    (item / items / work item / work items)."""
    key = re.sub(r"\s+", "", (scope_noun or "").lower())  # "work items" → "workitems"
    return _BULK_NOUN_TO_TYPE.get(key)


def _resolve_bulk_bug_ids(db: Session, pq, item_type: Optional[str]) -> list[int]:
    """Resolve the work-item ids a bulk action targets, respecting the named
    item type and any status/priority/environment/project filters. For
    set_status/set_priority the target value is already consumed by parse()."""
    stmt = select(Bug.id)
    if item_type is not None:
        stmt = stmt.where(Bug.item_type == item_type)
    if pq.statuses:
        stmt = stmt.where(Bug.status.in_(pq.statuses))
    if pq.priorities:
        stmt = stmt.where(Bug.priority.in_(pq.priorities))
    if pq.environments:
        stmt = stmt.where(Bug.environment.in_(pq.environments))
    if pq.project_ids:
        stmt = stmt.where(Bug.project_id.in_(pq.project_ids))
    # Fetch one over the cap so the caller can detect and refuse an oversized scope.
    stmt = stmt.order_by(Bug.id).limit(_BULK_ACTION_CAP + 1)
    return [int(i) for i in db.scalars(stmt).all()]


def _bulk_scope_phrase(db: Session, bug_ids: list[int], item_type: Optional[str]) -> str:
    """Human description of what a bulk action will touch. An all-types scope is
    broken down by type so the user sees Requirements/Tasks before confirming."""
    n = len(bug_ids)
    if item_type is not None:
        return f"all {n} {item_type}{'' if n == 1 else 's'}"
    rows = db.execute(
        select(Bug.item_type, func.count(Bug.id))
        .where(Bug.id.in_(bug_ids))
        .group_by(Bug.item_type)
    ).all()
    parts = [f"{int(c)} {t}{'' if int(c) == 1 else 's'}" for t, c in sorted(rows)]
    noun = "work item" if n == 1 else "work items"
    return f"all {n} {noun} ({', '.join(parts)})" if parts else f"all {n} {noun}"


def _bulk_summary(kind: str, value: Optional[str], names: str, scope: str) -> str:
    if kind == "assign":
        return f"Assign {names} to {scope}"
    if kind == "unassign":
        return f"Unassign {names} from {scope}"
    if kind == "set_status":
        return f"Set status of {scope} to {value}"
    return f"Set priority of {scope} to {value}"


# "Remove/clear the assignees" — an unassign with no named user (clear everyone).
# The status/priority words that scope it ("...from closed bugs") otherwise
# misfire the single-action parser into a set-status command, so this is matched
# and handled ahead of that path.
_CLEAR_ASSIGNEES_RE = re.compile(
    r"\b(?:un-?assign|de-?assign|remove|clear|drop|strip|take\s+off|pull\s+off)\b"
    r".*?\b(?:assignees?|everyone|everybody)\b",
    re.IGNORECASE | re.DOTALL,
)


def _item_type_hint(message: str) -> Optional[str]:
    """Restrict a bulk scope to the work-item type named in the message
    ("...closed bugs" → Bug), or None to sweep every type."""
    m = re.search(r"\b(bugs?|issues?|tickets?|defects?|requirements?|tasks?)\b",
                  message, re.IGNORECASE)
    return _bulk_item_type(m.group(1)) if m else None


def _stage_unassign_all(actor: User, *, bug_id: Optional[int] = None,
                        bug_ids: Optional[list[int]] = None, summary: str) -> Response:
    """Stage a confirmable 'remove all assignees' plan and return its confirm Response."""
    from app.chatbot import actions as _actions
    from app.chatbot.memory import store as _mem
    plan = _actions.ActionPlan(
        kind="unassign", actor_user_id=actor.id,
        bug_id=bug_id, bug_ids=list(bug_ids or []),
        unassign_all=True, summary_human=summary)
    _mem.stage_pending(actor.id, plan.to_dict())
    return _actions.stage_with_confirm(plan)


def _clear_assignees_bulk(message: str, db: Session, actor: User, pq) -> Response:
    """Resolve the filtered set for a clear-all-assignees command and stage it,
    or return an error Response when the scope is missing, empty, or too large."""
    has_filter = bool(pq.statuses or pq.priorities or pq.environments or pq.project_ids)
    if not (has_filter or _BULK_SCOPE_RE.search(message)):
        return Response(
            blocks=[Block("text", {"text":
                "Which items? Name a bug or a filter — e.g. "
                "*remove assignees from all closed bugs* or "
                "*clear assignees from #12*."})],
            summary="Need a scope", intent="action_invalid")

    item_type = _item_type_hint(message)
    bug_ids = _resolve_bulk_bug_ids(db, pq, item_type)
    if not bug_ids:
        scope_word = (item_type + "s") if item_type else "items"
        return Response(
            blocks=[Block("text", {"text":
                f"I couldn't find any matching {scope_word} to update."})],
            summary="No matching items", intent="action_invalid")
    if len(bug_ids) > _BULK_ACTION_CAP:
        return Response(
            blocks=[Block("text", {"text":
                f"That would affect more than {_BULK_ACTION_CAP} items, which is "
                "too many to change in one go. Please narrow it (add a project, "
                "status, priority, or environment) and try again."})],
            summary="Bulk scope too large", intent="action_invalid")

    scope = _bulk_scope_phrase(db, bug_ids, item_type)
    return _stage_unassign_all(actor, bug_ids=bug_ids,
                               summary=f"Remove all assignees from {scope}")


def _maybe_handle_clear_assignees(message: str, db: Session, actor: User, pq) -> Optional[Response]:
    """Stage a 'remove all assignees' unassign for one bug or a filtered set, or
    None when the message isn't a clear-assignees command."""
    from app.chatbot import actions as _actions
    if not _CLEAR_ASSIGNEES_RE.search(message):
        return None
    # A named user means a targeted unassign, not a clear-all — let normal flow run.
    if pq.assignee_ids:
        return None
    denied = _actions.sleuth_write_denied(actor)
    if denied:
        return Response(blocks=[Block("text", {"text": denied})],
                        summary=_WRITE_DENIED_SUMMARY, intent="action_denied")
    if pq.bug_id is not None:
        return _stage_unassign_all(actor, bug_id=pq.bug_id,
                                   summary=f"Remove all assignees from #{pq.bug_id}")
    return _clear_assignees_bulk(message, db, actor, pq)


def _maybe_handle_bulk_action(message: str, db: Session, actor: User, pq, ctx) -> Optional[Response]:
    """Catch bulk write commands and stage them as one confirmable plan, or
    None when the message isn't a bulk write so normal handling continues."""
    from app.chatbot import actions as _actions
    from app.chatbot.memory import store as _mem
    if pq.bug_id is not None:
        return None  # a specific id → not a bulk request
    scope_match = _BULK_SCOPE_RE.search(message)
    if not scope_match:
        return None
    kind, value = _bulk_kind_and_value(message, pq)
    if kind is None:
        return None

    item_type = _bulk_item_type(scope_match.group(2))

    # Sleuth writes are admin-only; surface the denial before staging anything.
    denied = _actions.sleuth_write_denied(actor)
    if denied:
        return Response(blocks=[Block("text", {"text": denied})],
                        summary=_WRITE_DENIED_SUMMARY, intent="action_denied")

    assignee_ids = list(pq.assignee_ids)
    assignee_names = list(pq.assignee_names)
    if kind in ("assign", "unassign") and not assignee_ids:
        assignee_ids, assignee_names = _resolve_trailing_assignee(message, ctx)
    if kind in ("assign", "unassign") and not assignee_ids:
        return Response(
            blocks=[Block("text", {"text":
                "Which user? Try *assign all the bugs to alice*."})],
            summary="Need an assignee", intent="action_invalid")
    if kind in ("set_status", "set_priority") and not value:
        return None  # let normal flow ask for the missing value

    bug_ids = _resolve_bulk_bug_ids(db, pq, item_type)
    if not bug_ids:
        scope_word = (item_type + "s") if item_type else "items"
        return Response(
            blocks=[Block("text", {"text":
                f"I couldn't find any matching {scope_word} to update."})],
            summary="No matching items", intent="action_invalid")
    if len(bug_ids) > _BULK_ACTION_CAP:
        # Refuse rather than stage a per-item mutation loop over too many rows.
        return Response(
            blocks=[Block("text", {"text":
                f"That would affect more than {_BULK_ACTION_CAP} items, which is "
                "too many to change in one go. Please narrow it (add a project, "
                "status, priority, or environment) and try again."})],
            summary="Bulk scope too large", intent="action_invalid")

    names = ", ".join(assignee_names) or "user(s)"
    scope = _bulk_scope_phrase(db, bug_ids, item_type)
    plan = _actions.ActionPlan(
        kind=kind, actor_user_id=actor.id, bug_ids=bug_ids,
        target_user_ids=assignee_ids,
        target_user_names=assignee_names,
        new_value=value,
        summary_human=_bulk_summary(kind, value, names, scope),
    )
    _mem.stage_pending(actor.id, plan.to_dict())
    return _actions.stage_with_confirm(plan)


def _handle_action_request(pq, actor: User) -> Response:
    """Route an action_* intent to plan-building and confirmation staging."""
    from app.chatbot import actions as _actions
    from app.chatbot.memory import store as _mem
    # Surface the denial before staging a plan that would fail at execution.
    denied = _actions.sleuth_write_denied(actor)
    if denied:
        return Response(blocks=[Block("text", {"text": denied})],
                        summary=_WRITE_DENIED_SUMMARY, intent="action_denied")
    plan, err = _build_action_plan(pq, actor)
    if plan is None:
        return Response(
            blocks=[Block("text", {"text": err})],
            summary=err[:80],
            intent="action_invalid",
        )
    # Stage the plan; the router reads it back when the user replies "yes"/"no".
    _mem.stage_pending(actor.id, plan.to_dict())
    return _actions.stage_with_confirm(plan)


def _handle_confirm_yes(db: Session, actor: User) -> Response:
    from app.chatbot import actions as _actions
    from app.chatbot.memory import store as _mem
    raw = _mem.take_pending(actor.id)
    if raw is None:
        return Response(
            blocks=[Block("text", {"text":
                "Nothing to confirm right now. Tell me what to do — for "
                "example *close bug 5* or *assign #12 to alice*"})],
            summary="No pending action",
            intent="confirm_idle",
        )
    if raw.get("kind") == "llm_tool_call":
        from app.chatbot import llm_tools_agent
        return llm_tools_agent.confirm_pending_tool_call(db, actor, raw)
    plan = _actions.ActionPlan.from_dict(raw)
    resp = _actions.execute_plan(plan, db, actor)
    # Keep last-bug-id fresh so pronouns ("close it") still resolve.
    if plan.bug_id:
        _mem.remember_bug(actor.id, plan.bug_id)
    return resp


def _handle_confirm_no(actor: User) -> Response:
    from app.chatbot.memory import store as _mem
    raw = _mem.take_pending(actor.id)
    msg = ("Cancelled — I haven't changed anything"
           if raw is not None else
           "Nothing was pending — nothing changed")
    return Response(
        blocks=[Block("text", {"text": msg})],
        summary="Cancelled",
        intent="confirm_cancel",
    )


# Staged document ingest (admin): a doc is parsed and parked on upload, created
# only on an explicit go-ahead. A go-ahead targets the STAGED items — a bare
# affirmation or "create/add … them/these/all/the bugs" — not "create a bug
# titled X" (a single-item request handled elsewhere).
_INGEST_CONFIRM_RE = re.compile(
    # Bare affirmations are anchored to the whole message so an incidental "ok"
    # in an unrelated question can't trigger a bulk create.
    r"^\s*(?:yes|yep|yeah|yup|sure|ok|okay|confirm|proceed|go ahead|do it|please do|go for it)\b[\s!.]*$"
    # Explicit object phrase; bare "make"/"it" excluded (matched "make it high priority").
    r"|\b(?:create|add|import|insert|file|save)\b.{0,14}\b(?:them|these|all|the\s+(?:bugs?|items?|tasks?|requirements?))\b",
    re.IGNORECASE,
)
_INGEST_CANCEL_RE = re.compile(
    r"\b(cancel|no|nope|discard|forget|throw|never ?mind|stop|don'?t|do not)\b",
    re.IGNORECASE,
)


def _ingest_created_response(summary: dict) -> Response:
    n = summary["created"]
    text = (
        f"✅ Done — created **{n}** work item{'' if n == 1 else 's'} in project "
        f"**{summary['project_name']}**."
    )
    items = summary["items"][:50]
    blocks = [Block("text", {"text": text})]
    if items:
        blocks.append(Block("table", {
            "headers": ["#", "Title"],
            "rows": [[f"#{it['id']}", it["title"]] for it in items],
            "row_bug_ids": [it["id"] for it in items],
        }))
    if n > len(items):
        blocks.append(Block("text", {"text": f"…and {n - len(items)} more."}))
    return Response(blocks=blocks, summary=f"Created {n} items", intent="ingest_done")


def _maybe_handle_ingest_create(message: str, db: Session, actor: User) -> Optional[Response]:
    """Act on a staged document if the message is a go-ahead or cancel, else
    None so normal chat handling proceeds."""
    from app.chatbot.memory import store as _mem
    staged = _mem.peek_ingest(actor.id)
    if not staged:
        return None
    msg = (message or "").strip().lower()
    if _INGEST_CANCEL_RE.search(msg):
        _mem.take_ingest(actor.id)
        return Response(
            blocks=[Block("text", {"text":
                "Okay — I've discarded those. Nothing was created."})],
            summary="Ingest cancelled", intent="ingest_cancelled",
        )
    if not _INGEST_CONFIRM_RE.search(msg):
        return None  # neither yes nor no — leave the staged doc parked
    data = _mem.take_ingest(actor.id)
    from app.chatbot import ingest as _ingest
    try:
        summary = _ingest.create_bugs_from_specs(
            db, data.get("specs") or [], actor, data.get("project_id"),
        )
    except ValueError as exc:
        return Response(
            blocks=[Block("text", {"text": f"I couldn't create those: {exc}"})],
            summary="Ingest failed", intent="ingest_error",
        )
    return _ingest_created_response(summary)


def _resolve_me_pronoun(pq, actor: User) -> None:
    """Resolve "me"/"mine"/"I" to the actor's id in the appropriate filter slot."""
    if not (pq.used_pronoun_me and actor is not None):
        return
    if pq.me_role == "reporter":
        if actor.id not in pq.reporter_ids:
            pq.reporter_ids.append(actor.id)
            pq.reporter_names.append(actor.name)
        return
    if actor.id not in pq.assignee_ids:
        pq.assignee_ids.append(actor.id)
        pq.assignee_names.append(actor.name)


def _empty_intent_response() -> Response:
    return Response(
        blocks=[Block("text", {"text":
            "Type a question to get started — e.g. *open bugs assigned "
            "to me* or *summary*."})],
        summary="Empty input",
        intent="empty",
    )


def _remember_detail_bug(actor: User, pq, read_only: bool) -> None:
    """Update last_bug_id for pronoun resolution. Skipped on the read-only
    cloud/agent path, so a model-chosen lookup can't repoint the pronoun target."""
    if pq.bug_id and not read_only:
        from app.chatbot.memory import store as _mem
        _mem.remember_bug(actor.id, pq.bug_id)


def _dispatch_read_intent(intent: str, db: Session, pq, actor: User, ctx,
                          read_only: bool = False, *, accessible: set[int]) -> Optional[Response]:
    """Dispatch a read-side intent to its handler, or None for unknown intents.

    read_only gates whether bug_detail updates last_bug_id: False for the normal
    flow so "show bug 5" then "close it" works; True on the cloud/agent path so a
    model-chosen lookup can't repoint the pronoun target."""
    if intent == "empty":
        return _empty_intent_response()
    if intent == "greeting":
        return _handle_greeting(actor)
    if intent == "thanks":
        return _handle_thanks()
    if intent == "help":
        return _handle_help()
    if intent == "about":
        return _handle_about(pq.raw_message or "")
    if intent == "list_users":
        return _handle_list_users(db, pq, actor)
    if intent == "list_projects":
        return _handle_list_projects(db, accessible)
    if intent == "bug_detail":
        _remember_detail_bug(actor, pq, read_only)
        return _handle_bug_detail(db, pq, accessible)
    if intent == "stats":
        return _handle_stats(db, accessible)
    if intent == "recent_activity":
        return _handle_recent_activity(db, pq, actor, accessible)
    if intent == "list_bugs":
        return _handle_list_bugs(db, pq, ctx, owner_id=actor.id, accessible=accessible)
    if intent == "report":
        return _handle_report(db, pq, actor, accessible)
    return None


def _classifier_action_invalid(pred_intent: str) -> Response:
    """Tell the user what action was guessed but ask for a more specific phrasing."""
    verb = pred_intent[len("action_"):]
    return Response(
        blocks=[Block("text", {"text":
            f"I think you want to **{verb}** "
            f"something, but I couldn't pin down which bug or who. "
            f"Try a more concrete phrasing — for example: "
            f"*assign bug 5 to alice* or *close #12*."})],
        summary=f"Classifier guessed {pred_intent}",
        intent="action_invalid",
    )


def _try_classifier(message: str, db: Session, pq, actor: User, ctx,
                    accessible: set[int]) -> Optional[Response]:
    """Layer 2 fallback: statistical classifier. Returns a Response or None."""
    from app.chatbot import classifier as _clf
    pred = _clf.predict(message)
    if pred is None:
        return None
    read_resp = _dispatch_read_intent(pred.intent, db, pq, actor, ctx, accessible=accessible)
    if read_resp is not None:
        return read_resp
    if pred.intent.startswith("action_"):
        return _classifier_action_invalid(pred.intent)
    return None


def _try_llm(message: str, db: Session, actor: User) -> Optional[Response]:
    """Layer 3 fallback: optional local LLM. Faults are swallowed so the
    chat path stays up even when the LLM is unavailable or broken."""
    try:
        from app.chatbot import llm as _llm
        if _llm.is_available():
            return _llm.try_understand(message, db, actor)
    except Exception:
        _LOGGER.exception(
            "Sleuth LLM fallback raised — swallowing and returning unknown."
        )
    return None


# Bare tokens that get canned replies without a cloud round-trip, covering the
# one-word pleasantries the greeting/help/thanks regexes don't classify.
_CLOUD_SKIP_EXACT = frozenset({
    "hi", "hello", "hey", "yo", "help", "?", "thanks", "thank you",
    "ok", "okay", "good morning", "good afternoon", "good evening",
})

# Pleasantries the parser already classifies must never take a cloud round-trip
# that could come back data-shaped (the "hi AI" → "there are no bugs" regression).
# Gating on pq.intent keeps this in sync with nlu.py; confirm_yes/no handled earlier.
_SKIP_CLOUD_INTENTS = frozenset({"greeting", "thanks", "help", "empty"})


def _cloud_available() -> bool:
    """True when the cloud layer is enabled and usable, so a rule miss can defer
    to it rather than return a canned prompt. Never raises."""
    try:
        from app.chatbot import cloud_llm as _cloud
        return _cloud.is_available()
    except Exception:
        return False


def _try_cloud_llm(message: str, db: Session, actor: User,
                   now: Optional[datetime]) -> Optional[Response]:
    """Layer 4 fallback: optional cloud LLM (Groq / OpenRouter), off unless
    enabled with a key. Read-only by construction — data questions route back
    through the SQL handlers and action_* intents are dropped in cloud_llm."""
    try:
        from app.chatbot import cloud_llm as _cloud
        if _cloud.is_available():
            return _cloud.try_understand(message, db, actor, now=now)
    except Exception:
        _LOGGER.exception(
            "Sleuth cloud fallback raised — swallowing and returning unknown."
        )
    return None


def _action_intent_step(pq, actor: User) -> tuple[Optional[Response], Optional[Response]]:
    """Route an action_* intent. Returns (response_to_return_now, deferred_fallback):
    the fallback is only set when the intent misfired and the cloud layer is live
    to take a smarter pass before the canned "which bug?" prompt is used."""
    if not pq.intent.startswith("action_"):
        return None, None
    action_resp = _handle_action_request(pq, actor)
    if action_resp.intent != "action_invalid" or not _cloud_available():
        return action_resp, None
    return None, action_resp


def _maybe_cloud_response(message: str, db: Session, actor: User, pq, now: datetime) -> Optional[Response]:
    """Route to the cloud LLM layer unless the intent/message opts out of that hop."""
    msg_norm = message.strip().lower()
    if not msg_norm or pq.intent in _SKIP_CLOUD_INTENTS or msg_norm in _CLOUD_SKIP_EXACT:
        return None
    return _try_cloud_llm(message, db, actor, now)


def execute(message: str, db: Session, actor: User,
            now: Optional[datetime] = None) -> Response:
    """Parse the message and dispatch to the right handler.

    `now` lets tests inject a fixed timestamp; defaults to UTC now.
    """
    now = now or datetime.now(timezone.utc)
    ctx = build_context(db, actor)
    pq = parse(message, ctx, now=now)
    # Project scope threaded into every read handler so restricted users can't
    # see other projects' data. Writes are already admin-only.
    accessible = accessible_project_ids(db, actor)

    _resolve_pronouns(pq, actor)
    _resolve_me_pronoun(pq, actor)

    # Staged document beats the confirm flow and cloud layer: a go-ahead means
    # create those items, nothing else.
    ingest_resp = _maybe_handle_ingest_create(message, db, actor)
    if ingest_resp is not None:
        return ingest_resp

    # Confirmation answers before anything else.
    if pq.intent == "confirm_yes":
        return _handle_confirm_yes(db, actor)
    if pq.intent == "confirm_no":
        return _handle_confirm_no(actor)

    # When the fully LLM-driven tool-calling agent is enabled, it is
    # the sole mainline engine for everything except confirm_yes/no above —
    # no deterministic rule-engine fallback runs underneath it.
    from app.chatbot import llm_tools_agent
    if llm_tools_agent.is_available():
        return llm_tools_agent.run(message, db, actor)

    # "Remove all assignees from <bug|filter>" — caught ahead of the single-action
    # handler, whose set-status detector otherwise hijacks the scoping status word.
    clear_resp = _maybe_handle_clear_assignees(message, db, actor, pq)
    if clear_resp is not None:
        return clear_resp

    # Bulk write commands are caught before the single-action handler and cloud
    # layer, then staged as one confirmable plan.
    bulk_resp = _maybe_handle_bulk_action(message, db, actor, pq, ctx)
    if bulk_resp is not None:
        return bulk_resp

    # A parsed write-intent stages a confirmable plan. But action_invalid means
    # an action verb matched without enough to act on — often an over-eager match
    # on a message that isn't really a write ("which bugs are closed?", "remove
    # the assignee from the closed ones"). When the cloud layer is live, let it
    # take a turn instead of dead-ending on the canned "which bug?" prompt; keep
    # that prompt as the last resort if the smarter layers below all pass.
    action_now, action_fallback = _action_intent_step(pq, actor)
    if action_now is not None:
        return action_now

    # When enabled, the cloud layer leads for anything that isn't a confirmation
    # or explicit write: keyword rules misfire on free-form chat ("can you revoke
    # someone's session?" would return an admins table), so it routes data
    # questions into canonical queries through the same SQL handlers. Pleasantries
    # skip the hop (gated on pq.intent); cloud faults fall back to the chain below.
    cloud_resp = _maybe_cloud_response(message, db, actor, pq, now)
    if cloud_resp is not None:
        return cloud_resp

    read_resp = _dispatch_read_intent(pq.intent, db, pq, actor, ctx, accessible=accessible)
    if read_resp is not None:
        return read_resp

    classifier_resp = _try_classifier(message, db, pq, actor, ctx, accessible)
    if classifier_resp is not None:
        return classifier_resp

    llm_resp = _try_llm(message, db, actor)
    if llm_resp is not None:
        return llm_resp

    return action_fallback or _handle_unknown(message)


__all__ = ["Block", "Response", "execute", "build_context"]
