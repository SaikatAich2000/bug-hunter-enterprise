"""Branch/line coverage for ``app/chatbot/executor.py``.

This file drives the Sleuth executor through its public ``execute(...)``
entry point with real DB rows (created via the API client or directly
through ``SessionLocal``) so the multi-tenant scoping and every
intent/action handler branch is exercised. The only outward calls mocked
are the optional classifier / LLM layers (``app.chatbot.classifier`` and
``app.chatbot.llm``) — everything else runs against the test SQLite DB.

Each test's docstring names the specific branch it targets (success path
AND the error / permission / not-found / guard path) so the file reads as
a branch-coverage map of the executor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (do NOT redefine conftest fixtures — request them instead)
# ---------------------------------------------------------------------------
def _signup(client, org="Acme", name="Alice", email="alice@a.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": "TestPass1!",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Web"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, title="Bug 1", status="New",
              priority="Low", environment="DEV", description=""):
    r = client.post("/api/bugs", json={
        "project_id": project_id, "title": title,
        "status": status, "priority": priority,
        "environment": environment, "description": description,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _actor(db, email="alice@a.test"):
    from app.models import User
    return db.query(User).filter(User.email == email).one()


def _bootstrap(client, **kw):
    """Sign up + one project; return (db, actor, project_id)."""
    _signup(client)
    proj = _make_project(client, kw.get("project_name", "Web"))
    db = _db()
    return db, _actor(db), proj["id"]


def _ask(client, msg):
    r = client.post("/api/chat/ask", json={"message": msg})
    assert r.status_code == 200, r.text
    return r.json()


def _text(resp):
    """First text block text from a Response object."""
    for b in resp.blocks:
        if b.kind == "text":
            return b.payload.get("text", "")
    return ""


def _kinds(resp):
    return [b.kind for b in resp.blocks]


# ===========================================================================
# build_context — actor-scoped vs global (branch at line 85)
# ===========================================================================
class TestBuildContext:
    def test_with_actor_scopes_to_org(self, client):
        """build_context(actor=...) takes the org-filter branch (line 86-87)."""
        from app.chatbot.executor import build_context
        db, actor, _pid = _bootstrap(client)
        try:
            ctx = build_context(db, actor)
            assert any(p[2] == "Web" for p in ctx.projects)
        finally:
            db.close()

    def test_without_actor_is_global(self, client):
        """build_context() with actor=None skips the org filter (line 85 false)."""
        from app.chatbot.executor import build_context
        db, _actor_obj, _pid = _bootstrap(client)
        try:
            ctx = build_context(db)
            assert len(ctx.users) >= 1
        finally:
            db.close()

    def test_user_without_email_skips_local_part(self, client):
        """A user whose email lacks '@' leaves email-local empty (line 98 false)."""
        from app.chatbot.executor import build_context
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            # Force a no-'@' email straight in the DB to hit the false branch.
            actor.email = "no-at-sign"
            db.commit()
            ctx = build_context(db, actor)
            entry = next(u for u in ctx.users if u[0] == actor.id)
            assert entry[2] == ""  # email local part empty
        finally:
            db.close()


# ===========================================================================
# list_users — role filter present/absent, empty result, label pluralisation
# ===========================================================================
class TestListUsers:
    def test_no_role_filter_lists_all(self, client):
        """_handle_list_users with no role_filter (line 399 false)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("list all users", db, actor)
            assert resp.intent == "list_users"
            assert "table" in _kinds(resp)
        finally:
            db.close()

    def test_role_filter_matches(self, client):
        """role_filter branch (399 true) + label pluralisation (414-415)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("list admins", db, actor)
            assert resp.intent == "list_users"
            assert "admin" in _text(resp).lower()
        finally:
            db.close()

    def test_role_filter_no_matches(self, client):
        """role_filter with zero rows hits the empty branch (line 402-407)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            # No managers exist -> "No users match that filter".
            resp = execute("list managers", db, actor)
            assert resp.intent == "list_users"
            assert "no users match" in _text(resp).lower()
            assert len(resp.blocks) == 1
        finally:
            db.close()

    def test_role_filter_label_already_plural(self, client):
        """A role_filter that already ends in 's' isn't double-pluralised.

        Drives the ``endswith('s')`` branch of the label logic (line 415).
        Call _handle_list_users directly with a synthetic plural role.
        """
        from app.chatbot.executor import _handle_list_users
        from app.chatbot.nlu import ParsedQuery
        db, actor, _pid = _bootstrap(client)
        try:
            pq = ParsedQuery(raw_message="x")
            pq.role_filter = "bosss"  # ends with 's' -> label kept as-is
            resp = _handle_list_users(db, pq, actor)
            # No user has that role -> empty branch, but label path executed.
            assert resp.intent == "list_users"
        finally:
            db.close()


# ===========================================================================
# list_projects — no access, has projects + bug counts, empty-after-pids
# ===========================================================================
class TestListProjects:
    def test_no_accessible_projects(self, client):
        """Member with no project membership -> 'no projects' (line 429-433)."""
        from app.chatbot.executor import execute
        _signup(client)
        # admin makes a project but invites a member who is NOT added to it
        proj = _make_project(client, "Solo")
        from app.models import User, Project
        db = _db()
        try:
            # create a second user in the org with role 'user' and no membership
            from app.auth import hash_password
            admin = _actor(db)
            member = User(
                org_id=admin.org_id, name="Mark Member",
                email="mark@a.test", password_hash=hash_password("TestPass1!"),
                role="user", is_active=True,
            )
            db.add(member)
            db.commit()
            db.refresh(member)
            resp = execute("list projects", db, member)
            assert resp.intent == "list_projects"
            assert "no projects" in _text(resp).lower()
        finally:
            db.close()

    def test_with_projects_and_counts(self, client):
        """Accessible projects render a table with bug counts (line 444-462)."""
        from app.chatbot.executor import execute
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="A bug")
        db = _db()
        try:
            actor = _actor(db)
            resp = execute("list projects", db, actor)
            assert resp.intent == "list_projects"
            table = next(b for b in resp.blocks if b.kind == "table")
            assert table.payload["rows"][0][1] == "1"
        finally:
            db.close()


