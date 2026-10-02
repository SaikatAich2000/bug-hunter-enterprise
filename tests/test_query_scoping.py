"""NLU scoping tests: over-match guards, classifier degenerate input, item-type scoping.
DB-backed cases (end-to-end filtering, get_event truncation) use the client fixture.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.chatbot import classifier, nlu
from tests.conftest import default_org_id


def _ctx(projects=None, users=None):
    return nlu.Context(users=users or [], projects=projects or [])


# Time window parsing tolerates irregular internal whitespace.
def test_time_window_tolerates_extra_whitespace():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    for phrase in ("this week", "this  week", "this\tweek"):
        tw = nlu._parse_time_window(f"bugs from {phrase}", now=now)
        assert tw is not None, phrase
        assert tw.label == "this week"


# The free-text extractor should strip trailing filter clauses.
def test_free_text_strips_trailing_filter_clauses():
    assert nlu._extract_text_search("bugs about crash reported by alice") == "crash"
    assert nlu._extract_text_search("issues about login with environment PROD") == "login"
    assert nlu._extract_text_search("bugs about timeout with status closed") == "timeout"


# A project whose name is a common keyword requires an explicit cue to match.
def test_project_named_open_not_matched_without_cue():
    ctx = _ctx(projects=[(1, "open", "Open")])
    pq = nlu.parse("show me open bugs", ctx)
    assert pq.project_ids == []           # "open" reads as a status word here
    pq2 = nlu.parse("show bugs in project Open", ctx)
    assert pq2.project_ids == [1]         # explicit "in project" cue resolves it


# Ambiguous status/environment words should not over-match.
def test_bare_later_is_not_a_status():
    assert nlu._extract_statuses("I'll look at this later") == []
    assert nlu._extract_statuses("mark it resolve later") == ["Resolve Later"]


def test_ambiguous_env_words_require_context():
    assert nlu._extract_environments("test the export") == []
    assert nlu._extract_environments("go live tomorrow") == []
    assert nlu._extract_environments("attach the local file") == []
    assert nlu._extract_environments("bugs in test") == ["UAT"]
    assert nlu._extract_environments("deploy to live") == ["PROD"]
    assert nlu._extract_environments("the local environment") == ["DEV"]
    # Unambiguous words still work.
    assert nlu._extract_environments("bugs in prod") == ["PROD"]


# _RECENT_RE should respect word boundaries.
def test_recent_re_word_boundaries():
    assert nlu._RECENT_RE.search("prehistory") is None
    assert nlu._RECENT_RE.search("what happenedx") is None
    assert nlu._RECENT_RE.search("what happened today") is not None
    assert nlu._RECENT_RE.search("show recent activity") is not None
    assert nlu._RECENT_RE.search("the audit log") is not None


# The classifier should reject number-only input rather than guessing an intent.
def test_classifier_rejects_num_only_input():
    assert classifier.predict("is it 5?") is None
    assert classifier.predict("42") is None


def test_classifier_still_classifies_real_queries():
    pred = classifier.predict("show me all the open bugs")
    assert pred is not None
    assert pred.intent


# Item-type scoping via noun detection.
def test_item_types_detected_from_nouns():
    ctx = _ctx()
    assert nlu.parse("list tasks", ctx).item_types == ["Task"]
    assert nlu.parse("show requirements", ctx).item_types == ["Requirement"]
    assert nlu.parse("how many bugs", ctx).item_types == ["Bug"]
    assert nlu.parse("how many defects", ctx).item_types == ["Bug"]
    # "items" is generic and should not scope to any type.
    assert nlu.parse("list all items", ctx).item_types == []
    # Substrings like "multitask" and "debug" must not trigger scoping.
    assert nlu.parse("debug the multitask runner", ctx).item_types == []


# DB-backed: item-type scope is applied to list/count queries.
def _texts(resp):
    return " ".join(b.payload.get("text", "") for b in resp.blocks if b.kind == "text")


def test_chat_count_scopes_to_item_type(client):
    from app import models
    from app.chatbot import executor
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(org_id=default_org_id(), name="Scope", description="")
        db.add(proj)
        db.commit()
        db.add_all([
            models.Bug(title="b", status="New", priority="Low", environment="DEV",
                       project_id=proj.id, reporter_id=admin.id, item_type="Bug"),
            models.Bug(title="t", status="New", priority="Low", environment="DEV",
                       project_id=proj.id, reporter_id=admin.id, item_type="Task"),
        ])
        db.commit()
        # 1 task + 1 bug = 2 items total; scoped queries should see only 1 each.
        assert "**1**" in _texts(executor.execute("how many tasks", db, admin))
        assert "**1**" in _texts(executor.execute("how many bugs", db, admin))
        assert "**2**" in _texts(executor.execute("how many items", db, admin))
    finally:
        db.close()


# Priority normalization via normalize_choice, consistent with the REST layer.
def test_create_bug_normalizes_lowercase_priority(client):
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(org_id=default_org_id(), name="P2", description="")
        db.add(proj)
        db.commit()
        plan = actions.ActionPlan(
            kind="create_bug", actor_user_id=admin.id, new_title="Lower pri",
            new_value="high", new_project_id=proj.id, summary_human="x",
        )
        resp = actions._apply_create_bug(db, plan, admin)
        assert resp.intent == "action_done"
        bug = db.query(models.Bug).filter_by(title="Lower pri").first()
        assert bug.priority == "High"   # lowercase "high" must be canonicalized
    finally:
        db.close()


def test_create_bug_rejects_bogus_priority(client):
    from app import models
    from app.chatbot import actions
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(org_id=default_org_id(), name="P3", description="")
        db.add(proj)
        db.commit()
        plan = actions.ActionPlan(
            kind="create_bug", actor_user_id=admin.id, new_title="Bad pri",
            new_value="ultra", new_project_id=proj.id, summary_human="x",
        )
        resp = actions._apply_create_bug(db, plan, admin)
        assert resp.intent == "action_error"
        assert "priority" in _texts(resp).lower()
    finally:
        db.close()


# get_event sets items_truncated when the result is capped by _EVENT_ITEMS_MAX.
def test_get_event_items_truncated(admin_client, monkeypatch):
    import app.routes.events as ev_mod
    from app import models
    from app.database import SessionLocal

    monkeypatch.setattr(ev_mod, "_EVENT_ITEMS_MAX", 1)
    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(org_id=default_org_id(), name="EvP", description="")
        db.add(proj)
        db.commit()
        ev = models.Event(org_id=default_org_id(), name="Sprint")
        db.add(ev)
        db.commit()
        for i in range(2):
            db.add(models.Bug(title=f"i{i}", status="New", priority="Low",
                              environment="DEV", project_id=proj.id,
                              reporter_id=admin.id, event_id=ev.id))
        db.commit()
        eid = ev.id
    finally:
        db.close()

    body = admin_client.get(f"/api/events/{eid}").json()
    assert body["items_truncated"] is True
    assert len(body["items"]) == 1


def test_get_event_not_truncated_for_small_event(admin_client):
    from app import models
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        admin = db.query(models.User).filter_by(role="admin").first()
        proj = models.Project(org_id=default_org_id(), name="EvP2", description="")
        db.add(proj)
        db.commit()
        ev = models.Event(org_id=default_org_id(), name="Tiny")
        db.add(ev)
        db.commit()
        db.add(models.Bug(title="solo", status="New", priority="Low",
                          environment="DEV", project_id=proj.id,
                          reporter_id=admin.id, event_id=ev.id))
        db.commit()
        eid = ev.id
    finally:
        db.close()

    body = admin_client.get(f"/api/events/{eid}").json()
    assert body["items_truncated"] is False
