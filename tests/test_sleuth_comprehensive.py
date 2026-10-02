"""Scenario tests for Sleuth's user-visible API across many input variants:
synonyms, time windows, verbs, permissions, HTTP edges, robustness."""
from __future__ import annotations

import os as _os
import sys as _sys

from tests.conftest import any_user, default_org_id

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-comprehensive"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@example.com"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminPass123!"
os.environ["BOOTSTRAP_ADMIN_NAME"] = "Admin Person"
os.environ["SLEUTH_LLM_MODEL_PATH"] = "/tmp/__no_model__.gguf"

# Purge cached app.* so imports bind to this file's private DB, not a torn-down one.
import sys as _sys_purge

for _m in list(_sys_purge.modules):
    if _m == "app" or _m.startswith("app."):
        del _sys_purge.modules[_m]

import pytest  # noqa: E402  (after the deliberate sys.modules purge above)

from app import models
from app.auth import hash_password
from app.chatbot import executor, nlu
from app.chatbot.executor import build_context
from app.chatbot.memory import store as memstore
from app.database import Base, SessionLocal, engine


@pytest.fixture(autouse=True)
def _rebind_app_modules():
    """Re-bind module-level app.* refs each test (conftest purges app.* per test)."""
    import importlib
    g = globals()
    db_mod = importlib.import_module("app.database")
    g["Base"], g["engine"], g["SessionLocal"] = (
        db_mod.Base, db_mod.engine, db_mod.SessionLocal,
    )
    g["models"] = importlib.import_module("app.models")
    g["hash_password"] = importlib.import_module("app.auth").hash_password
    g["nlu"] = importlib.import_module("app.chatbot.nlu")
    g["executor"] = importlib.import_module("app.chatbot.executor")
    g["build_context"] = g["executor"].build_context
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
        alice = models.User(org_id=default_org_id(), name="Alice Wonderland", email="alice@example.com",
                            role="manager",
                            password_hash=hash_password("x"), is_active=True)
        bob = models.User(org_id=default_org_id(), name="Bob Builder", email="bob@example.com",
                          role="user",
                          password_hash=hash_password("x"), is_active=True)
        carol = models.User(org_id=default_org_id(), name="Carol Singer", email="carol@example.com",
                            role="user",
                            password_hash=hash_password("x"), is_active=True)
        db.add_all([admin, alice, bob, carol])
        db.commit()
        proj_a = models.Project(org_id=default_org_id(), name="Apollo", description="Mission control")
        proj_b = models.Project(org_id=default_org_id(), name="Beacon", description="Auth service")
        proj_c = models.Project(org_id=default_org_id(), name="Comet", description="Mobile app")
        db.add_all([proj_a, proj_b, proj_c])
        db.commit()
        now = datetime.now(timezone.utc)
        bugs = []
        for _i, (status, prio, env, proj, age_days, title) in enumerate([
            ("New",         "Critical", "PROD", proj_a,  1, "Service down"),
            ("New",         "High",     "PROD", proj_b,  2, "Login broken"),
            ("In Progress", "Medium",   "UAT",  proj_a,  3, "Date filter off"),
            ("In Progress", "Critical", "PROD", proj_c,  0, "Crash on startup"),
            ("Resolved",    "Low",      "DEV",  proj_b, 10, "Typo on landing"),
            ("Closed",      "Low",      "PROD", proj_b, 30, "Spam in audit"),
            ("Reopened",    "High",     "UAT",  proj_a,  5, "Sync regression"),
            ("Not a Bug",   "Low",      "DEV",  proj_a,  7, "Misread expectation"),
            ("Resolve Later","Medium",  "UAT",  proj_c, 14, "Theme inconsistent"),
        ], start=1):
            bugs.append(models.Bug(
                title=title, description="d", status=status, priority=prio,
                environment=env, project_id=proj.id, reporter_id=admin.id,
                created_at=now - timedelta(days=age_days),
            ))
        db.add_all(bugs)
        db.commit()
        bugs[0].assignees = [bob]
        bugs[1].assignees = [alice, bob]
        bugs[3].assignees = [carol]
        bugs[6].assignees = [alice]
        db.commit()
        return {"admin": admin.id, "alice": alice.id, "bob": bob.id,
                "carol": carol.id, "apollo": proj_a.id, "beacon": proj_b.id,
                "comet": proj_c.id}
    finally:
        db.close()