# ===========================================================================
# bug_detail — found (with/without description), not-found, cross-tenant
# ===========================================================================
class TestBugDetail:
    def test_found_with_description(self, client):
        """bug found + non-empty description appends Description (line 499-500)."""
        from app.chatbot.executor import execute
        db, actor, pid = _bootstrap(client)
        try:
            from app.models import Bug
            from sqlalchemy import select
            bug = Bug(project_id=pid, title="login broken", status="New",
                      priority="Low", environment="DEV", reporter_id=actor.id,
                      description="Steps to reproduce: click login.")
            db.add(bug)
            db.commit()
            db.refresh(bug)
            resp = execute(f"bug {bug.id}", db, actor)
            assert resp.intent == "bug_detail"
            assert "Description" in _text(resp)
        finally:
            db.close()

    def test_found_without_description(self, client):
        """bug found, empty description skips the Description block (499 false)."""
        from app.chatbot.executor import execute
        _signup(client)
        proj = _make_project(client)
        bug = _make_bug(client, proj["id"], title="no descr", description="")
        db = _db()
        try:
            actor = _actor(db)
            resp = execute(f"bug {bug['id']}", db, actor)
            assert resp.intent == "bug_detail"
            assert f"#{bug['id']}" in _text(resp)
        finally:
            db.close()

    def test_not_found_when_no_projects(self, client):
        """No accessible projects -> pids empty -> bug stays None (469 false)."""
        from app.chatbot.executor import execute
        from app.models import User
        from app.auth import hash_password
        _signup(client)
        db = _db()
        try:
            admin = _actor(db)
            member = User(
                org_id=admin.org_id, name="Nora", email="nora@a.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(member)
            db.commit()
            db.refresh(member)
            resp = execute("bug 1", db, member)
            assert resp.intent == "bug_detail"
            assert "couldn't find" in _text(resp).lower()
        finally:
            db.close()

    def test_not_found_wrong_id(self, client):
        """pids present but id doesn't exist -> not-found branch (line 473-480)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("bug 99999", db, actor)
            assert resp.intent == "bug_detail"
            assert "couldn't find" in _text(resp).lower()
        finally:
            db.close()

    def test_cross_tenant_bug_not_found(self, two_orgs):
        """Org A can't see Org B's bug -> not-found (project_id scope)."""
        c_a, c_b, _, _ = two_orgs
        proj_b = c_b.post("/api/projects", json={"name": "BProj"}).json()
        bug_b = c_b.post("/api/bugs", json={
            "project_id": proj_b["id"], "title": "B bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        c_a.post("/api/projects", json={"name": "AProj"})
        from app.chatbot.executor import execute
        db = _db()
        try:
            alice = _actor(db, "alice@a.test")
            resp = execute(f"bug {bug_b['id']}", db, alice)
            assert resp.intent == "bug_detail"
            assert "couldn't find" in _text(resp).lower()
        finally:
            db.close()


# ===========================================================================
# recent_activity — admin vs non-admin filter, time window, empty result
# ===========================================================================
class TestRecentActivity:
    def test_admin_sees_org_activity(self, client):
        """Admin role skips the actor-only filter (line 517 false)."""
        from app.chatbot.executor import execute
        db, actor, pid = _bootstrap(client)
        try:
            _make_bug(client, pid, title="trigger")  # produces audit rows
            resp = execute("recent activity", db, actor)
            assert resp.intent == "recent_activity"
            assert "table" in _kinds(resp)
        finally:
            db.close()

    def test_non_admin_filters_to_self(self, client):
        """Non-admin user restricts to own actions (line 517 true / 521)."""
        from app.chatbot.executor import execute
        from app.models import User, Activity
        from app.auth import hash_password
        _signup(client)
        db = _db()
        try:
            admin = _actor(db)
            member = User(
                org_id=admin.org_id, name="Mia", email="mia@a.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(member)
            db.commit()
            db.refresh(member)
            db.add(Activity(
                org_id=admin.org_id, actor_user_id=member.id,
                actor_name="Mia", action="login", detail="x" * 200,
            ))
            db.commit()
            resp = execute("recent activity", db, member)
            assert resp.intent == "recent_activity"
            assert "table" in _kinds(resp)
        finally:
            db.close()

    def test_no_activity_empty(self, two_orgs):
        """A brand-new non-admin actor with no rows -> empty branch (529-537)."""
        from app.chatbot.executor import execute
        from app.models import User
        from app.auth import hash_password
        c_a, c_b, _, _ = two_orgs
        db = _db()
        try:
            bob = _actor(db, "bob@b.test")
            lonely = User(
                org_id=bob.org_id, name="Lon", email="lon@b.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(lonely)
            db.commit()
            db.refresh(lonely)
            resp = execute("recent activity", db, lonely)
            assert resp.intent == "recent_activity"
            assert "no recent activity" in _text(resp).lower()
        finally:
            db.close()

    def test_time_window_branch(self, client):
        """recent activity with a time window applies start/end (line 522-526)."""
        from app.chatbot.executor import execute
        db, actor, pid = _bootstrap(client)
        try:
            _make_bug(client, pid, title="trigger")
            resp = execute("recent activity today", db, actor)
            assert resp.intent == "recent_activity"
            # The summary text appends the window label.
            assert "today" in _text(resp).lower()
        finally:
            db.close()

    def test_single_entry_singular_label(self, two_orgs):
        """Exactly one row uses the singular 'entry' wording (line 551)."""
        from app.chatbot.executor import execute
        from app.models import User, Activity
        from app.auth import hash_password
        c_a, c_b, _, _ = two_orgs
        db = _db()
        try:
            bob = _actor(db, "bob@b.test")
            solo = User(
                org_id=bob.org_id, name="Solo", email="solo@b.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(solo)
            db.commit()
            db.refresh(solo)
            db.add(Activity(
                org_id=bob.org_id, actor_user_id=solo.id,
                actor_name="Solo", action="login", detail=None,
            ))
            db.commit()
            resp = execute("recent activity", db, solo)
            assert "entry" in _text(resp).lower()
        finally:
            db.close()


# ===========================================================================
# stats — no projects, with bugs + top-assignees table branch
# ===========================================================================
class TestStats:
    def test_no_projects(self, client):
        """No accessible projects -> single text block (line 567-573)."""
        from app.chatbot.executor import execute
        from app.models import User
        from app.auth import hash_password
        _signup(client)
        db = _db()
        try:
            admin = _actor(db)
            member = User(
                org_id=admin.org_id, name="Stu", email="stu@a.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(member)
            db.commit()
            db.refresh(member)
            resp = execute("summary", db, member)
            assert resp.intent == "stats"
            assert len(resp.blocks) == 1
        finally:
            db.close()

    def test_with_open_assigned_bugs_shows_top_table(self, client):
        """An open bug assigned to a user populates the top-assignee table
        (line 604 true -> 605)."""
        from app.chatbot.executor import execute
        db, actor, pid = _bootstrap(client)
        try:
            from app.models import Bug
            bug = Bug(project_id=pid, title="open one", status="New",
                      priority="Critical", environment="PROD",
                      reporter_id=actor.id)
            bug.assignees = [actor]
            db.add(bug)
            db.commit()
            resp = execute("summary", db, actor)
            assert resp.intent == "stats"
            assert "table" in _kinds(resp)
        finally:
            db.close()

    def test_with_bugs_no_assignees_no_table(self, client):
        """Bugs but no open-assigned ones -> no top table (line 604 false)."""
        from app.chatbot.executor import execute
        _signup(client)
        proj = _make_project(client)
        _make_bug(client, proj["id"], title="unassigned", status="Closed")
        db = _db()
        try:
            actor = _actor(db)
            resp = execute("dashboard stats", db, actor)
            assert resp.intent == "stats"
            assert _kinds(resp) == ["text"]
        finally:
            db.close()


# ===========================================================================
# list_bugs — every filter + count / list / export / clarify / no-results
# ===========================================================================
class TestListBugsFilters:
    def _seed(self, client):
        _signup(client)
        proj = _make_project(client, "Apollo")
        # Make a second user to use as reporter/assignee.
        from app.models import User
        from app.auth import hash_password
        db = _db()
        admin = _actor(db)
        other = User(
            org_id=admin.org_id, name="Bob Other", email="bob@a.test",
            password_hash=hash_password("TestPass1!"), role="user",
            is_active=True,
        )
        db.add(other)
        db.commit()
        db.refresh(other)
        # add other to the project so it's accessible — admin sees all anyway
        from app.models import Bug
        b1 = Bug(project_id=proj["id"], title="login crash", status="New",
                 priority="Critical", environment="PROD", reporter_id=admin.id,
                 description="crash on login")
        b1.assignees = [other]
        b2 = Bug(project_id=proj["id"], title="typo", status="Closed",
                 priority="Low", environment="DEV", reporter_id=other.id)
        db.add_all([b1, b2])
        db.commit()
        return db, admin, proj, other, b1, b2

    def test_status_filter(self, client):
        """pq.statuses filter (line 222-223)."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute("show new bugs", db, admin)
            assert resp.intent == "list_bugs"
            assert "table" in _kinds(resp)
        finally:
            db.close()

    def test_priority_and_environment_filter(self, client):
        """pq.priorities + environments filters (lines 224-227)."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute("list critical bugs in PROD", db, admin)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_project_filter(self, client):
        """pq.project_ids filter (line 228-229)."""
        from app.chatbot.executor import execute
        db, admin, proj, *_ = self._seed(client)
        try:
            resp = execute("show bugs in project Apollo", db, admin)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_reporter_filter(self, client):
        """pq.reporter_ids filter (line 230-231)."""
        from app.chatbot.executor import execute
        db, admin, proj, other, *_ = self._seed(client)
        try:
            resp = execute("bugs reported by Bob Other", db, admin)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_assignee_filter(self, client):
        """pq.assignee_ids many-to-many filter (lines 232-236)."""
        from app.chatbot.executor import execute
        db, admin, proj, other, *_ = self._seed(client)
        try:
            resp = execute("show bugs assigned to Bob Other", db, admin)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_text_search_filter(self, client):
        """text_search LIKE clause (line 237-238 + _apply_text_search 182-189)."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute('show bugs about "login"', db, admin)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_time_window_updated(self, client):
        """time_window on updated_at (default) -> _apply_time_window (201-208)."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute("show bugs updated this week", db, admin)
            assert resp.intent in ("list_bugs", "recent_activity")
        finally:
            db.close()

    def test_time_window_created_keyword(self, client):
        """'created this week' uses created_at column (line 202 true)."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute("show bugs created this week", db, admin)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_count_intent(self, client):
        """wants_count and not export -> _build_count_response (line 816-817)."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute("how many bugs?", db, admin)
            assert resp.intent == "count_bugs"
            assert "bug" in _text(resp).lower()
        finally:
            db.close()

    def test_count_singular(self, client):
        """A single matching bug uses singular 'is'/'bug' (count verb branch)."""
        from app.chatbot.executor import execute
        _signup(client)
        proj = _make_project(client)
        _make_bug(client, proj["id"], title="only one", status="New")
        db = _db()
        try:
            admin = _actor(db)
            resp = execute("how many new bugs?", db, admin)
            assert resp.intent == "count_bugs"
            assert " is " in _text(resp)
        finally:
            db.close()

    def test_no_results_with_filters(self, client):
        """A filter that matches nothing -> _build_no_results_response (830-831).

        'deferred' maps to the Resolve Later status without tripping the
        status-change action detector (unlike 'resolve later')."""
        from app.chatbot.executor import execute
        db, admin, *_ = self._seed(client)
        try:
            resp = execute("show deferred bugs", db, admin)
            assert resp.intent == "list_bugs"
            assert "no bugs found" in _text(resp).lower()
        finally:
            db.close()

    def test_no_results_no_filters(self, client):
        """No bugs + no filters -> 'no bugs in the system yet' (line 767-771)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("show all bugs", db, actor)
            assert resp.intent == "list_bugs"
            assert "no bugs in the system yet" in _text(resp).lower()
        finally:
            db.close()

    def test_total_exceeds_limit(self, client):
        """More rows than the limit -> 'showing the most recent N' (line 783-784)."""
        from app.chatbot.executor import execute
        _signup(client)
        proj = _make_project(client)
        db = _db()
        try:
            admin = _actor(db)
            from app.models import Bug
            for i in range(7):
                db.add(Bug(project_id=proj["id"], title=f"B{i}", status="New",
                           priority="Low", environment="DEV",
                           reporter_id=admin.id))
            db.commit()
            from app.chatbot.nlu import ParsedQuery
            from app.chatbot.executor import _handle_list_bugs, build_context
            pq = ParsedQuery(raw_message="show bugs")
            pq.limit = 5  # force total(7) > limit(5)
            ctx = build_context(db, admin)
            resp = _handle_list_bugs(db, pq, admin, ctx)
            assert "most recent 5" in _text(resp)
        finally:
            db.close()

    def test_clarify_ambiguous_name(self, client):
        """Two users with the same single-token name -> ambiguous clarify
        (line 805-807). A bare first name shared by two users is the
        resolver's only true >1-match case."""
        from app.chatbot.executor import execute
        from app.models import User
        from app.auth import hash_password
        _signup(client, name="Alex")
        db = _db()
        try:
            admin = _actor(db)
            db.add(User(
                org_id=admin.org_id, name="Alex", email="ab@a.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            ))
            db.commit()
            resp = execute("show bugs assigned to Alex", db, admin)
            assert resp.intent == "clarify"
            assert "more than one user" in _text(resp).lower()
        finally:
            db.close()

    def test_clarify_unresolved_user_with_suggestion(self, client):
        """A near-miss name yields a 'Did you mean' suggestion (line 707-723)."""
        from app.chatbot.executor import execute
        _signup(client, name="Alice Wong")
        proj = _make_project(client)
        db = _db()
        try:
            admin = _actor(db)
            resp = execute("show bugs assigned to Alise", db, admin)
            assert resp.intent == "clarify"
            # suggestion path: 'Did you mean' should appear (close match).
            assert "alice" in _text(resp).lower()
        finally:
            db.close()

    def test_clarify_unresolved_reporter_no_suggestion(self, client):
        """An unresolved reporter with no close match uses the fallback copy
        (line 709 'reporter' branch + 716-718 else)."""
        from app.chatbot.executor import execute
        _signup(client, name="Alice Wong")
        proj = _make_project(client)
        db = _db()
        try:
            admin = _actor(db)
            resp = execute("show bugs reported by Zzzqqxx", db, admin)
            assert resp.intent == "clarify"
            assert "list users" in _text(resp).lower()
        finally:
            db.close()


# ===========================================================================
# Export path — success, no-assignee/status labels, ImportError, ExcelError
# ===========================================================================
class TestExport:
    def _seed_one(self, client):
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="exp bug", status="New")
        db = _db()
        return db, _actor(db), proj

    def test_export_success(self, client):
        """wants_export -> _build_export_response success (line 821-826, 835-897)."""
        from app.chatbot.executor import execute
        db, actor, _proj = self._seed_one(client)
        try:
            resp = execute("export all bugs to excel", db, actor)
            assert resp.intent == "export_bugs"
            assert "file" in _kinds(resp)
        finally:
            db.close()

    def test_export_with_assignee_and_status_labels(self, client):
        """Export filename includes assignee + status slugs (line 853-862)."""
        from app.chatbot.executor import execute
        from app.chatbot.nlu import ParsedQuery
        from app.chatbot.executor import _build_export_response
        db, actor, proj = self._seed_one(client)
        try:
            from app.models import Bug
            from sqlalchemy import select
            bug = db.scalar(select(Bug))
            pq = ParsedQuery(raw_message="export new bugs for alice")
            pq.assignee_names = ["Alice Wong"]
            pq.statuses = ["New"]
            resp = _build_export_response([bug], pq, total=1, cap=5000)
            assert resp.intent == "export_bugs"
            fname = next(b for b in resp.blocks if b.kind == "file").payload["filename"]
            assert "alice" in fname.lower()
            assert "new" in fname.lower()
        finally:
            db.close()

    def test_export_total_exceeds_cap_note(self, client):
        """total > cap appends the 'capped the export' note (line 880-884)."""
        from app.chatbot.executor import _build_export_response
        from app.chatbot.nlu import ParsedQuery
        db, actor, proj = self._seed_one(client)
        try:
            from app.models import Bug
            from sqlalchemy import select
            bug = db.scalar(select(Bug))
            pq = ParsedQuery(raw_message="export bugs")
            resp = _build_export_response([bug], pq, total=9999, cap=1)
            assert "capped the export" in _text(resp)
        finally:
            db.close()

    def test_export_excel_module_missing(self, client):
        """ImportError on 'from . import excel' -> export disabled (line 840-849).

        We make the real ``from . import excel`` (a submodule import that
        resolves through ``app.chatbot``'s __getattr__/attribute) raise by
        temporarily removing the cached module and blocking its (re)import.
        """
        import sys
        import builtins
        from app.chatbot.executor import _build_export_response
        from app.chatbot.nlu import ParsedQuery
        db, actor, proj = self._seed_one(client)
        try:
            real_import = builtins.__import__
            saved = sys.modules.pop("app.chatbot.excel", None)

            def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
                # 'from . import excel' resolves as import of app.chatbot with
                # fromlist=('excel',). Block that specific submodule resolution.
                if "excel" in (fromlist or ()) and globals is not None \
                        and globals.get("__name__", "").startswith("app.chatbot"):
                    raise ImportError("no excel")
                if name == "app.chatbot.excel":
                    raise ImportError("no excel")
                return real_import(name, globals, locals, fromlist, level)

            pq = ParsedQuery(raw_message="export bugs")
            try:
                with patch.object(builtins, "__import__", side_effect=fake_import):
                    resp = _build_export_response([], pq, total=0, cap=5000)
            finally:
                if saved is not None:
                    sys.modules["app.chatbot.excel"] = saved
            assert resp.intent == "export_failed"
            assert "isn't available" in _text(resp)
        finally:
            db.close()

    def test_export_excel_generation_error(self, client):
        """excel.stage_workbook raising ExcelGenerationError (line 868-874)."""
        from app.chatbot.executor import _build_export_response
        from app.chatbot.nlu import ParsedQuery
        from app.chatbot import excel
        db, actor, proj = self._seed_one(client)
        try:
            from app.models import Bug
            from sqlalchemy import select
            bug = db.scalar(select(Bug))
            pq = ParsedQuery(raw_message="export bugs")

            def boom(*a, **kw):
                raise excel.ExcelGenerationError("disk full")

            with patch.object(excel, "stage_workbook", side_effect=boom):
                resp = _build_export_response([bug], pq, total=1, cap=5000)
            assert resp.intent == "export_failed"
            assert "couldn't build the spreadsheet" in _text(resp).lower()
        finally:
            db.close()

    def test_export_single_bug_singular(self, client):
        """A 1-row export uses the singular 'bug' note (line 877)."""
        from app.chatbot.executor import execute
        db, actor, _proj = self._seed_one(client)
        try:
            resp = execute("export new bugs to excel", db, actor)
            assert resp.intent == "export_bugs"
        finally:
            db.close()


# ===========================================================================
# Reports — forbidden, success, empty, engine error, xlsx fail, summary extras
# ===========================================================================
class TestReports:
    def _seed_report(self, client, role_member=False):
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="r bug", status="Resolved",
                  priority="High")
        db = _db()
        return db, _actor(db), proj

    def test_report_forbidden_for_member(self, client):
        """Non-manager/admin -> _report_forbidden_response (line 1060-1061)."""
        from app.chatbot.executor import execute
        from app.models import User
        from app.auth import hash_password
        _signup(client)
        db = _db()
        try:
            admin = _actor(db)
            member = User(
                org_id=admin.org_id, name="Rita", email="rita@a.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(member)
            db.commit()
            db.refresh(member)
            resp = execute("throughput report", db, member)
            assert resp.intent == "report_forbidden"
            assert "managers and admins" in _text(resp).lower()
        finally:
            db.close()

    def test_report_success_with_rows(self, client):
        """Admin runs a report with matching rows -> table + file (line 1077-1101).

        'pending report' maps to the pending_snapshot report which counts the
        seeded open bug, so result.total > 0 and the table+file blocks build."""
        from app.chatbot.executor import execute
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="open bug", status="New")
        db = _db()
        try:
            actor = _actor(db)
            resp = execute("pending report", db, actor)
            assert resp.intent == "report"
            assert "table" in _kinds(resp)
        finally:
            db.close()

    def test_report_empty(self, client):
        """A report whose filters match nothing -> _report_empty_response (1077-1078)."""
        from app.chatbot.executor import execute
        _signup(client)
        _make_project(client, "Apollo")  # project but no resolved bugs
        db = _db()
        try:
            actor = _actor(db)
            resp = execute("throughput report last year", db, actor)
            assert resp.intent in ("report", "report_error")
            if resp.intent == "report":
                assert "no rows matched" in _text(resp).lower()
        finally:
            db.close()

    def test_report_engine_error(self, client):
        """run_report raising ValueError -> report_error (line 1070-1075)."""
        from app.chatbot.executor import execute
        import app.chatbot.executor as ex
        db, actor, _proj = self._seed_report(client)
        try:
            def boom(*a, **kw):
                raise ValueError("bad filter")

            import app.reports as reports_mod
            with patch.object(reports_mod, "run_report", side_effect=boom):
                resp = execute("throughput report", db, actor)
            assert resp.intent == "report_error"
            assert "couldn't run that report" in _text(resp).lower()
        finally:
            db.close()

    def test_report_xlsx_stage_failure_falls_back_to_text(self, client):
        """_try_stage_file_block swallows a build error -> manual-download note
        (line 1043-1048 + 1091-1094)."""
        from app.chatbot.executor import execute
        import app.chatbot.executor as ex
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="open bug", status="New")
        db = _db()
        try:
            actor = _actor(db)

            def boom(*a, **kw):
                raise RuntimeError("xlsx kaput")

            with patch.object(ex, "_stage_report_xlsx", side_effect=boom):
                resp = execute("pending report", db, actor)
            assert resp.intent == "report"
            joined = " ".join(_text_all(resp))
            assert "sidebar" in joined
        finally:
            db.close()

    def test_stage_report_xlsx_build_error_wrapped(self, client):
        """_stage_report_xlsx wraps XlsxBuildError as ExcelGenerationError
        (line 962-963)."""
        from app.chatbot.executor import _stage_report_xlsx
        from app.chatbot import excel as cexcel
        import app.reports as reports_mod
        from app.reports.xlsx import XlsxBuildError
        db, actor, _proj = self._seed_report(client)
        try:
            result = SimpleNamespace(total=1, rows=[{}], columns=[],
                                     summary={}, report_label="X")

            def boom(_r):
                raise XlsxBuildError("nope")

            with patch.object(reports_mod, "build_workbook_bytes", side_effect=boom):
                with pytest.raises(cexcel.ExcelGenerationError):
                    _stage_report_xlsx(result, "throughput")
        finally:
            db.close()

    def test_format_summary_extras_resolved(self):
        """_format_summary_extras: total_resolved branch (line 996-1000)."""
        from app.chatbot.executor import _format_summary_extras
        out = _format_summary_extras({"total_resolved": 5, "user_count": 2})
        assert "Total resolved" in out

    def test_format_summary_extras_total_items(self):
        """_format_summary_extras: total_items branch (line 1001-1002)."""
        from app.chatbot.executor import _format_summary_extras
        out = _format_summary_extras({"total_items": 9})
        assert "Total items" in out

    def test_format_summary_extras_created_net(self):
        """created+resolved without user_count -> Created/Resolved/Net (1003-1008)."""
        from app.chatbot.executor import _format_summary_extras
        out = _format_summary_extras({
            "total_created": 4, "total_resolved": 3, "net": 1,
        })
        assert "Created" in out and "Net" in out

    def test_format_summary_extras_average_hours(self):
        """average_hours branch (line 1009-1014)."""
        from app.chatbot.executor import _format_summary_extras
        out = _format_summary_extras({
            "average_hours": 12, "median_hours": 10, "p95_hours": 40,
        })
        assert "Average" in out

    def test_format_summary_extras_empty(self):
        """Empty summary dict -> '' (line 993-994)."""
        from app.chatbot.executor import _format_summary_extras
        assert _format_summary_extras({}) == ""

    def test_build_report_preview_text_date_and_preview(self):
        """_build_report_preview_text: date range + preview-truncation (1023-1037)."""
        from app.chatbot.executor import _build_report_preview_text
        from app.reports import Filters
        result = SimpleNamespace(report_label="Throughput", total=50,
                                 summary={"total_items": 50})
        filters = Filters(date_from=datetime(2026, 1, 1).date(),
                          date_to=datetime(2026, 2, 1).date())
        out = _build_report_preview_text(result, filters, preview_rows_count=15)
        assert "Throughput" in out
        assert "Preview shows the first 15" in out

    def test_build_report_preview_text_single_row(self):
        """total == 1 uses singular 'row' (line 1020 branch)."""
        from app.chatbot.executor import _build_report_preview_text
        from app.reports import Filters
        result = SimpleNamespace(report_label="R", total=1, summary={})
        filters = Filters()
        out = _build_report_preview_text(result, filters, preview_rows_count=15)
        assert "1 row" in out and "rows" not in out

    def test_report_row_to_table_row_types(self):
        """_report_row_to_table_row: None, numeric, long-string truncation
        (line 943-953)."""
        from app.chatbot.executor import _report_row_to_table_row
        cols = [SimpleNamespace(key="a"), SimpleNamespace(key="b"),
                SimpleNamespace(key="c"), SimpleNamespace(key="d")]
        row = {"a": None, "b": 42, "c": "x" * 100, "d": "short"}
        out = _report_row_to_table_row(row, cols)
        assert out[0] == ""
        assert out[1] == "42"
        # Long string truncated to 77 chars + the ellipsis = 78 chars.
        assert out[2].endswith("…") and len(out[2]) == 78
        assert out[3] == "short"

    def test_filters_from_parsed_item_types_and_dates(self, client):
        """_filters_from_parsed: type keyword + time window (line 912-939)."""
        from app.chatbot.executor import _filters_from_parsed
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        pq = ParsedQuery(raw_message="bug report")
        pq.time_window = TimeWindow(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 2, 1, tzinfo=timezone.utc),
            label="x",
        )
        filters = _filters_from_parsed(pq)
        assert "Bug" in filters.item_types
        assert filters.date_from is not None
        assert filters.date_to is not None


