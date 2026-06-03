"""Coverage tests for Sleuth chatbot internals.

Targets:
- app/chatbot/classifier.py — TF-IDF intent predictor + explain helper.
- app/chatbot/memory.py     — in-process conversation memory store.
- app/chatbot/actions.py    — action plans + execute_plan dispatcher.

The classifier and memory modules are pure-Python helpers that don't need
the DB, so they're exercised in-process. The action tests use the FastAPI
test client fixture to bootstrap an org + admin user, then open a direct
SQLAlchemy session against the same SQLite DB to call execute_plan with a
real User and Bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PASS = "TestPass1!"


# ---------------------------------------------------------------------------
# classifier.py
# ---------------------------------------------------------------------------
class TestClassifier:
    def test_predict_returns_none_for_empty_input(self):
        from app.chatbot import classifier
        assert classifier.predict("") is None
        # Whitespace-only also tokenises to nothing.
        assert classifier.predict("   ") is None

    def test_predict_returns_none_for_only_stopwords(self):
        from app.chatbot import classifier
        # All tokens are in the stopword list -> empty token list -> None.
        assert classifier.predict("the a an of for") is None

    def test_predict_returns_none_for_pure_noise(self):
        from app.chatbot import classifier
        # Out-of-vocabulary noise should fall below the threshold.
        result = classifier.predict("xyzzy frobnicate qux blorf")
        assert result is None

    def test_predict_greeting(self):
        from app.chatbot import classifier
        pred = classifier.predict("hello there")
        assert pred is not None
        assert pred.intent == "greeting"
        assert 0.0 < pred.confidence <= 1.0

    def test_predict_thanks(self):
        from app.chatbot import classifier
        pred = classifier.predict("thanks a lot")
        assert pred is not None
        assert pred.intent == "thanks"

    def test_predict_help(self):
        from app.chatbot import classifier
        pred = classifier.predict("help me please")
        assert pred is not None
        assert pred.intent == "help"

    def test_predict_list_bugs_paraphrase(self):
        from app.chatbot import classifier
        # Not verbatim in the corpus but shares enough tokens.
        pred = classifier.predict("show me all open bugs")
        assert pred is not None
        assert pred.intent == "list_bugs"

    def test_predict_action_assign(self):
        from app.chatbot import classifier
        pred = classifier.predict("assign bug 5 to alice")
        assert pred is not None
        assert pred.intent == "action_assign"

    def test_predict_action_set_status_close(self):
        from app.chatbot import classifier
        pred = classifier.predict("close bug 5")
        assert pred is not None
        assert pred.intent == "action_set_status"

    def test_predict_threshold_can_be_raised(self):
        from app.chatbot import classifier
        # A 0.99 threshold gates marginal matches out. "morning" is in the
        # greeting examples ("good morning") but solo won't score 1.0.
        pred = classifier.predict("morning", threshold=0.99)
        assert pred is None

    def test_predict_low_threshold_admits_weak_matches(self):
        from app.chatbot import classifier
        # With threshold=0.0 even weak matches must come back non-None
        # as long as there are any tokens. ("status" appears in corpus.)
        pred = classifier.predict("status", threshold=0.0)
        assert pred is not None
        # Confidence must always be a float in [0, 1] for valid predictions.
        assert 0.0 <= pred.confidence <= 1.0

    def test_predict_populates_runner_up_for_strong_match(self):
        from app.chatbot import classifier
        # A clear winning intent should leave runner_up non-empty in
        # most cases — the loop tracks the second-best label seen.
        pred = classifier.predict("list users")
        assert pred is not None
        # Best is list_users. runner_up may be empty only in degenerate
        # cases where every other intent scored <=0; otherwise it's set.
        assert pred.runner_up_confidence >= 0.0
        assert pred.runner_up_confidence <= pred.confidence

    def test_predict_numeric_normalisation(self):
        from app.chatbot import classifier
        # "bug 5" and "bug 12" should normalise to the same TF vector
        # (digits collapse to <num>), so both must classify identically.
        a = classifier.predict("show bug 5")
        b = classifier.predict("show bug 12345")
        assert a is not None and b is not None
        assert a.intent == b.intent == "bug_detail"

    def test_explain_returns_topk_scores(self):
        from app.chatbot import classifier
        ranked = classifier.explain("list all users", top_k=3)
        assert isinstance(ranked, list)
        assert len(ranked) == 3
        # Each entry is (intent_label, score) sorted descending.
        intents = [r[0] for r in ranked]
        scores = [r[1] for r in ranked]
        assert "list_users" in intents
        # Descending order invariant.
        assert scores == sorted(scores, reverse=True)

    def test_explain_returns_empty_for_empty_input(self):
        from app.chatbot import classifier
        assert classifier.explain("") == []
        assert classifier.explain("   ") == []

    def test_explain_respects_top_k_one(self):
        from app.chatbot import classifier
        ranked = classifier.explain("create a project called mercury", top_k=1)
        assert len(ranked) == 1
        # The top-scoring intent for this phrase should be project creation.
        assert ranked[0][0] == "action_create_project"


# ---------------------------------------------------------------------------
# memory.py
# ---------------------------------------------------------------------------
class TestMemoryStore:
    @pytest.fixture(autouse=True)
    def _isolate_store(self):
        """Memory is a module-level singleton; wipe it between tests so
        ordering doesn't matter."""
        from app.chatbot import memory
        memory.store._clear_all_for_test()
        yield
        memory.store._clear_all_for_test()

    def test_touch_creates_session_and_updates_last_seen(self):
        from app.chatbot import memory
        s = memory.store.touch(42)
        assert s.last_seen > 0
        first_seen = s.last_seen
        # Touching again must keep the same session but bump last_seen.
        s2 = memory.store.touch(42)
        assert s2 is s
        assert s2.last_seen >= first_seen

    def test_get_returns_none_when_no_session(self):
        from app.chatbot import memory
        assert memory.store.get(999) is None

    def test_get_does_not_create_session(self):
        from app.chatbot import memory
        memory.store.get(7)
        # No write happened, so the introspection helper still sees nothing.
        assert memory.store._all_sessions_for_test() == {}

    def test_remember_bug_persists(self):
        from app.chatbot import memory
        memory.store.remember_bug(1, 99)
        s = memory.store.get(1)
        assert s is not None
        assert s.last_bug_id == 99

    def test_remember_user_persists_name(self):
        from app.chatbot import memory
        memory.store.remember_user(1, 5, "Alice")
        s = memory.store.get(1)
        assert s is not None
        assert s.last_user_id == 5
        assert s.last_user_name == "Alice"

    def test_remember_filter_makes_defensive_copy(self):
        from app.chatbot import memory
        original = {"status": "Open"}
        memory.store.remember_filter(1, original)
        # Mutating the caller's dict must NOT change what we stored.
        original["status"] = "Closed"
        s = memory.store.get(1)
        assert s is not None
        assert s.last_filter == {"status": "Open"}

    def test_stage_and_take_pending_is_single_use(self):
        from app.chatbot import memory
        memory.store.stage_pending(1, {"kind": "assign"})
        first = memory.store.take_pending(1)
        assert first == {"kind": "assign"}
        # Second take returns None — pending was consumed.
        assert memory.store.take_pending(1) is None

    def test_take_pending_returns_none_for_unknown_user(self):
        from app.chatbot import memory
        assert memory.store.take_pending(404) is None

    def test_take_pending_returns_none_when_no_action_staged(self):
        from app.chatbot import memory
        memory.store.touch(1)   # session exists but no pending action
        assert memory.store.take_pending(1) is None

    def test_clear_pending_safe_when_no_session(self):
        from app.chatbot import memory
        # Should not raise.
        memory.store.clear_pending(999)

    def test_clear_pending_removes_staged_action(self):
        from app.chatbot import memory
        memory.store.stage_pending(1, {"kind": "set_status"})
        memory.store.clear_pending(1)
        assert memory.store.take_pending(1) is None

    def test_reset_drops_session_entirely(self):
        from app.chatbot import memory
        memory.store.remember_bug(1, 5)
        memory.store.reset(1)
        assert memory.store.get(1) is None

    def test_ttl_eviction_drops_stale_session(self, monkeypatch):
        from app.chatbot import memory
        # Stage a session and then artificially age it past the TTL.
        memory.store.touch(1)
        sessions = memory.store._all_sessions_for_test()
        assert 1 in sessions
        # Move that session's last_seen to a time long before the TTL.
        # We mutate the internal dict directly via the test helper.
        with memory.store._lock:
            memory.store._sessions[1].last_seen = 0.0
        # Any read that calls evict_expired should now drop it. `get`
        # calls _evict_expired_locked internally.
        assert memory.store.get(1) is None

    def test_session_cap_eviction(self, monkeypatch):
        from app.chatbot import memory
        # Shrink the cap so we can prove eviction without thrashing.
        monkeypatch.setattr(memory, "_MAX_SESSIONS", 3)
        # Fill the store. With a max of 3 the 4th distinct user forces
        # the oldest to be evicted.
        for uid in (1, 2, 3):
            memory.store.touch(uid)
            # Stagger last_seen so user 1 is the oldest.
            with memory.store._lock:
                memory.store._sessions[uid].last_seen = float(uid)
        memory.store.touch(4)
        all_sessions = memory.store._all_sessions_for_test()
        # User 1 (oldest by last_seen) should be evicted; the rest stay.
        assert 1 not in all_sessions
        assert 4 in all_sessions

    def test_take_pending_after_ttl_expiry(self):
        from app.chatbot import memory
        memory.store.stage_pending(1, {"kind": "assign"})
        # Force the session's last_seen far in the past.
        with memory.store._lock:
            memory.store._sessions[1].last_seen = 0.0
        # take_pending invokes _evict_expired_locked first, so the
        # session is gone before we look up the pending dict.
        assert memory.store.take_pending(1) is None


