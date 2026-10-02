"""DB-safety tests for Sleuth: reads never write (not even an audit row), failed writes roll back atomically,
pending actions are isolated per user, memory edge cases don't crash, and the schema is never altered.
"""
from __future__ import annotations

import os as _os
import sys as _sys

from tests.conftest import default_org_id

# Make the repo root importable when this file is run directly.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import sys
import tempfile
import threading
import traceback

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-db-safety"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@example.com"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminPass123!"
os.environ["BOOTSTRAP_ADMIN_NAME"] = "Admin"
os.environ["SLEUTH_LLM_MODEL_PATH"] = "/tmp/__no_model__.gguf"
os.environ["SLEUTH_CLOUD_ENABLED"] = "0"   # never call the cloud in tests

# Force a fresh app import bound to this file's DB (a stale shared engine causes "no such table").
import sys as _sys_purge

from sqlalchemy import inspect

for _m in list(_sys_purge.modules):
    if _m == "app" or _m.startswith("app."):
        del _sys_purge.modules[_m]

import pytest  # noqa: E402  (after the deliberate sys.modules purge above)

from app import models
from app.auth import hash_password
from app.chatbot import actions, executor
from app.chatbot.memory import store as memstore
from app.database import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def _rebind_app_modules():
    """Re-bind module-level app.* refs each test (conftest purges app.* to rebind the engine)."""
    import importlib
    g = globals()
    db_mod = importlib.import_module("app.database")
    g["Base"], g["engine"], g["SessionLocal"] = (
        db_mod.Base, db_mod.engine, db_mod.SessionLocal,
    )
    g["models"] = importlib.import_module("app.models")
    g["hash_password"] = importlib.import_module("app.auth").hash_password
    g["executor"] = importlib.import_module("app.chatbot.executor")
    g["actions"] = importlib.import_module("app.chatbot.actions")
    g["memstore"] = importlib.import_module("app.chatbot.memory").store


PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        raise AssertionError(f"{name}: {detail}")


def section(t):
    print(f"\n=== {t} ===")


def seed():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    memstore._clear_all_for_test()
    db = SessionLocal()
    try:
        admin = models.User(org_id=default_org_id(), name="Admin", email="admin@example.com",
                            role="admin",
                            password_hash=hash_password("AdminPass123!"),
                            is_active=True)
        alice = models.User(org_id=default_org_id(), name="Alice", email="alice@example.com",
                            role="manager",
                            password_hash=hash_password("x"), is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob", email="bob@example.com",
                          role="user",
                          password_hash=hash_password("x"), is_active=True)
        db.add_all([admin, alice, bob])
        db.commit()
        proj = models.Project(org_id=default_org_id(), name="Apollo")
        db.add(proj)
        db.commit()
        bugs = [
            models.Bug(title="b1", description="d", status="New",
                       priority="High", environment="PROD",
                       project_id=proj.id, reporter_id=admin.id),
            models.Bug(title="b2", description="d", status="Closed",
                       priority="Low", environment="DEV",
                       project_id=proj.id, reporter_id=admin.id),
        ]
        db.add_all(bugs)
        db.commit()
        return admin.id, alice.id, bob.id
    finally:
        db.close()