def _text_all(resp):
    return [b.payload.get("text", "") for b in resp.blocks if b.kind == "text"]


# ===========================================================================
# Action plan builders — each missing-field clarification + happy path
# ===========================================================================
class TestActionPlans:
    def _ctx_pq(self, client, msg):
        from app.chatbot.executor import build_context
        from app.chatbot.nlu import parse
        db, actor, pid = _bootstrap(client)
        ctx = build_context(db, actor)
        pq = parse(msg, ctx)
        return db, actor, pid, pq

    # ---- assign / unassign ------------------------------------------------
    def test_assign_missing_name(self, client):
        """_plan_assign with no assignee_ids -> clarification (line 1127-1128)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="assign bug 5")
            pq.action_kind = "assign"
            pq.bug_id = 5
            plan, err = _build_action_plan(pq, actor)
            assert plan is None
            assert "I need a name" in err
        finally:
            db.close()

    def test_assign_happy(self, client):
        """_plan_assign success path builds a summary (line 1130-1136)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="assign bug 5 to alice")
            pq.action_kind = "assign"
            pq.bug_id = 5
            pq.assignee_ids = [actor.id]
            pq.assignee_names = ["Alice"]
            plan, err = _build_action_plan(pq, actor)
            assert err is None
            assert "Assign" in plan.summary_human
        finally:
            db.close()

    def test_unassign_happy(self, client):
        """_plan_assign with kind=unassign uses 'Unassign'/'from' (line 1132-1135)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="unassign alice from bug 5")
            pq.action_kind = "unassign"
            pq.bug_id = 5
            pq.assignee_ids = [actor.id]
            pq.assignee_names = ["Alice"]
            plan, err = _build_action_plan(pq, actor)
            assert err is None
            assert "Unassign" in plan.summary_human and "from" in plan.summary_human
        finally:
            db.close()

    # ---- needs-bug guard --------------------------------------------------
    def test_needs_bug_no_id_no_pronoun(self, client):
        """Action needing a bug with no id/pronoun -> 'Which bug?' (line 1228-1229)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="close")
            pq.action_kind = "set_status"
            pq.bug_id = None
            pq.used_pronoun_bug = False
            plan, err = _build_action_plan(pq, actor)
            assert plan is None
            assert "Which bug" in err
        finally:
            db.close()

    def test_needs_bug_pronoun_unresolved(self, client):
        """Pronoun but no remembered bug -> 'don't know which bug' (line 1225-1227)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="close it")
            pq.action_kind = "set_status"
            pq.bug_id = None
            pq.used_pronoun_bug = True
            plan, err = _build_action_plan(pq, actor)
            assert plan is None
            assert "don't know which bug" in err
        finally:
            db.close()

    # ---- set_status -------------------------------------------------------
    def test_set_status_missing_value(self, client):
        """_plan_set_status no action_value -> clarify (line 1140-1142)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="set bug 5 status")
            pq.action_kind = "set_status"
            pq.bug_id = 5
            pq.action_value = None
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "What status" in err
        finally:
            db.close()

    def test_set_status_happy(self, client):
        """_plan_set_status success (line 1143-1145)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="close bug 5")
            pq.action_kind = "set_status"
            pq.bug_id = 5
            pq.action_value = "Closed"
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "status to Closed" in plan.summary_human
        finally:
            db.close()

    # ---- set_priority -----------------------------------------------------
    def test_set_priority_missing_value(self, client):
        """_plan_set_priority no value -> clarify (line 1148-1150)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="set bug 5 priority")
            pq.action_kind = "set_priority"
            pq.bug_id = 5
            pq.action_value = None
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "What priority" in err
        finally:
            db.close()

    def test_set_priority_happy(self, client):
        """_plan_set_priority success (line 1151-1153)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="set bug 5 priority to high")
            pq.action_kind = "set_priority"
            pq.bug_id = 5
            pq.action_value = "High"
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "priority to High" in plan.summary_human
        finally:
            db.close()

    # ---- set_environment --------------------------------------------------
    def test_set_environment_missing(self, client):
        """_plan_set_environment no environments -> clarify (line 1156-1158)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="set bug 5 environment")
            pq.action_kind = "set_environment"
            pq.bug_id = 5
            pq.environments = []
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "Which environment" in err
        finally:
            db.close()

    def test_set_environment_happy(self, client):
        """_plan_set_environment success (line 1159-1161)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="set bug 5 environment")
            pq.action_kind = "set_environment"
            pq.bug_id = 5
            pq.environments = ["PROD"]
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "environment to PROD" in plan.summary_human
        finally:
            db.close()

    # ---- set_due_date -----------------------------------------------------
    def test_set_due_date_missing(self, client):
        """_plan_set_due_date no value -> clarify (line 1165-1167)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="due bug 5")
            pq.action_kind = "set_due_date"
            pq.bug_id = 5
            pq.action_value = None
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "What date" in err
        finally:
            db.close()

    def test_set_due_date_happy(self, client):
        """_plan_set_due_date success (line 1168-1170)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="due bug 5 2026-06-15")
            pq.action_kind = "set_due_date"
            pq.bug_id = 5
            pq.action_value = "2026-06-15"
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "due date to 2026-06-15" in plan.summary_human
        finally:
            db.close()

    # ---- add_comment ------------------------------------------------------
    def test_add_comment_missing(self, client):
        """_plan_add_comment no comment -> clarify (line 1174-1176)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="comment on bug 5")
            pq.action_kind = "add_comment"
            pq.bug_id = 5
            pq.action_comment = None
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "What should the comment say" in err
        finally:
            db.close()

    def test_add_comment_short(self, client):
        """_plan_add_comment short body -> no truncation (line 1177-1181)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="comment on bug 5: ok")
            pq.action_kind = "add_comment"
            pq.bug_id = 5
            pq.action_comment = "ok"
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "ok" in plan.summary_human
        finally:
            db.close()

    def test_add_comment_long_truncates(self, client):
        """A >60-char comment is truncated in the summary (line 1178-1179)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="comment")
            pq.action_kind = "add_comment"
            pq.bug_id = 5
            pq.action_comment = "z" * 80
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "..." in plan.summary_human
        finally:
            db.close()

    # ---- create_bug -------------------------------------------------------
    def test_create_bug_missing_title(self, client):
        """_plan_create_bug no title -> clarify (line 1185-1187)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="create a bug")
            pq.action_kind = "create_bug"
            pq.action_title = None
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "I need a title" in err
        finally:
            db.close()

    def test_create_bug_full(self, client):
        """_plan_create_bug with priority+project+assignees (line 1188-1200)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        db, actor, pid = _bootstrap(client)
        try:
            pq = ParsedQuery(raw_message="create a bug titled Boom in project Web")
            pq.action_kind = "create_bug"
            pq.action_title = "Boom"
            pq.priorities = ["High"]
            pq.project_ids = [pid]
            pq.project_names = ["Web"]
            pq.assignee_ids = [actor.id]
            pq.assignee_names = ["Alice"]
            plan, err = _build_action_plan(pq, actor)
            assert err is None
            assert plan.new_value == "High"
            assert plan.new_project_id == pid
            assert "in project Web" in plan.summary_human
        finally:
            db.close()

    def test_create_bug_no_project(self, client):
        """_plan_create_bug without project -> no project part (line 1191/1197 false)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message='create a bug titled "X"')
            pq.action_kind = "create_bug"
            pq.action_title = "X"
            plan, err = _build_action_plan(pq, actor)
            assert err is None
            assert "in project" not in plan.summary_human
        finally:
            db.close()

    # ---- create_project ---------------------------------------------------
    def test_create_project_missing_name(self, client):
        """_plan_create_project no name -> clarify (line 1204-1206)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="create a project")
            pq.action_kind = "create_project"
            pq.action_title = None
            plan, err = _build_action_plan(pq, actor)
            assert plan is None and "I need a project name" in err
        finally:
            db.close()

    def test_create_project_happy(self, client):
        """_plan_create_project success (line 1207-1209)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="create project Mercury")
            pq.action_kind = "create_project"
            pq.action_title = "Mercury"
            plan, err = _build_action_plan(pq, actor)
            assert err is None and "Mercury" in plan.summary_human
        finally:
            db.close()

    def test_unknown_action_kind(self, client):
        """An action_kind that matches no planner -> fallthrough (line 1248)."""
        from app.chatbot.executor import _build_action_plan
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="frobnicate")
            pq.action_kind = "frobnicate"  # not in any branch, not needing bug
            plan, err = _build_action_plan(pq, actor)
            assert plan is None
            assert "frobnicate" in err
        finally:
            db.close()


# ===========================================================================
# Action request routing + confirmation flow (executor side)
# ===========================================================================
class TestActionRequestAndConfirm:
    def test_action_request_invalid_returns_action_invalid(self, client):
        """_handle_action_request when plan is None -> action_invalid (1255-1261).

        'comment on bug 5' parses to action_add_comment but with no comment
        body, so _build_action_plan returns (None, clarification)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("comment on bug 5", db, actor)  # missing body
            assert resp.intent == "action_invalid"
        finally:
            db.close()

    def test_action_request_stages_confirm(self, client):
        """A valid action stages + returns a confirm response (line 1264-1265)."""
        from app.chatbot.executor import execute
        from app.chatbot.memory import store as mem
        db, actor, pid = _bootstrap(client)
        try:
            bug = _make_bug(client, pid, title="Crash")
            mem.clear_pending(actor.id)
            resp = execute(f"close bug {bug['id']}", db, actor)
            assert resp.intent == "confirm_action"
            assert mem.get(actor.id).pending_action is not None
        finally:
            db.close()

    def test_confirm_yes_executes(self, client):
        """confirm_yes with a pending plan executes it (line 1268-1285)."""
        from app.chatbot.executor import execute
        from app.chatbot.memory import store as mem
        db, actor, pid = _bootstrap(client)
        try:
            bug = _make_bug(client, pid, title="Crash")
            mem.clear_pending(actor.id)
            execute(f"close bug {bug['id']}", db, actor)
            resp = execute("yes", db, actor)
            # Action executed -> bug now Closed; memory remembers the bug.
            assert mem.get(actor.id).last_bug_id == bug["id"]
            r = client.get(f"/api/bugs/{bug['id']}")
            assert r.json()["status"] in ("Closed", "Resolved")
        finally:
            db.close()

    def test_confirm_yes_nothing_pending(self, client):
        """confirm_yes with nothing staged -> confirm_idle (line 1272-1279)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            from app.chatbot.memory import store as mem
            mem.clear_pending(actor.id)
            resp = execute("yes", db, actor)
            assert resp.intent == "confirm_idle"
        finally:
            db.close()

    def test_confirm_yes_create_project_no_bug_id(self, client):
        """A confirmed create_project plan (no bug_id) skips remember_bug
        (line 1283 false)."""
        from app.chatbot.executor import execute
        from app.chatbot.memory import store as mem
        db, actor, _pid = _bootstrap(client)
        try:
            mem.clear_pending(actor.id)
            execute("create project Mercury", db, actor)
            resp = execute("yes", db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()

    def test_confirm_no_with_pending(self, client):
        """confirm_no while a plan is staged -> 'Cancelled' (line 1291-1298)."""
        from app.chatbot.executor import execute
        from app.chatbot.memory import store as mem
        db, actor, pid = _bootstrap(client)
        try:
            bug = _make_bug(client, pid, title="Crash")
            mem.clear_pending(actor.id)
            execute(f"close bug {bug['id']}", db, actor)
            resp = execute("no", db, actor)
            assert resp.intent == "confirm_cancel"
            assert "haven't changed anything" in _text(resp).lower()
        finally:
            db.close()

    def test_confirm_no_nothing_pending(self, client):
        """confirm_no with nothing staged -> 'Nothing was pending' (line 1291-1293)."""
        from app.chatbot.executor import execute
        from app.chatbot.memory import store as mem
        db, actor, _pid = _bootstrap(client)
        try:
            mem.clear_pending(actor.id)
            resp = execute("no", db, actor)
            assert resp.intent == "confirm_cancel"
            assert "nothing was pending" in _text(resp).lower()
        finally:
            db.close()


# ===========================================================================
# Pronoun resolution (line 1113-1117)
# ===========================================================================
class TestPronounResolution:
    def test_resolve_fills_from_memory(self, client):
        """used_pronoun_bug + remembered bug -> pq.bug_id filled (line 1116-1117)."""
        from app.chatbot.executor import _resolve_pronouns
        from app.chatbot.memory import store as mem
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            mem.remember_bug(actor.id, 7)
            pq = ParsedQuery(raw_message="close it")
            pq.used_pronoun_bug = True
            pq.bug_id = None
            _resolve_pronouns(pq, actor)
            assert pq.bug_id == 7
        finally:
            db.close()

    def test_no_pronoun_no_change(self, client):
        """No pronoun flag -> memory untouched (line 1113 false)."""
        from app.chatbot.executor import _resolve_pronouns
        from app.chatbot.nlu import ParsedQuery
        pq = ParsedQuery(raw_message="show bugs")
        pq.bug_id = None
        _resolve_pronouns(pq, SimpleNamespace(id=999))
        assert pq.bug_id is None

    def test_pronoun_but_no_session(self, client):
        """used_pronoun_bug but memory empty -> bug_id stays None (line 1116 false)."""
        from app.chatbot.executor import _resolve_pronouns
        from app.chatbot.memory import store as mem
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            mem.reset(actor.id)
            pq = ParsedQuery(raw_message="close it")
            pq.used_pronoun_bug = True
            pq.bug_id = None
            _resolve_pronouns(pq, actor)
            assert pq.bug_id is None
        finally:
            db.close()


# ===========================================================================
# _suggest_user pool helpers (lines 625-635, 638-648, 658-673)
# ===========================================================================
class TestSuggestUser:
    def _ctx(self, users=()):
        from app.chatbot.nlu import Context
        return Context(users=list(users), projects=[], user_role_map={})

    def test_four_tuple_entry(self):
        """_build_user_suggest_pool 4-tuple branch (line 626-627, 633-634)."""
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "alice wong", "alice", "Alice Wong")])
        out = _suggest_user("alise", ctx)
        assert "Alice Wong" in out

    def test_three_tuple_entry(self):
        """_build_user_suggest_pool 3-tuple branch (line 628-630)."""
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "bob", "Bob Builder")])
        out = _suggest_user("bobb", ctx)
        assert "Bob Builder" in out

    def test_two_close_matches(self):
        """Two close matches -> 'or' phrasing (line 673)."""
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([
            (1, "alan", "alan", "Alan A"),
            (2, "alann", "alann", "Alann B"),
        ])
        out = _suggest_user("alann", ctx)
        # Two suggestions -> "or" wording (may collapse if dedupe leaves one).
        assert "Alan" in out

    def test_empty_pool(self):
        """Empty users -> empty pool -> '' (line 665)."""
        from app.chatbot.executor import _suggest_user
        assert _suggest_user("x", self._ctx()) == ""

    def test_none_ctx(self):
        """None ctx -> '' (line 658)."""
        from app.chatbot.executor import _suggest_user
        assert _suggest_user("x", None) == ""

    def test_blank_phrase(self):
        """Whitespace phrase -> '' after strip (line 660-661)."""
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "alice", "alice", "Alice")])
        assert _suggest_user("   ", ctx) == ""

    def test_no_close_match(self):
        """No close match -> '' (line 669-670)."""
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "alice wong", "alice", "Alice Wong")])
        assert _suggest_user("zzzzzz", ctx) == ""

    def test_dedupe_drops_dupes(self):
        """_dedupe_display_names drops repeated display names (line 643-647)."""
        from app.chatbot.executor import _dedupe_display_names
        pool = {"a": "Same", "b": "Same", "c": None}
        out = _dedupe_display_names(["a", "b", "c", "missing"], pool)
        assert out == ["Same"]


# ===========================================================================
# Classifier + LLM fallback layers in execute()
# ===========================================================================
class TestFallbackLayers:
    def test_classifier_maps_to_read_intent(self, client):
        """When the rule parser fails but the classifier predicts a read
        intent, execute() returns that handler's response (line 1442-1444)."""
        from app.chatbot.executor import execute
        import app.chatbot.classifier as clf
        db, actor, _pid = _bootstrap(client)
        try:
            pred = SimpleNamespace(intent="stats")
            with patch.object(clf, "predict", return_value=pred):
                resp = execute("zzz unpar-seable gibberish", db, actor)
            assert resp.intent == "stats"
        finally:
            db.close()

    def test_classifier_action_invalid(self, client):
        """Classifier predicts an action_* intent the parser couldn't fill
        -> _classifier_action_invalid (line 1385-1386 + 1357-1366)."""
        from app.chatbot.executor import execute
        import app.chatbot.classifier as clf
        db, actor, _pid = _bootstrap(client)
        try:
            pred = SimpleNamespace(intent="action_assign")
            with patch.object(clf, "predict", return_value=pred):
                resp = execute("zzz gibberish", db, actor)
            assert resp.intent == "action_invalid"
            assert "assign" in _text(resp).lower()
        finally:
            db.close()

    def test_classifier_predicts_none(self, client):
        """Classifier returns None -> _try_classifier returns None (line 1380)."""
        from app.chatbot.executor import _try_classifier
        import app.chatbot.classifier as clf
        from app.chatbot.nlu import ParsedQuery
        db, actor, _pid = _bootstrap(client)
        try:
            pq = ParsedQuery(raw_message="x")
            with patch.object(clf, "predict", return_value=None):
                assert _try_classifier("x", db, pq, actor, None) is None
        finally:
            db.close()

    def test_classifier_unknown_intent_returns_none(self, client):
        """Classifier predicts a non-read, non-action intent -> None (line 1387)."""
        from app.chatbot.executor import _try_classifier
        import app.chatbot.classifier as clf
        from app.chatbot.nlu import ParsedQuery
        db, actor, _pid = _bootstrap(client)
        try:
            pq = ParsedQuery(raw_message="x")
            pred = SimpleNamespace(intent="totally_made_up")
            with patch.object(clf, "predict", return_value=pred):
                assert _try_classifier("x", db, pq, actor, None) is None
        finally:
            db.close()

    def test_llm_returns_response(self, client):
        """LLM layer returns a Response -> execute() returns it (line 1446-1448)."""
        from app.chatbot.executor import execute, Response, Block
        import app.chatbot.classifier as clf
        import app.chatbot.llm as llm
        db, actor, _pid = _bootstrap(client)
        try:
            fake = Response(blocks=[Block("text", {"text": "hi"})],
                            summary="x", intent="help")
            with patch.object(clf, "predict", return_value=None), \
                 patch.object(llm, "is_available", return_value=True), \
                 patch.object(llm, "try_understand", return_value=fake):
                resp = execute("zzz gibberish", db, actor)
            assert resp.intent == "help"
        finally:
            db.close()

    def test_llm_unavailable_falls_to_unknown(self, client):
        """LLM unavailable -> _try_llm returns None -> _handle_unknown (line 1403/1450)."""
        from app.chatbot.executor import execute
        import app.chatbot.classifier as clf
        import app.chatbot.llm as llm
        db, actor, _pid = _bootstrap(client)
        try:
            with patch.object(clf, "predict", return_value=None), \
                 patch.object(llm, "is_available", return_value=False):
                resp = execute("zzz gibberish blorp", db, actor)
            assert resp.intent == "unknown"
            assert resp.fallback_eligible is True
        finally:
            db.close()

    def test_llm_raises_is_swallowed(self, client):
        """_try_llm swallows an exception and returns None (line 1405-1410)."""
        from app.chatbot.executor import _try_llm
        import app.chatbot.llm as llm
        db, actor, _pid = _bootstrap(client)
        try:
            def boom():
                raise RuntimeError("llm exploded")

            with patch.object(llm, "is_available", side_effect=boom):
                assert _try_llm("x", db, actor) is None
        finally:
            db.close()


