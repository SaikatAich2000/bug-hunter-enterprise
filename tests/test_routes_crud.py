"""Coverage tests for CRUD routes (projects, users, sessions, audit, notifications).

Uses the conftest fixtures; row-level setups go through the app's SessionLocal.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BOOTSTRAP_EMAIL, default_org_id

_BOOTSTRAP_EMAIL = "admin@test.local"
_BOOTSTRAP_PW = "Admin1234"


# Local helpers, kept independent of other test files.
def _login(c: TestClient, email: str, password: str) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _as_admin(c: TestClient) -> None:
    _login(c, _BOOTSTRAP_EMAIL, _BOOTSTRAP_PW)


def _mk_project(c: TestClient, name: str, color: str = "#c9764f") -> dict:
    r = c.post("/api/projects", json={"name": name, "color": color})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_user(c: TestClient, name: str, email: str, role: str = "user",
             password: str = "User12345", project_ids=()) -> dict:
    r = c.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
        "project_ids": list(project_ids),
    })
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(c: TestClient, project_id: int, **extra) -> dict:
    body = {"project_id": project_id, "title": "cov bug",
            "priority": "Medium", "environment": "DEV"}
    body.update(extra)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _new_client() -> TestClient:
    """Second TestClient on the same app/db so two users can hold independent session cookies."""
    from app.main import app
    return TestClient(app)


# --- projects.py ---
def test_cov_get_project_by_id_ok(admin_client):
    """GET /api/projects/{id} returns the created row."""
    p = _mk_project(admin_client, "Cov Get OK")
    r = admin_client.get(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == p["id"]
    assert body["name"] == "Cov Get OK"


def test_cov_get_project_by_id_404(admin_client):
    """GET a non-existent project id → 404."""
    r = admin_client.get("/api/projects/999999")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_update_project_duplicate_name_409(admin_client):
    """PUT that renames a project to an existing name hits the IntegrityError rollback → 409."""
    _mk_project(admin_client, "Cov Dup First")
    second = _mk_project(admin_client, "Cov Dup Second")
    r = admin_client.put(f"/api/projects/{second['id']}",
                         json={"name": "Cov Dup First", "color": "#123abc"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Project name already exists"


def test_cov_update_project_404(admin_client):
    """PUT a missing project → 404."""
    r = admin_client.put("/api/projects/888888", json={"name": "Cov Missing Upd"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_delete_project_success(admin_client):
    """DELETE a project with no bugs returns the success message and removes the row."""
    p = _mk_project(admin_client, "Cov Del OK")
    r = admin_client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Project deleted"
    assert admin_client.get(f"/api/projects/{p['id']}").status_code == 404


def test_cov_delete_project_404(admin_client):
    """DELETE a non-existent project → 404."""
    r = admin_client.delete("/api/projects/777777")
    assert r.status_code == 404
    assert r.json()["detail"] == "Project not found"


def test_cov_delete_project_conflict_when_bugs_exist(admin_client):
    """DELETE is blocked with 409 while bugs belong to the project."""
    p = _mk_project(admin_client, "Cov Del Blocked")
    _mk_bug(admin_client, p["id"], title="blocks delete")
    r = admin_client.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 409
    assert "bug(s) belong to this project" in r.json()["detail"]


# --- users.py ---
def test_cov_list_users_exclude_inactive(admin_client):
    """GET /api/users?include_inactive=false omits deactivated users."""
    dormant = _mk_user(admin_client, "Cov Dormant", "cov.dormant@example.com")
    r = admin_client.put(f"/api/users/{dormant['id']}", json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    active_ids = {u["id"] for u in admin_client.get(
        "/api/users?include_inactive=false").json()}
    assert dormant["id"] not in active_ids
    # Default (include_inactive=true) still shows them.
    all_ids = {u["id"] for u in admin_client.get("/api/users").json()}
    assert dormant["id"] in all_ids


def test_cov_get_user_by_id_ok(admin_client):
    """GET /api/users/{id} returns the created user."""
    u = _mk_user(admin_client, "Cov GetUser", "cov.getuser@example.com")
    r = admin_client.get(f"/api/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["email"] == "cov.getuser@example.com"


def test_cov_get_user_by_id_404(admin_client):
    """GET a missing user id → 404."""
    r = admin_client.get("/api/users/424242")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_update_user_cannot_deactivate_self(admin_client):
    """Admin cannot set is_active=false on their own account (self-edit guardrail → 400)."""
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"is_active": False})
    assert r.status_code == 400
    assert "deactivate yourself" in r.json()["detail"].lower()


def test_cov_update_user_self_edit_name_only_succeeds(admin_client):
    """Self-editing a non-guarded field (name) must not trip any self-edit guardrail."""
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"name": "Renamed Self"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed Self"


def test_cov_update_user_last_admin_demote_self_blocked(admin_client):
    """Sole admin self-demote is blocked 400 (self-edit guard fires before the unreachable last-admin raise)."""
    me = admin_client.get("/api/auth/me").json()
    r = admin_client.put(f"/api/users/{me['id']}", json={"role": "user"})
    assert r.status_code == 400
    assert "demote yourself" in r.json()["detail"].lower()


def test_cov_update_user_demote_other_admin_runs_last_admin_check(admin_client):
    """Demoting another admin skips the self-edit guard and runs the last-admin count (>=1, so it succeeds)."""
    other = _mk_user(admin_client, "Cov Admin3", "cov.admin3@example.com",
                     role="admin", password="Admin3Pass9")
    r = admin_client.put(f"/api/users/{other['id']}", json={"role": "user"})
    assert r.status_code == 200
    assert r.json()["role"] == "user"


def test_cov_update_user_404(admin_client):
    """PUT a missing user id → 404."""
    r = admin_client.put("/api/users/313131", json={"name": "Cov Nope"})
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_update_user_duplicate_email_409(admin_client):
    """Updating a user's email to one already in use hits the IntegrityError branch → 409."""
    a = _mk_user(admin_client, "Cov EmailA", "cov.email.a@example.com")
    _mk_user(admin_client, "Cov EmailB", "cov.email.b@example.com")
    r = admin_client.put(f"/api/users/{a['id']}",
                         json={"email": "cov.email.b@example.com"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Email already exists"


def test_cov_update_user_role_change_and_password_reset(admin_client):
    """Promote to manager + reset password in one PUT."""
    u = _mk_user(admin_client, "Cov Promote", "cov.promote@example.com")
    assert admin_client.put(f"/api/users/{u['id']}", json={"password": "changeme"}).status_code == 422
    r = admin_client.put(f"/api/users/{u['id']}",
                         json={"role": "manager", "password": "Promoted2026"})
    assert r.status_code == 200
    assert r.json()["role"] == "manager"
    # Confirm the new password actually works.
    other_c = _new_client()
    rl = other_c.post("/api/auth/login",
                      json={"email": "cov.promote@example.com", "password": "Promoted2026"})
    assert rl.status_code == 200


def test_cov_delete_user_404(admin_client):
    """DELETE a missing user id → 404."""
    r = admin_client.delete("/api/users/565656")
    assert r.status_code == 404
    assert r.json()["detail"] == "User not found"


def test_cov_delete_user_admin_runs_last_admin_count(admin_client):
    """Deleting another admin runs the last-admin count (bootstrap still active, so >=1 and delete succeeds)."""
    target = _mk_user(admin_client, "Cov DelAdmin", "cov.deladmin@example.com",
                      role="admin", password="DelAdmin99")
    r = admin_client.delete(f"/api/users/{target['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "User deleted"
    assert admin_client.get(f"/api/users/{target['id']}").status_code == 404


def test_cov_delete_user_success(admin_client):
    """DELETE a regular user succeeds and returns message."""
    u = _mk_user(admin_client, "Cov DelOK", "cov.delok@example.com")
    r = admin_client.delete(f"/api/users/{u['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "User deleted"
    assert admin_client.get(f"/api/users/{u['id']}").status_code == 404


# --- sessions.py ---
def test_cov_sessions_list_and_is_current(admin_client):
    """Session list returns multiple rows and marks exactly the admin's own as is_current."""
    _mk_user(admin_client, "Cov SessU", "cov.sessu@example.com")
    other_c = _new_client()
    _login(other_c, "cov.sessu@example.com", "User12345")

    rows = admin_client.get("/api/sessions").json()
    assert isinstance(rows, list)
    assert len(rows) >= 2
    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1, "exactly one row should be the admin's own session"
    assert current[0]["user_email"] == _BOOTSTRAP_EMAIL


def test_cov_sessions_hides_then_retention_removes_expired_rows(admin_client):
    """Listing sessions hides rows whose expires_at is past; the retention job deletes them."""
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app.models import Session as SessionRow

    _mk_user(admin_client, "Cov Expired", "cov.expired@example.com")
    other_c = _new_client()
    _login(other_c, "cov.expired@example.com", "User12345")

    # Back-date the user's session row so the sweep logic will remove it.
    db = SessionLocal()
    try:
        from app.models import User
        uid = db.query(User).filter(User.email == "cov.expired@example.com").one().id
        row = db.query(SessionRow).filter(SessionRow.user_id == uid).one()
        target_id = row.id
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
    finally:
        db.close()

    rows = admin_client.get("/api/sessions").json()
    assert all(r["id"] != target_id for r in rows), "expired row should be hidden"

    from app.jobs.retention import run_once
    run_once()
    db2 = SessionLocal()
    try:
        assert db2.get(SessionRow, target_id) is None
    finally:
        db2.close()


def test_cov_revoke_specific_session(admin_client):
    """Admin revokes another user's session, gets the message, and the row is gone."""
    _mk_user(admin_client, "Cov Revoke", "cov.revoke@example.com")
    other_c = _new_client()
    _login(other_c, "cov.revoke@example.com", "User12345")

    rows = admin_client.get("/api/sessions").json()
    victim = next(r for r in rows if r["user_email"] == "cov.revoke@example.com")
    r = admin_client.delete(f"/api/sessions/{victim['id']}")
    assert r.status_code == 200
    assert r.json()["message"] == "Session revoked"
    after = {row["id"] for row in admin_client.get("/api/sessions").json()}
    assert victim["id"] not in after


def test_cov_revoke_own_session_blocked(admin_client):
    """Admin cannot revoke their own current session."""
    rows = admin_client.get("/api/sessions").json()
    mine = next(r for r in rows if r["is_current"])
    r = admin_client.delete(f"/api/sessions/{mine['id']}")
    assert r.status_code == 400
    assert "your own current session" in r.json()["detail"].lower()


def test_cov_revoke_session_404(admin_client):
    """Revoking a non-existent session id → 404."""
    r = admin_client.delete("/api/sessions/909090")
    assert r.status_code == 404
    assert r.json()["detail"] == "Session not found"


# --- audit.py ---
def test_cov_audit_filter_by_entity_actor_and_numeric_query(admin_client):
    """Audit filter/pagination: entity_type, actor_user_id, numeric free-text (OR-clause builder), limit/offset."""
    me = admin_client.get("/api/auth/me").json()
    p = _mk_project(admin_client, "Cov Audit Proj")
    bug = _mk_bug(admin_client, p["id"], title="Cov Audit Bug")

    # entity_type + actor filter + free-text + numeric (bug id) + pagination.
    r = admin_client.get(
        "/api/audit",
        params={
            "entity_type": "bug",
            "actor_user_id": me["id"],
            "q": f"bug {bug['id']}",
            "limit": 50,
            "offset": 0,
        },
    )
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert any(row.get("bug_id") == bug["id"] or row.get("entity_id") == bug["id"]
               for row in rows), rows


def test_cov_audit_query_with_hash_and_offset(admin_client):
    """'#'-prefixed numeric query with non-zero offset hits the entity_id substring cast clause."""
    p = _mk_project(admin_client, "Cov Audit Proj 2")
    _mk_bug(admin_client, p["id"], title="Cov Audit Bug 2")
    r = admin_client.get("/api/audit", params={"q": "#1", "limit": 10, "offset": 1})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# --- notifications.py ---
def test_cov_mark_read_already_read_skips_commit(admin_client):
    """Mark-read on an already-read notification short-circuits (read_at set) and still returns {'ok': True}."""
    p = _mk_project(admin_client, "Cov Notif Proj")
    bob = _mk_user(admin_client, "Cov Notif Bob", "cov.notif.bob@example.com", project_ids=[p["id"]])
    _mk_bug(admin_client, p["id"], title="Cov Notif Bug", assignee_ids=[bob["id"]])

    bob_c = _new_client()
    _login(bob_c, "cov.notif.bob@example.com", "User12345")
    notifs = bob_c.get("/api/notifications").json()
    assert notifs, "Bob should have an 'assigned' notification"
    nid = notifs[0]["id"]

    r1 = bob_c.post(f"/api/notifications/{nid}/read")
    assert r1.status_code == 200
    assert r1.json() == {"ok": True}

    # Second call hits the skip branch (read_at already set).
    r2 = bob_c.post(f"/api/notifications/{nid}/read")
    assert r2.status_code == 200
    assert r2.json() == {"ok": True}

    # Still exactly one row, still marked read.
    row = next(n for n in bob_c.get("/api/notifications").json() if n["id"] == nid)
    assert row["read_at"] is not None


# Intentionally-uncovered defensive/dead-code paths (unreachable by clean API tests):
#   sessions.py _is_current `if not parsed` — auth rejects malformed cookies before the handler runs.
#   audit.py `except ValueError` after int() — the group is re.search(r"\d+"), always ASCII digits.
#   users.py last-admin guardrail raises (update/delete) — self-edit guard or count>=1 always pre-empts them
#     (count queries covered by test_cov_update_user_demote_other_admin_runs_last_admin_check /
#      test_cov_delete_user_admin_runs_last_admin_count).


# app/routes/bugs.py — helpers + route edge cases
def test_upload_policy_extension_check(client):
    from app.upload_policy import check_extension
    assert "without an extension" in check_extension("README")
    assert ".exe" in check_extension("evil.exe")
    assert check_extension("notes.TXT") is None


def test_people_resolve_user_unknown_raises_400(client):
    # None is allowed (no filter); a non-existent id is a hard 400.
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.people import resolve_user
    db = SessionLocal()
    try:
        org_id = default_org_id()
        assert resolve_user(db, None, org_id) is None
        with pytest.raises(HTTPException) as ei:
            resolve_user(db, 999999, org_id)
        assert ei.value.status_code == 400
    finally:
        db.close()


def test_bugs_reject_overflow_ids_raises_422(client):
    # An id filter outside the int4 range is a clean 422.
    from fastapi import HTTPException

    from app.services.bug_query import reject_overflow_ids as _reject_overflow_ids
    from app.util import MAX_PK_INT as _MAX_PK_INT
    with pytest.raises(HTTPException) as ei:
        _reject_overflow_ids(reporter_id=_MAX_PK_INT + 1)
    assert ei.value.status_code == 422


def test_bugs_directional_link_reaches_multi_hop(client):
    # BFS walks an intermediate node before finding the goal one hop further.
    from sqlalchemy import select

    from app import models
    from app.database import SessionLocal
    from app.services.bug_links import directional_link_reaches as _directional_link_reaches

    db = SessionLocal()
    try:
        actor = db.scalar(
            select(models.User).where(models.User.email == BOOTSTRAP_EMAIL))
        proj = models.Project(org_id=default_org_id(), name="LinkProj", description="")
        db.add(proj)
        db.flush()
        a = models.Bug(title="A", description="", status="New", priority="Low",
                       environment="DEV", project_id=proj.id, reporter_id=actor.id)
        b = models.Bug(title="B", description="", status="New", priority="Low",
                       environment="DEV", project_id=proj.id, reporter_id=actor.id)
        c = models.Bug(title="C", description="", status="New", priority="Low",
                       environment="DEV", project_id=proj.id, reporter_id=actor.id)
        db.add_all([a, b, c])
        db.flush()
        # A --blocks--> B --blocks--> C; reaching C from A walks through B.
        db.add_all([
            models.BugLink(source_bug_id=a.id, target_bug_id=b.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=b.id, target_bug_id=c.id,
                           link_type="blocks", created_by_user_id=actor.id),
        ])
        db.commit()
        reached = _directional_link_reaches(db, a.id, c.id, "blocks")
    finally:
        db.close()
    assert reached is True


def test_bugs_directional_link_reaches_diamond_revisits_node(client):
    # A --blocks--> B --blocks--> E and A --blocks--> C --blocks--> E, then
    # E --blocks--> D (goal): the same frontier level discovers E twice (via B
    # and via C) before D is reached, so the second discovery must skip E as
    # already-seen rather than re-queuing it.
    from sqlalchemy import select

    from app import models
    from app.database import SessionLocal
    from app.services.bug_links import directional_link_reaches as _directional_link_reaches

    db = SessionLocal()
    try:
        actor = db.scalar(
            select(models.User).where(models.User.email == BOOTSTRAP_EMAIL))
        proj = models.Project(org_id=default_org_id(), name="DiamondProj", description="")
        db.add(proj)
        db.flush()
        a, b, c, e, d = (
            models.Bug(title=t, description="", status="New", priority="Low",
                      environment="DEV", project_id=proj.id, reporter_id=actor.id)
            for t in ("A", "B", "C", "E", "D")
        )
        db.add_all([a, b, c, e, d])
        db.flush()
        db.add_all([
            models.BugLink(source_bug_id=a.id, target_bug_id=b.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=a.id, target_bug_id=c.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=b.id, target_bug_id=e.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=c.id, target_bug_id=e.id,
                           link_type="blocks", created_by_user_id=actor.id),
            models.BugLink(source_bug_id=e.id, target_bug_id=d.id,
                           link_type="blocks", created_by_user_id=actor.id),
        ])
        db.commit()
        reached = _directional_link_reaches(db, a.id, d.id, "blocks")
    finally:
        db.close()
    assert reached is True


def test_bugs_reject_updated_at_drift_aware_current(client):
    # tz-aware current_updated skips naive coercion; same instant, no conflict.
    from app.services.bug_update import reject_if_updated_at_drifted as _reject_if_updated_at_drifted
    aware = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
    _reject_if_updated_at_drifted(aware.isoformat(), aware)


def test_bugs_list_deep_page_returns_empty(admin_client):
    # A page far past the end short-circuits to [] instead of a deep OFFSET scan.
    proj = admin_client.post("/api/projects", json={"name": "DeepPage"}).json()
    admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "only one", "priority": "Low",
        "environment": "DEV",
    })
    r = admin_client.get("/api/bugs?page=10000000&page_size=20")
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_bugs_attachment_dangerous_ext_rejected(admin_client):
    proj = admin_client.post("/api/projects", json={"name": "AttProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "needs file", "priority": "Low",
        "environment": "DEV",
    }).json()
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "exe" in r.json()["detail"].lower()


def _mk_user_via_api(admin_client, name, email, project_ids=()):
    r = admin_client.post("/api/users", json={
        "name": name, "email": email, "role": "user", "password": "User12345",
        "project_ids": list(project_ids),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_bugs_update_changes_reporter_and_notifies(admin_client):
    # Reporter change is recorded; a tracked-field update stages 'updated' notifications.
    proj = admin_client.post("/api/projects", json={"name": "UpdProj"}).json()
    uid = _mk_user_via_api(admin_client, "Redo", "redo@test.local", [proj["id"]])
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "orig", "priority": "Low",
        "environment": "DEV", "assignee_ids": [uid],
    }).json()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "title": "renamed", "reporter_id": uid,
    })
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "renamed"
    assert r.json()["reporter"]["id"] == uid