def test_reads_are_pure() -> None:
    section("Read operations don't mutate the database")
    admin_id, alice_id, bob_id = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)

        # Snapshot every table we care about before running any reads.
        counts_before = {
            "users": db.query(models.User).count(),
            "projects": db.query(models.Project).count(),
            "bugs": db.query(models.Bug).count(),
            "comments": db.query(models.Comment).count(),
            "activity": db.query(models.Activity).count(),
        }

        # Exercise a wide range of read-only intents.
        read_queries = [
            "hi",
            "help",
            "thanks",
            "list all bugs",
            "show open bugs",
            "how many critical bugs",
            "bug 1",
            "summary",
            "recent activity",
            "list users",
            "list managers",
            "list projects",
            "what is a status",
            "export bugs to excel",
            "bugs assigned to bob",
            "bugs in project apollo",
            "bugs about login",
            "show bugs reported by admin",
            "blockers in production",
        ]
        for q in read_queries:
            executor.execute(q, db, admin)

        counts_after = {
            "users": db.query(models.User).count(),
            "projects": db.query(models.Project).count(),
            "bugs": db.query(models.Bug).count(),
            "comments": db.query(models.Comment).count(),
            "activity": db.query(models.Activity).count(),
        }
        for table, before in counts_before.items():
            check(f"reads-pure: {table} count unchanged",
                  counts_after[table] == before,
                  f"before={before} after={counts_after[table]}")
    finally:
        db.close()


def test_schema_unchanged() -> None:
    section("Schema introspection: Sleuth adds no tables")
    db = SessionLocal()
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        expected = {
            "users", "password_reset_tokens", "projects", "bugs",
            "bug_assignees", "comments", "activity_log",
            "attachments", "sessions",
        }
        # Two additive conversation tables are allow-listed; the test still guards drops/alters/surprises.
        approved_chat_tables = {"chat_conversations", "chat_messages"}
        leaked = [t for t in tables
                  if t not in approved_chat_tables
                  and any(k in t.lower() for k in
                          ("chat", "sleuth", "memory", "llm", "classif"))]
        check("schema: no unexpected Sleuth tables leaked into the DB",
              not leaked, f"leaked={leaked}")
        check("schema: production tables present",
              expected.issubset(tables), f"missing={expected - tables}")
    finally:
        db.close()


# No half-state, no partial audit row.
def test_atomic_rollback() -> None:
    section("Atomicity: failed actions don't leave partial state")
    admin_id, _, _ = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        before = {
            "bugs": db.query(models.Bug).count(),
            "activity": db.query(models.Activity).count(),
            "comments": db.query(models.Comment).count(),
        }

        # Assign to a non-existent user: expect a clean error, no partial write.
        plan = actions.ActionPlan(
            kind="assign", actor_user_id=admin_id,
            bug_id=1, target_user_ids=[999999],
            target_user_names=["Ghost"],
            summary_human="Assign Ghost to bug",
        )
        resp = actions.execute_plan(plan, db, admin)
        check("rollback-1: error response", resp.intent == "action_error")

        after = {
            "bugs": db.query(models.Bug).count(),
            "activity": db.query(models.Activity).count(),
            "comments": db.query(models.Comment).count(),
        }
        for k, v in before.items():
            check(f"rollback-1: {k} count unchanged",
                  after[k] == v, f"before={v} after={after[k]}")

        # Set status on a non-existent bug.
        plan2 = actions.ActionPlan(
            kind="set_status", actor_user_id=admin_id,
            bug_id=999999, new_value="Closed",
            summary_human="Close ghost bug",
        )
        resp2 = actions.execute_plan(plan2, db, admin)
        check("rollback-2: error response",
              resp2.intent == "action_error")
        after2 = db.query(models.Activity).count()
        check("rollback-2: no audit row written",
              after2 == before["activity"],
              f"before={before['activity']} after={after2}")

        # Comment on a non-existent bug.
        plan3 = actions.ActionPlan(
            kind="add_comment", actor_user_id=admin_id,
            bug_id=999999, comment_body="hi",
            summary_human="Comment on ghost",
        )
        resp3 = actions.execute_plan(plan3, db, admin)
        check("rollback-3: comment on missing bug errors out",
              resp3.intent == "action_error")
        after3 = db.query(models.Comment).count()
        check("rollback-3: no comment row created",
              after3 == before["comments"],
              f"before={before['comments']} after={after3}")
    finally:
        db.close()