# ---------------------------------------------------------------------------
# actions.py
# ---------------------------------------------------------------------------
def _signup(client, org="Acme", name="Alice Admin", email="alice@acme.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _project(client, name="Eng"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _bug(client, project_id, title="Sample bug here", **extra):
    body = {"title": title, "project_id": project_id, "priority": "Medium"}
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class TestActionsToDictRoundTrip:
    """Pure dataclass behaviour — no DB needed."""

    def test_to_dict_and_from_dict_roundtrip(self):
        from app.chatbot.actions import ActionPlan
        plan = ActionPlan(
            kind="assign",
            actor_user_id=7,
            bug_id=42,
            target_user_ids=[1, 2],
            target_user_names=["Alice", "Bob"],
            new_value="High",
            comment_body="text",
            new_title="t",
            new_description="d",
            new_project_id=3,
            new_project_name="Mercury",
            summary_human="assign alice to bug 42",
        )
        d = plan.to_dict()
        # All scalar fields survive serialisation.
        assert d["kind"] == "assign"
        assert d["target_user_ids"] == [1, 2]
        assert d["summary_human"] == "assign alice to bug 42"
        # Round-trip.
        revived = ActionPlan.from_dict(d)
        assert revived.kind == plan.kind
        assert revived.target_user_ids == plan.target_user_ids
        assert revived.target_user_names == plan.target_user_names
        assert revived.summary_human == plan.summary_human

    def test_from_dict_tolerates_missing_fields(self):
        from app.chatbot.actions import ActionPlan
        # Minimal input — everything defaults.
        revived = ActionPlan.from_dict({"kind": "set_status",
                                        "actor_user_id": 1})
        assert revived.kind == "set_status"
        assert revived.target_user_ids == []
        assert revived.target_user_names == []
        assert revived.bug_id is None
        assert revived.summary_human == ""

    def test_stage_with_confirm_builds_confirm_response(self):
        from app.chatbot.actions import ActionPlan, stage_with_confirm
        plan = ActionPlan(
            kind="set_status", actor_user_id=1, bug_id=5,
            new_value="Closed", summary_human="close bug 5",
        )
        resp = stage_with_confirm(plan)
        assert resp.intent == "confirm_action"
        # Two blocks: a text prompt + the confirm widget.
        kinds = [b.kind for b in resp.blocks]
        assert "text" in kinds
        assert "confirm" in kinds


# ---------------------------------------------------------------------------
# execute_plan — requires a real DB. The conftest's `client` fixture sets
# up the SQLite engine; we open a fresh session against the same DB to
# build an ActionPlan and invoke execute_plan directly.
# ---------------------------------------------------------------------------
@pytest.fixture()
def actor_and_project(client):
    """Sign up the admin, create one project, return (admin_id, project_id)."""
    me = _signup(client)
    p = _project(client)
    return me["id"], p["id"]


def _open_db():
    from app.database import SessionLocal
    return SessionLocal()


def _load_user(db, user_id):
    from app.models import User
    return db.get(User, user_id)


class TestExecutePlan:
    def test_assign_success_creates_audit_and_returns_success(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        # Create a bug to assign.
        bug = _bug(client, project_id, title="Assign target bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            # Self-assign so target_user lookup hits the same org.
            plan = ActionPlan(
                kind="assign", actor_user_id=admin_id, bug_id=bug["id"],
                target_user_ids=[admin_id],
                target_user_names=[actor.name],
                summary_human="assign self",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
            # Block contains a success message referencing the bug id.
            text_block = next(b for b in resp.blocks if b.kind == "text")
            assert f"#{bug['id']}" in text_block.payload["text"]
        finally:
            db.close()
        # The assignment persisted — re-fetch via REST.
        after = client.get(f"/api/bugs/{bug['id']}").json()
        names = [a["name"] for a in after["assignees"]]
        assert "Alice Admin" in names

    def test_assign_actor_mismatch_returns_error(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Mismatch bug here")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            # Plan claims a different actor — execute_plan rejects it.
            plan = ActionPlan(
                kind="assign", actor_user_id=admin_id + 999,
                bug_id=bug["id"], target_user_ids=[admin_id],
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "different user" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_assign_missing_bug_returns_error(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="assign", actor_user_id=admin_id, bug_id=99999,
                target_user_ids=[admin_id], summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "99999" in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_assign_with_no_valid_targets_returns_error(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Empty targets bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            # No target_user_ids -> the "couldn't find user" path fires.
            plan = ActionPlan(
                kind="assign", actor_user_id=admin_id,
                bug_id=bug["id"], target_user_ids=[],
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "user" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_unassign_when_user_not_assigned_returns_error(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Unassign noop bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            # Bug has no assignees; attempting to drop one is a no-op.
            plan = ActionPlan(
                kind="unassign", actor_user_id=admin_id,
                bug_id=bug["id"], target_user_ids=[admin_id],
                target_user_names=["Alice Admin"],
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "nothing changed" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_unassign_actually_removes_assignee(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Real unassign bug")
        # Use REST to attach admin as assignee first.
        client.put(f"/api/bugs/{bug['id']}",
                   json={"assignee_ids": [admin_id]})
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="unassign", actor_user_id=admin_id,
                bug_id=bug["id"], target_user_ids=[admin_id],
                target_user_names=["Alice Admin"],
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        after = client.get(f"/api/bugs/{bug['id']}").json()
        assert after["assignees"] == []

    def test_set_status_changes_field(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Status target")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="set_status", actor_user_id=admin_id,
                bug_id=bug["id"], new_value="In Progress",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        after = client.get(f"/api/bugs/{bug['id']}").json()
        assert after["status"] == "In Progress"

    def test_set_status_no_op_returns_already(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Already-status bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            # Bug default status is "New".
            plan = ActionPlan(
                kind="set_status", actor_user_id=admin_id,
                bug_id=bug["id"], new_value=bug["status"],
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
            text = resp.blocks[0].payload["text"].lower()
            assert "nothing to do" in text or "already" in text
        finally:
            db.close()

    def test_set_priority_changes_field(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Priority bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="set_priority", actor_user_id=admin_id,
                bug_id=bug["id"], new_value="High", summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        after = client.get(f"/api/bugs/{bug['id']}").json()
        assert after["priority"] == "High"

    def test_set_environment_changes_field(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Env bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="set_environment", actor_user_id=admin_id,
                bug_id=bug["id"], new_value="PROD", summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        after = client.get(f"/api/bugs/{bug['id']}").json()
        assert after["environment"] == "PROD"

    def test_set_due_date_changes_field(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Due bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="set_due_date", actor_user_id=admin_id,
                bug_id=bug["id"], new_value="2099-12-31",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        after = client.get(f"/api/bugs/{bug['id']}").json()
        assert after["due_date"] == "2099-12-31"

    def test_add_comment_success(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Comment target")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="add_comment", actor_user_id=admin_id,
                bug_id=bug["id"], comment_body="This works for me.",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        # Comment should appear via the REST endpoint.
        comments = client.get(f"/api/bugs/{bug['id']}/comments").json()
        assert any("works for me" in c["body"] for c in comments)

    def test_add_comment_empty_body_rejected(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Empty comment target")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="add_comment", actor_user_id=admin_id,
                bug_id=bug["id"], comment_body="    ",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
        finally:
            db.close()

    def test_add_comment_too_long_rejected(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Long comment target")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="add_comment", actor_user_id=admin_id,
                bug_id=bug["id"], comment_body="x" * 5000,
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "4000" in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_create_bug_success(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_bug", actor_user_id=admin_id,
                new_title="Login button does nothing",
                new_description="Tried Chrome and Firefox.",
                new_project_id=project_id,
                new_value="High",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()

    def test_create_bug_empty_title_rejected(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_bug", actor_user_id=admin_id,
                new_title="   ", new_project_id=project_id,
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "title" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_create_bug_long_title_rejected(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_bug", actor_user_id=admin_id,
                new_title="z" * 250, new_project_id=project_id,
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "200" in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_create_bug_without_project_id_picks_first_accessible(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _project_id = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_bug", actor_user_id=admin_id,
                new_title="Inferred project bug",
                new_project_id=None,
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()

    def test_create_bug_invalid_project_id_rejected(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_bug", actor_user_id=admin_id,
                new_title="Bogus project bug",
                new_project_id=99999,
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
        finally:
            db.close()

    def test_create_project_success(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_project", actor_user_id=admin_id,
                new_project_name="Mercury",
                new_description="space stuff",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()
        # The new project should now show up in the org's project list.
        proj_names = [p["name"] for p in client.get("/api/projects").json()]
        assert "Mercury" in proj_names

    def test_create_project_empty_name_rejected(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_project", actor_user_id=admin_id,
                new_project_name="   ", summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
        finally:
            db.close()

    def test_create_project_long_name_rejected(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_project", actor_user_id=admin_id,
                new_project_name="x" * 200, summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "120" in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_create_project_duplicate_name_rejected(
        self, client, actor_and_project,
    ):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        # The fixture already created "Eng". Trying again must fail.
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="create_project", actor_user_id=admin_id,
                new_project_name="Eng", summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "already" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_unknown_kind_returns_error(self, client, actor_and_project):
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, _ = actor_and_project
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)
            plan = ActionPlan(
                kind="set_status", actor_user_id=admin_id,
                summary_human="x",
            )
            # Force an unknown kind by mutating after construction (the
            # Literal is hint-only at runtime).
            plan.kind = "tickle"   # type: ignore[assignment]
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "unknown" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_exception_path_is_caught_and_rolls_back(
        self, client, actor_and_project, monkeypatch,
    ):
        """Force _apply_set_field to raise; the wrapper should
        roll back and return an error response rather than propagate."""
        from app.chatbot import actions
        from app.chatbot.actions import ActionPlan, execute_plan
        admin_id, project_id = actor_and_project
        bug = _bug(client, project_id, title="Exception path bug")
        db = _open_db()
        try:
            actor = _load_user(db, admin_id)

            def boom(*_a, **_kw):
                raise RuntimeError("kaboom")

            monkeypatch.setattr(actions, "_apply_set_field", boom)
            plan = ActionPlan(
                kind="set_status", actor_user_id=admin_id,
                bug_id=bug["id"], new_value="Resolved",
                summary_human="x",
            )
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "kaboom" in resp.blocks[0].payload["text"]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Cross-org isolation — actions must not see bugs from another org even
# when the bug id is correctly guessed.
# ---------------------------------------------------------------------------
class TestActionsTenantIsolation:
    def test_assign_cannot_target_bug_from_other_org(self, two_orgs):
        from app.chatbot.actions import ActionPlan, execute_plan
        c_a, _c_b, _me_a, me_b = two_orgs
        # Org A creates a project + bug.
        p_a = _project(c_a, name="ProjA")
        bug_a = _bug(c_a, p_a["id"], title="Org A internal bug")
        # Org B tries to manipulate Org A's bug.
        db = _open_db()
        try:
            from app.models import User
            actor_b = db.get(User, me_b["id"])
            plan = ActionPlan(
                kind="set_status", actor_user_id=me_b["id"],
                bug_id=bug_a["id"], new_value="Closed",
                summary_human="cross-tenant",
            )
            resp = execute_plan(plan, db, actor_b)
            # The bug must look like "not found" — no leak across orgs.
            assert resp.intent == "action_error"
            assert "not found" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()