def test_bugs_update_reporter_change_same_display_name_not_tracked(admin_client):
    # Two different users sharing a display name: the reporter_id genuinely
    # changes, and even though the plain display-name label is identical, the
    # write must not be silently rolled back as a no-op — the audit entry
    # disambiguates via email instead.
    proj = admin_client.post("/api/projects", json={"name": "SameNameProj"}).json()
    uid1 = _mk_user_via_api(admin_client, "Same Name", "same1@test.local", [proj["id"]])
    uid2 = _mk_user_via_api(admin_client, "Same Name", "same2@test.local", [proj["id"]])
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "orig", "priority": "Low",
        "environment": "DEV", "reporter_id": uid1,
    }).json()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": uid2})
    assert r.status_code == 200, r.text
    assert r.json()["reporter"]["id"] == uid2
    activity = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()
    assert any(
        "reporter" in a["detail"].lower() and "same2@test.local" in a["detail"]
        for a in activity
    )


def test_bugs_update_legacy_invalid_status_tolerated(admin_client):
    # Legacy status invalid for the current type is tolerated when unchanged by the update.
    from app.database import SessionLocal
    from app.models import Bug
    proj = admin_client.post("/api/projects", json={"name": "LegacyProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "legacy", "priority": "Low",
        "environment": "DEV",
    }).json()
    # 'Done' is Task-only; force it directly to simulate a legacy row.
    db = SessionLocal()
    try:
        row = db.get(Bug, bug["id"])
        row.status = "Done"  # valid enum, but not valid for item_type Bug
        db.commit()
    finally:
        db.close()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "title": "legacy-renamed", "status": "Done",
    })
    assert r.status_code == 200, r.text