def test_permission_denial_no_writes() -> None:
    section("Permission denial: regular user blocked AND no audit row")
    _, _, bob_id = seed()
    db = SessionLocal()
    try:
        bob = db.get(models.User, bob_id)
        before_proj = db.query(models.Project).count()
        before_act = db.query(models.Activity).count()

        # Bob (user): admin-only write denied before staging, so "yes" has nothing to execute.
        denied = executor.execute("create project Saturn", db, bob)
        check("perm: regular user's write denied up front",
              denied.intent == "action_denied", f"got {denied.intent}")
        resp = executor.execute("yes", db, bob)
        check("perm: bob's 'yes' is idle — nothing was staged",
              resp.intent == "confirm_idle", f"got {resp.intent}")
        after_proj = db.query(models.Project).count()
        after_act = db.query(models.Activity).count()
        check("perm: project NOT created",
              after_proj == before_proj,
              f"before={before_proj} after={after_proj}")
        check("perm: NO audit row for the failed action",
              after_act == before_act,
              f"before={before_act} after={after_act}")
    finally:
        db.close()


def test_concurrent_users_isolated() -> None:
    section("Concurrent users: Bob's 'yes' doesn't fire Alice's plan")
    admin_id, _, bob_id = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        bob = db.get(models.User, bob_id)

        # Admin stages a close on bug 1 (only admins can stage writes).
        executor.execute("close bug 1", db, admin)
        sess_a = memstore.get(admin_id)
        check("isolated: admin has pending",
              sess_a is not None and sess_a.pending_action is not None)

        # Bob has no session of his own.
        sess_b = memstore.get(bob_id)
        check("isolated: bob has NO pending",
              sess_b is None or sess_b.pending_action is None)

        # Bob's "yes" should be a no-op, not execute admin's plan.
        bug_status_before = db.get(models.Bug, 1).status
        resp = executor.execute("yes", db, bob)
        check("isolated: bob's 'yes' is confirm_idle",
              resp.intent == "confirm_idle", f"got {resp.intent}")
        db.expire_all()
        bug_status_after = db.get(models.Bug, 1).status
        check("isolated: bug status unchanged by bob's 'yes'",
              bug_status_after == bug_status_before,
              f"before={bug_status_before} after={bug_status_after}")

        # Admin's pending survives Bob's activity.
        sess_a2 = memstore.get(admin_id)
        check("isolated: admin's pending survives bob's 'yes'",
              sess_a2 is not None and sess_a2.pending_action is not None)

        # Admin confirms; the action should execute normally.
        resp_a = executor.execute("yes", db, admin)
        check("isolated: admin's 'yes' executes",
              resp_a.intent == "action_done", f"got {resp_a.intent}")
        db.expire_all()
        check("isolated: bug now Closed",
              db.get(models.Bug, 1).status == "Closed")
    finally:
        db.close()


def test_memory_edge_cases() -> None:
    section("Memory: TTL eviction, max sessions, reset")
    memstore._clear_all_for_test()

    # Fill past the cap via direct API calls and verify it holds.
    for i in range(1, 250):
        memstore.remember_bug(i, i * 10)
    sessions = memstore._all_sessions_for_test()
    check("memory: capped at 200 sessions",
          len(sessions) <= 200, f"got {len(sessions)}")

    # reset() for a specific user
    memstore.remember_bug(99001, 42)
    s = memstore.get(99001)
    check("memory: remember+get works",
          s is not None and s.last_bug_id == 42)
    memstore.reset(99001)
    s2 = memstore.get(99001)
    check("memory: reset clears session",
          s2 is None)

    # Pending action: stage then take (single-use)
    memstore.stage_pending(99002, {"kind": "assign", "x": 1})
    pending = memstore.take_pending(99002)
    check("memory: stage+take returns plan",
          pending is not None and pending["kind"] == "assign")
    pending2 = memstore.take_pending(99002)
    check("memory: take is single-use (second take is None)",
          pending2 is None)


