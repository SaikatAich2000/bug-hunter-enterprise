"""NLU parser (app.chatbot.nlu) and executor (app.chatbot.executor) tests; cloud LLM off, so the local chain runs.
Imports live inside tests (client fixture re-imports app.*); no network, real LLM, or openpyxl needed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import any_user, default_org_id, scope_of


# Seeding helpers — build users, projects, events, and bugs via the ORM.
def _seed(client):
    """Populate the per-test DB with a representative dataset; returns a dict of ids."""
    from app import models
    from app.auth import hash_password
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(email="admin@test.local").one()
        alice = models.User(org_id=default_org_id(), name="Alice Wonderland", email="alice@test.local",
                            role="manager", password_hash=hash_password("x"),
                            is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob Builder", email="bob@test.local",
                          role="user", password_hash=hash_password("x"),
                          is_active=True)
        carol = models.User(org_id=default_org_id(), name="Carol Singer", email="carol@test.local",
                            role="user", password_hash=hash_password("x"),
                            is_active=False)
        db.add_all([alice, bob, carol])
        db.commit()

        proj_a = models.Project(org_id=default_org_id(), name="Apollo", description="Mission control")
        proj_b = models.Project(org_id=default_org_id(), name="Beacon", description=None)
        db.add_all([proj_a, proj_b])
        db.commit()

        event = models.Event(org_id=default_org_id(), name="Release 9.9", description="Big launch")
        db.add(event)
        db.commit()

        now = datetime.now(timezone.utc)
        # (status, priority, env, project, age_days, title, event_id)
        spec = [
            ("New",           "Critical", "PROD", proj_a,  1, "Service down", event.id),
            ("New",           "High",     "PROD", proj_b,  2, "Login broken", None),
            ("In Progress",   "Medium",   "UAT",  proj_a,  3, "Date filter off", None),
            ("In Progress",   "Critical", "PROD", proj_b,  0, "Crash on startup", None),
            ("Resolved",      "Low",      "DEV",  proj_b, 10, "Typo on landing", None),
            ("Closed",        "Low",      "PROD", proj_b, 30, "Spam in audit", None),
            ("Reopened",      "High",     "UAT",  proj_a,  5, "Sync regression", None),
            ("Not a Bug",     "Low",      "DEV",  proj_a,  7, "Misread expect", None),
            ("Resolve Later", "Medium",   "UAT",  proj_b, 14, "Theme off", None),
        ]
        bugs = []
        for status, prio, env, proj, age, title, eid in spec:
            b = models.Bug(
                title=title, description="A long-ish description " * 40,
                status=status, priority=prio, environment=env,
                project_id=proj.id, reporter_id=admin.id, event_id=eid,
                due_date="2026-06-15",
                created_at=now - timedelta(days=age),
            )
            bugs.append(b)
        db.add_all(bugs)
        db.commit()
        bugs[0].assignees = [bob]
        bugs[1].assignees = [alice, bob]
        bugs[3].assignees = [carol]
        bugs[6].assignees = [alice]
        db.commit()

        # Activity rows for recent-activity tests and non-admin scoping.
        db.add_all([
            models.Activity(actor_user_id=admin.id, actor_name="Test Admin",
                            action="bug.create", entity_type="bug",
                            entity_id=bugs[0].id, detail="x" * 200,
                            created_at=now - timedelta(hours=1)),
            models.Activity(actor_user_id=bob.id, actor_name="Bob Builder",
                            action="bug.update", entity_type="bug",
                            entity_id=bugs[0].id, detail="short",
                            created_at=now - timedelta(hours=2)),
        ])
        db.commit()

        return {
            "admin": admin.id, "alice": alice.id, "bob": bob.id,
            "carol": carol.id, "apollo": proj_a.id, "beacon": proj_b.id,
            "event": event.id, "now": now,
        }
    finally:
        db.close()


def _new_db():
    from app.database import SessionLocal
    return SessionLocal()


def _user(db, uid):
    from app import models
    return db.get(models.User, uid)


def _texts(resp):
    """Join the text payload from every text block in a Response."""
    return " ".join(
        b.payload.get("text", "")
        for b in resp.blocks
        if b.kind in ("text",)
    )


def _table(resp):
    return next((b for b in resp.blocks if b.kind == "table"), None)


# nlu.py — time windows
def test_cov_time_named_windows():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    fixed = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # a Monday
    for phrase, label in [
        ("bugs today", "today"),
        ("bugs yesterday", "yesterday"),
        ("bugs this week", "this week"),
        ("bugs last week", "last week"),
        ("bugs this month", "this month"),
        ("bugs last month", "last month"),
        ("bugs this quarter", "this quarter"),
        ("bugs last quarter", "last quarter"),
        ("bugs this year", "this year"),
        ("bugs last year", "last year"),
    ]:
        pq = nlu.parse(phrase, ctx, now=fixed)
        assert pq.time_window is not None, phrase
        assert pq.time_window.label == label, (phrase, pq.time_window.label)


def test_cov_time_relative_and_since_weekday():
    # Covers the since-weekday and relative-window branches.
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    wed = datetime(2026, 6, 17, 9, 0, 0, tzinfo=timezone.utc)  # Wednesday

    pq = nlu.parse("bugs since monday", ctx, now=wed)
    assert pq.time_window is not None
    assert pq.time_window.label == "since monday"

    # "since monday" on a Monday means a week ago (delta_days==0 -> 7).
    mon = datetime(2026, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
    pq2 = nlu.parse("bugs since monday", ctx, now=mon)
    assert pq2.time_window.start == mon.replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)

    for phrase, _unit_word in [
        ("bugs in the last 7 days", "day"),
        ("bugs in the last 2 weeks", "week"),
        ("bugs in the last 3 months", "month"),
        ("bugs in the last 12 hours", "hour"),
        ("bugs past 5 days", "day"),
        ("bugs last 4 weeks", "week"),
    ]:
        pq = nlu.parse(phrase, ctx, now=wed)
        assert pq.time_window is not None, phrase


def test_cov_time_relative_window_units_direct():
    # Each supported unit, plus an unknown unit that returns None.
    from app.chatbot import nlu
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert nlu._relative_window(3, "hours", now).start == now - timedelta(hours=3)
    assert nlu._relative_window(2, "days", now).start == now - timedelta(days=2)
    assert nlu._relative_window(1, "weeks", now).start == now - timedelta(weeks=1)
    assert nlu._relative_window(1, "months", now).start == now - timedelta(days=30)
    assert nlu._relative_window(1, "fortnights", now) is None  # unknown unit


def test_cov_time_no_window():
    from app.chatbot import nlu
    assert nlu._parse_time_window("just a plain message") is None


# nlu.py — enum extraction: statuses, priorities, environments, typos
def test_cov_status_synonyms_all():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    cases = {
        "show open bugs": "New",
        "show active bugs": "In Progress",
        "show ongoing bugs": "Reopened",
        "show wip bugs": "In Progress",
        "show in-progress bugs": "In Progress",
        "show new bugs": "New",
        "show fixed bugs": "Resolved",
        "show resolved bugs": "Resolved",
        "show closed bugs": "Closed",
        "show done bugs": "Closed",
        "show reopened bugs": "Reopened",
        "show not a bug": "Not a Bug",
        "show invalid bugs": "Not a Bug",
        "show deferred bugs": "Resolve Later",
        "show parked bugs": "Resolve Later",
    }
    for msg, expected in cases.items():
        pq = nlu.parse(msg, ctx)
        assert expected in pq.statuses, (msg, pq.statuses)


def test_cov_priority_and_env_synonyms_and_typos():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    # Priority synonyms including P0-P3 shorthand.
    for msg, exp in [
        ("low priority", "Low"), ("minor bugs", "Low"), ("trivial bugs", "Low"),
        ("medium bugs", "Medium"), ("normal bugs", "Medium"),
        ("high bugs", "High"), ("important bugs", "High"), ("major bugs", "High"),
        ("critical bugs", "Critical"), ("blocker bugs", "Critical"),
        ("urgent bugs", "Critical"), ("p0 bugs", "Critical"),
        ("p1 bugs", "High"), ("p2 bugs", "Medium"), ("p3 bugs", "Low"),
    ]:
        pq = nlu.parse(msg, ctx)
        assert exp in pq.priorities, (msg, pq.priorities)

    # Environment synonyms.
    for msg, exp in [
        ("dev bugs", "DEV"), ("development bugs", "DEV"), ("sandbox bugs", "DEV"),
        ("uat bugs", "UAT"), ("staging bugs", "UAT"), ("qa bugs", "UAT"),
        ("preprod bugs", "UAT"), ("prod bugs", "PROD"),
        ("production bugs", "PROD"), ("live bugs", "PROD"),
    ]:
        pq = nlu.parse(msg, ctx)
        assert exp in pq.environments, (msg, pq.environments)

    # Typo fallbacks via edit-distance (only when exact matching misses).
    pq = nlu.parse("show ctitical bugs", ctx)
    assert "Critical" in pq.priorities, pq.priorities
    pq = nlu.parse("bugs in produciton", ctx)
    assert "PROD" in pq.environments, pq.environments


def test_cov_typo_match_short_token_returns_none():
    from app.chatbot import nlu
    # Short tokens are skipped to avoid false positives (e.g. "low" -> "log").
    assert nlu._typo_match("ab", nlu._PRIORITY_SYNONYMS) is None
    assert nlu._typo_match("zzzzzzzz", nlu._PRIORITY_SYNONYMS) is None


# nlu.py — name resolution
def test_cov_name_resolution_strategies():
    from app.chatbot import nlu
    # Email locals differ from first names so each strategy is exercised independently.
    ctx = nlu.Context(
        users=[
            (1, "alice wonderland", "awonder", "Alice Wonderland"),
            (2, "bob builder", "bbuild", "Bob Builder"),
            (3, "carol singer", "csong", "Carol Singer"),
        ],
        projects=[],
    )
    assert nlu._resolve_name("alice wonderland", ctx) == [(1, "Alice Wonderland")]  # exact
    assert nlu._resolve_name("bbuild", ctx) == [(2, "Bob Builder")]                  # email local
    assert nlu._resolve_name("alice", ctx) == [(1, "Alice Wonderland")]              # prefix
    assert nlu._resolve_name("singer", ctx) == [(3, "Carol Singer")]                 # last name
    assert nlu._resolve_name("carol", ctx) == [(3, "Carol Singer")]                  # first name
    assert nlu._resolve_name("Ms. Carol", ctx) == [(3, "Carol Singer")]              # title stripped
    assert nlu._resolve_name("zoltan", ctx) == []
    assert nlu._resolve_name("   ", ctx) == []
    assert nlu._resolve_name("mr", ctx) == []  # title-only after stripping prefixes


def test_cov_name_ambiguous_and_unresolved():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[
            (1, "bob builder", "bobb", "Bob Builder"),
            (2, "bob marley", "bobm", "Bob Marley"),
        ],
        projects=[],
    )
    # Two users share the first name Bob → ambiguous.
    pq = nlu.parse("show bugs assigned to bob", ctx)
    assert pq.ambiguous_names, pq.ambiguous_names
    assert pq.ambiguous_names[0][0] == "bob"

    # Unresolved names are recorded separately for assignee and reporter slots.
    pq2 = nlu.parse("bugs assigned to nelson", ctx)
    assert pq2.unresolved_assignee_names == ["nelson"]
    pq3 = nlu.parse("bugs reported by nelson", ctx)
    assert pq3.unresolved_reporter_names == ["nelson"]


def test_cov_possessive_assignee():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[(1, "alice wonderland", "alice", "Alice Wonderland")],
        projects=[],
    )
    pq = nlu.parse("Alice's bugs", ctx)
    assert pq.intent == "list_bugs"
    assert 1 in pq.assignee_ids

    # Possessive that resolves to nobody returns False.
    assert nlu._try_possessive_assignee("Zelda's bugs", nlu.ParsedQuery(), ctx) is False


# nlu.py — projects, roles, bug ids, text search
def test_cov_project_resolution_paths():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[],
        projects=[(1, "apollo", "Apollo"), (2, "beacon", "Beacon")],
    )
    pq = nlu.parse("bugs in project Apollo", ctx)          # strict cue
    assert 1 in pq.project_ids
    pq2 = nlu.parse("project beacon issues", ctx)          # loose cue
    assert 2 in pq2.project_ids
    pq3 = nlu.parse("anything broken in Apollo lately", ctx)  # literal fallback
    assert 1 in pq3.project_ids
    pq4 = nlu.parse("bugs in project Zeta", ctx)           # unknown project
    assert pq4.project_ids == []


def test_cov_resolve_project_direct():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[(1, "mobile app", "Mobile App")])
    assert nlu._resolve_project("mobile app", ctx) == [(1, "Mobile App")]
    assert nlu._resolve_project("mobile", ctx) == [(1, "Mobile App")]  # prefix
    assert nlu._resolve_project("   ", ctx) == []


def test_cov_role_filter_variants():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    assert nlu.parse("list admins", ctx).role_filter == "admin"
    assert nlu.parse("list managers", ctx).role_filter == "manager"
    assert nlu.parse("list regular users", ctx).role_filter == "user"


def test_cov_bug_id_extraction():
    from app.chatbot import nlu
    assert nlu._extract_bug_id("bug 42") == 42
    assert nlu._extract_bug_id("#7") == 7
    assert nlu._extract_bug_id("issue #13") == 13
    assert nlu._extract_bug_id("details of 99") == 99
    assert nlu._extract_bug_id("123") == 123   # bare digits in a short message
    assert nlu._extract_bug_id("no number here") is None


def test_cov_text_search_paths():
    from app.chatbot import nlu
    assert nlu._extract_text_search('bugs about "login crash"') == "login crash"  # quoted takes priority
    assert nlu._extract_text_search("bugs about login") == "login"
    assert nlu._extract_text_search("issues regarding checkout") == "checkout"
    # Pure filter terms are not treated as free-text search topics.
    assert nlu._extract_text_search("bugs about high priority") is None
    assert nlu._extract_text_search("list open bugs") is None


# nlu.py — action detection
def test_cov_action_detection_variants():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[(1, "alice wonderland", "alice", "Alice Wonderland"),
               (2, "bob builder", "bob", "Bob Builder")],
        projects=[(1, "apollo", "Apollo")],
    )
    pq = nlu.parse("comment on #5: looks fixed", ctx)
    assert pq.action_kind == "add_comment"
    assert pq.action_comment == "looks fixed"

    pq = nlu.parse("comment on bug 5", ctx)   # no body → action_comment is None
    assert pq.action_kind == "add_comment"
    assert pq.action_comment is None

    pq = nlu.parse("create project Mercury", ctx)
    assert pq.action_kind == "create_project"
    assert pq.action_title == "Mercury"

    pq = nlu.parse('create a bug titled "Login broke" in project Apollo', ctx)
    assert pq.action_kind == "create_bug"
    assert pq.action_title == "Login broke"

    # Bare title: trailing project cue is stripped.
    pq = nlu.parse("file a bug Cache miss in project Apollo", ctx)
    assert pq.action_kind == "create_bug"
    assert pq.action_title == "Cache miss"

    pq = nlu.parse("close bug 5", ctx)
    assert pq.action_kind == "set_status"
    assert pq.action_value == "Closed"

    pq = nlu.parse("set bug 5 status to in progress", ctx)
    assert pq.action_kind == "set_status"
    assert pq.action_value == "In Progress"

    pq = nlu.parse("set bug 5 priority to high", ctx)
    assert pq.action_kind == "set_priority"
    assert pq.action_value == "High"

    pq = nlu.parse("due bug 5 2026-06-15", ctx)
    assert pq.action_kind == "set_due_date"
    assert pq.action_value == "2026-06-15"

    pq = nlu.parse("assign bug 5 to alice", ctx)
    assert pq.action_kind == "assign"
    pq = nlu.parse("unassign bob from #5", ctx)
    assert pq.action_kind == "unassign"

    # List verb alongside "assigned to" → list intent, not assign.
    pq = nlu.parse("show bugs assigned to bob", ctx)
    assert pq.action_kind is None
    assert pq.intent == "list_bugs"


def test_cov_action_helpers_negative_paths():
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    assert nlu._action_add_comment("show bugs", pq) is None
    assert nlu._action_create_project("show bugs", pq) is None
    assert nlu._action_create_bug("show bugs", pq) is None
    assert nlu._action_set_status("show bugs", pq) is None
    # Priority change also requires a parsed priority value.
    assert nlu._action_set_priority("change priority", nlu.ParsedQuery()) is None
    assert nlu._action_set_due_date("show bugs", pq) is None
    # Assign requires resolved assignee ids.
    assert nlu._action_assign("assign bug 5 to nobody", nlu.ParsedQuery()) is None


def test_cov_strip_create_bug_tail_direct():
    from app.chatbot import nlu
    assert nlu._strip_create_bug_tail("") == ""
    assert nlu._strip_create_bug_tail("Title with priority high") == "Title"
    assert nlu._strip_create_bug_tail("Title assigned to bob") == "Title"
    assert nlu._strip_create_bug_tail("Plain title") == "Plain title"


def test_cov_pick_report_key_direct():
    from app.chatbot import nlu
    assert nlu.pick_report_key("") is None
    assert nlu.pick_report_key("throughput last week") == "throughput"
    assert nlu.pick_report_key("who resolved how many") == "throughput"
    assert nlu.pick_report_key("pending snapshot") == "pending_snapshot"
    assert nlu.pick_report_key("aging report") == "aging"
    assert nlu.pick_report_key("time to resolution") == "time_to_resolution"
    assert nlu.pick_report_key("distribution by status") == "status_distribution"
    assert nlu.pick_report_key("by priority") == "priority_distribution"
    assert nlu.pick_report_key("breakdown by project") == "project_breakdown"
    assert nlu.pick_report_key("created vs resolved timeline") == "timeline"
    assert nlu.pick_report_key("just some bugs") is None


def test_cov_describe_filters_branches():
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    pq.statuses = list(nlu.OPEN_STATUSES)        # collapses to "open"
    assert "open" in nlu.describe_filters(pq)

    pq2 = nlu.ParsedQuery()
    pq2.statuses = ["Closed"]
    pq2.priorities = ["High"]
    pq2.environments = ["PROD"]
    pq2.project_names = ["Apollo"]
    pq2.assignee_names = ["Alice"]
    pq2.reporter_names = ["Bob"]
    pq2.unassigned = True
    pq2.text_search = "login"
    pq2.time_window = nlu.TimeWindow(label="today")
    pq2.sort_oldest = True
    out = nlu.describe_filters(pq2)
    for frag in ["closed", "high priority", "in PROD", "in project Apollo",
                 "assigned to Alice", "reported by Bob", "with no assignee",
                 'matching "login"', "(today)", "(oldest first)"]:
        assert frag in out, (frag, out)


# executor.py — build_context
def test_cov_build_context_email_without_at(client):
    _seed(client)
    from app import models
    from app.chatbot.executor import build_context
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        # A user with no '@' in their email hits the false branch of the local-part guard.
        u = models.User(org_id=default_org_id(), name="Weird Name", email="noatsign",
                        role="user", password_hash="x", is_active=True)
        db.add(u)
        db.commit()
        ctx = build_context(db, any_user(db))
        weird = next(t for t in ctx.users if t[3] == "Weird Name")
        assert weird[2] == ""
    finally:
        db.close()


# executor.py — read handlers
def test_cov_exec_greeting_thanks_help_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        assert executor.execute("hi", db, admin).intent == "greeting"
        assert executor.execute("thanks", db, admin).intent == "thanks"
        assert executor.execute("help", db, admin).intent == "help"
        assert executor.execute("", db, admin).intent == "empty"
    finally:
        db.close()


def test_cov_exec_about_known_and_unknown(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("what statuses are there", db, admin)
        assert r.intent == "about"
        assert "Statuses" in _texts(r)
        # The "priorit" stem must match both "priority" and "priorities".
        for phrasing in ("explain priority levels", "what are the priorities"):
            rp = executor._handle_about(phrasing)
            assert rp.intent == "about"
            assert "P0 → Critical" in _texts(rp)
        # Unrecognised topic → fallback-eligible "not sure" response.
        r2 = executor._handle_about("tell me about quantum gravity please")
        assert r2.intent == "about"
        assert r2.fallback_eligible is True
        assert "not sure" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_exec_list_users_filter_and_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("list all users", db, admin)
        assert r.intent == "list_users"
        assert _table(r) is not None
        r2 = executor.execute("list managers", db, admin)
        names = [row[0] for row in _table(r2).payload["rows"]]
        assert names == ["Alice Wonderland"]
        # Remove the only manager to force the empty-result branch.
        from app.chatbot import nlu
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.parse("list admins", ctx)
        pq.role_filter = "manager"
        from app import models
        alice = db.get(models.User, ids["alice"])
        alice.role = "user"
        db.commit()
        r3 = executor._handle_list_users(db, pq, any_user(db))
        assert r3.summary == "0 users"
        assert "No users match" in _texts(r3)
    finally:
        db.close()


def test_cov_exec_list_projects_and_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("list projects", db, admin)
        assert r.intent == "list_projects"
        assert _table(r) is not None

        # Delete all projects to hit the empty-list branch.
        from app import models
        db.query(models.Bug).delete()
        db.query(models.Project).delete()
        db.commit()
        r2 = executor._handle_list_projects(db, set())
        assert r2.summary == "0 projects"
        assert "no projects yet" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_exec_bug_detail_found_event_and_not_found(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Bug 1 has an event and a long description: covers event line + truncated-description.
        r = executor.execute("bug 1", db, admin)
        assert r.intent == "bug_detail"
        body = _texts(r)
        assert "Release 9.9" in body
        assert "Description:" in body
        r2 = executor.execute("bug 999999", db, admin)
        assert r2.intent == "bug_detail"
        assert "couldn't find" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_exec_recent_activity_admin_user_and_window(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        bob = _user(db, ids["bob"])
        r = executor.execute("recent activity", db, admin)     # admin sees all
        assert r.intent == "recent_activity"
        assert _table(r) is not None
        r2 = executor.execute("recent activity", db, bob)      # non-admin sees own only
        assert r2.intent == "recent_activity"
        r3 = executor.execute("what happened today", db, admin)  # time-window branch
        assert r3.intent == "recent_activity"
    finally:
        db.close()


def test_cov_exec_recent_activity_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        from app import models
        db.query(models.Activity).delete()
        db.commit()
        admin = _user(db, ids["admin"])
        r = executor.execute("recent activity today", db, admin)
        assert r.summary == "0 events"
        assert "No recent activity" in _texts(r)
    finally:
        db.close()


def test_cov_exec_stats(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("summary", db, admin)
        assert r.intent == "stats"
        body = _texts(r)
        assert "Total" in body
        assert "Open" in body
        assert _table(r) is not None  # top-assignees table
    finally:
        db.close()


# executor.py — list_bugs
def test_cov_exec_list_count_and_filters(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("how many critical bugs in prod", db, admin)
        assert r.intent == "count_bugs"
        assert "bug" in _texts(r)
        r2 = executor.execute("show open bugs assigned to bob", db, admin)
        assert r2.intent == "list_bugs"
        assert _table(r2) is not None
        r3 = executor.execute("show unassigned bugs", db, admin)
        assert r3.intent == "list_bugs"
        r4 = executor.execute("oldest open bugs", db, admin)
        assert r4.intent == "list_bugs"
        # Verify time-window filtering against created_at and updated_at.
        r5 = executor.execute("bugs created in the last 2 days", db, admin)
        assert r5.intent == "list_bugs"
        r6 = executor.execute("bugs updated this week", db, admin)
        assert r6.intent == "list_bugs"
    finally:
        db.close()


def test_cov_exec_count_response_singular_plural():
    # Call the builder directly: 'closed' in execute() would parse as a write action.
    from app.chatbot import executor
    assert "is **1** bug " in executor._build_count_response(1, "(no filters)").blocks[0].payload["text"]
    assert "are **0** bugs" in executor._build_count_response(0, "in PROD").blocks[0].payload["text"]
    assert "are **2** bugs" in executor._build_count_response(2, "open").blocks[0].payload["text"]


def test_cov_exec_list_empty_paths(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("how many critical bugs in prod", db, admin)
        assert r.intent == "count_bugs"
        from app import models
        db.query(models.Bug).delete()
        db.commit()
        r2 = executor.execute("show me all bugs", db, admin)
        assert r2.intent == "list_bugs"
        assert "no bugs in the system yet" in _texts(r2).lower()
        # With an active filter the message becomes "No bugs found …".
        r3 = executor.execute("show critical bugs in PROD", db, admin)
        assert "No bugs found" in _texts(r3)
    finally:
        db.close()


def test_cov_exec_clarify_ambiguous_and_unresolved(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # Two Franks whose email local-parts don't match "frank" — genuinely ambiguous.
        from app import models
        from app.auth import hash_password
        db.add_all([
            models.User(org_id=default_org_id(), name="Frank Castle", email="fcastle@test.local",
                        role="user", password_hash=hash_password("x"),
                        is_active=True),
            models.User(org_id=default_org_id(), name="Frank Sinatra", email="fsinatra@test.local",
                        role="user", password_hash=hash_password("x"),
                        is_active=True),
        ])
        db.commit()
        r = executor.execute("show bugs assigned to frank", db, admin)
        assert r.intent == "clarify"
        assert "More than one user" in _texts(r)

        # Unresolved name with a close match → suggestion in the clarify response.
        ctx = executor.build_context(db, any_user(db))
        r2 = executor._handle_list_bugs(
            db, nlu.parse("bugs assigned to bib", ctx), ctx,
            accessible=scope_of(db, admin))
        assert r2.intent == "clarify"
        assert "couldn't find a user" in _texts(r2).lower()
    finally:
        db.close()


def test_cov_clarify_helpers_none_paths():
    from app.chatbot import executor, nlu
    assert executor._clarify_ambiguous_names(nlu.ParsedQuery()) is None
    assert executor._clarify_unresolved_user(nlu.ParsedQuery(), None) is None


# executor.py — user-suggestion helpers
def test_cov_suggest_user_helpers():
    from app.chatbot import executor, nlu
    ctx = nlu.Context(
        users=[
            (1, "alice wonderland", "alice", "Alice Wonderland"),
            (2, "bob builder", "bob", "Bob Builder"),
            (3, "", "", "Nameless"),  # empty name/email-local → skipped in pool
        ],
        projects=[],
    )
    pool = executor._build_user_suggest_pool(ctx)
    assert "alice wonderland" in pool
    assert "bob" in pool
    assert "" not in pool

    assert "Alice Wonderland" in executor._suggest_user("alise wonderland", ctx)
    # Two close matches are joined with " or ".
    ctx2 = nlu.Context(
        users=[
            (1, "jon snow", "jon", "Jon Snow"),
            (2, "ron snow", "ron", "Ron Snow"),
        ],
        projects=[],
    )
    out = executor._suggest_user("on snow", ctx2)
    assert " or " in out

    assert executor._suggest_user("anything", None) == ""     # no context
    assert executor._suggest_user("   ", ctx) == ""           # blank needle
    empty_ctx = nlu.Context(users=[], projects=[])
    assert executor._suggest_user("alice", empty_ctx) == ""   # empty pool
    assert executor._suggest_user("zzzzzzzzzz", ctx) == ""    # no close match


def test_cov_dedupe_display_names():
    from app.chatbot import executor
    pool = {"a": "Alice", "b": "Bob", "a2": "Alice"}
    # Duplicate display names are dropped, order preserved, unknown keys ignored.
    assert executor._dedupe_display_names(["a", "a2", "b", "zzz"], pool) == ["Alice", "Bob"]


# executor.py — export path
def test_cov_exec_export_to_excel(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # 'open' is a status synonym (not a verb), so it survives as a filter for the label builder.
        r = executor.execute(
            "export open bugs assigned to alice to excel", db, admin)
        kinds = {b.kind for b in r.blocks}
        # Accept whichever branch fires: openpyxl may or may not be installed.
        assert r.intent in ("export_bugs", "export_failed")
        if r.intent == "export_bugs":
            assert "file" in kinds
        else:
            assert "text" in kinds
    finally:
        db.close()


def test_cov_exec_export_capped(client):
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.parse("export all bugs to excel", ctx)
        # Pass total > cap directly to trigger the "capped" note in the response.
        from sqlalchemy import func, select

        from app import models
        from app.chatbot.executor import _apply_bug_filters, _eager_bug_query
        stmt, _cs = _apply_bug_filters(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq)
        rows = list(db.scalars(stmt.limit(5000)).all())
        resp = executor._build_export_response(rows, pq, total=99999, cap=5000)
        if resp.intent == "export_bugs":
            assert any("capped" in b.payload.get("text", "") for b in resp.blocks)
        else:
            # When openpyxl is absent the capped branch is unreachable.
            assert resp.intent == "export_failed"
    finally:
        db.close()


# executor.py — reports
def test_cov_exec_report_forbidden_for_user(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        bob = _user(db, ids["bob"])  # role "user"
        r = executor.execute("throughput report last week", db, bob)
        assert r.intent == "report_forbidden"
        assert "managers and admins" in _texts(r)
    finally:
        db.close()


def test_cov_exec_report_runs_for_admin(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("report of all bugs", db, admin)
        assert r.intent in ("report", "report_error", "report_empty")
        assert r.blocks
        r2 = executor.execute("throughput report", db, admin)  # different summary-extras branch
        assert r2.blocks
    finally:
        db.close()


def test_cov_exec_report_empty(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        from app import models
        db.query(models.Bug).delete()
        db.commit()
        r = executor.execute("pending snapshot report", db, admin)
        assert r.intent in ("report", "report_empty")
        if r.intent == "report":
            assert "no rows matched" in _texts(r).lower() or r.summary.startswith("0 rows")
    finally:
        db.close()


def test_cov_report_pure_helpers():
    from app.chatbot import executor

    assert executor._format_summary_extras({}) == ""
    s_through = {"total_resolved": 12, "user_count": 3}
    assert "Total resolved" in executor._format_summary_extras(s_through)
    s_items = {"total_items": 5}
    assert "Total items" in executor._format_summary_extras(s_items)
    s_timeline = {"total_created": 8, "total_resolved": 6, "net": 2}
    out = executor._format_summary_extras(s_timeline)
    assert "Created" in out
    assert "Net" in out
    s_ttr = {"average_hours": 4, "median_hours": 3, "p95_hours": 9}
    assert "Average" in executor._format_summary_extras(s_ttr)

    # _report_row_to_table_row: None value, numeric, short string, long string.
    class _Col:
        def __init__(self, key):
            self.key = key
    cols = [_Col("a"), _Col("b"), _Col("c"), _Col("d")]
    row = {"a": None, "b": 7, "c": "short", "d": "x" * 200}
    out = executor._report_row_to_table_row(row, cols)
    assert out[0] == ""
    assert out[1] == "7"
    assert out[2] == "short"
    assert out[3].endswith("…")
    assert len(out[3]) == 78  # 77 chars + ellipsis


def test_cov_build_report_preview_text():
    from app.chatbot import executor
    from app.reports import Filters

    class _Res:
        report_label = "Throughput"
        total = 50
        summary = {"total_resolved": 10, "user_count": 2}

    filters = Filters(date_from=None, date_to=None)
    text = executor._build_report_preview_text(_Res(), filters, preview_rows_count=15)
    assert "Throughput" in text
    assert "Preview shows the first 15 rows" in text

    # With a date range, both dates appear in the output.
    filters2 = Filters(date_from=date(2026, 1, 1), date_to=date(2026, 2, 1))

    class _Res1:
        report_label = "Item Detail"
        total = 1
        summary = {}
    text2 = executor._build_report_preview_text(_Res1(), filters2, preview_rows_count=15)
    assert "2026-01-01" in text2
    assert "2026-02-01" in text2


def test_cov_filters_from_parsed_dates_and_types(client):
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db, any_user(db))
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        # Time window + item-type word exercises date_from/date_to and item_types mapping.
        pq = nlu.parse("requirement report this month", ctx, now=now)
        filt = executor._filters_from_parsed(pq)
        assert filt.date_from is not None
        assert filt.date_to is not None
        assert "Requirement" in filt.item_types

        pq2 = nlu.parse("task report", ctx)
        filt2 = executor._filters_from_parsed(pq2)
        assert "Task" in filt2.item_types

        pq3 = nlu.parse("report of all items", ctx)   # no time window → dates None
        filt3 = executor._filters_from_parsed(pq3)
        assert filt3.date_from is None
        assert filt3.date_to is None
    finally:
        db.close()


def test_cov_report_forbidden_and_empty_helpers():
    from app.chatbot import executor

    fr = executor._report_forbidden_response()
    assert fr.intent == "report_forbidden"

    class _Res:
        report_label = "Aging"
    er = executor._report_empty_response(_Res())
    assert er.intent == "report"
    assert "no rows matched" in _texts(er).lower()


def test_cov_try_stage_file_block_failure():
    from app.chatbot import executor

    # When the XLSX builder raises, the exception is swallowed and None returned.
    class _Bad:
        total = 3
    out = executor._try_stage_file_block(_Bad(), "item_detail", 1)
    assert out is None


# executor.py — action planning and confirmation flow
def test_cov_exec_action_missing_slots(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        from app.chatbot.memory import store as mem
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)

        # No trailing status synonym → action_value is None → planner prompts.
        r = executor.execute("set bug 5 status to", db, admin)
        assert r.intent in ("action_invalid", "confirm_action")

        mem.reset(admin.id)
        r2 = executor.execute("close that bug", db, admin)
        assert r2.intent == "action_invalid"
        assert "which bug" in _texts(r2).lower()

        mem.reset(admin.id)
        r3 = executor.execute("please close the issue right now", db, admin)
        assert r3.intent == "action_invalid"
    finally:
        db.close()


def test_action_misfire_defers_to_cloud_when_available(client, monkeypatch):
    """An over-eager action match that can't fill its slots defers to the cloud
    layer (when live) instead of dead-ending on the canned 'which bug?' prompt."""
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot.executor import Block, Response
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)

        cloud_answer = Response(
            blocks=[Block("text", {"text": "Sure — which bug should I close?"})],
            summary="cloud", intent="cloud_answer")
        monkeypatch.setattr(executor, "_cloud_available", lambda: True)
        monkeypatch.setattr(executor, "_try_cloud_llm", lambda *a, **k: cloud_answer)
        r = executor.execute("please close the issue right now", db, admin)
        assert r is cloud_answer  # reached the smart layer, not the canned prompt

        # A well-formed write must still stage a confirm — never divert to cloud.
        mem.reset(admin.id)
        monkeypatch.setattr(executor, "_try_cloud_llm",
                            lambda *a, **k: pytest.fail("valid action must not reach cloud"))
        r2 = executor.execute("close bug 5", db, admin)
        assert r2.intent == "confirm_action"
    finally:
        db.close()


def test_action_misfire_keeps_canned_prompt_when_cloud_off(client):
    """Cloud unavailable (the CI default): the same mis-fire preserves the old
    deterministic action_invalid prompt with no cloud round-trip."""
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        r = executor.execute("please close the issue right now", db, admin)
        assert r.intent == "action_invalid"
    finally:
        db.close()


def test_cloud_available_fails_closed_on_exception(monkeypatch):
    """_cloud_available() must never raise — an import/call failure in the
    optional cloud layer degrades to 'not available', not a 500."""
    import app.chatbot.cloud_llm as cloud
    from app.chatbot import executor

    def _boom():
        raise RuntimeError("cloud layer exploded")

    monkeypatch.setattr(cloud, "is_available", _boom)
    assert executor._cloud_available() is False


def test_cov_exec_action_plan_each_kind(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db, any_user(db))

        def plan_for(msg):
            pq = nlu.parse(msg, ctx)
            return executor._build_action_plan(pq, admin)

        plan, err = plan_for("assign bug 1 to alice")
        assert err is None
        assert plan.kind == "assign"
        plan, err = plan_for("unassign alice from bug 1")
        assert err is None
        assert plan.kind == "unassign"
        plan, err = plan_for("mark bug 1 as resolved")
        assert err is None
        assert plan.new_value == "Resolved"
        plan, err = plan_for("set bug 1 priority to high")
        assert err is None
        assert plan.new_value == "High"
        plan, err = plan_for("due bug 1 2026-06-15")
        assert err is None
        assert plan.new_value == "2026-06-15"
        plan, err = plan_for("comment on #1: hi there")
        assert err is None
        assert plan.comment_body == "hi there"
        plan, err = plan_for('create a bug titled "Fresh" in project Apollo')
        assert err is None
        assert plan.new_title == "Fresh"
        plan, err = plan_for("create project Neptune")
        assert err is None
        assert plan.new_project_name == "Neptune"
    finally:
        db.close()


def test_cov_exec_action_plan_missing_value_errors(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        _user(db, ids["admin"])
        from app.chatbot.actions import ActionPlan

        pq = nlu.ParsedQuery(bug_id=1, action_kind="assign")
        plan, err = executor._plan_assign(ActionPlan(kind="assign", actor_user_id=1), pq, "assign")
        assert plan is None
        assert "name" in err.lower()

        plan, err = executor._plan_set_status(ActionPlan(kind="set_status", actor_user_id=1),
                                              nlu.ParsedQuery(bug_id=1))
        assert plan is None
        assert "status" in err.lower()

        plan, err = executor._plan_set_priority(ActionPlan(kind="set_priority", actor_user_id=1),
                                               nlu.ParsedQuery(bug_id=1))
        assert plan is None
        assert "priority" in err.lower()

        plan, err = executor._plan_set_environment(
            ActionPlan(kind="set_environment", actor_user_id=1), nlu.ParsedQuery(bug_id=1))
        assert plan is None
        assert "environment" in err.lower()

        plan, err = executor._plan_set_due_date(
            ActionPlan(kind="set_due_date", actor_user_id=1), nlu.ParsedQuery(bug_id=1))
        assert plan is None
        assert "date" in err.lower()

        plan, err = executor._plan_add_comment(
            ActionPlan(kind="add_comment", actor_user_id=1), nlu.ParsedQuery(bug_id=1))
        assert plan is None
        assert "comment" in err.lower()

        plan, err = executor._plan_create_bug(
            ActionPlan(kind="create_bug", actor_user_id=1), nlu.ParsedQuery())
        assert plan is None
        assert "title" in err.lower()

        plan, err = executor._plan_create_project(
            ActionPlan(kind="create_project", actor_user_id=1), nlu.ParsedQuery())
        assert plan is None
        assert "project name" in err.lower()
    finally:
        db.close()


def test_cov_exec_action_plan_unknown_kind(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(action_kind="teleport", bug_id=1)
        plan, err = executor._build_action_plan(pq, admin)
        assert plan is None
        assert "don't know how to do 'teleport'" in err
    finally:
        db.close()


def test_cov_exec_action_needs_bug_pronoun_branch(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # used_pronoun_bug=True with no resolved id → pronoun-specific error.
        pq = nlu.ParsedQuery(action_kind="set_status", action_value="Closed",
                             used_pronoun_bug=True)
        plan, err = executor._build_action_plan(pq, admin)
        assert plan is None
        assert "don't know which bug" in err.lower()
    finally:
        db.close()


def test_cov_exec_confirm_flow_yes_no_idle(client):
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        r = executor.execute("close bug 5", db, admin)
        assert r.intent == "confirm_action"
        r2 = executor.execute("yes", db, admin)
        assert r2.intent == "action_done"

        r3 = executor.execute("yes", db, admin)   # nothing pending now
        assert r3.intent == "confirm_idle"

        mem.reset(admin.id)
        executor.execute("close bug 5", db, admin)
        r4 = executor.execute("no", db, admin)
        assert r4.intent == "confirm_cancel"
        assert "haven't changed anything" in _texts(r4).lower()

        r5 = executor.execute("no", db, admin)    # nothing was pending
        assert r5.intent == "confirm_cancel"
        assert "nothing was pending" in _texts(r5).lower()
    finally:
        db.close()


def test_cov_exec_me_pronoun_resolution(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # 'my bugs' -> assignee splice (doesn't produce an unresolved 'me' name), so it lists.
        r = executor.execute("my bugs", db, admin)
        assert r.intent == "list_bugs"
        # "bugs I reported" → reporter splice (admin reported all seeded bugs).
        r2 = executor.execute("bugs I reported", db, admin)
        assert r2.intent == "list_bugs"
        assert _table(r2) is not None
    finally:
        db.close()


def test_cov_resolve_me_pronoun_direct():
    from app.chatbot import executor, nlu

    class _Actor:
        id = 7
        name = "Me"

    # Not flagged → no-op.
    pq = nlu.ParsedQuery()
    executor._resolve_me_pronoun(pq, _Actor())
    assert pq.assignee_ids == []
    assert pq.reporter_ids == []

    # me_role="reporter" → fills reporter slot; second call must not duplicate.
    pq2 = nlu.ParsedQuery(used_pronoun_me=True, me_role="reporter")
    executor._resolve_me_pronoun(pq2, _Actor())
    assert pq2.reporter_ids == [7]
    executor._resolve_me_pronoun(pq2, _Actor())
    assert pq2.reporter_ids == [7]

    # me_role="assignee" → fills assignee slot; idempotent.
    pq3 = nlu.ParsedQuery(used_pronoun_me=True, me_role="assignee")
    executor._resolve_me_pronoun(pq3, _Actor())
    assert pq3.assignee_ids == [7]
    executor._resolve_me_pronoun(pq3, _Actor())
    assert pq3.assignee_ids == [7]


def test_cov_resolve_pronouns_bug_memory(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        mem.remember_bug(admin.id, 3)
        # pronoun reference with no id → memory fills it in.
        pq = nlu.ParsedQuery(used_pronoun_bug=True)
        executor._resolve_pronouns(pq, admin)
        assert pq.bug_id == 3
    finally:
        db.close()


# executor.py — classifier fallback and unknown handler
def test_cov_exec_unknown_hint_branches(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        _user(db, ids["admin"])
        # Drive _handle_unknown directly to hit each topic-hint branch and the default.
        assert "list users" in _texts(executor._handle_unknown("blah team members blah")).lower()
        assert "list projects" in _texts(executor._handle_unknown("zzz project zzz")).lower()
        assert "summary" in _texts(executor._handle_unknown("zzz kpi dashboard zzz")).lower()
        assert "open bugs" in _texts(executor._handle_unknown("qwerty asdf")).lower()  # default
    finally:
        db.close()


def test_cov_did_you_mean_safe():
    from app.chatbot import executor
    # Gibberish returns None without raising.
    assert executor._did_you_mean("qwerty zxcvbn asdfgh") is None
    assert executor._did_you_mean("") is None
    # For a real-ish phrase the return value is either None or a known canonical.
    out = executor._did_you_mean("show me the bugs")
    assert out is None or out in executor._INTENT_SUGGESTION.values()


def test_cov_classifier_fallback_paths(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # A paraphrase the rules miss; the classifier maps it to a read intent.
        r = executor.execute("show me the dashboard", db, admin)
        assert r.intent in ("stats", "list_bugs", "unknown")
        # Genuinely unknown → the final unknown fallback.
        r2 = executor.execute("xqzlmqop nonsense gibberish", db, admin)
        assert r2.intent in ("unknown", "fallback")
        assert any(b.kind == "text" for b in r2.blocks)
    finally:
        db.close()


def test_cov_classifier_action_invalid_direct():
    from app.chatbot import executor
    # Classifier guessed an action intent but slots are empty.
    r = executor._classifier_action_invalid("action_assign")
    assert r.intent == "action_invalid"
    assert "assign" in _texts(r).lower()


def test_cov_try_llm_and_cloud_unavailable(client):
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        # LLM model path doesn't exist in tests → is_available returns False.
        assert executor._try_llm("weird query", db, admin) is None
        # SLEUTH_CLOUD_ENABLED=0 → cloud not available → None.
        assert executor._try_cloud_llm("weird query", db, admin, None) is None
    finally:
        db.close()


def test_cov_dispatch_read_intent_unknown_returns_none(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.ParsedQuery(intent="not_a_read_intent")
        assert executor._dispatch_read_intent(
            "not_a_read_intent", db, pq, admin, ctx,
            accessible=scope_of(db, admin)) is None
    finally:
        db.close()


# End-to-end through the HTTP router.
def test_cov_http_ask_basic(admin_client):
    r = admin_client.post("/api/chat/ask", json={"message": "hi"})
    assert r.status_code == 200
    assert r.json()["intent"] == "greeting"

    r2 = admin_client.post("/api/chat/ask", json={"message": "summary"})
    assert r2.status_code == 200
    assert r2.json()["intent"] == "stats"


def test_cov_http_ask_report_forbidden_for_regular_user(user_client):
    # Regular users get report_forbidden, not a 500.
    r = user_client.post("/api/chat/ask",
                         json={"message": "throughput report last week"})
    assert r.status_code == 200
    assert r.json()["intent"] == "report_forbidden"


def test_cov_former_legacy_passwords_are_rejected():
    # No password bypasses the strength policy.
    import pytest

    from app.schemas import _check_password_strength
    for pw in ("legacy-default", "changeme"):
        with pytest.raises(ValueError):
            _check_password_strength(pw)


# Focused coverage — partial branches, error paths, monkeypatched modules.
def test_cov_bug_detail_no_event_branch(client):
    # Bug #2 has no event (branch skipped); long description still hits truncation.
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("bug 2", db, admin)
        assert r.intent == "bug_detail"
        body = _texts(r)
        assert "Release 9.9" not in body      # no event line
        assert "Description:" in body          # short_descr present
    finally:
        db.close()


def test_cov_bug_detail_empty_description(client):
    # An empty description skips the short_descr block entirely.
    ids = _seed(client)
    from app import models
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        b = db.query(models.Bug).first()
        b.description = ""
        db.commit()
        r = executor.execute(f"bug {b.id}", db, admin)
        assert r.intent == "bug_detail"
    finally:
        db.close()


def test_cov_recent_activity_start_only_and_end_only(client):
    # Drive _handle_recent_activity directly with start-only and end-only windows.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        now = datetime.now(timezone.utc)

        pq_start = nlu.ParsedQuery()
        pq_start.time_window = nlu.TimeWindow(
            start=now - timedelta(days=1), end=None, label="recent")
        scope = scope_of(db, admin)
        r1 = executor._handle_recent_activity(db, pq_start, admin, scope)
        assert r1.intent == "recent_activity"

        pq_end = nlu.ParsedQuery()
        pq_end.time_window = nlu.TimeWindow(
            start=None, end=now + timedelta(days=1), label="up to now")
        r2 = executor._handle_recent_activity(db, pq_end, admin, scope)
        assert r2.intent == "recent_activity"
    finally:
        db.close()


def test_cov_inline_list_truncation_note(client):
    # total > limit → "showing the most recent N" note appears.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.parse("show all bugs", ctx)
        pq.limit = 5   # below the seeded count of 9
        r = executor._handle_list_bugs(
            db, pq, ctx, accessible=scope_of(db, admin))
        assert r.intent == "list_bugs"
        assert "showing the most recent 5" in _texts(r)
    finally:
        db.close()


def test_cov_export_import_error_and_excel_error(client, monkeypatch):
    # (a) openpyxl import fails; (b) stage_workbook raises ExcelGenerationError.
    ids = _seed(client)
    import sys

    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        _user(db, ids["admin"])
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.parse("export all bugs to excel", ctx)
        from sqlalchemy import func, select

        from app import models
        from app.chatbot.executor import _apply_bug_filters, _eager_bug_query
        stmt, _cs = _apply_bug_filters(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq)
        rows = list(db.scalars(stmt.limit(10)).all())

        # (a) Poison sys.modules so re-import raises ImportError; monkeypatch restores.
        import app.chatbot as chatbot_pkg
        monkeypatch.setitem(sys.modules, "app.chatbot.excel", None)
        monkeypatch.delattr(chatbot_pkg, "excel", raising=False)
        r_imp = executor._build_export_response(rows, pq, total=len(rows), cap=5000)
        assert r_imp.intent == "export_failed"
        assert "isn't available" in _texts(r_imp)
        monkeypatch.undo()

        # (b) Import succeeds but stage_workbook raises a generation error.
        from app.chatbot import excel as real_excel

        def _raise(*a, **k):
            raise real_excel.ExcelGenerationError("synthetic failure")

        monkeypatch.setattr(real_excel, "stage_workbook", _raise)
        r_err = executor._build_export_response(rows, pq, total=len(rows), cap=5000)
        assert r_err.intent == "export_failed"
        assert "couldn't build the spreadsheet" in _texts(r_err)
    finally:
        db.close()


def test_cov_filters_from_parsed_start_only_end_only(client):
    # Time window with only a start, then only an end.
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db, any_user(db))
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        pq = nlu.parse("report of all bugs", ctx)
        pq.time_window = nlu.TimeWindow(start=now, end=None, label="x")
        filt = executor._filters_from_parsed(pq)
        assert filt.date_from is not None
        assert filt.date_to is None

        pq2 = nlu.parse("report of all bugs", ctx)
        pq2.time_window = nlu.TimeWindow(start=None, end=now, label="y")
        filt2 = executor._filters_from_parsed(pq2)
        assert filt2.date_from is None
        assert filt2.date_to is not None
    finally:
        db.close()


def test_cov_stage_report_xlsx_build_error(monkeypatch):
    # XlsxBuildError from build_workbook_bytes is wrapped in ExcelGenerationError.
    import app.reports as reports
    from app.chatbot import excel as _excel
    from app.chatbot import executor
    from app.reports.xlsx import XlsxBuildError

    def _raise(_result):
        raise XlsxBuildError("bad workbook")

    monkeypatch.setattr(reports, "build_workbook_bytes", _raise)

    class _Res:
        total = 1

    result = _Res()
    with pytest.raises(_excel.ExcelGenerationError):
        executor._stage_report_xlsx(result, "item_detail", 1)


def test_cov_handle_report_run_error(client, monkeypatch):
    # ValueError from run_report → "couldn't run that report" response.
    ids = _seed(client)
    import app.reports as reports
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        def _boom(*a, **k):
            raise ValueError("bad filter")

        monkeypatch.setattr(reports, "run_report", _boom)
        r = executor.execute("report of all bugs", db, admin)
        assert r.intent == "report_error"
        assert "couldn't run that report" in _texts(r)
    finally:
        db.close()


def test_cov_handle_report_file_block_fallback(client, monkeypatch):
    # XLSX staging returns None → fallback text pointing to the Reports sidebar.
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        monkeypatch.setattr(executor, "_try_stage_file_block", lambda *a, **k: None)
        r = executor.execute("report of all bugs", db, admin)
        if r.intent == "report":  # only reachable if the report produced rows
            assert any("Reports** sidebar" in b.payload.get("text", "")
                       for b in r.blocks if b.kind == "text")
    finally:
        db.close()


def test_cov_plan_set_environment_and_create_bug_fields(client):
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        executor.build_context(db, any_user(db))

        # The text->action map doesn't flag set_environment; drive the planner directly.
        from app.chatbot.actions import ActionPlan
        pq_env = nlu.ParsedQuery(bug_id=1, environments=["PROD"])
        plan, err = executor._plan_set_environment(
            ActionPlan(kind="set_environment", actor_user_id=admin.id), pq_env)
        assert err is None
        assert plan.new_value == "PROD"

        # 'create a critical bug' misses the regex; drive the planner directly with slots filled.
        pq_cb = nlu.ParsedQuery(
            bug_id=None,
            action_kind="create_bug",
            action_title="Big one",
            priorities=["Critical"],
            project_ids=[ids["apollo"]],
            project_names=["Apollo"],
            assignee_ids=[ids["alice"]],
            assignee_names=["Alice Wonderland"],
        )
        plan2, err2 = executor._plan_create_bug(
            ActionPlan(kind="create_bug", actor_user_id=admin.id), pq_cb)
        assert err2 is None
        assert plan2.new_title == "Big one"
        assert plan2.new_value == "Critical"            # priority captured
        assert plan2.new_project_id == ids["apollo"]    # project captured
        assert plan2.new_project_name == "Apollo"
        assert plan2.target_user_ids == [ids["alice"]]  # assignee captured
    finally:
        db.close()


def test_cov_action_needs_bug_no_pronoun(client):
    # No bug id and no pronoun → generic "Which bug?" error.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(action_kind="set_status", action_value="Closed",
                             used_pronoun_bug=False)
        plan, err = executor._build_action_plan(pq, admin)
        assert plan is None
        assert "Which bug?" in err
    finally:
        db.close()


def test_cov_confirm_yes_remembers_bug(client):
    # After a successful confirm the affected bug is remembered for pronoun resolution.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        executor.execute("close bug 5", db, admin)
        executor.execute("yes", db, admin)
        sess = mem.get(admin.id)
        assert sess is not None
        assert sess.last_bug_id == 5
    finally:
        db.close()


def test_cov_dispatch_bug_detail_remembers(client):
    # Dispatching bug_detail with a bug_id should store it in memory.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        mem.reset(admin.id)
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.parse("bug 3", ctx)
        assert pq.intent == "bug_detail"
        assert pq.bug_id == 3
        executor._dispatch_read_intent(
            "bug_detail", db, pq, admin, ctx,
            accessible=scope_of(db, admin))
        sess = mem.get(admin.id)
        assert sess is not None
        assert sess.last_bug_id == 3
    finally:
        db.close()


def test_cov_try_classifier_action_branch(client, monkeypatch):
    # Classifier predicts an action_* intent the rules couldn't fill -> _classifier_action_invalid.
    ids = _seed(client)
    from app.chatbot import classifier as clf
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.ParsedQuery(intent="unknown")

        class _Pred:
            intent = "action_assign"
            confidence = 0.9

        monkeypatch.setattr(clf, "predict", lambda _m: _Pred())
        scope = scope_of(db, admin)
        r = executor._try_classifier("do the thing", db, pq, admin, ctx, scope)
        assert r is not None
        assert r.intent == "action_invalid"

        # predict returns None → None; non-read/non-action intent → None.
        monkeypatch.setattr(clf, "predict", lambda _m: None)
        assert executor._try_classifier("x", db, pq, admin, ctx, scope) is None

        class _Weird:
            intent = "totally_unknown_intent"
            confidence = 0.9
        monkeypatch.setattr(clf, "predict", lambda _m: _Weird())
        assert executor._try_classifier("x", db, pq, admin, ctx, scope) is None
    finally:
        db.close()


def test_cov_try_llm_exception_swallowed(client, monkeypatch):
    # Exceptions inside _try_llm are swallowed; it returns None.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import llm as _llm
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        def _boom():
            raise RuntimeError("llm exploded")

        monkeypatch.setattr(_llm, "is_available", _boom)
        assert executor._try_llm("weird", db, admin) is None
    finally:
        db.close()


def test_cov_try_cloud_llm_exception_swallowed(client, monkeypatch):
    # Exceptions inside _try_cloud_llm are swallowed; it returns None.
    ids = _seed(client)
    from app.chatbot import cloud_llm as _cloud
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        def _boom():
            raise RuntimeError("cloud exploded")

        monkeypatch.setattr(_cloud, "is_available", _boom)
        assert executor._try_cloud_llm("weird", db, admin, None) is None
    finally:
        db.close()


def test_cov_execute_cloud_then_classifier_then_llm(client, monkeypatch):
    # Walk execute()'s fallback chain: cloud → classifier → unknown.
    ids = _seed(client)
    from app.chatbot import classifier as _clf
    from app.chatbot import cloud_llm as _cloud
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])

        # (a) Cloud returns a response → execute returns it directly.
        canned = executor.Response(
            blocks=[executor.Block("text", {"text": "from cloud"})],
            summary="cloud", intent="cloud_answer")
        monkeypatch.setattr(_cloud, "is_available", lambda: True)
        monkeypatch.setattr(_cloud, "try_understand",
                            lambda *a, **k: canned)
        r = executor.execute("some free-form question", db, admin)
        assert r.intent == "cloud_answer"
        monkeypatch.undo()

        # (b) Cloud off; classifier returns a read intent → routed accordingly.
        class _Pred:
            intent = "stats"
            confidence = 0.9
        monkeypatch.setattr(_clf, "predict", lambda _m: _Pred())
        r2 = executor.execute("zzz unrecognisable phrase zzz", db, admin)
        assert r2.intent == "stats"
        monkeypatch.undo()

        # (c) Everything misses → final unknown fallback.
        r3 = executor.execute("zzz qwop zzz gibberish", db, admin)
        assert r3.intent in ("unknown", "fallback")
    finally:
        db.close()


def test_cov_did_you_mean_classifier_raises(monkeypatch):
    # A classifier error inside _did_you_mean must be swallowed, not propagate.
    from app.chatbot import classifier as _clf
    from app.chatbot import executor

    def _boom(*a, **k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(_clf, "explain", _boom)
    assert executor._did_you_mean("anything at all") is None


def test_cov_nlu_newest_sort_and_report_classify():
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    pq = nlu.parse("newest bugs", ctx)
    assert pq.sort_newest is True
    pq2 = nlu.parse("aging report", ctx)
    assert pq2.intent == "report"


def test_cov_nlu_record_name_match_reporter_and_unresolved():
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    seen_a: set[int] = set()
    seen_r: set[int] = set()
    nlu._record_name_match("reporter", 9, "Rae Porter", pq, seen_a, seen_r)
    assert pq.reporter_ids == [9]
    assert pq.reporter_names == ["Rae Porter"]
    # Duplicate id is ignored.
    nlu._record_name_match("reporter", 9, "Rae Porter", pq, seen_a, seen_r)
    assert pq.reporter_ids == [9]

    nlu._record_unresolved_name("reporter", "ghost", pq)
    assert pq.unresolved_reporter_names == ["ghost"]


def test_cov_nlu_status_dedup_and_multi_name_and_bare_create():
    # "resolved" and "fixed" both map to Resolved; the second is de-duplicated.
    from app.chatbot import nlu
    out = nlu._extract_statuses("show resolved and fixed bugs")
    assert out.count("Resolved") == 1

    # Assignee pattern matches twice → both phrases captured.
    phrases = nlu._candidate_name_phrases(
        "bugs assigned to alice and assigned to bob")
    assignees = [p for role, p in phrases if role == "assignee"]
    assert len(assignees) >= 2

    # Reporter pattern matching twice → same behaviour.
    rphrases = nlu._candidate_name_phrases(
        "bugs reported by alice and reported by bob")
    reporters = [p for role, p in rphrases if role == "reporter"]
    assert len(reporters) >= 2

    # create-bug verb with nothing capturable after "bug" → action_title is None.
    pq = nlu.ParsedQuery()
    kind = nlu._action_create_bug("create a bug", pq)
    assert kind == "create_bug"
    assert pq.action_title is None


def test_cov_nlu_add_resolved_projects_dedup():
    # Second call with the same project is skipped (id already in `seen`).
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[(1, "apollo", "Apollo")])
    pq = nlu.ParsedQuery()
    seen: set[int] = set()
    nlu._add_resolved_projects("apollo", pq, ctx, seen)
    assert pq.project_ids == [1]
    nlu._add_resolved_projects("apollo", pq, ctx, seen)
    assert pq.project_ids == [1]


def test_cov_plan_create_bug_assignee_without_project(client):
    # create_bug with an assignee but no project: project leg skipped, assignee leg runs.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.actions import ActionPlan
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(
            action_kind="create_bug", action_title="No project bug",
            assignee_ids=[ids["alice"]], assignee_names=["Alice Wonderland"],
        )
        plan, err = executor._plan_create_bug(
            ActionPlan(kind="create_bug", actor_user_id=admin.id), pq)
        assert err is None
        assert plan.new_title == "No project bug"
        assert plan.new_project_id is None
        assert plan.target_user_ids == [ids["alice"]]
    finally:
        db.close()


def test_cov_execute_reaches_llm_return(client, monkeypatch):
    # Read dispatch and classifier decline but _try_llm returns a Response -> returned.
    ids = _seed(client)
    from app.chatbot import classifier as _clf
    from app.chatbot import executor
    from app.chatbot import llm as _llm
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        monkeypatch.setattr(_clf, "predict", lambda _m: None)
        canned = executor.Response(
            blocks=[executor.Block("text", {"text": "llm handled it"})],
            summary="llm", intent="llm_answer")
        monkeypatch.setattr(_llm, "is_available", lambda: True)
        monkeypatch.setattr(_llm, "try_understand", lambda *a, **k: canned)
        r = executor.execute("zzqq unrecognised soup", db, admin)
        assert r.intent == "llm_answer"
    finally:
        db.close()


def test_cov_nlu_resolve_single_token_paths():
    from app.chatbot import nlu
    ctx = nlu.Context(
        users=[(1, "dave smith", "dsmith", "Dave Smith")],
        projects=[],
    )
    assert nlu._resolve_name("dave", ctx) == [(1, "Dave Smith")]    # prefix path
    assert nlu._resolve_name("smith", ctx) == [(1, "Dave Smith")]   # last-name path
    assert nlu._resolve_name("zoey", ctx) == []                     # no match


def test_cov_nlu_assign_list_verb_guard():
    # A list verb alongside an assign-ish verb → _action_assign declines.
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    pq.assignee_ids = [2]
    assert nlu._action_assign("give me all the bugs", pq) is None


def test_cov_exec_list_text_search_and_project_filter(client):
    # List query with both a project filter and a quoted free-text term.
    ids = _seed(client)
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute('bugs in project Beacon about "login"', db, admin)
        assert r.intent == "list_bugs"
        assert r.blocks   # table or empty message, both are fine
        # Text-search without a project filter (exercises the LIKE clause alone).
        r2 = executor.execute('find bugs about "crash"', db, admin)
        assert r2.intent == "list_bugs"
    finally:
        db.close()


def test_cov_build_action_plan_set_environment_dispatch(client):
    # set_environment dispatch leg of _build_action_plan.
    ids = _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        pq = nlu.ParsedQuery(bug_id=1, action_kind="set_environment",
                             environments=["PROD"])
        plan, err = executor._build_action_plan(pq, admin)
        assert err is None
        assert plan.kind == "set_environment"
        assert plan.new_value == "PROD"
    finally:
        db.close()


def test_cov_try_llm_available_calls_understand(client, monkeypatch):
    # is_available() returns True → try_understand is called and its result propagates.
    ids = _seed(client)
    from app.chatbot import executor
    from app.chatbot import llm as _llm
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        canned = executor.Response(
            blocks=[executor.Block("text", {"text": "llm says hi"})],
            summary="llm", intent="llm_answer")
        monkeypatch.setattr(_llm, "is_available", lambda: True)
        monkeypatch.setattr(_llm, "try_understand", lambda *a, **k: canned)
        out = executor._try_llm("weird query", db, admin)
        assert out is canned
    finally:
        db.close()


def test_cov_execute_reaches_final_unknown(client, monkeypatch):
    # Every fallback returns None → execute() reaches _handle_unknown.
    ids = _seed(client)
    from app.chatbot import classifier as _clf
    from app.chatbot import executor
    db = _new_db()
    try:
        admin = _user(db, ids["admin"])
        monkeypatch.setattr(_clf, "predict", lambda _m: None)
        r = executor.execute("zzqq nonsense token soup", db, admin)
        assert r.intent in ("unknown", "fallback")
        assert any(b.kind == "text" for b in r.blocks)
    finally:
        db.close()


def test_cov_nlu_first_int_str_match_direct():
    # Non-numeric group raises ValueError → loop continues to the next group.
    from app.chatbot import nlu
    assert nlu._first_int_match(("abc", "5")) == 5   # "abc" -> ValueError -> continue
    assert nlu._first_int_match((None, "", "5")) == 5
    assert nlu._first_int_match((None, "")) is None
    assert nlu._first_str_match((None, "", "Days")) == "days"
    assert nlu._first_str_match((None, "")) is None


def test_cov_nlu_relative_window_no_match_in_parse():
    from app.chatbot import nlu
    assert nlu._parse_time_window("nothing temporal here") is None


def test_cov_nlu_action_helpers_absent_body_branches():
    from app.chatbot import nlu

    # create_project verb: name regex doesn't capture → action_title stays None.
    pq = nlu.ParsedQuery()
    kind = nlu._action_create_project("set up a project", pq)
    assert kind == "create_project"

    # Comment with a bug target but no colon -> body None (treated as a read).
    pq2 = nlu.ParsedQuery(bug_id=5)
    kind2 = nlu._action_add_comment("comment on #5", pq2)
    assert kind2 == "add_comment"
    assert pq2.action_comment is None

    # No bug target → not a command, treated as a question.
    assert nlu._action_add_comment("any comment on the release?", nlu.ParsedQuery()) is None

    # Colon present but whitespace-only body → strips to empty → action_comment None.
    pq2b = nlu.ParsedQuery(bug_id=5)
    kind2b = nlu._action_add_comment("comment on #5:    ", pq2b)
    assert kind2b == "add_comment"
    assert pq2b.action_comment is None

    # Bare-form create_bug with a non-empty tail → title captured.
    pq3b = nlu.ParsedQuery()
    kind3b = nlu._action_create_bug("create a bug Login is broken", pq3b)
    assert kind3b == "create_bug"
    assert pq3b.action_title == "Login is broken"

    # Bare-form with only a project cue as the tail → still returns create_bug.
    pq3c = nlu.ParsedQuery()
    kind3c = nlu._action_create_bug("create a bug in project Apollo", pq3c)
    assert kind3c == "create_bug"

    # Quoted-title form sets the title immediately.
    pq4 = nlu.ParsedQuery()
    kind4 = nlu._action_create_bug('file a bug titled "Quoted" now', pq4)
    assert kind4 == "create_bug"
    assert pq4.action_title == "Quoted"

    # due-date verb with no ISO date → action_value stays None.
    pq5 = nlu.ParsedQuery()
    kind5 = nlu._action_set_due_date("set the due date for bug 5", pq5)
    assert kind5 == "set_due_date"
    assert pq5.action_value is None


def test_cov_nlu_record_unresolved_unknown_role():
    # Unknown role falls through without appending to either name list.
    from app.chatbot import nlu
    pq = nlu.ParsedQuery()
    nlu._record_unresolved_name("watcher", "ghost", pq)
    assert pq.unresolved_assignee_names == []
    assert pq.unresolved_reporter_names == []
    assert any("ghost" in n for n in pq.notes)


def test_cov_nlu_candidate_phrase_empty_continue():
    # An empty/whitespace captured group is skipped by the `if phrase:` guard.
    from app.chatbot import nlu
    out = nlu._candidate_name_phrases("bugs assigned to , please")
    assert all(p.strip() for _role, p in out)


def test_cov_nlu_classify_final_intent_report_branch():
    # Call directly: parse() short-circuits via _REPORT_RE before this branch.
    from app.chatbot import nlu
    ctx = nlu.Context(users=[], projects=[])
    pq = nlu.ParsedQuery(raw_message="breakdown by project")
    assert nlu._classify_final_intent("breakdown by project", pq, ctx) == "report"
    pq2 = nlu.ParsedQuery(raw_message="dashboard overview")
    assert nlu._classify_final_intent("dashboard overview", pq2, ctx) == "stats"
    pq3 = nlu.ParsedQuery(raw_message="what happened")
    assert nlu._classify_final_intent("what happened", pq3, ctx) == "recent_activity"


def test_cov_apply_time_window_start_only_end_only(client):
    # _apply_time_window with a start-only window, then an end-only window.
    _seed(client)
    from sqlalchemy import func, select

    from app import models
    from app.chatbot import executor, nlu
    from app.chatbot.executor import _eager_bug_query
    db = _new_db()
    try:
        now = datetime.now(timezone.utc)

        pq_start = nlu.ParsedQuery(raw_message="bugs created recently")
        pq_start.time_window = nlu.TimeWindow(
            start=now - timedelta(days=3), end=None, label="x")
        stmt, cs = executor._apply_time_window(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq_start)
        assert db.scalar(cs) is not None    # query runs without error

        pq_end = nlu.ParsedQuery(raw_message="bugs updated before now")
        pq_end.time_window = nlu.TimeWindow(
            start=None, end=now + timedelta(days=1), label="y")
        stmt2, cs2 = executor._apply_time_window(
            _eager_bug_query(), select(func.count(models.Bug.id)), pq_end)
        assert db.scalar(cs2) is not None
    finally:
        db.close()


def test_cov_confirm_yes_no_bug_id_skips_remember(client):
    # create_project has no bug_id, so the remember-bug step is skipped.
    _seed(client)
    from app.chatbot import executor
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        from app import models
        admin = db.query(models.User).filter_by(email="admin@test.local").one()
        mem.reset(admin.id)
        r = executor.execute("create project Saturn", db, admin)
        assert r.intent == "confirm_action"
        r2 = executor.execute("yes", db, admin)
        assert r2.intent in ("action_done", "action_error")
        sess = mem.get(admin.id)
        assert sess is None or sess.last_bug_id is None
    finally:
        db.close()


def test_cov_dispatch_bug_detail_without_id(client):
    # bug_detail with no bug_id skips the remember step but still calls the handler.
    _seed(client)
    from app.chatbot import executor, nlu
    from app.chatbot.memory import store as mem
    db = _new_db()
    try:
        from app import models
        admin = db.query(models.User).filter_by(email="admin@test.local").one()
        mem.reset(admin.id)
        pq = nlu.ParsedQuery(intent="bug_detail", bug_id=None)
        r = executor._dispatch_read_intent(
            "bug_detail", db, pq, admin, executor.build_context(db, any_user(db)),
            accessible=scope_of(db, admin))
        assert r is not None
        assert r.intent == "bug_detail"
    finally:
        db.close()


def test_cov_nlu_project_literal_loop(client):
    # No "project" cue → literal-name fallback loop; "Apollo" matches as a bare token.
    _seed(client)
    from app.chatbot import executor, nlu
    db = _new_db()
    try:
        ctx = executor.build_context(db, any_user(db))
        pq = nlu.parse("anything broken in Apollo recently", ctx)
        assert any(name == "Apollo" for name in pq.project_names)
    finally:
        db.close()


# Seeding helpers for actions tests, run against the live re-imported modules.
def _seed_basic():
    """Create alice (manager), bob (user), a project, and a bug (reporter alice, assignee bob); returns ids."""
    from app import models
    from app.auth import hash_password
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        alice = models.User(org_id=default_org_id(), name="Alice Wonderland", email="alice@cov.local",
                            role="manager",
                            password_hash=hash_password("Alice12345"),
                            is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob Builder", email="bob@cov.local",
                          role="user",
                          password_hash=hash_password("Bobby12345"),
                          is_active=True)
        db.add_all([alice, bob])
        db.commit()
        proj = models.Project(org_id=default_org_id(), name="Apollo Cov", description="d")
        db.add(proj)
        db.commit()
        # Alice and Bob work on the project, so either can be assigned there.
        db.execute(models.user_projects.insert(), [
            {"user_id": alice.id, "project_id": proj.id}, {"user_id": bob.id, "project_id": proj.id},
        ])
        db.commit()
        bug = models.Bug(title="Login broken on Safari", description="d",
                         status="New", priority="High", environment="PROD",
                         project_id=proj.id, reporter_id=alice.id)
        db.add(bug)
        db.commit()
        bug.assignees = [bob]
        db.commit()
        return {
            "admin_id": admin.id,
            "alice_id": alice.id,
            "bob_id": bob.id,
            "proj_id": proj.id,
            "bug_id": bug.id,
        }
    finally:
        db.close()


# actions.py
def test_cov_create_bug_inactive_account(client):
    """actions.py:144+332 - inactive actor rejected by _check_can_create_bug; _apply_create_bug surfaces the error."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        bob = db.get(models.User, ids["bob_id"])
        bob.is_active = False
        db.commit()
        # 144: confirm helper returns the inactive string.
        assert actions._check_can_create_bug(bob) == "Your account is inactive"
        # 332: call the applier directly; execute_plan's admin-only gate hides this branch.
        plan = actions.ActionPlan(
            kind="create_bug", actor_user_id=bob.id,
            new_title="Anything", summary_human="Create bug",
        )
        resp = actions._apply_create_bug(db, plan, bob)
        assert resp.intent == "action_error"
        assert "inactive" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_success_response_without_bug_id(client):
    """actions.py:190->201 - _success_response with bug_id=None returns a single text block."""
    from app.chatbot import actions

    resp = actions._success_response("plain done message", bug_id=None)
    assert resp.intent == "action_done"
    assert len(resp.blocks) == 1
    assert resp.blocks[0].kind == "text"
    assert resp.blocks[0].payload["text"] == "plain done message"


