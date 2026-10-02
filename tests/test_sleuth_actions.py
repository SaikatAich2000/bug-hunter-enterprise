"""Assistant write side: action plans, confirmation flow, permission denial, audit, pronouns, atomicity (temp-SQLite)."""
from __future__ import annotations

import os as _os
import sys as _sys

from tests.conftest import default_org_id

# Ensure the repo root is on the path when running this file directly.
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import sys
import tempfile
import traceback

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-secret-actions-only"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@example.com"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminPass123!"
os.environ["BOOTSTRAP_ADMIN_NAME"] = "Admin Person"

# Force a fresh import of app.* bound to this file's DB; a stale engine would fail create_all.
import sys as _sys_purge

for _m in list(_sys_purge.modules):
    if _m == "app" or _m.startswith("app."):
        del _sys_purge.modules[_m]

import pytest  # noqa: E402  (after the deliberate sys.modules purge above)

from app import models
from app.auth import hash_password
from app.chatbot import executor
from app.chatbot.memory import store as memstore
from app.database import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def _rebind_app_modules():
    """Re-bind module-level app.* refs to the current import generation (conftest purges app.* between tests)."""
    import importlib
    g = globals()
    db_mod = importlib.import_module("app.database")
    g["Base"], g["engine"], g["SessionLocal"] = (
        db_mod.Base, db_mod.engine, db_mod.SessionLocal,
    )
    g["models"] = importlib.import_module("app.models")
    g["hash_password"] = importlib.import_module("app.auth").hash_password
    g["executor"] = importlib.import_module("app.chatbot.executor")
    g["nlu"] = importlib.import_module("app.chatbot.nlu")
    g["memstore"] = importlib.import_module("app.chatbot.memory").store


PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append((name, detail))
        raise AssertionError(f"{name}: {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def seed_fresh() -> tuple[int, int, int, int]:
    """Re-seed and return (admin_id, alice_id, bob_id, bug1_id)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    memstore._clear_all_for_test()
    db = SessionLocal()
    try:
        admin = models.User(org_id=default_org_id(), name="Admin Person", email="admin@example.com",
                            role="admin",
                            password_hash=hash_password("AdminPass123!"),
                            is_active=True)
        alice = models.User(org_id=default_org_id(), name="Alice Wonderland", email="alice@example.com",
                            role="manager",
                            password_hash=hash_password("Alice12345!"),
                            is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob Builder", email="bob@example.com",
                          role="user",
                          password_hash=hash_password("Bob12345!"),
                          is_active=True)
        regular = models.User(org_id=default_org_id(), name="Carol Singer", email="carol@example.com",
                              role="user",
                              password_hash=hash_password("Carol12345!"),
                              is_active=True)
        db.add_all([admin, alice, bob, regular])
        db.commit()
        proj = models.Project(org_id=default_org_id(), name="Apollo", description="Mission control")
        proj_b = models.Project(org_id=default_org_id(), name="Beacon", description="Auth service")
        db.add_all([proj, proj_b])
        db.commit()
        b1 = models.Bug(title="Login broken on Safari", description="d",
                        status="New", priority="High", environment="PROD",
                        project_id=proj.id, reporter_id=alice.id)
        b2 = models.Bug(title="Memory leak", description="d",
                        status="In Progress", priority="Medium",
                        environment="DEV",
                        project_id=proj_b.id, reporter_id=admin.id)
        db.add_all([b1, b2])
        db.commit()
        b1.assignees = [bob]
        db.commit()
        return admin.id, alice.id, bob.id, b1.id
    finally:
        db.close()


def test_assign_flow() -> None:
    section("Assign flow (parse → confirm → execute → audit)")
    admin_id, alice_id, bob_id, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        # Parse and stage the action.
        resp = executor.execute(f"assign bug {bug1} to alice", db, admin)
        check("assign — intent is confirm",
              resp.intent == "confirm_action", f"got {resp.intent}")
        check("assign — confirm block present",
              any(b.kind == "confirm" for b in resp.blocks))
        sess = memstore.get(admin_id)
        check("assign — memory has pending_action",
              sess is not None and sess.pending_action is not None)

        # DB should be unchanged until confirmed.
        bug_pre = db.get(models.Bug, bug1)
        db.refresh(bug_pre)
        names_pre = sorted(a.name for a in bug_pre.assignees)
        check("assign — DB unchanged before confirm",
              names_pre == ["Bob Builder"], f"got {names_pre}")

        resp2 = executor.execute("yes", db, admin)
        check("assign — confirm executes",
              resp2.intent == "action_done", f"got {resp2.intent}")

        db.expire_all()
        bug_post = db.get(models.Bug, bug1)
        names_post = sorted(a.name for a in bug_post.assignees)
        check("assign — DB now has alice + bob",
              names_post == ["Alice Wonderland", "Bob Builder"],
              f"got {names_post}")

        acts = list(db.query(models.Activity).filter_by(bug_id=bug1).all())
        check("assign — audit entry written",
              any("assignees" in (a.detail or "").lower() for a in acts),
              f"acts={[a.detail for a in acts]}")
        check("assign — uses REST assignees_changed verb",
              any(a.action == "assignees_changed" for a in acts),
              f"actions={[a.action for a in acts]}")
    finally:
        db.close()


def test_cancel_flow() -> None:
    section("Cancellation: 'no' on staged action")
    admin_id, alice_id, bob_id, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        executor.execute(f"close bug {bug1}", db, admin)
        # 'no' should clear the staged plan without touching the DB.
        resp = executor.execute("no", db, admin)
        check("cancel — intent is cancel",
              resp.intent == "confirm_cancel", f"got {resp.intent}")
        sess = memstore.get(admin_id)
        check("cancel — memory cleared",
              sess is None or sess.pending_action is None)
        bug = db.get(models.Bug, bug1)
        db.refresh(bug)
        check("cancel — bug status unchanged",
              bug.status == "New", f"got {bug.status}")
    finally:
        db.close()


def test_status_change() -> None:
    section("Status change: 'close bug N'")
    admin_id, alice_id, bob_id, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        resp = executor.execute(f"close bug {bug1}", db, admin)
        check("close — staged confirm",
              resp.intent == "confirm_action", f"got {resp.intent}")
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        check("close — status==Closed",
              bug.status == "Closed", f"got {bug.status}")

        resp = executor.execute(f"reopen bug {bug1}", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        check("reopen — status==Reopened",
              bug.status == "Reopened", f"got {bug.status}")

        resp = executor.execute(f"mark bug {bug1} as resolved", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        check("mark resolved — status==Resolved",
              bug.status == "Resolved", f"got {bug.status}")
    finally:
        db.close()


def test_priority_change() -> None:
    section("Priority change: 'set bug N priority to low'")
    admin_id, _, _, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        executor.execute(f"set bug {bug1} priority to low", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        check("priority — Low set",
              bug.priority == "Low", f"got {bug.priority}")

        executor.execute(f"make bug {bug1} critical", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        check("priority — Critical set",
              bug.priority == "Critical", f"got {bug.priority}")
    finally:
        db.close()


def test_add_comment() -> None:
    section("Add comment: 'comment on #N: text'")
    admin_id, _, _, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        msg = f"comment on #{bug1}: this is fixed in commit abc123"
        resp = executor.execute(msg, db, admin)
        check("comment — staged confirm",
              resp.intent == "confirm_action", f"got {resp.intent}")
        executor.execute("yes", db, admin)
        comments = list(db.query(models.Comment).filter_by(bug_id=bug1).all())
        check("comment — comment row written",
              any("commit abc123" in c.body for c in comments),
              f"got {[c.body for c in comments]}")
        check("comment — author_user_id == admin",
              all(c.author_user_id == admin.id for c in comments))
        # Audit trail
        acts = list(db.query(models.Activity)
                    .filter_by(bug_id=bug1, action="comment_added").all())
        check("comment — audit entry written", len(acts) >= 1)
    finally:
        db.close()


def test_create_bug() -> None:
    section("Create bug: 'create a bug titled \"X\" in project Apollo'")
    admin_id, _, _, _ = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        before = db.query(models.Bug).count()
        msg = 'create a bug titled "Cache miss in checkout" in project Apollo'
        resp = executor.execute(msg, db, admin)
        check("create_bug — staged confirm",
              resp.intent == "confirm_action", f"got {resp.intent}")
        executor.execute("yes", db, admin)
        after = db.query(models.Bug).count()
        check("create_bug — bug count incremented",
              after == before + 1, f"before={before} after={after}")
        new = db.query(models.Bug).order_by(models.Bug.id.desc()).first()
        check("create_bug — title set",
              "Cache miss in checkout" in (new.title or ""),
              f"title={new.title!r}")
        check("create_bug — project is Apollo",
              new.project.name == "Apollo", f"proj={new.project.name}")
        check("create_bug — reporter is actor",
              new.reporter_id == admin_id)
    finally:
        db.close()


def test_create_project_perm() -> None:
    section("Create project — admin allowed, regular user denied")
    admin_id, _, bob_id, _ = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        bob = db.get(models.User, bob_id)

        executor.execute("create project Mercury", db, admin)
        executor.execute("yes", db, admin)
        proj = db.query(models.Project).filter_by(name="Mercury").first()
        check("create_project (admin) — project exists", proj is not None)

        # Admin-only write: denied before staging, so the follow-up "yes" is idle.
        denied = executor.execute("create project Saturn", db, bob)
        check("create_project (regular) — denied up front",
              denied.intent == "action_denied", f"got {denied.intent}")
        resp = executor.execute("yes", db, bob)
        check("create_project (regular) — 'yes' is idle",
              resp.intent == "confirm_idle", f"got {resp.intent}")
        proj2 = db.query(models.Project).filter_by(name="Saturn").first()
        check("create_project (regular) — Saturn NOT created", proj2 is None)
    finally:
        db.close()


def test_pronoun_resolution() -> None:
    section("Pronoun resolution: 'close it' after viewing a bug")
    admin_id, _, _, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        # Viewing the bug populates memory.last_bug_id.
        executor.execute(f"bug #{bug1}", db, admin)
        sess = memstore.get(admin_id)
        check("pronoun — memory has last_bug_id",
              sess is not None and sess.last_bug_id == bug1,
              f"sess={sess.last_bug_id if sess else None}")

        resp = executor.execute("close it", db, admin)
        check("pronoun — close it staged confirm",
              resp.intent == "confirm_action", f"got {resp.intent}")
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        check("pronoun — bug closed via pronoun",
              bug.status == "Closed", f"status={bug.status}")
    finally:
        db.close()


def test_unassign() -> None:
    section("Unassign: 'unassign bob from #N'")
    admin_id, _, bob_id, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        executor.execute(f"unassign bob from #{bug1}", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, bug1)
        names = sorted(a.name for a in bug.assignees)
        check("unassign — bob removed", names == [], f"got {names}")
    finally:
        db.close()


def test_action_no_target() -> None:
    section("Action with missing target — should respond gracefully")
    admin_id, _, _, _ = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        resp = executor.execute("close that bug", db, admin)
        # No bug in memory yet, so this should return a friendly error.
        check("missing target — action_invalid intent",
              resp.intent == "action_invalid", f"got {resp.intent}")

        # No colon → no comment body → action_invalid before touching the DB.
        resp = executor.execute("comment on bug 999999", db, admin)
        check("missing comment — action_invalid",
              resp.intent == "action_invalid", f"got {resp.intent}")
    finally:
        db.close()


def test_confirm_idle() -> None:
    section("'yes' with nothing pending — friendly response")
    admin_id, _, _, _ = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        memstore.reset(admin_id)
        resp = executor.execute("yes", db, admin)
        check("confirm idle — confirm_idle intent",
              resp.intent == "confirm_idle", f"got {resp.intent}")
        check("confirm idle — text mentions nothing",
              any("Nothing to confirm" in b.payload.get("text", "")
                  for b in resp.blocks if b.kind == "text"),
              "no friendly text")
    finally:
        db.close()


def test_audit_atomicity() -> None:
    section("Atomicity: failed action shouldn't write a half row")
    admin_id, _, _, bug1 = seed_fresh()
    db = SessionLocal()
    try:
        admin = db.get(models.User, admin_id)
        # Non-existent user id forces a controlled failure path.
        from app.chatbot import actions as _actions
        plan = _actions.ActionPlan(
            kind="assign", actor_user_id=admin_id,
            bug_id=bug1, target_user_ids=[999999],
            target_user_names=["Ghost"],
            summary_human="Assign Ghost to bug",
        )
        before_acts = db.query(models.Activity).count()
        resp = _actions.execute_plan(plan, db, admin)
        check("atomicity — error response",
              resp.intent == "action_error", f"got {resp.intent}")
        after_acts = db.query(models.Activity).count()
        check("atomicity — no audit row written",
              after_acts == before_acts,
              f"before={before_acts} after={after_acts}")
        bug = db.get(models.Bug, bug1)
        names = sorted(a.name for a in bug.assignees)
        check("atomicity — bug unchanged",
              names == ["Bob Builder"], f"got {names}")
    finally:
        db.close()


def test_action_via_http() -> None:
    section("HTTP path: end-to-end action through /api/chat/ask")
    admin_id, _, bob_id, bug1 = seed_fresh()
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.post("/api/auth/login",
                    json={"email": "admin@example.com",
                          "password": "AdminPass123!"})
    check("http: login", r.status_code == 200,
          f"st={r.status_code} body={r.text[:200]}")

    r = client.post("/api/chat/ask",
                    json={"message": f"assign bug {bug1} to alice"})
    check("http: stage assign — 200", r.status_code == 200)
    if r.status_code == 200:
        body = r.json()
        check("http: stage assign — confirm intent",
              body.get("intent") == "confirm_action",
              str(body.get("intent")))
        check("http: stage assign — confirm block present",
              any(b["kind"] == "confirm" for b in body.get("blocks", [])))

    r = client.post("/api/chat/ask", json={"message": "yes"})
    check("http: confirm yes — 200", r.status_code == 200)
    if r.status_code == 200:
        body = r.json()
        check("http: confirm yes — action_done",
              body.get("intent") == "action_done",
              str(body.get("intent")))


if __name__ == "__main__":
    try:
        test_assign_flow()
        test_cancel_flow()
        test_status_change()
        test_priority_change()
        test_add_comment()
        test_create_bug()
        test_create_project_perm()
        test_pronoun_resolution()
        test_unassign()
        test_action_no_target()
        test_confirm_idle()
        test_audit_atomicity()
        test_action_via_http()
    except Exception:
        traceback.print_exc()
        FAILED.append(("HARNESS", "uncaught crash"))

    print("\n=== RESULTS ===")
    print(f"Passed: {len(PASSED)}")
    print(f"Failed: {len(FAILED)}")
    if FAILED:
        for n, d in FAILED:
            print(f"  FAIL  {n}  {d}")
        sys.exit(1)
    print("All checks passed")
    sys.exit(0)