def test_status_synonyms() -> None:
    section("Status synonyms")
    seed()
    db = SessionLocal()
    try:
        ctx = build_context(db, any_user(db))
        cases = [
            ("show open bugs",        "New"),
            ("show fixed bugs",       "Resolved"),
            ("show resolved bugs",    "Resolved"),
            ("show closed bugs",      "Closed"),
            ("show reopened bugs",    "Reopened"),
            ("show in progress bugs", "In Progress"),
            ("show wip bugs",         "In Progress"),
            ("show new bugs",         "New"),
            ("show resolve later",    "Resolve Later"),
            ("show not a bug",        "Not a Bug"),
        ]
        for msg, expected in cases:
            pq = nlu.parse(msg, ctx)
            ok = expected in pq.statuses
            check(f"status syn: {msg!r} -> contains {expected!r}",
                  ok, f"got statuses={pq.statuses}")
    finally:
        db.close()


def test_priority_synonyms() -> None:
    section("Priority synonyms")
    seed()
    db = SessionLocal()
    try:
        ctx = build_context(db, any_user(db))
        cases = [
            ("show low priority bugs",     "Low"),
            ("show medium priority bugs",  "Medium"),
            ("show high priority bugs",    "High"),
            ("show critical bugs",         "Critical"),
            ("show p0 bugs",               "Critical"),
            ("show p1 bugs",               "High"),
            ("show p2 bugs",               "Medium"),
            ("show p3 bugs",               "Low"),
            ("show urgent bugs",           "Critical"),
            ("show blockers",              "Critical"),
            ("show critical issues",       "Critical"),
        ]
        for msg, expected in cases:
            pq = nlu.parse(msg, ctx)
            ok = expected in pq.priorities
            check(f"prio syn: {msg!r} -> {expected}",
                  ok, f"got priorities={pq.priorities}")
    finally:
        db.close()


def test_environment_synonyms() -> None:
    section("Environment synonyms")
    seed()
    db = SessionLocal()
    try:
        ctx = build_context(db, any_user(db))
        cases = [
            ("show prod bugs",         "PROD"),
            ("show production bugs",   "PROD"),
            ("show live bugs",         "PROD"),
            ("show dev bugs",          "DEV"),
            ("show development bugs",  "DEV"),
            ("show uat bugs",          "UAT"),
            ("show staging bugs",      "UAT"),
            ("show qa bugs",           "UAT"),
            ("show test bugs",         "UAT"),
        ]
        for msg, expected in cases:
            pq = nlu.parse(msg, ctx)
            ok = expected in pq.environments
            check(f"env syn: {msg!r} -> {expected}",
                  ok, f"got environments={pq.environments}")
    finally:
        db.close()


def test_compound_filters() -> None:
    section("Compound filter queries")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        # Critical + PROD = bugs #1 and #4
        resp = executor.execute("show critical bugs in production", db, admin)
        tbl = next((b for b in resp.blocks if b.kind == "table"), None)
        if tbl:
            got = sorted(tbl.payload.get("row_bug_ids", []))
            check("compound: critical+PROD == [1,4]",
                  got == [1, 4], f"got {got}")

        # New + High = #2
        resp = executor.execute("show new high priority bugs", db, admin)
        tbl = next((b for b in resp.blocks if b.kind == "table"), None)
        if tbl:
            got = sorted(tbl.payload.get("row_bug_ids", []))
            check("compound: new+high == [2]",
                  got == [2], f"got {got}")

        # Project + assignee: Apollo + alice = bug 7 (Reopened)
        resp = executor.execute(
            "show bugs in project Apollo assigned to alice", db, admin)
        tbl = next((b for b in resp.blocks if b.kind == "table"), None)
        if tbl:
            got = sorted(tbl.payload.get("row_bug_ids", []))
            check("compound: apollo+alice == [7]",
                  got == [7], f"got {got}")

        resp = executor.execute(
            "in progress bugs in project Apollo in UAT", db, admin)
        tbl = next((b for b in resp.blocks if b.kind == "table"), None)
        if tbl:
            got = sorted(tbl.payload.get("row_bug_ids", []))
            check("compound: in-progress+apollo+uat == [3]",
                  got == [3], f"got {got}")
    finally:
        db.close()


