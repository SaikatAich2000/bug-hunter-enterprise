"""Additional coverage for app/chatbot/executor.py and app/chatbot/llm.py.

These tests drive the chatbot mostly through the public API endpoint
(``POST /api/chat/ask``) so the multi-tenant scoping done by
``build_context`` and ``_scope_to_actor`` is exercised end-to-end against
the test SQLite database. A handful of lower-level helpers are also
called directly to hit edge branches the API can't easily reach.

LLM helpers are tested by monkeypatching ``app.chatbot.llm`` so no
``.gguf`` file or ``llama_cpp`` install is required.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Tiny helpers reused by most tests
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
              priority="Low", environment="DEV"):
    r = client.post("/api/bugs", json={
        "project_id": project_id, "title": title,
        "status": status, "priority": priority,
        "environment": environment,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _ask(client, msg):
    r = client.post("/api/chat/ask", json={"message": msg})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Read-intent dispatch via the public endpoint
# ---------------------------------------------------------------------------
class TestExecutorReadIntents:
    def test_greeting(self, client):
        _signup(client)
        body = _ask(client, "hi")
        assert body["intent"] == "greeting"
        assert any(b["kind"] == "text" for b in body["blocks"])

    def test_thanks(self, client):
        _signup(client)
        body = _ask(client, "thanks!")
        assert body["intent"] == "thanks"

    def test_help_via_keyword(self, client):
        _signup(client)
        body = _ask(client, "help")
        assert body["intent"] == "help"
        # Some product copy is in the block text.
        assert any("Sleuth" in (b["payload"].get("text") or "")
                   for b in body["blocks"])

    def test_list_projects_when_none(self, client):
        _signup(client)
        body = _ask(client, "list projects")
        assert body["intent"] == "list_projects"
        # Without any project the response is a single text block.
        assert body["blocks"][0]["kind"] == "text"

    def test_list_projects_with_data(self, client):
        _signup(client)
        proj = _make_project(client, "Apollo")
        _make_bug(client, proj["id"], title="A bug")
        body = _ask(client, "list projects")
        assert body["intent"] == "list_projects"
        # Two blocks: text summary + table.
        kinds = [b["kind"] for b in body["blocks"]]
        assert "table" in kinds
        table = next(b for b in body["blocks"] if b["kind"] == "table")
        # Bug-count column reflects the bug we just made.
        rows = table["payload"]["rows"]
        assert rows and rows[0][1] == "1"

    def test_list_users(self, client):
        _signup(client)
        body = _ask(client, "list all users")
        assert body["intent"] == "list_users"
        kinds = [b["kind"] for b in body["blocks"]]
        assert "table" in kinds

    def test_list_users_with_role_filter(self, client):
        _signup(client)
        body = _ask(client, "list admins")
        assert body["intent"] == "list_users"
        # First block summarises the count using a role-derived label.
        text = body["blocks"][0]["payload"]["text"]
        assert "admin" in text.lower()

    def test_stats_no_projects(self, client):
        _signup(client)
        body = _ask(client, "summary")
        assert body["intent"] == "stats"
        # No accessible projects -> one text block, no top-assignees table.
        assert len(body["blocks"]) == 1

    def test_stats_with_bugs(self, client):
        _signup(client)
        proj = _make_project(client)
        _make_bug(client, proj["id"], title="Bug 1", priority="Critical",
                  environment="PROD")
        _make_bug(client, proj["id"], title="Bug 2", status="Resolved")
        body = _ask(client, "summary")
        assert body["intent"] == "stats"
        text = body["blocks"][0]["payload"]["text"]
        # The KPI labels are in the snapshot text.
        assert "Total" in text
        assert "Critical" in text

    def test_bug_detail_found(self, client):
        _signup(client)
        proj = _make_project(client)
        bug = _make_bug(client, proj["id"], title="login broken")
        body = _ask(client, f"bug {bug['id']}")
        assert body["intent"] == "bug_detail"
        # The detail text includes the bug id and title.
        text = body["blocks"][0]["payload"]["text"]
        assert f"#{bug['id']}" in text
        assert "login broken" in text

    def test_bug_detail_missing(self, client):
        _signup(client)
        # Make at least one accessible project so the lookup proceeds.
        _make_project(client)
        body = _ask(client, "bug 9999")
        assert body["intent"] == "bug_detail"
        text = body["blocks"][0]["payload"]["text"]
        assert "couldn't find" in text.lower()

    def test_list_bugs_no_results(self, client):
        _signup(client)
        _make_project(client)
        body = _ask(client, "show all bugs")
        # Either intent == list_bugs (no results) or stats; for "show all
        # bugs" we expect list_bugs and a 0-bug text response.
        assert body["intent"] == "list_bugs"
        text = body["blocks"][0]["payload"]["text"]
        assert "no bugs" in text.lower()

    def test_list_bugs_with_data(self, client):
        _signup(client)
        proj = _make_project(client)
        for i in range(3):
            _make_bug(client, proj["id"], title=f"Bug {i}")
        body = _ask(client, "show all bugs")
        assert body["intent"] == "list_bugs"
        # text summary, table, suggestion block (export to excel).
        kinds = [b["kind"] for b in body["blocks"]]
        assert "table" in kinds
        assert "suggestions" in kinds
        suggestion = next(b for b in body["blocks"] if b["kind"] == "suggestions")
        assert suggestion["payload"]["items"][0]["label"] == "Export to Excel"

    def test_count_bugs(self, client):
        _signup(client)
        proj = _make_project(client)
        _make_bug(client, proj["id"], title="One")
        body = _ask(client, "how many bugs?")
        assert body["intent"] == "count_bugs"
        text = body["blocks"][0]["payload"]["text"]
        assert "1" in text

    def test_recent_activity(self, client):
        _signup(client)
        # The signup event itself produces audit rows.
        proj = _make_project(client)
        _make_bug(client, proj["id"], title="trigger")
        body = _ask(client, "recent activity")
        assert body["intent"] == "recent_activity"
        kinds = [b["kind"] for b in body["blocks"]]
        # With audit rows for the admin actor we should get a table.
        assert "table" in kinds

    def test_about_known_topic(self, client):
        _signup(client)
        body = _ask(client, "what is a priority?")
        assert body["intent"] == "about"
        text = body["blocks"][0]["payload"]["text"]
        assert "Priorit" in text  # "Priorities" or "Priority"

    def test_about_unknown_topic_falls_back(self, client):
        _signup(client)
        body = _ask(client, "what is the meaning of life")
        # Unknown about-topic returns a fallback "not sure" intent.
        assert body["intent"] in ("about", "unknown")


# ---------------------------------------------------------------------------
# Action intents — confirmation dance
# ---------------------------------------------------------------------------
class TestExecutorActions:
    def test_action_needs_bug_id(self, client):
        _signup(client)
        body = _ask(client, "close it")
        # No prior bug context -> pronoun branch returns "don't know which bug"
        assert body["intent"] == "action_invalid"
        text = body["blocks"][0]["payload"]["text"].lower()
        assert "bug" in text

    def test_action_missing_bug_no_pronoun(self, client):
        _signup(client)
        # "set priority high" without a bug id, no pronoun -> generic prompt.
        body = _ask(client, "set bug priority high")
        # Either action_invalid or unknown depending on the rule parser,
        # both are acceptable as "we asked for clarification".
        assert body["intent"] in ("action_invalid", "unknown")

    def test_close_bug_confirm_flow(self, client):
        _signup(client)
        proj = _make_project(client)
        bug = _make_bug(client, proj["id"], title="Crash")
        # Step 1: ask to close — should stage and request confirmation.
        body = _ask(client, f"close bug {bug['id']}")
        assert body["intent"] in ("action_confirm", "confirm", "confirm_action")
        # Step 2: confirm.
        body = _ask(client, "yes")
        # The action_done / action_executed / action_complete intents are
        # acceptable — the important contract is that the bug now reports
        # a closed status.
        r = client.get(f"/api/bugs/{bug['id']}")
        assert r.status_code == 200
        assert r.json()["status"] in ("Closed", "Resolved")

    def test_cancel_confirmation(self, client):
        _signup(client)
        proj = _make_project(client)
        bug = _make_bug(client, proj["id"], title="Crash")
        # Stage an action.
        _ask(client, f"close bug {bug['id']}")
        # Decline.
        body = _ask(client, "no")
        assert body["intent"] == "confirm_cancel"
        # Bug stays New.
        r = client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["status"] == "New"

    def test_confirm_yes_with_nothing_pending(self, client):
        _signup(client)
        body = _ask(client, "yes")
        assert body["intent"] == "confirm_idle"

    def test_confirm_no_with_nothing_pending(self, client):
        _signup(client)
        body = _ask(client, "no")
        assert body["intent"] == "confirm_cancel"


# ---------------------------------------------------------------------------
# Direct unit tests for executor.execute() — covers branches that the
# /api/chat/ask path runs as well but lets us assert lower-level details.
# ---------------------------------------------------------------------------
class TestExecutorDirect:
    def _bootstrap(self, client):
        """Sign up and return (db_session, actor, project_id).

        Pulled out as a helper because every test in this class needs the
        same setup.
        """
        _signup(client)
        proj = _make_project(client)
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        actor = db.query(User).filter(User.email == "alice@a.test").one()
        return db, actor, proj["id"]

    def test_execute_dispatches_empty(self, client):
        from app.chatbot.executor import execute
        db, actor, _pid = self._bootstrap(client)
        try:
            resp = execute("", db, actor)
            assert resp.intent == "empty"
        finally:
            db.close()

    def test_execute_help(self, client):
        from app.chatbot.executor import execute
        db, actor, _pid = self._bootstrap(client)
        try:
            resp = execute("help", db, actor)
            assert resp.intent == "help"
        finally:
            db.close()

    def test_execute_bug_detail_remembers_for_pronouns(self, client):
        """A successful bug_detail lookup should set last_bug_id so a
        follow-up like 'close it' can resolve it."""
        from app.chatbot.executor import execute
        from app.chatbot.memory import store as mem
        db, actor, pid = self._bootstrap(client)
        try:
            from app.models import Bug
            from sqlalchemy import select
            bug = db.scalar(select(Bug).where(Bug.project_id == pid))
            # In case the API created bug ids in a different way:
            if bug is None:
                # create one in DB directly
                bug = Bug(
                    project_id=pid, title="Mem test", status="New",
                    priority="Low", environment="DEV",
                    reporter_id=actor.id,
                )
                db.add(bug)
                db.commit()
                db.refresh(bug)
            # Clear any prior memory
            mem.clear_pending(actor.id)
            execute(f"bug {bug.id}", db, actor)
            sess = mem.get(actor.id)
            assert sess is not None
            assert sess.last_bug_id == bug.id
        finally:
            db.close()

    def test_execute_unknown_falls_through(self, client):
        """An unrecognised message hits _handle_unknown after rule,
        classifier and llm all return nothing."""
        from app.chatbot.executor import execute
        db, actor, _pid = self._bootstrap(client)
        try:
            resp = execute("asdfqwertyzzz blorpity", db, actor)
            # rule layer says unknown; classifier may or may not match.
            assert resp.intent in ("unknown", "about", "list_bugs")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# build_context — scoping
# ---------------------------------------------------------------------------
class TestBuildContext:
    def test_build_context_scopes_to_actor_org(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        # A creates a project + bug
        proj_a = c_a.post("/api/projects", json={"name": "AProj"}).json()
        c_a.post("/api/bugs", json={
            "project_id": proj_a["id"], "title": "A bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        })
        proj_b = c_b.post("/api/projects", json={"name": "BProj"}).json()
        c_b.post("/api/bugs", json={
            "project_id": proj_b["id"], "title": "B bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        })
        from app.chatbot.executor import build_context
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            alice = db.query(User).filter(User.email == "alice@a.test").one()
            ctx_a = build_context(db, alice)
            # ctx.projects is a list of (id, normalized_name, display_name)
            names = [p[2] for p in ctx_a.projects]
            assert "AProj" in names
            assert "BProj" not in names
        finally:
            db.close()

    def test_build_context_without_actor_is_global(self, client):
        """No actor -> no scoping, returns all users and projects."""
        _signup(client)
        _make_project(client, "Web")
        from app.chatbot.executor import build_context
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            ctx = build_context(db)
            assert len(ctx.users) >= 1
            assert any(p[2] == "Web" for p in ctx.projects)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# _suggest_user helper — only requires a Context (no DB)
# ---------------------------------------------------------------------------
class TestSuggestUser:
    def _ctx(self, users=()):
        from app.chatbot.nlu import Context
        return Context(users=list(users), projects=[], user_role_map={})

    def test_empty_pool_returns_empty(self):
        from app.chatbot.executor import _suggest_user
        assert _suggest_user("alice", self._ctx()) == ""

    def test_close_match_single(self):
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "alice wong", "alice", "Alice Wong")])
        out = _suggest_user("alise", ctx)
        # The "alise" -> "alice" match is close enough.
        assert "Alice Wong" in out
        assert out.startswith("Did you mean")

    def test_no_close_match_returns_empty(self):
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "alice wong", "alice", "Alice Wong")])
        # Total mismatch -> no suggestion.
        assert _suggest_user("zzzzzzzz", ctx) == ""

    def test_three_tuple_user_entries_still_work(self):
        """Older Context.users tuples are (id, normalized_name, display)."""
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "bob", "Bob Builder")])
        out = _suggest_user("bobb", ctx)
        assert "Bob Builder" in out

    def test_no_phrase_returns_empty(self):
        from app.chatbot.executor import _suggest_user
        ctx = self._ctx([(1, "alice", "alice", "Alice")])
        assert _suggest_user("", ctx) == ""
        assert _suggest_user("   ", ctx) == ""

    def test_none_ctx_returns_empty(self):
        from app.chatbot.executor import _suggest_user
        assert _suggest_user("anything", None) == ""


# ---------------------------------------------------------------------------
# Internal dispatch helpers
# ---------------------------------------------------------------------------
class TestDispatchHelpers:
    def test_dispatch_read_returns_none_for_unknown_intent(self, client):
        from app.chatbot.executor import _dispatch_read_intent
        from app.chatbot.nlu import ParsedQuery
        from app.database import SessionLocal
        from app.models import User
        _signup(client)
        db = SessionLocal()
        try:
            actor = db.query(User).filter(User.email == "alice@a.test").one()
            pq = ParsedQuery(raw_message="?")
            assert _dispatch_read_intent("not_a_real_intent", db, pq, actor, None) is None
        finally:
            db.close()

    def test_handle_unknown_marked_fallback_eligible(self):
        from app.chatbot.executor import _handle_unknown
        resp = _handle_unknown()
        assert resp.intent == "unknown"
        assert resp.fallback_eligible is True

    def test_handle_greeting_with_unnamed_actor(self):
        from app.chatbot.executor import _handle_greeting
        # An actor with no .name still produces a usable greeting.
        actor = SimpleNamespace(name="")
        resp = _handle_greeting(actor)
        assert resp.intent == "greeting"
        # No KeyError on the name slot.
        assert "Sleuth" in resp.blocks[0].payload["text"]

    def test_resolve_pronouns_fills_bug_id_from_memory(self, client):
        """If used_pronoun_bug is set and the session remembers a bug,
        _resolve_pronouns substitutes it."""
        from app.chatbot.executor import _resolve_pronouns
        from app.chatbot.memory import store as mem
        from app.chatbot.nlu import ParsedQuery
        _signup(client)
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            actor = db.query(User).filter(User.email == "alice@a.test").one()
            mem.remember_bug(actor.id, 7)
            pq = ParsedQuery(raw_message="close it")
            pq.used_pronoun_bug = True
            pq.bug_id = None
            _resolve_pronouns(pq, actor)
            assert pq.bug_id == 7
        finally:
            db.close()

    def test_resolve_pronouns_no_op_without_pronoun(self):
        from app.chatbot.executor import _resolve_pronouns
        from app.chatbot.nlu import ParsedQuery
        pq = ParsedQuery(raw_message="show bugs")
        pq.bug_id = None
        # Should NOT touch memory when the pronoun flag isn't set.
        _resolve_pronouns(pq, SimpleNamespace(id=99))
        assert pq.bug_id is None


# ---------------------------------------------------------------------------
# LLM helpers — pure / monkeypatch-able
# ---------------------------------------------------------------------------
class TestLLMHelpers:
    def test_extract_json_simple(self):
        from app.chatbot.llm import _extract_json
        out = _extract_json('{"intent": "stats"}')
        assert out == {"intent": "stats"}

    def test_extract_json_with_markdown_fences(self):
        from app.chatbot.llm import _extract_json
        raw = '```json\n{"intent": "help", "bug_id": null}\n```'
        out = _extract_json(raw)
        assert out["intent"] == "help"

    def test_extract_json_with_prose_before(self):
        from app.chatbot.llm import _extract_json
        raw = 'sure! here is the json: {"intent": "list_bugs", "x": 1}'
        out = _extract_json(raw)
        assert out["intent"] == "list_bugs"

    def test_extract_json_no_brace_returns_none(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json("plain prose, no json") is None

    def test_extract_json_unbalanced_braces_returns_none(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json("{this is not valid json") is None

    def test_extract_json_invalid_json_returns_none(self):
        from app.chatbot.llm import _extract_json
        # Balanced braces but not valid JSON.
        assert _extract_json("{not: valid, json}") is None

    def test_extract_json_empty_returns_none(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json("") is None

    def test_extract_json_nested(self):
        from app.chatbot.llm import _extract_json
        out = _extract_json('{"outer": {"inner": 1}}')
        assert out == {"outer": {"inner": 1}}

    def test_build_prompt_includes_system_and_user(self):
        from app.chatbot.llm import _build_prompt
        out = _build_prompt("hello world")
        assert "hello world" in out
        # Marker tokens are present.
        assert "<|system|>" in out
        assert "<|user|>" in out
        assert "<|assistant|>" in out

    def test_status_priority_environment_constants(self):
        from app.chatbot.llm import (
            _LLM_STATUSES, _LLM_PRIORITIES, _LLM_ENVIRONMENTS,
        )
        assert "New" in _LLM_STATUSES
        assert "Critical" in _LLM_PRIORITIES
        assert "PROD" in _LLM_ENVIRONMENTS

    def test_build_pq_drops_unknown_values(self):
        from app.chatbot.llm import _build_pq_from_llm
        parsed = {
            "filters": {
                "status": ["New", "bogus"],
                "priority": ["High", "bogus"],
                "environment": ["DEV", "bogus"],
            },
            "bug_id": 4,
        }
        pq = _build_pq_from_llm("show bugs", parsed)
        assert pq.statuses == ["New"]
        assert pq.priorities == ["High"]
        assert pq.environments == ["DEV"]
        assert pq.bug_id == 4

    def test_build_pq_handles_null_filters(self):
        from app.chatbot.llm import _build_pq_from_llm
        pq = _build_pq_from_llm("x", {"filters": None, "bug_id": None})
        assert pq.statuses == []
        assert pq.priorities == []
        assert pq.environments == []
        assert pq.bug_id is None

    def test_build_pq_ignores_non_int_bug_id(self):
        from app.chatbot.llm import _build_pq_from_llm
        pq = _build_pq_from_llm("x", {"bug_id": "not-an-int"})
        assert pq.bug_id is None

    def test_build_pq_keeps_raw_message(self):
        from app.chatbot.llm import _build_pq_from_llm
        pq = _build_pq_from_llm("hello LLM", {})
        assert pq.raw_message == "hello LLM"


class TestLLMAvailability:
    def test_is_available_no_model_file(self, monkeypatch, tmp_path):
        """No GGUF file -> is_available() returns False."""
        from app.chatbot import llm
        bogus = tmp_path / "missing.gguf"
        monkeypatch.setattr(llm, "_MODEL_PATH", bogus)
        assert llm.is_available() is False

    def test_memory_budget_no_model_file(self, monkeypatch, tmp_path):
        from app.chatbot import llm
        bogus = tmp_path / "missing.gguf"
        monkeypatch.setattr(llm, "_MODEL_PATH", bogus)
        assert llm.memory_budget() is None
        assert llm.memory_shortfall_message() is None

    def test_memory_budget_with_file_and_shortfall(self, monkeypatch, tmp_path):
        """Force memory_budget into the shortfall branch by stubbing the
        available-memory probe."""
        from app.chatbot import llm
        fake = tmp_path / "model.gguf"
        # ~5 MB file
        fake.write_bytes(b"\0" * (5 * 1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", fake)
        # Force "available" memory down so it's smaller than the floor.
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 50)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 50)
        budget = llm.memory_budget()
        assert budget is not None
        assert budget.model_size_mb >= 4  # ~5 MB
        assert budget.sufficient is False
        # And the user-facing one-liner kicks in.
        msg = llm.memory_shortfall_message()
        assert msg is not None
        assert "unavailable" in msg.lower()

    def test_memory_budget_with_enough_memory(self, monkeypatch, tmp_path):
        from app.chatbot import llm
        fake = tmp_path / "model.gguf"
        fake.write_bytes(b"\0" * (2 * 1024 * 1024))  # ~2 MB
        monkeypatch.setattr(llm, "_MODEL_PATH", fake)
        # Plenty of memory.
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 4096)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        budget = llm.memory_budget()
        assert budget.sufficient is True
        assert llm.memory_shortfall_message() is None

    def test_is_available_no_llama_cpp(self, monkeypatch, tmp_path):
        """Model file exists but llama_cpp isn't installed."""
        from app.chatbot import llm
        fake = tmp_path / "m.gguf"
        fake.write_bytes(b"\0" * (1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", fake)
        # Block the import. _check_llama_cpp_import in the source is the
        # `import llama_cpp` inside is_available(). We block via builtins.
        import builtins
        original_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "llama_cpp":
                raise ImportError("not installed")
            return original_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # Reset the warning flag so the warning branch executes.
        monkeypatch.setattr(llm, "_shortfall_warned", False, raising=False)
        assert llm.is_available() is False

    def test_unload_clears_state(self, monkeypatch):
        from app.chatbot import llm
        # Pretend a model is loaded.
        monkeypatch.setattr(llm, "_llm", object(), raising=False)
        llm._unload()
        # The module-level _llm should now be None.
        assert llm._llm is None


# ---------------------------------------------------------------------------
# LLM intent dispatch — uses the rule-based handlers, no inference needed.
# ---------------------------------------------------------------------------
class TestLLMDispatch:
    def _setup(self, client):
        _signup(client)
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        actor = db.query(User).filter(User.email == "alice@a.test").one()
        return db, actor

    def test_dispatch_help(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            resp = _dispatch_llm_intent("help", db, pq, ctx, actor)
            assert resp is not None
            assert resp.intent == "help"
        finally:
            db.close()

    def test_dispatch_stats(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            resp = _dispatch_llm_intent("stats", db, pq, ctx, actor)
            assert resp.intent == "stats"
        finally:
            db.close()

    def test_dispatch_recent_activity(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            resp = _dispatch_llm_intent("recent_activity", db, pq, ctx, actor)
            assert resp.intent == "recent_activity"
        finally:
            db.close()

    def test_dispatch_list_users(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            resp = _dispatch_llm_intent("list_users", db, pq, ctx, actor)
            assert resp.intent == "list_users"
        finally:
            db.close()

    def test_dispatch_list_projects(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            resp = _dispatch_llm_intent("list_projects", db, pq, ctx, actor)
            assert resp.intent == "list_projects"
        finally:
            db.close()

    def test_dispatch_bug_detail_requires_bug_id(self, client):
        """bug_detail intent with no bug_id returns None (caller fallback)."""
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            assert _dispatch_llm_intent("bug_detail", db, pq, ctx, actor) is None
        finally:
            db.close()

    def test_dispatch_bug_detail_with_bug_id(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            # Make a bug so the detail handler has something to find.
            from app.models import Bug, Project
            proj = Project(name="P", org_id=actor.org_id)
            db.add(proj)
            db.commit()
            db.refresh(proj)
            bug = Bug(
                project_id=proj.id, title="LLM test", status="New",
                priority="Low", environment="DEV", reporter_id=actor.id,
            )
            db.add(bug)
            db.commit()
            db.refresh(bug)
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("show bug", {"bug_id": bug.id})
            resp = _dispatch_llm_intent("bug_detail", db, pq, ctx, actor)
            assert resp is not None
            assert resp.intent == "bug_detail"
        finally:
            db.close()

    def test_dispatch_list_bugs(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            resp = _dispatch_llm_intent("list_bugs", db, pq, ctx, actor)
            assert resp is not None
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_dispatch_unknown_intent_returns_none(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        from app.chatbot.executor import build_context
        db, actor = self._setup(client)
        try:
            ctx = build_context(db, actor)
            pq = _build_pq_from_llm("anything", {})
            assert _dispatch_llm_intent("nope", db, pq, ctx, actor) is None
        finally:
            db.close()


# ---------------------------------------------------------------------------
# try_understand — full integration test of the LLM entry point via
# monkeypatching is_available and _run_inference.
# ---------------------------------------------------------------------------
class TestTryUnderstand:
    def _setup(self, client):
        _signup(client)
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        actor = db.query(User).filter(User.email == "alice@a.test").one()
        return db, actor

    def test_disabled_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: False)
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("anything", db, actor) is None
        finally:
            db.close()

    def test_inference_returns_none_blows_through(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference", lambda msg: None)
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("anything", db, actor) is None
        finally:
            db.close()

    def test_intent_unknown_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference",
                            lambda msg: {"intent": "unknown"})
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("???", db, actor) is None
        finally:
            db.close()

    def test_intent_blank_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference", lambda msg: {"intent": ""})
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("???", db, actor) is None
        finally:
            db.close()

    def test_intent_help_routed(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference",
                            lambda msg: {"intent": "help"})
        db, actor = self._setup(client)
        try:
            resp = llm.try_understand("???", db, actor)
            assert resp is not None
            assert resp.intent == "help"
        finally:
            db.close()

    def test_intent_stats_routed(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference",
                            lambda msg: {"intent": "stats", "filters": {}})
        db, actor = self._setup(client)
        try:
            resp = llm.try_understand("hi", db, actor)
            assert resp is not None
            assert resp.intent == "stats"
        finally:
            db.close()

    def test_intent_list_bugs_with_filters(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference", lambda msg: {
            "intent": "list_bugs",
            "filters": {"status": ["New"], "priority": ["Critical"],
                        "environment": ["PROD"]},
            "bug_id": None,
        })
        db, actor = self._setup(client)
        try:
            resp = llm.try_understand("critical prod bugs", db, actor)
            assert resp is not None
            assert resp.intent == "list_bugs"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# _run_inference branches we can reach without a real model
# ---------------------------------------------------------------------------
class TestRunInference:
    def test_load_failure_returns_none(self, monkeypatch):
        """If _ensure_loaded() raises, _run_inference returns None."""
        from app.chatbot import llm

        def boom():
            raise FileNotFoundError("nope")

        monkeypatch.setattr(llm, "_ensure_loaded", boom)
        assert llm._run_inference("hi") is None

    def test_inference_exception_returns_none(self, monkeypatch):
        """If the model call raises, _run_inference catches it."""
        from app.chatbot import llm

        class _FakeLlama:
            def __call__(self, *a, **kw):
                raise RuntimeError("model crashed")

        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _FakeLlama())
        assert llm._run_inference("hi") is None

    def test_malformed_output_returns_none(self, monkeypatch):
        """Model returns no parsable choices -> None."""
        from app.chatbot import llm

        class _FakeLlama:
            def __call__(self, *a, **kw):
                return {"choices": []}   # no [0]

        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _FakeLlama())
        assert llm._run_inference("hi") is None

    def test_good_output_returns_parsed_json(self, monkeypatch):
        from app.chatbot import llm

        class _FakeLlama:
            def __call__(self, *a, **kw):
                return {"choices": [
                    {"text": '{"intent": "list_bugs"}'},
                ]}

        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _FakeLlama())
        out = llm._run_inference("hi")
        assert out == {"intent": "list_bugs"}


# ---------------------------------------------------------------------------
# Executor's LLM fallback hook (_try_llm) — make sure exceptions are
# swallowed and the chat path stays up.
# ---------------------------------------------------------------------------
class TestExecutorLLMFallback:
    def test_try_llm_swallows_exceptions(self, client, monkeypatch):
        """If the optional LLM module itself raises, executor.execute()
        falls through to _handle_unknown rather than crashing."""
        # Patch the llm module so is_available raises.
        from app.chatbot import llm
        def boom():
            raise RuntimeError("llm exploded")
        monkeypatch.setattr(llm, "is_available", boom)

        _signup(client)
        body = _ask(client, "completely incomprehensible gibberish ZZQQXX")
        # Even with the LLM blowing up, we get a normal 200 response.
        assert "intent" in body