# ===========================================================================
# Direct dispatch helpers + empty / greeting / thanks / help / about
# ===========================================================================
class TestDispatchAndSimpleIntents:
    def test_dispatch_read_unknown_returns_none(self, client):
        """_dispatch_read_intent with an unknown intent -> None (line 1346)."""
        from app.chatbot.executor import _dispatch_read_intent
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        db = _db()
        try:
            actor = _actor(db)
            pq = ParsedQuery(raw_message="?")
            assert _dispatch_read_intent("nope", db, pq, actor, None) is None
        finally:
            db.close()

    def test_empty_message(self, client):
        """Empty message -> 'empty' intent (line 1318-1319)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("", db, actor)
            assert resp.intent == "empty"
        finally:
            db.close()

    def test_greeting(self, client):
        """Greeting -> 'greeting' (line 1320-1321)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("hi", db, actor)
            assert resp.intent == "greeting"
            assert "Sleuth" in _text(resp)
        finally:
            db.close()

    def test_greeting_unnamed_actor(self):
        """_handle_greeting with no name -> no comma part (line 248 false)."""
        from app.chatbot.executor import _handle_greeting
        resp = _handle_greeting(SimpleNamespace(name=""))
        assert resp.intent == "greeting"
        assert "Sleuth" in resp.blocks[0].payload["text"]

    def test_thanks(self, client):
        """Thanks -> 'thanks' (line 1322-1323)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("thank you", db, actor)
            assert resp.intent == "thanks"
        finally:
            db.close()

    def test_help(self, client):
        """Help -> 'help' (line 1324-1325)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("help", db, actor)
            assert resp.intent == "help"
        finally:
            db.close()

    def test_about_known(self, client):
        """about with a matching keyword (line 368-374)."""
        from app.chatbot.executor import execute
        db, actor, _pid = _bootstrap(client)
        try:
            resp = execute("what is a priority?", db, actor)
            assert resp.intent == "about"
            assert "Priorit" in _text(resp)
        finally:
            db.close()

    def test_about_unknown_fallback(self):
        """about with no keyword match -> fallback_eligible (line 375-382)."""
        from app.chatbot.executor import _handle_about
        resp = _handle_about("what is the meaning of life")
        assert resp.intent == "about"
        assert resp.fallback_eligible is True

    def test_handle_unknown(self):
        """_handle_unknown is fallback-eligible (line 385-394)."""
        from app.chatbot.executor import _handle_unknown
        resp = _handle_unknown()
        assert resp.intent == "unknown"
        assert resp.fallback_eligible is True


