"""Edge-case and failure-path tests for chatbot actions, router, excel, redaction, classifier.
Imports live inside tests (client re-imports app.*); flags toggled on config.Settings; cloud off.
"""
from __future__ import annotations

import pytest

from tests.conftest import default_org_id


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
    excel.clear_all_for_test()
    now = 10_000.0
    excel._cache["dead"] = (b"x", "a.xlsx", now - 1, 1)
    excel._cache["live"] = (b"y", "b.xlsx", now + 1000, 1)
    excel._evict_expired_locked(now)  # 69: pops 'dead'
    assert "dead" not in excel._cache
    assert "live" in excel._cache
    excel.clear_all_for_test()


def test_cov_excel_evict_oldest_over_cap(client):
    """excel.py:77-78 - at capacity the soonest-expiring entry is dropped."""
    from app.chatbot import excel
    excel.clear_all_for_test()
    cap = excel._MAX_ENTRIES
    base = 50_000.0
    # Fill to capacity; 'tok0' has the earliest expiry.
    for i in range(cap):
        excel._cache[f"tok{i}"] = (b"x", f"f{i}.xlsx", base + i, 1)
    assert len(excel._cache) == cap
    excel._evict_oldest_locked()  # 77 (len check) + 78 (pop oldest)
    assert "tok0" not in excel._cache
    assert len(excel._cache) == cap - 1
    excel.clear_all_for_test()


def test_cov_excel_build_workbook_unavailable(client, monkeypatch):
    """excel.py:119 - _build_workbook raises ExcelGenerationError when openpyxl is unavailable."""
    from app.chatbot import excel
    monkeypatch.setattr(excel, "OPENPYXL_AVAILABLE", False)
    with pytest.raises(excel.ExcelGenerationError):
        excel._build_workbook([{"id": 1, "title": "t"}], "desc")


def test_cov_excel_fetch_staged_empty_token_and_expired(client, monkeypatch):
    """excel.py:200 (falsy token) + 209-210 (expired pop); the sweep is no-opped to isolate the defensive guard."""
    from app.chatbot import excel
    excel.clear_all_for_test()

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
    excel.clear_all_for_test()


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