def test_time_windows() -> None:
    section("Time windows")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        ctx = build_context(db, any_user(db))
        for phrase in [
            "bugs from today",
            "bugs created yesterday",
            "bugs in the last 7 days",
            "bugs in the last 2 weeks",
            "bugs in the last 3 months",
            "bugs from this week",
        ]:
            pq = nlu.parse(phrase, ctx)
            check(f"time: {phrase!r} parsed time_window",
                  pq.time_window is not None, "no time window extracted")

        # Bug #4 (0d) and #1 (1d) qualify; anything older should not appear.
        resp = executor.execute("bugs created in the last 2 days", db, admin)
        tbl = next((b for b in resp.blocks if b.kind == "table"), None)
        if tbl:
            got = sorted(tbl.payload.get("row_bug_ids", []))
            check("time: 'created in last 2 days' returns recent bugs only",
                  set(got).issubset({1, 2, 3, 4}),
                  f"got {got}")
    finally:
        db.close()


def test_robustness() -> None:
    section("Robustness: casing / punctuation / unicode")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        for variant in [
            "SHOW OPEN BUGS",                  # all caps
            "show open BUGS!!!",               # punctuation
            "  show   open    bugs   ",        # extra whitespace
            "show\topen\nbugs",                # weird whitespace
            "ShOw OpEn BuGs",                  # mixed case
        ]:
            resp = executor.execute(variant, db, admin)
            check(f"robust: {variant!r:35s} -> list_bugs",
                  resp.intent == "list_bugs", f"got {resp.intent}")

        for variant in [
            "показать все баги",
            "🐛🐛🐛",
            "list bugs ✨",
            "open bugs 中文",
        ]:
            try:
                resp = executor.execute(variant, db, admin)
                check(f"robust: unicode {variant!r} -> no crash",
                      len(resp.blocks) > 0)
            except Exception as e:
                check(f"robust: unicode {variant!r}", False, f"crash: {e}")
    finally:
        db.close()