def test_bugs_add_reverse_relates_link_is_idempotent(admin_client):
    # Reverse-direction 'relates' returns the existing edge, no duplicate.
    proj = admin_client.post("/api/projects", json={"name": "RelProj"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye bug", "priority": "Low", "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee bug", "priority": "Low", "environment": "DEV",
    }).json()
    r1 = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "relates",
    })
    assert r1.status_code == 201, r1.text
    r2 = admin_client.post(f"/api/bugs/{b['id']}/links", json={
        "target_bug_id": a["id"], "link_type": "relates",
    })
    assert r2.status_code in (200, 201)


def test_bugs_remove_link_other_endpoint_present(admin_client):
    # Other endpoint exists, so the authz/stakeholder-notification branch is taken.
    proj = admin_client.post("/api/projects", json={"name": "DelLink"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye bug", "priority": "Low", "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee bug", "priority": "Low", "environment": "DEV",
    }).json()
    link = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks",
    }).json()
    r = admin_client.delete(f"/api/bugs/{a['id']}/links/{link['id']}")
    assert r.status_code == 200, r.text


def test_bugs_attachment_grows_past_limit_after_strip(admin_client, monkeypatch):
    # Stripping can re-encode larger; post-strip re-check must 413 over the cap.
    import app.routes.bugs as bugs_mod
    monkeypatch.setattr(bugs_mod, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(bugs_mod, "strip_image_metadata", lambda _d, _ct: b"x" * 500)

    proj = admin_client.post("/api/projects", json={"name": "GrowProj"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "grow test", "priority": "Low",
        "environment": "DEV",
    }).json()
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("pic.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert r.status_code == 413


def test_bugs_reload_link_missing_raises_409(client):
    # A concurrently deleted row (db.scalar returns None) must give a clean 409.
    from fastapi import HTTPException

    from app.services.bug_links import reload_link as _reload_link

    class _FakeDB:
        def scalar(self, _stmt):
            return None

    fake_db = _FakeDB()
    with pytest.raises(HTTPException) as ei:
        _reload_link(fake_db, 123)
    assert ei.value.status_code == 409


def test_bugs_insert_link_race_returns_existing(admin_client, monkeypatch):
    # Losing the unique-index race returns the existing edge idempotently, not a 500.
    import app.routes.bugs as bugs_mod

    proj = admin_client.post("/api/projects", json={"name": "RaceProj"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye bug", "priority": "Low", "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee bug", "priority": "Low", "environment": "DEV",
    }).json()
    first = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks",
    }).json()

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import BugLink

    db = SessionLocal()
    try:
        existing_link = db.scalar(select(BugLink).where(BugLink.id == first["id"]))
        existing_id = existing_link.id
    finally:
        db.close()

    # Simulate losing the race: created=False triggers the idempotent return.
    def lost_race(_db, _link, _refetch):
        from app.database import SessionLocal as SL
        from app.models import BugLink as BL
        s = SL()
        try:
            row = s.scalar(select(BL).where(BL.id == existing_id))
            s.expunge(row)
        finally:
            s.close()
        return row, False  # created=False

    monkeypatch.setattr(bugs_mod, "insert_link_or_existing", lost_race)
    # Fresh (source, target, type) so _find_existing is None and our patched helper runs.
    again = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "duplicate",
    })
    assert again.status_code in (200, 201)