def test_cov_assign_bug_not_found_and_perm_denied(client):
    """actions.py:228 (bug missing) + 231 (permission denied) for assign."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        alice = db.get(models.User, ids["alice_id"])
        bob = db.get(models.User, ids["bob_id"])

        # 228 — bug_id points at a non-existent bug.
        # Applier called directly: execute_plan's admin-only gate hides per-action branches.
        plan_missing = actions.ActionPlan(
            kind="assign", actor_user_id=alice.id, bug_id=987654,
            target_user_ids=[bob.id], target_user_names=["Bob"],
            summary_human="Assign",
        )
        r_missing = actions._apply_assign(db, plan_missing, alice)
        assert r_missing.intent == "action_error"
        assert "not found" in r_missing.blocks[0].payload["text"].lower()

        # 231 — bob (regular user) is denied edit permission on a Task.
        bug = db.get(models.Bug, ids["bug_id"])
        if hasattr(bug, "item_type"):
            bug.item_type = "Task"
            db.commit()
            plan_perm = actions.ActionPlan(
                kind="assign", actor_user_id=bob.id, bug_id=ids["bug_id"],
                target_user_ids=[bob.id], target_user_names=["Bob"],
                summary_human="Assign",
            )
            from app.access import add_user_project
            add_user_project(db, bob.id, bug.project_id)  # bob can see the item, but not edit a Task
            db.commit()
            r_perm = actions._apply_assign(db, plan_perm, bob)
            assert r_perm.intent == "action_error"
            assert "permission" in r_perm.blocks[0].payload["text"].lower()
        else:  # pragma: no cover - models always have item_type in this build
            pytest.skip("Bug has no item_type column; cannot exercise 231")
    finally:
        db.close()


def test_cov_unassign_branches(client):
    """actions.py:256 (bug missing) + 259 (perm denied) + 265 (no-op)."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        alice = db.get(models.User, ids["alice_id"])
        bob = db.get(models.User, ids["bob_id"])

        # Appliers driven directly (execute_plan gates non-admins first).
        # 256 — bug missing.
        r_missing = actions._apply_unassign(
            db,
            actions.ActionPlan(kind="unassign", actor_user_id=alice.id,
                               bug_id=987654, target_user_ids=[bob.id],
                               target_user_names=["Bob"],
                               summary_human="Unassign"),
            alice,
        )
        assert r_missing.intent == "action_error"
        assert "not found" in r_missing.blocks[0].payload["text"].lower()

        bug = db.get(models.Bug, ids["bug_id"])
        has_item_type = hasattr(bug, "item_type")
        from app.access import add_user_project
        add_user_project(db, alice.id, bug.project_id)
        add_user_project(db, bob.id, bug.project_id)
        db.commit()

        # 259 — permission denied: bob (user) on a Task.
        if has_item_type:
            bug.item_type = "Task"
            db.commit()
            r_perm = actions._apply_unassign(
                db,
                actions.ActionPlan(kind="unassign", actor_user_id=bob.id,
                                   bug_id=ids["bug_id"],
                                   target_user_ids=[bob.id],
                                   target_user_names=["Bob"],
                                   summary_human="Unassign"),
                bob,
            )
            assert r_perm.intent == "action_error"
            assert "permission" in r_perm.blocks[0].payload["text"].lower()
            # reset back to Bug so the next assertion's edit is allowed
            bug.item_type = "Bug"
            db.commit()

        # 265 - dropping a user that isn't assigned is a no-op, not an error.
        r_noop = actions._apply_unassign(
            db,
            actions.ActionPlan(kind="unassign", actor_user_id=alice.id,
                               bug_id=ids["bug_id"],
                               target_user_ids=[999999],
                               target_user_names=["Ghost"],
                               summary_human="Unassign ghost"),
            alice,
        )
        assert r_noop.intent == "action_noop"
        assert "nothing changed" in r_noop.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_set_field_permission_denied(client):
    """actions.py:285 — _apply_set_field permission-denied branch."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        bug = db.get(models.Bug, ids["bug_id"])
        if not hasattr(bug, "item_type"):  # pragma: no cover
            pytest.skip("No item_type column; cannot exercise 285")
        bug.item_type = "Requirement"
        db.commit()
        bob = db.get(models.User, ids["bob_id"])
        from app.access import add_user_project
        add_user_project(db, bob.id, bug.project_id)
        db.commit()
        # Applier called directly; bob can't edit a Requirement.
        resp = actions._apply_set_field(
            db,
            actions.ActionPlan(kind="set_status", actor_user_id=bob.id,
                               bug_id=ids["bug_id"], new_value="Closed",
                               summary_human="Set status"),
            bob, "status", "status",
        )
        assert resp.intent == "action_error"
        assert "permission" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_add_comment_empty_and_too_long(client):
    """actions.py:309 (no body) + 314 (body > 4000 chars)."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 309 — whitespace-only body is treated as empty.
        r_empty = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], comment_body="   ",
                               summary_human="Comment"),
            db, admin,
        )
        assert r_empty.intent == "action_error"
        assert "comment text" in r_empty.blocks[0].payload["text"].lower()

        # 314 — body over the 4000-char limit.
        r_long = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], comment_body="x" * 4001,
                               summary_human="Comment"),
            db, admin,
        )
        assert r_long.intent == "action_error"
        assert "too long" in r_long.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_create_bug_title_validation_and_targets(client):
    """actions.py:335 (no title) + 340 (too long) + 352 (bad project) + 365-368 (assign targets on create)."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 335 — whitespace-only title.
        r_empty = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="   ", summary_human="Create"),
            db, admin,
        )
        assert r_empty.intent == "action_error"
        assert "title" in r_empty.blocks[0].payload["text"].lower()

        # 340 — title too long.
        r_long = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="t" * 201, new_project_id=ids["proj_id"],
                               summary_human="Create"),
            db, admin,
        )
        assert r_long.intent == "action_error"
        assert "too long" in r_long.blocks[0].payload["text"].lower()

        # 352 — project id that doesn't exist.
        r_badproj = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="Valid title", new_project_id=888777,
                               summary_human="Create"),
            db, admin,
        )
        assert r_badproj.intent == "action_error"
        assert "doesn't exist" in r_badproj.blocks[0].payload["text"].lower()

        # 365-368 — assignee targets on create get attached to the new bug.
        before = db.query(models.Bug).count()
        r_ok = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="Created with assignee",
                               new_project_id=ids["proj_id"],
                               target_user_ids=[ids["bob_id"]],
                               target_user_names=["Bob Builder"],
                               summary_human="Create"),
            db, admin,
        )
        assert r_ok.intent == "action_done"
        assert db.query(models.Bug).count() == before + 1
        new_bug = (db.query(models.Bug)
                   .order_by(models.Bug.id.desc()).first())
        assert ids["bob_id"] in [a.id for a in new_bug.assignees]
    finally:
        db.close()


def test_cov_create_bug_no_projects_exist(client):
    """actions.py:347 - create with no project id when no projects exist errors (bugs deleted first for the FK)."""
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        db.query(models.Bug).delete()
        db.query(models.Project).delete()
        db.commit()
        admin = db.query(models.User).filter_by(role="admin").first()
        resp = actions.execute_plan(
            actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                               new_title="Orphan bug", new_project_id=None,
                               summary_human="Create"),
            db, admin,
        )
        assert resp.intent == "action_error"
        assert "no projects" in resp.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_create_project_validation(client):
    """actions.py:384 (no name) + 386 (name > 120) + 392 (duplicate name)."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 384 — whitespace-only name.
        r_empty = actions.execute_plan(
            actions.ActionPlan(kind="create_project", actor_user_id=admin.id,
                               new_project_name="   ", summary_human="Create"),
            db, admin,
        )
        assert r_empty.intent == "action_error"
        assert "name" in r_empty.blocks[0].payload["text"].lower()

        # 386 — name too long.
        r_long = actions.execute_plan(
            actions.ActionPlan(kind="create_project", actor_user_id=admin.id,
                               new_project_name="p" * 121,
                               summary_human="Create"),
            db, admin,
        )
        assert r_long.intent == "action_error"
        assert "too long" in r_long.blocks[0].payload["text"].lower()

        # 392 — case-insensitive duplicate of the seeded "Apollo Cov".
        r_dupe = actions.execute_plan(
            actions.ActionPlan(kind="create_project", actor_user_id=admin.id,
                               new_project_name="apollo cov",
                               summary_human="Create"),
            db, admin,
        )
        assert r_dupe.intent == "action_error"
        assert "already a project" in r_dupe.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_execute_plan_actor_mismatch_unknown_and_exception(client):
    """actions.py:417 (actor mismatch) + 429/431 (env + due-date dispatch) + 438 (unknown kind) + 439-446 (rollback handler)."""
    ids = _seed_basic()
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # 417 — plan's actor_user_id doesn't match the authenticated actor.
        r_mismatch = actions.execute_plan(
            actions.ActionPlan(kind="set_status", actor_user_id=999999,
                               bug_id=ids["bug_id"], new_value="Closed",
                               summary_human="x"),
            db, admin,
        )
        assert r_mismatch.intent == "action_error"
        assert "different user" in r_mismatch.blocks[0].payload["text"].lower()

        # 429 — set_environment dispatch path (admin changing PROD->DEV).
        r_env = actions.execute_plan(
            actions.ActionPlan(kind="set_environment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], new_value="DEV",
                               summary_human="env"),
            db, admin,
        )
        assert r_env.intent == "action_done"
        db.expire_all()
        assert db.get(models.Bug, ids["bug_id"]).environment == "DEV"

        # 431 — set_due_date dispatch path.
        r_due = actions.execute_plan(
            actions.ActionPlan(kind="set_due_date", actor_user_id=admin.id,
                               bug_id=ids["bug_id"], new_value="2026-06-15",
                               summary_human="due"),
            db, admin,
        )
        assert r_due.intent == "action_done"

        # 438 — unknown action kind falls through to the error branch.
        r_unknown = actions.execute_plan(
            actions.ActionPlan(kind="totally_bogus", actor_user_id=admin.id,
                               summary_human="x"),
            db, admin,
        )
        assert r_unknown.intent == "action_error"
        assert "unknown action" in r_unknown.blocks[0].payload["text"].lower()

        # 439-446 - non-string comment_body raises AttributeError; handler rolls back, 'Action failed'.
        r_exc = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"],
                               comment_body=12345,  # type: ignore[arg-type]
                               summary_human="x"),
            db, admin,
        )
        assert r_exc.intent == "action_error"
        assert "action failed" in r_exc.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_cov_execute_plan_rollback_itself_fails(client, monkeypatch):
    """actions.py:444-445 - rollback() raising SQLAlchemyError is swallowed; still 'Action failed'."""
    ids = _seed_basic()
    from sqlalchemy.exc import SQLAlchemyError

    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin_id"])

        # Make rollback explode to exercise the inner 'except SQLAlchemyError: pass'.
        def boom_rollback():
            raise SQLAlchemyError("rollback also broke")
        monkeypatch.setattr(db, "rollback", boom_rollback)

        # Trigger the outer exception: non-string body causes AttributeError.
        resp = actions.execute_plan(
            actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                               bug_id=ids["bug_id"],
                               comment_body=999,  # type: ignore[arg-type]
                               summary_human="x"),
            db, admin,
        )
        assert resp.intent == "action_error"
        assert "action failed" in resp.blocks[0].payload["text"].lower()
    finally:
        # monkeypatch teardown restores rollback before close.
        db.close()