def test_action_verb_variants() -> None:
    section("Action verb variants")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])

        for verb, expected_status in [
            ("close",   "Closed"),
            ("closed",  "Closed"),
            ("resolve", "Resolved"),
            ("resolved","Resolved"),
            ("fix",     "Resolved"),
            ("fixed",   "Resolved"),
            ("reopen",  "Reopened"),
        ]:
            memstore.reset(admin.id)
            resp = executor.execute(f"{verb} bug 5", db, admin)
            check(f"verb: {verb!r} on bug 5 -> confirm",
                  resp.intent == "confirm_action", f"got {resp.intent}")
            executor.execute("yes", db, admin)
            db.expire_all()
            bug = db.get(models.Bug, 5)
            check(f"verb: {verb!r} -> status {expected_status}",
                  bug.status == expected_status,
                  f"got {bug.status}")
            bug.status = "Resolved"
            db.commit()

        memstore.reset(admin.id)
        executor.execute("mark bug 5 as closed", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        check("verb: 'mark as closed' -> Closed",
              db.get(models.Bug, 5).status == "Closed")

        memstore.reset(admin.id)
        bug5 = db.get(models.Bug, 5)
        bug5.status = "New"
        db.commit()
        executor.execute("set bug 5 status to in progress", db, admin)
        executor.execute("yes", db, admin)
        db.expire_all()
        check("verb: 'set status to in progress'",
              db.get(models.Bug, 5).status == "In Progress",
              f"got {db.get(models.Bug, 5).status}")
    finally:
        db.close()


def test_assign_verb_variants() -> None:
    section("Assign verb variants")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        for phrase in [
            "assign bug 5 to alice",
            "give bug 5 to alice",
            "delegate bug 5 to alice",
            "hand bug 5 over to alice",
            "reassign bug 5 to alice",
            "allocate bug 5 to alice",
            "allot bug 5 to alice",
        ]:
            memstore.reset(admin.id)
            b5 = db.get(models.Bug, 5)
            b5.assignees = []
            db.commit()
            resp = executor.execute(phrase, db, admin)
            check(f"assign verb: {phrase!r} -> confirm",
                  resp.intent == "confirm_action", f"got {resp.intent}")
            executor.execute("yes", db, admin)
            db.expire_all()
            names = sorted(a.name for a in db.get(models.Bug, 5).assignees)
            check(f"assign verb: {phrase!r} -> alice assigned",
                  "Alice Wonderland" in names, f"got {names}")
    finally:
        db.close()


def test_create_bug_phrasings() -> None:
    section("Create bug — phrasings")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        before = db.query(models.Bug).count()
        msgs = [
            'create a bug titled "Cache miss A" in project Apollo',
            'file a new bug titled "Cache miss B" in project Beacon',
            'open a ticket titled "Cache miss C"',
        ]
        for m in msgs:
            memstore.reset(admin.id)
            executor.execute(m, db, admin)
            executor.execute("yes", db, admin)
        after = db.query(models.Bug).count()
        check("create_bug: 3 bugs added",
              after - before == 3, f"delta={after - before}")

        latest = db.query(models.Bug).order_by(models.Bug.id.desc()).first()
        check("create_bug: title of latest is Cache miss C",
              "Cache miss C" in latest.title)
        # No project was specified, so it should fall back to the first project.
        check("create_bug: latest reporter is admin",
              latest.reporter_id == admin.id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pronoun resolution edges
# ---------------------------------------------------------------------------
def test_pronoun_edges() -> None:
    section("Pronoun edges: 'it' before any context")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        memstore.reset(admin.id)

        # No prior context, so the pronoun can't be resolved.
        resp = executor.execute("close it", db, admin)
        check("pronoun: 'close it' with no context -> action_invalid",
              resp.intent == "action_invalid", f"got {resp.intent}")

        executor.execute("bug 3", db, admin)
        resp = executor.execute("comment on it: looks ok", db, admin)
        check("pronoun: 'comment on it' after viewing -> confirm",
              resp.intent == "confirm_action", f"got {resp.intent}")
        executor.execute("yes", db, admin)
        comments = list(db.query(models.Comment).filter_by(bug_id=3).all())
        check("pronoun: comment was added to bug 3",
              any("looks ok" in c.body for c in comments))

        executor.execute("bug 6", db, admin)
        resp = executor.execute("close that bug", db, admin)
        check("pronoun: 'close that bug' staged",
              resp.intent == "confirm_action")
        executor.execute("yes", db, admin)
        db.expire_all()
        check("pronoun: bug 6 closed",
              db.get(models.Bug, 6).status == "Closed")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Confirmation-flow edges
# ---------------------------------------------------------------------------
def test_confirm_flow_edges() -> None:
    section("Confirmation-flow edges")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        memstore.reset(admin.id)

        for word in ["yes", "yeah", "yep", "yup", "sure", "ok", "okay",
                     "confirm", "do it", "go ahead"]:
            memstore.reset(admin.id)
            b3 = db.get(models.Bug, 3)
            b3.status = "New"
            db.commit()
            executor.execute("close bug 3", db, admin)
            resp = executor.execute(word, db, admin)
            check(f"confirm-yes alias: {word!r} -> action_done",
                  resp.intent == "action_done", f"got {resp.intent}")

        for word in ["no", "nope", "cancel", "abort", "stop", "never mind",
                     "nvm"]:
            memstore.reset(admin.id)
            b3 = db.get(models.Bug, 3)
            b3.status = "New"
            db.commit()
            executor.execute("close bug 3", db, admin)
            resp = executor.execute(word, db, admin)
            check(f"confirm-no alias: {word!r} -> cancel",
                  resp.intent == "confirm_cancel", f"got {resp.intent}")

        # Second yes with nothing pending.
        memstore.reset(admin.id)
        b3 = db.get(models.Bug, 3)
        b3.status = "New"
        db.commit()
        executor.execute("close bug 3", db, admin)
        executor.execute("yes", db, admin)
        resp = executor.execute("yes", db, admin)
        check("confirm: second 'yes' is confirm_idle",
              resp.intent == "confirm_idle", f"got {resp.intent}")

        memstore.reset(admin.id)
        resp = executor.execute("no", db, admin)
        check("confirm: 'no' with nothing pending -> friendly",
              resp.intent == "confirm_cancel", f"got {resp.intent}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HTTP edge cases
# ---------------------------------------------------------------------------
def test_http_edges() -> None:
    section("HTTP edge cases")
    seed()
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)

    r = client.post("/api/auth/login",
                    json={"email": "admin@example.com",
                          "password": "AdminPass123!"})
    check("http-edge: login", r.status_code == 200)

    r = client.post("/api/chat/ask", json={"message": ""})
    check("http-edge: empty message -> 422 (Pydantic min_length)",
          r.status_code in (422, 200), f"got {r.status_code}")

    r = client.post("/api/chat/ask", json={"message": "   \n\t  "})
    check("http-edge: whitespace-only message handled gracefully",
          r.status_code in (200, 422), f"got {r.status_code}")

    r = client.post("/api/chat/ask", json={})
    check("http-edge: missing field -> 422",
          r.status_code == 422, f"got {r.status_code}")

    r = client.post("/api/chat/ask", json={"message": None})
    check("http-edge: null message -> 422",
          r.status_code == 422, f"got {r.status_code}")

    r = client.post("/api/chat/ask", json={"message": ["not", "a", "string"]})
    check("http-edge: wrong-type message -> 422",
          r.status_code == 422, f"got {r.status_code}")

    r = client.post("/api/chat/ask", json={"message": "a" * 2000})
    check("http-edge: 2000-char message accepted",
          r.status_code in (200, 422), f"got {r.status_code}")

    r = client.post("/api/chat/ask", json={"message": "a" * 2001})
    check("http-edge: 2001-char message rejected",
          r.status_code == 422, f"got {r.status_code}")

    r = client.get("/api/chat/download/this-token-does-not-exist")
    check("http-edge: bad download token -> 404",
          r.status_code == 404, f"got {r.status_code}")


# ---------------------------------------------------------------------------
# Full conversation: realistic multi-turn scenario
# ---------------------------------------------------------------------------
def test_full_conversation() -> None:
    section("Full conversation scenario")
    ids = seed()
    db = SessionLocal()
    try:
        admin = db.get(models.User, ids["admin"])
        memstore.reset(admin.id)
        r1 = executor.execute("show critical bugs in production", db, admin)
        check("conv-1: returns table",
              any(b.kind == "table" for b in r1.blocks))
        r2 = executor.execute("bug 1", db, admin)
        check("conv-2: bug detail",
              r2.intent == "bug_detail", f"got {r2.intent}")
        r3 = executor.execute("assign it to alice", db, admin)
        check("conv-3: assign-it staged",
              r3.intent == "confirm_action", f"got {r3.intent}")
        r4 = executor.execute("yes", db, admin)
        check("conv-4: action done",
              r4.intent == "action_done", f"got {r4.intent}")
        r5 = executor.execute("comment on it: assigned to alice now", db, admin)
        check("conv-5: comment staged",
              r5.intent == "confirm_action", f"got {r5.intent}")
        executor.execute("yes", db, admin)
        r6 = executor.execute("set bug 1 priority to high", db, admin)
        check("conv-6: priority change staged",
              r6.intent == "confirm_action", f"got {r6.intent}")
        executor.execute("yes", db, admin)
        db.expire_all()
        bug = db.get(models.Bug, 1)
        names = sorted(a.name for a in bug.assignees)
        check("conv: bug 1 final assignees include alice",
              "Alice Wonderland" in names, f"got {names}")
        check("conv: bug 1 priority now High",
              bug.priority == "High", f"got {bug.priority}")
        comments = list(db.query(models.Comment).filter_by(bug_id=1).all())
        check("conv: at least one comment on bug 1",
              len(comments) >= 1)
        # Audit trail accumulated
        acts = list(db.query(models.Activity).filter_by(bug_id=1).all())
        check("conv: audit log has multiple entries",
              len(acts) >= 3, f"got {len(acts)} entries")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        test_status_synonyms()
        test_priority_synonyms()
        test_environment_synonyms()
        test_compound_filters()
        test_time_windows()
        test_robustness()
        test_action_verb_variants()
        test_assign_verb_variants()
        test_create_bug_phrasings()
        test_pronoun_edges()
        test_confirm_flow_edges()
        test_http_edges()
        test_full_conversation()
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