def _orphan_link(admin_client, *, drop="other"):
    """Create A--blocks-->B, delete one bug with FK off so the link survives orphaned; returns (path_bug_id, link_id)."""
    from sqlalchemy import delete, text

    from app.database import SessionLocal
    from app.models import Bug

    proj = admin_client.post("/api/projects", json={"name": f"Orphan{drop}"}).json()
    a = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Aye orphan", "priority": "Low",
        "environment": "DEV",
    }).json()
    b = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bee orphan", "priority": "Low",
        "environment": "DEV",
    }).json()
    link = admin_client.post(f"/api/bugs/{a['id']}/links", json={
        "target_bug_id": b["id"], "link_type": "blocks",
    }).json()
    victim = b["id"] if drop == "other" else a["id"]
    db = SessionLocal()
    try:
        db.execute(text("PRAGMA foreign_keys = OFF"))
        db.execute(delete(Bug).where(Bug.id == victim))
        db.commit()
    finally:
        db.close()
    return a["id"], link["id"]


def test_bugs_remove_link_other_endpoint_gone(admin_client):
    # Other endpoint gone (orphaned): the block is skipped and the link deleted directly.
    path_bug_id, link_id = _orphan_link(admin_client, drop="other")
    r = admin_client.delete(f"/api/bugs/{path_bug_id}/links/{link_id}")
    assert r.status_code == 200, r.text