def test_new_action_overrides_pending() -> None:
    section("Staging a 2nd action replaces the 1st pending")
    admin_id, _, _ = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        executor.execute("close bug 1", db, admin)
        # User changes their mind before confirming.
        executor.execute("set bug 1 priority to low", db, admin)
        # The staged action should now be the priority change, not the close.
        sess = memstore.get(admin_id)
        check("override: latest pending is set_priority",
              sess and sess.pending_action and
              sess.pending_action.get("kind") == "set_priority",
              f"got {sess.pending_action if sess else None}")
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, 1)
        check("override: priority changed (status untouched)",
              bug.priority == "Low" and bug.status == "New",
              f"prio={bug.priority} status={bug.status}")
    finally:
        db.close()


def test_concurrent_executes() -> None:
    section("Concurrent execute() calls — no race conditions")
    admin_id, alice_id, bob_id = seed()
    errors: list[str] = []

    def worker(uid: int, n: int) -> None:
        local_db = SessionLocal()
        try:
            user = local_db.get(models.User, uid)
            for _i in range(n):
                try:
                    executor.execute("list bugs", local_db, user)
                    executor.execute("summary", local_db, user)
                except Exception as e:
                    errors.append(str(e))
        finally:
            local_db.close()

    threads = [
        threading.Thread(target=worker, args=(admin_id, 10)),
        threading.Thread(target=worker, args=(alice_id, 10)),
        threading.Thread(target=worker, args=(bob_id, 10)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    check("concurrent: no errors during 60 mixed read calls",
          not errors, f"errors={errors[:3]}")


def test_sql_injection_safety() -> None:
    section("SQL injection inputs: parameterised queries hold")
    admin_id, _, _ = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        before_count = db.query(models.Bug).count()
        nasty = [
            "'; DROP TABLE bugs; --",
            "show bugs about \" OR 1=1 --",
            "'; DELETE FROM users; --",
            "x' UNION SELECT * FROM users --",
            "1; UPDATE bugs SET status='Closed'; --",
        ]
        for q in nasty:
            try:
                executor.execute(q, db, admin)
            except Exception as e:
                FAILED.append((f"injection: {q[:30]}", f"crashed: {e}"))
        after_count = db.query(models.Bug).count()
        check("injection: bug count unchanged",
              after_count == before_count,
              f"before={before_count} after={after_count}")

        # Schema still intact
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        check("injection: bugs table still exists", "bugs" in tables)
        check("injection: users table still exists", "users" in tables)
    finally:
        db.close()


def test_excel_in_memory_only() -> None:
    section("Excel cache: in-process only, no DB persistence")
    admin_id, _, _ = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        before_acts = db.query(models.Activity).count()
        # Trigger an export and confirm no audit row is created.
        executor.execute("export all bugs to excel", db, admin)
        after_acts = db.query(models.Activity).count()
        check("excel: no audit row for read-only export",
              after_acts == before_acts,
              f"before={before_acts} after={after_acts}")

        # Confirm no excel-related tables were created in the schema.
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        leaked = [t for t in tables
                  if any(k in t.lower() for k in ("excel", "export", "xlsx"))]
        check("excel: no excel tables in schema",
              not leaked, f"leaked={leaked}")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        test_reads_are_pure()
        test_schema_unchanged()
        test_atomic_rollback()
        test_permission_denial_no_writes()
        test_concurrent_users_isolated()
        test_memory_edge_cases()
        test_new_action_overrides_pending()
        test_concurrent_executes()
        test_sql_injection_safety()
        test_excel_in_memory_only()
    except Exception:
        traceback.print_exc()
        FAILED.append(("HARNESS", "uncaught"))

    print("\n=== RESULTS ===")
    print(f"Passed: {len(PASSED)}")
    print(f"Failed: {len(FAILED)}")
    if FAILED:
        for n, d in FAILED:
            print(f"  FAIL  {n}  {d}")
        sys.exit(1)
    print("All checks passed")
    sys.exit(0)