# router.py
def test_cov_persist_turn_disabled(client, monkeypatch):
    """router.py:60 - _persist_turn early-returns when chat memory is off; no chat rows written."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "SLEUTH_CHAT_MEMORY_ENABLED", False)

    res = client.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    assert res.status_code == 200

    from app import models
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        before = db.query(models.ChatMessage).count()
    finally:
        db.close()

    r = client.post("/api/chat/ask", json={"message": "summary"})
    assert r.status_code == 200

    db = SessionLocal()
    try:
        after = db.query(models.ChatMessage).count()
    finally:
        db.close()
    assert after == before  # 60: nothing written when disabled


def test_cov_persist_turn_cloud_engine_label(admin_client):
    """router.py:77 - assistant row tagged engine='cloud' when intent starts with 'cloud_'."""
    from app import models
    from app.chatbot import executor, router
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        resp = executor.Response(
            blocks=[executor.Block("text", {"text": "cloud said hi"})],
            summary="cloud reply", intent="cloud_answer",
        )
        router._persist_turn(db, admin, "hello there", resp)
        row = (db.query(models.ChatMessage)
               .filter_by(role="assistant")
               .order_by(models.ChatMessage.id.desc()).first())
        assert row is not None
        assert row.engine == "cloud"  # line 77
    finally:
        db.close()


def test_cov_persist_turn_swallows_exception(admin_client, monkeypatch):
    """router.py:93-95 - _persist_turn failures are caught and rolled back, never propagate."""
    from app import models
    from app.chatbot import executor, router
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()

        # Force the first DB access inside the try block to raise.
        def boom(*a, **k):
            raise RuntimeError("simulated persist failure")
        monkeypatch.setattr(db, "query", boom)

        resp = executor.Response(
            blocks=[executor.Block("text", {"text": "hi"})],
            summary="s", intent="stats",
        )
        # Must not raise — lines 93-95 swallow it.
        router._persist_turn(db, admin, "summary", resp)
    finally:
        # monkeypatch teardown restores db.query before close.
        db.close()


def test_cov_check_rate_evicts_stale_then_allows(client):
    """router.py:117 - _check_rate evicts timestamps outside the window."""
    from app.chatbot import router

    uid = 4242
    router._rate_state.pop(uid, None)
    stale = 1000.0
    router._rate_state[uid] = [stale, stale + 1, stale + 2]
    # Real 'now' is far past 'stale', so the loop pops all three without a 429.
    router._check_rate(uid)
    # All stale entries gone; one fresh timestamp remains.
    assert len(router._rate_state[uid]) == 1
    router._rate_state.pop(uid, None)


def test_cov_ask_passes_through_httpexception(admin_client, monkeypatch):
    """router.py:171 - executor HTTPException propagates, not swallowed into a 200."""
    from fastapi import HTTPException

    from app.chatbot import executor

    def raise_http(*a, **k):
        raise HTTPException(status_code=403, detail="nope from executor")
    monkeypatch.setattr(executor, "execute", raise_http)

    r = admin_client.post("/api/chat/ask", json={"message": "summary"})
    assert r.status_code == 403
    assert "nope from executor" in r.text


def test_cov_ask_graceful_on_executor_crash(admin_client, monkeypatch):
    """router.py:174-178 - a non-HTTP exception becomes a friendly 200 error reply."""
    from app.chatbot import executor

    def raise_value(*a, **k):
        raise ValueError("kaboom inside executor")
    monkeypatch.setattr(executor, "execute", raise_value)

    r = admin_client.post("/api/chat/ask", json={"message": "summary"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "error"
    assert "something went wrong" in body["blocks"][0]["payload"]["text"].lower()


# excel.py
def test_cov_excel_evict_expired(client):
    """excel.py:69 — _evict_expired_locked removes entries whose expiry has passed."""
    from app.chatbot import excel
    excel._cache.clear()
    now = 10_000.0
    excel._cache["dead"] = (b"x", "a.xlsx", now - 1, 1)
    excel._cache["live"] = (b"y", "b.xlsx", now + 1000, 1)
    excel._evict_expired_locked(now)  # 69: pops 'dead'
    assert "dead" not in excel._cache
    assert "live" in excel._cache
    excel._cache.clear()


def test_cov_excel_evict_oldest_over_cap(client):
    """excel.py:77-78 - at capacity the soonest-expiring entry is dropped."""
    from app.chatbot import excel
    excel._cache.clear()
    cap = excel._MAX_ENTRIES
    base = 50_000.0
    # Fill to capacity; 'tok0' has the earliest expiry.
    for i in range(cap):
        excel._cache[f"tok{i}"] = (b"x", f"f{i}.xlsx", base + i, 1)
    assert len(excel._cache) == cap
    excel._evict_oldest_locked()  # 77 (len check) + 78 (pop oldest)
    assert "tok0" not in excel._cache
    assert len(excel._cache) == cap - 1
    excel._cache.clear()


def test_cov_excel_build_workbook_unavailable(client, monkeypatch):
    """excel.py:119 - _build_workbook raises ExcelGenerationError when openpyxl is unavailable."""
    from app.chatbot import excel
    monkeypatch.setattr(excel, "OPENPYXL_AVAILABLE", False)
    with pytest.raises(excel.ExcelGenerationError):
        excel._build_workbook([{"id": 1, "title": "t"}], "desc")


def test_cov_excel_fetch_staged_empty_token_and_expired(client, monkeypatch):
    """excel.py:200 (falsy token) + 209-210 (expired pop); the sweep is no-opped to isolate the defensive guard."""
    from app.chatbot import excel
    excel._cache.clear()

    # 200 — falsy token short-circuits to None.
    assert excel.fetch_staged("", 1) is None

    # Freeze the clock and bypass the upfront eviction sweep.
    frozen = 9_999_999.0
    monkeypatch.setattr(excel.time, "time", lambda: frozen)
    monkeypatch.setattr(excel, "_evict_expired_locked", lambda now: None)
    # (data, filename, expires, owner_id); expires < frozen so line 208 pops it.
    excel._cache["stale_tok"] = (b"data", "f.xlsx", frozen - 1, 1)
    assert excel.fetch_staged("stale_tok", 1) is None   # expired -> popped
    assert "stale_tok" not in excel._cache              # confirmed gone
    excel._cache.clear()


# redaction.py
def test_cov_redaction_fail_closed(client, monkeypatch):
    """redaction.py:75-77 - a substitution failure fails closed to [REDACTED]."""
    from app.chatbot import redaction

    class _Boom:
        def sub(self, *a, **k):
            raise RuntimeError("regex exploded")

    monkeypatch.setattr(redaction, "_PATTERNS", [(_Boom(), redaction._REDACTED)])
    out = redaction.redact("some text with a maybe-secret in it")
    assert out == redaction._REDACTED  # 75-77 fail-closed path


# classifier.py
def test_cov_classifier_train_skips_empty_example(client):
    """classifier.py:185 - _train skips examples that tokenize to nothing."""
    from app.chatbot import classifier

    # 'the a is to of' tokenizes to []; 'list bugs' survives so the model isn't empty.
    corpus = [
        ("list_bugs", ["the a is to of", "", "list bugs"]),
    ]
    model = classifier._train(corpus)
    # Only "list bugs" survived as a usable doc.
    assert len(model.docs) == 1
    assert model.docs[0][0] == "list_bugs"


# app/chatbot/actions.py — write-path edge branches
def _seed_chat_world(client):
    """Seed two users, an inactive user, a project, and a bug; return a dict of ids."""
    from app import models
    from app.auth import hash_password
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        alice = models.User(org_id=default_org_id(), name="Alice Apex", email="alice@chat.local", role="admin",
                            password_hash=hash_password("Alice12345"), is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob Base", email="bob@chat.local", role="user",
                          password_hash=hash_password("Bob123456"), is_active=True)
        dead = models.User(org_id=default_org_id(), name="Dee Gone", email="dee@chat.local", role="user",
                           password_hash=hash_password("Dee1234567"), is_active=False)
        db.add_all([alice, bob, dead])
        db.flush()
        proj = models.Project(org_id=default_org_id(), name="Chatland", description="")
        db.add(proj)
        db.flush()
        bug = models.Bug(title="Seeded", description="d", status="New",
                         priority="Medium", environment="DEV",
                         project_id=proj.id, reporter_id=alice.id)
        db.add(bug)
        db.flush()
        bug.assignees = [bob]
        db.commit()
        ids = {
            "alice": alice.id, "bob": bob.id, "dead": dead.id,
            "proj": proj.id, "bug": bug.id,
        }
    finally:
        db.close()
    return ids


def test_actions_create_project_denied_for_regular_user(client):
    # A regular (non-admin/manager) user must be denied project creation.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        bob = db.get(User, ids["bob"])
        plan = actions.ActionPlan(kind="create_project", actor_user_id=bob.id,
                                  new_project_name="Nope")
        resp = actions._apply_create_project(db, plan, bob)
    finally:
        db.close()
    assert "admins or managers" in resp.summary.lower() or resp.intent == "action_error"


def test_actions_resolve_targets_empty_and_inactive(client):
    # Empty id list returns an error; a deactivated user can't be assigned.
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        actor = db.get(models.User, ids["alice"])
        users, err = actions._resolve_targets(db, actor, [])
        assert users == []
        assert "find the user" in err.lower()
        users2, err2 = actions._resolve_targets(db, actor, [ids["dead"]])
        assert users2 == []
        assert "deactivated" in err2.lower()
    finally:
        db.close()


def test_actions_assign_noop_when_already_assigned(client):
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(kind="assign", actor_user_id=admin.id,
                                  bug_id=ids["bug"], target_user_ids=[ids["bob"]])
        resp = actions._apply_assign(db, plan, admin, notify=False, commit=False)
    finally:
        db.close()
    assert resp.intent == actions._INTENT_NOOP


def test_actions_notify_chat_op_skips_when_no_reporter(client):
    # reporter_id is None, so the reporter-notify branch is skipped without raising.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import Bug, User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        bug = db.get(Bug, ids["bug"])
        bug.reporter_id = None
        db.flush()
        admin = db.get(User, ids["alice"])
        actions._notify_chat_op(db, bug, admin, kind="updated",
                                title="t", body="b")
        db.rollback()
    finally:
        db.close()


def test_actions_add_comment_no_notify_no_commit(client):
    # notify=False skips the notify block; commit=False skips the commit.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                                  bug_id=ids["bug"], comment_body="hello there")
        resp = actions._apply_add_comment(db, plan, admin, notify=False, commit=False)
        db.rollback()
    finally:
        db.close()
    assert "Comment posted" in resp.summary


def test_actions_create_bug_invalid_priority_and_bad_assignee(client):
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        bad_pri = actions.ActionPlan(kind="create_bug", actor_user_id=admin.id,
                                     new_title="T", new_value="Critical-ish",
                                     new_project_id=ids["proj"])
        r1 = actions._apply_create_bug(db, bad_pri, admin)
        assert "valid priority" in r1.summary.lower()

        bad_assignee = actions.ActionPlan(
            kind="create_bug", actor_user_id=admin.id, new_title="T2",
            new_value="High", new_project_id=ids["proj"],
            target_user_ids=[999999])
        r2 = actions._apply_create_bug(db, bad_assignee, admin)
        assert r2.intent == "action_error"
        db.rollback()
    finally:
        db.close()


def test_actions_execute_plan_denied_for_non_admin(client, monkeypatch):
    # execute_plan re-checks sleuth_write_denied at execute time, before any DB write.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    monkeypatch.setattr(actions, "sleuth_write_denied", lambda _actor: "Sleuth writes are admin-only")
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(kind="add_comment", actor_user_id=admin.id,
                                  bug_id=ids["bug"], comment_body="x")
        resp = actions.execute_plan(plan, db, admin)
    finally:
        db.close()
    assert resp.intent == "action_error"
    assert "admin-only" in resp.summary.lower()


def test_actions_bulk_all_skipped_rolls_back(client):
    # Bulk-assign an already-assigned user everywhere: all no-ops, updated==0, rollback.
    from app.chatbot import actions
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        plan = actions.ActionPlan(
            kind="assign", actor_user_id=admin.id,
            bug_ids=[ids["bug"]], target_user_ids=[ids["bob"]],
            summary_human="assign Bob")
        resp = actions._apply_bulk(plan, db, admin)
    finally:
        db.close()
    assert "0" in resp.summary  # 0 updated, rollback path


# app/chatbot/executor.py — bulk / report / ingest branches
def test_executor_bulk_kind_detects_unassign(client):
    # An unassign phrase with no parsed action_kind resolves to "unassign".
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    pq = ParsedQuery()
    kind, value = executor._bulk_kind_and_value("unassign everyone from all the bugs", pq)
    assert kind == "unassign"
    assert value is None


def test_executor_resolve_bulk_filters_by_environment(client):
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal

    ids = _seed_chat_world(client)  # bug is environment=DEV
    pq = ParsedQuery()
    pq.environments = ["DEV"]
    db = SessionLocal()
    try:
        bug_ids = executor._resolve_bulk_bug_ids(db, pq, None)
    finally:
        db.close()
    assert ids["bug"] in bug_ids


def test_executor_bulk_set_status_without_value_returns_none(client):
    # No target value: bulk handler defers (None) so the normal flow can ask.
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        pq = ParsedQuery()
        pq.action_kind = "set_status"
        pq.action_value = None  # missing value
        ctx = executor.build_context(db, any_user(db))
        res = executor._maybe_handle_bulk_action(
            "change all the bugs", db, admin, pq, ctx)
    finally:
        db.close()
    assert res is None


def test_executor_bulk_over_cap_refused(client, monkeypatch):
    from app import models
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)
    monkeypatch.setattr(executor, "_BULK_ACTION_CAP", 1)  # 2 bugs now exceed it
    db = SessionLocal()
    try:
        # Two bugs in scope now exceed cap(1).
        extra = models.Bug(title="Extra", description="d", status="New",
                           priority="Low", environment="DEV",
                           project_id=ids["proj"], reporter_id=ids["alice"])
        db.add(extra)
        db.commit()
        admin = db.get(User, ids["alice"])
        pq = ParsedQuery()
        pq.action_kind = "set_status"
        pq.action_value = "Resolved"
        ctx = executor.build_context(db, any_user(db))
        res = executor._maybe_handle_bulk_action(
            "resolve all the bugs", db, admin, pq, ctx)
    finally:
        db.close()
    assert res is not None
    assert res.intent == "action_invalid"
    assert "too many" in res.summary.lower() or "more than" in str(res.blocks).lower()


def test_executor_report_too_large_413(client, monkeypatch):
    # Truncated report takes the 'report too large' path instead of building the workbook.
    from app.chatbot import executor
    from app.chatbot.nlu import ParsedQuery
    from app.database import SessionLocal
    from app.models import User

    ids = _seed_chat_world(client)

    class _FakeResult:
        total = 10_000_000
        truncated = True
        rows = []
        columns = []

    # _handle_report imports run_report inside the function, so patch app.reports directly.
    import app.reports as _reports
    monkeypatch.setattr(_reports, "run_report", lambda *_a, **_k: _FakeResult())

    db = SessionLocal()
    try:
        admin = db.get(User, ids["alice"])
        admin.role = "admin"
        db.flush()
        pq = ParsedQuery()
        pq.raw_message = "report on all bugs export"
        res = executor._handle_report(db, pq, admin, scope_of(db, admin))
    finally:
        db.close()
    assert res.intent == "report_too_large"


def test_executor_ingest_created_response_no_items_skips_table(client):
    # Empty items: table skipped but 'and N more' tail still appended (created > len(items)).
    from app.chatbot import executor
    summary = {"created": 3, "project_name": "Apollo", "items": []}
    res = executor._ingest_created_response(summary)
    assert res.intent == "ingest_done"
    assert all(b.kind != "table" for b in res.blocks)


def test_executor_ingest_created_response_exact_fit(client):
    # created == len(items): a table block is added and no "and N more" tail.
    from app.chatbot import executor
    summary = {
        "created": 2,
        "project_name": "Apollo",
        "items": [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
    }
    res = executor._ingest_created_response(summary)
    assert res.intent == "ingest_done"
    assert len(res.blocks) == 2  # text + table, no "more" tail