def test_bugs_remove_link_path_bug_gone_404(admin_client):
    # The path bug itself is gone (orphaned link); the route must return 404.
    path_bug_id, link_id = _orphan_link(admin_client, drop="path")
    r = admin_client.delete(f"/api/bugs/{path_bug_id}/links/{link_id}")
    assert r.status_code == 404


def test_bugs_download_range_row_vanished_404(admin_client):
    # TOCTOU: row deleted between metadata read and byte-slice must 404, not empty 206.
    from app.database import SessionLocal, get_db
    from app.main import app

    proj = admin_client.post("/api/projects", json={"name": "DlRace"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "dl race", "priority": "Low",
        "environment": "DEV",
    }).json()
    up = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files={"file": ("note.txt", b"hello world data", "text/plain")},
    )
    assert up.status_code == 201, up.text
    att_id = up.json()["id"]

    class _SliceVanishSession:
        def __init__(self, real):
            self._real = real

        def scalar(self, stmt):
            if "substr" in str(stmt).lower():
                return None  # concurrent delete of the blob row
            return self._real.scalar(stmt)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _override_db():
        real = SessionLocal()
        try:
            yield _SliceVanishSession(real)
        finally:
            real.close()

    app.dependency_overrides[get_db] = _override_db
    try:
        r = admin_client.get(
            f"/api/bugs/{bug['id']}/attachments/{att_id}/download",
            headers={"Range": "bytes=0-3"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 404


def test_bugs_update_assignee_only_skips_changes_notify(admin_client):
    # Assignee-only update: empty changes but newly_assigned set, so only the assign-notify block runs.
    proj = admin_client.post("/api/projects", json={"name": "AssignOnly"}).json()
    uid = _mk_user_via_api(admin_client, "Assignee Only", "assignonly@test.local", [proj["id"]])
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "assignee only", "priority": "Low",
        "environment": "DEV",
    }).json()
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "title": "assignee only", "priority": "Low", "environment": "DEV",
        "assignee_ids": [uid],
    })
    assert r.status_code == 200, r.text
    assert uid in [a["id"] for a in r.json()["assignees"]]