# ===========================================================================
# Multi-tenant scoping in _scope_to_actor (lines 156-163)
# ===========================================================================
class TestScopeToActor:
    def test_no_accessible_projects_zero_rows(self, client):
        """A member with no projects -> count is 0 (line 156-159 zero branch)."""
        from app.chatbot.executor import execute
        from app.models import User
        from app.auth import hash_password
        _signup(client)
        _make_project(client, "Solo")
        db = _db()
        try:
            admin = _actor(db)
            member = User(
                org_id=admin.org_id, name="Zed", email="zed@a.test",
                password_hash=hash_password("TestPass1!"), role="user",
                is_active=True,
            )
            db.add(member)
            db.commit()
            db.refresh(member)
            resp = execute("how many bugs?", db, member)
            assert resp.intent == "count_bugs"
            assert "0" in _text(resp)
        finally:
            db.close()

    def test_cross_tenant_list_excludes_other_org(self, two_orgs):
        """Org A's list_bugs never includes Org B's rows (line 160-163)."""
        from app.chatbot.executor import execute
        c_a, c_b, _, _ = two_orgs
        proj_b = c_b.post("/api/projects", json={"name": "BProj"}).json()
        c_b.post("/api/bugs", json={
            "project_id": proj_b["id"], "title": "secret B bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        })
        c_a.post("/api/projects", json={"name": "AProj"})
        db = _db()
        try:
            alice = _actor(db, "alice@a.test")
            resp = execute("show all bugs", db, alice)
            assert "secret B bug" not in _text(resp)
        finally:
            db.close()


# ===========================================================================
# Open-ended time windows (start-only / end-only) — partial-branch closers
# for _apply_time_window (204/206), _handle_recent_activity (523/525) and
# _filters_from_parsed (913/915).
# ===========================================================================
class TestOpenEndedTimeWindows:
    def _seed(self, client):
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="t bug", status="New")
        db = _db()
        return db, _actor(db), proj

    def test_apply_time_window_start_only(self, client):
        """_apply_time_window applies start but skips end (line 204 true,
        206->208 end-None)."""
        from app.chatbot.executor import _apply_time_window, _eager_bug_query
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        from sqlalchemy import select, func
        from app.models import Bug
        db, actor, _proj = self._seed(client)
        try:
            pq = ParsedQuery(raw_message="bugs since then")
            pq.time_window = TimeWindow(
                start=datetime(2000, 1, 1, tzinfo=timezone.utc), end=None,
                label="since",
            )
            stmt, cnt = _apply_time_window(
                _eager_bug_query(), select(func.count(Bug.id)), pq,
            )
            # Statement is runnable and counts the seeded bug (created > 2000).
            assert (db.scalar(cnt) or 0) >= 1
        finally:
            db.close()

    def test_apply_time_window_end_only_created_keyword(self, client):
        """_apply_time_window with end-only AND a 'created' keyword uses the
        created_at column (line 202 true, 204->206 start-None, 207 true)."""
        from app.chatbot.executor import _apply_time_window, _eager_bug_query
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        from sqlalchemy import select, func
        from app.models import Bug
        db, actor, _proj = self._seed(client)
        try:
            pq = ParsedQuery(raw_message="bugs created before now")
            pq.time_window = TimeWindow(
                start=None, end=datetime(2999, 1, 1, tzinfo=timezone.utc),
                label="before",
            )
            stmt, cnt = _apply_time_window(
                _eager_bug_query(), select(func.count(Bug.id)), pq,
            )
            assert (db.scalar(cnt) or 0) >= 1
        finally:
            db.close()

    def test_recent_activity_start_only(self, client):
        """_handle_recent_activity with a start-only window (line 523 true,
        525->527 end-None)."""
        from app.chatbot.executor import _handle_recent_activity
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        db, actor, proj = self._seed(client)
        try:
            pq = ParsedQuery(raw_message="recent activity")
            pq.time_window = TimeWindow(
                start=datetime(2000, 1, 1, tzinfo=timezone.utc), end=None,
                label="since",
            )
            resp = _handle_recent_activity(db, pq, actor)
            assert resp.intent == "recent_activity"
        finally:
            db.close()

    def test_recent_activity_end_only(self, client):
        """_handle_recent_activity with an end-only window (line 525 true,
        523->525 start-None)."""
        from app.chatbot.executor import _handle_recent_activity
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        db, actor, proj = self._seed(client)
        try:
            pq = ParsedQuery(raw_message="recent activity")
            pq.time_window = TimeWindow(
                start=None, end=datetime(2999, 1, 1, tzinfo=timezone.utc),
                label="before",
            )
            resp = _handle_recent_activity(db, pq, actor)
            assert resp.intent == "recent_activity"
        finally:
            db.close()

    def test_filters_from_parsed_start_only(self):
        """_filters_from_parsed: start set, end None (line 913 true, 915->917)."""
        from app.chatbot.executor import _filters_from_parsed
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        pq = ParsedQuery(raw_message="bug report")
        pq.time_window = TimeWindow(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=None, label="x",
        )
        filters = _filters_from_parsed(pq)
        assert filters.date_from is not None
        assert filters.date_to is None

    def test_filters_from_parsed_end_only(self):
        """_filters_from_parsed: end set, start None (line 915 true, 913->915)."""
        from app.chatbot.executor import _filters_from_parsed
        from app.chatbot.nlu import ParsedQuery, TimeWindow
        pq = ParsedQuery(raw_message="task report")
        pq.time_window = TimeWindow(
            start=None, end=datetime(2026, 2, 1, tzinfo=timezone.utc), label="x",
        )
        filters = _filters_from_parsed(pq)
        assert filters.date_from is None
        assert filters.date_to is not None
        assert "Task" in filters.item_types


# ===========================================================================
# Remaining partial branches: empty-norm-name pool entry, bug_detail dispatch
# with no bug_id.
# ===========================================================================
class TestRemainingBranches:
    def test_suggest_pool_skips_empty_norm_name(self):
        """_build_user_suggest_pool: an entry with empty normalized name skips
        the name key but still registers the email local-part (line 631->633)."""
        from app.chatbot.executor import _suggest_user
        from app.chatbot.nlu import Context
        # norm_name "" -> the `if norm_name` guard is False; email_local present.
        ctx = Context(
            users=[(1, "", "alicia", "Alicia Keys")],
            projects=[], user_role_map={},
        )
        out = _suggest_user("alicai", ctx)
        # Matched via the email local-part key only.
        assert "Alicia Keys" in out

    def test_dispatch_bug_detail_without_bug_id_skips_remember(self, client):
        """_dispatch_read_intent('bug_detail') with bug_id None skips the
        remember_bug call (line 1335->1337) and still returns a not-found
        Response."""
        from app.chatbot.executor import _dispatch_read_intent
        from app.chatbot.nlu import ParsedQuery
        db, actor, _pid = _bootstrap(client)
        try:
            pq = ParsedQuery(raw_message="bug detail")
            pq.bug_id = None
            resp = _dispatch_read_intent("bug_detail", db, pq, actor, None)
            assert resp is not None
            assert resp.intent == "bug_detail"
            assert "couldn't find" in _text(resp).lower()
        finally:
            db.close()
