"""Regression tests for the v2.4 enterprise changes:

  1. First-run bootstrap admin from BOOTSTRAP_ADMIN_* env vars — creates
     one organization + one admin user on an empty database; idempotent
     once that user exists.
  2. Audit history survives bug deletion — the bug-delete handler
     detaches activity rows (bug_id → NULL) before issuing the DELETE,
     so the trail still shows the full original story plus the new
     `bug_deleted` row.
  3. Audit search hits live bug titles via the LEFT JOIN bugs and
     baked-in titles in the detail strings written from v2.4 onwards.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_app(monkeypatch, tmp_path, extra_env=None):
    """Spin up a clean app instance with the given env. Used for the
    bootstrap-admin tests so we can vary BOOTSTRAP_ADMIN_* per test."""
    db_path = tmp_path / f"bootstrap_{os.urandom(4).hex()}.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "v24_test_secret")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("CSRF_PROTECTION", "false")
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
    for k, v in (extra_env or {}).items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app), db_path


# ---------------------------------------------------------------------------
# 1. Bootstrap admin
# ---------------------------------------------------------------------------
def test_bootstrap_creates_admin_when_db_empty(monkeypatch, tmp_path):
    """A fresh DB with BOOTSTRAP_ADMIN_EMAIL set should yield a logged-in-able
    admin without the user having to hit /signup."""
    with _fresh_app(monkeypatch, tmp_path, {
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "BootPass1234",
        "BOOTSTRAP_ADMIN_NAME": "Boot Admin",
        "BOOTSTRAP_ORG_NAME": "Boot Co",
    })[0] as client:
        # Lifespan startup already ran in the with-block; the user should exist.
        r = client.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "BootPass1234",
        })
        assert r.status_code == 200, r.text
        me = r.json()
        assert me["email"] == "boot@bh.test"
        assert me["role"] == "admin"
        assert me["organization_name"] == "Boot Co"


def test_bootstrap_is_idempotent(monkeypatch, tmp_path):
    """Re-running with the same bootstrap user must not modify the existing
    user, must not create a duplicate, and must not blow up."""
    # First boot — create.
    with _fresh_app(monkeypatch, tmp_path, {
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "Original12345",
    })[0] as client:
        r = client.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "Original12345",
        })
        assert r.status_code == 200, r.text

    # Second boot with the SAME email but DIFFERENT password.
    # The bootstrap should NOT modify the existing user.
    db_path = tmp_path / "shared.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "v24_test_secret")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("CSRF_PROTECTION", "false")

    # First boot to populate the shared.db.
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "boot@bh.test")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Original12345")
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        pass

    # Second boot — different password in env.
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Different67890")
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import app as app2
    with TestClient(app2) as client2:
        # Original password still works — bootstrap did NOT overwrite.
        r = client2.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "Original12345",
        })
        assert r.status_code == 200, r.text
        # New env-var password should NOT work.
        r2 = client2.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "Different67890",
        })
        assert r2.status_code == 401


def test_bootstrap_disabled_when_email_blank(monkeypatch, tmp_path):
    """With BOOTSTRAP_ADMIN_EMAIL empty (the default), no user is created."""
    with _fresh_app(monkeypatch, tmp_path, {
        "BOOTSTRAP_ADMIN_EMAIL": "",
        "BOOTSTRAP_ADMIN_PASSWORD": "Whatever12345",
    })[0] as client:
        r = client.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "Whatever12345",
        })
        # No user, no login.
        assert r.status_code == 401


def test_bootstrap_disabled_when_password_blank(monkeypatch, tmp_path):
    """Safety: missing password must NOT create a passwordless admin."""
    with _fresh_app(monkeypatch, tmp_path, {
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "",
    })[0] as client:
        r = client.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "",
        })
        assert r.status_code == 401


def test_bootstrap_reset_password_overwrites_existing(monkeypatch, tmp_path):
    """The escape-hatch flag: when an existing user is found AND
    BOOTSTRAP_ADMIN_RESET_PASSWORD=true, their password is reset to
    the env-var value. This is the recovery path for deployments where
    the user was created with a stale password by an earlier run."""
    db_path = tmp_path / "reset.db"

    def _set_env(extra):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "v24_reset_secret")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]

    # Boot 1: create the user with the original password.
    _set_env({
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "OriginalPW123",
        "BOOTSTRAP_ADMIN_RESET_PASSWORD": "false",
    })
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        r = client.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "OriginalPW123",
        })
        assert r.status_code == 200

    # Boot 2: same email, different password, RESET FLAG OFF — the
    # bootstrap should be a NO-OP. Original password must still work.
    _set_env({
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "NewPW456",
        "BOOTSTRAP_ADMIN_RESET_PASSWORD": "false",
    })
    from app.main import app as app2
    with TestClient(app2) as client2:
        r = client2.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "OriginalPW123",
        })
        assert r.status_code == 200, "original password should still work when reset flag is OFF"
        r2 = client2.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "NewPW456",
        })
        assert r2.status_code == 401, "new password must NOT be accepted when reset flag is OFF"

    # Boot 3: same email, RESET FLAG ON — the bootstrap should reset
    # the password to the env value. New password works, old one does not.
    _set_env({
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "NewPW456",
        "BOOTSTRAP_ADMIN_RESET_PASSWORD": "true",
    })
    from app.main import app as app3
    with TestClient(app3) as client3:
        r = client3.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "NewPW456",
        })
        assert r.status_code == 200, "reset password should be accepted after BOOTSTRAP_ADMIN_RESET_PASSWORD=true boot"
        r2 = client3.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "OriginalPW123",
        })
        assert r2.status_code == 401, "original password must NOT work after reset"


def test_bootstrap_reset_promotes_disabled_user(monkeypatch, tmp_path):
    """The reset path also re-activates a disabled user and re-promotes
    them to admin — so a deactivated bootstrap user can be recovered."""
    db_path = tmp_path / "reset_disabled.db"

    def _set_env(extra):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "v24_reset_disabled")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        for k, v in extra.items():
            monkeypatch.setenv(k, v)
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]

    _set_env({
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "Initial123",
        "BOOTSTRAP_ADMIN_RESET_PASSWORD": "false",
    })
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        client.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "Initial123",
        })

    # Manually disable + downgrade the user in the DB.
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE users SET is_active = 0, role = 'member' "
            "WHERE email = 'boot@bh.test'"
        ))
    eng.dispose()

    # Boot again with the reset flag ON.
    _set_env({
        "BOOTSTRAP_ADMIN_EMAIL": "boot@bh.test",
        "BOOTSTRAP_ADMIN_PASSWORD": "Restored456",
        "BOOTSTRAP_ADMIN_RESET_PASSWORD": "true",
    })
    from app.main import app as app2
    with TestClient(app2) as client2:
        r = client2.post("/api/auth/login", json={
            "email": "boot@bh.test", "password": "Restored456",
        })
        assert r.status_code == 200, r.text
        # Role should be admin again.
        assert r.json()["role"] == "admin"
        assert r.json()["is_active"] is True


# ---------------------------------------------------------------------------
# 2. Audit history retention on bug delete
# ---------------------------------------------------------------------------
@pytest.fixture()
def admin_with_project(client):
    """Sign up an admin, create a project. Returns (client, project_id)."""
    r = client.post("/api/auth/signup", json={
        "organization_name": "Acme", "name": "Alice",
        "email": "alice@acme.test", "password": "AlicePass1!",
    })
    assert r.status_code == 201, r.text
    pr = client.post("/api/projects", json={"name": "Eng", "key": "ENG"})
    assert pr.status_code == 201, pr.text
    return client, pr.json()["id"]


def test_audit_history_survives_bug_delete(admin_with_project):
    client, project_id = admin_with_project
    # Create + edit + comment + delete.
    r = client.post("/api/bugs", json={
        "title": "Login broken bug v24",
        "project_id": project_id,
        "priority": "High",
        "environment": "PROD",
    })
    assert r.status_code == 201, r.text
    bug_id = r.json()["id"]
    client.put(f"/api/bugs/{bug_id}", json={"status": "In Progress"})
    client.post(f"/api/bugs/{bug_id}/comments", json={"body": "Investigating now"})

    before = client.get("/api/audit").json()
    rows_for_bug_before = [
        r for r in before
        if (r["entity_type"] == "bug" and r["entity_id"] == bug_id)
    ]
    assert len(rows_for_bug_before) >= 3, rows_for_bug_before

    # Delete.
    r = client.delete(f"/api/bugs/{bug_id}")
    assert r.status_code == 200, r.text

    after = client.get("/api/audit").json()
    rows_for_bug_after = [
        r for r in after
        if (r["entity_type"] == "bug" and r["entity_id"] == bug_id)
    ]
    actions = sorted({r["action"] for r in rows_for_bug_after})
    # All the old events plus the new bug_deleted summary row should be there.
    assert "bug_created" in actions
    assert "comment_added" in actions
    assert "status_changed" in actions
    assert "bug_deleted" in actions
    assert len(rows_for_bug_after) >= len(rows_for_bug_before), (
        f"audit history shrank after delete: {len(rows_for_bug_after)} < {len(rows_for_bug_before)}"
    )


# ---------------------------------------------------------------------------
# 3. Audit search — live title, baked-in title, item-type via Bug join
# ---------------------------------------------------------------------------
def test_audit_search_by_baked_in_title(admin_with_project):
    """The v2.4 bug_created detail bakes the title into the row, so
    pasting a title fragment into the audit search box returns hits
    even after renames."""
    client, project_id = admin_with_project
    r = client.post("/api/bugs", json={
        "title": "Payment-gateway timeout v24",
        "project_id": project_id,
    })
    assert r.status_code == 201, r.text
    bug_id = r.json()["id"]

    r = client.get("/api/audit", params={"q": "Payment-gateway"})
    rows = r.json()
    assert any(rw["entity_id"] == bug_id for rw in rows), rows


def test_audit_search_by_bug_number_finds_history(admin_with_project):
    client, project_id = admin_with_project
    r = client.post("/api/bugs", json={
        "title": "Some other thing", "project_id": project_id,
    })
    bug_id = r.json()["id"]
    client.put(f"/api/bugs/{bug_id}", json={"priority": "Critical"})

    # Search by the bug number alone.
    r = client.get("/api/audit", params={"q": f"#{bug_id}"})
    rows = r.json()
    assert any(rw["entity_id"] == bug_id for rw in rows), rows


def test_audit_search_after_rename_uses_left_join(admin_with_project):
    """A bug renamed AFTER an audit row was written should still be
    findable by the new title — that's what the LEFT JOIN on bugs
    gives us."""
    client, project_id = admin_with_project
    r = client.post("/api/bugs", json={
        "title": "Original title here",
        "project_id": project_id,
    })
    bug_id = r.json()["id"]
    # Rename — audit row for the rename mentions BOTH titles, but the
    # bug_created row from earlier only mentions "Original title".
    client.put(f"/api/bugs/{bug_id}", json={"title": "Renamed title now"})

    # Search the post-rename title — should still find the bug_created
    # row (via the LEFT JOIN on bugs.title).
    r = client.get("/api/audit", params={"q": "Renamed title"})
    rows = r.json()
    found_create_event = any(
        rw["entity_id"] == bug_id and rw["action"] == "bug_created"
        for rw in rows
    )
    assert found_create_event, (
        f"bug_created should be findable via post-rename title (LEFT JOIN bugs): {rows[:5]}"
    )


def test_audit_search_orgs_isolated(two_orgs):
    """Audit search must remain org-scoped — Alice's queries never
    return Bob's rows even when the search term matches both."""
    c_a, c_b, me_a, me_b = two_orgs
    # Each side creates a project + bug with the same title fragment.
    c_a.post("/api/projects", json={"name": "P-A", "key": "PA"})
    c_b.post("/api/projects", json={"name": "P-B", "key": "PB"})
    proj_a = c_a.get("/api/projects").json()[0]["id"]
    proj_b = c_b.get("/api/projects").json()[0]["id"]
    c_a.post("/api/bugs", json={"title": "shared-keyword-X", "project_id": proj_a})
    c_b.post("/api/bugs", json={"title": "shared-keyword-X", "project_id": proj_b})

    rows_a = c_a.get("/api/audit", params={"q": "shared-keyword"}).json()
    rows_b = c_b.get("/api/audit", params={"q": "shared-keyword"}).json()
    # Each side should see ONLY its own audit rows.
    assert rows_a, rows_a
    assert rows_b, rows_b
    assert all(r["entity_type"] == "bug" for r in rows_a if r["entity_id"])
    # Verify no cross-tenant leak by checking actor_user_id matches.
    a_user = me_a["id"]
    b_user = me_b["id"]
    a_actors = {r["actor_user_id"] for r in rows_a if r["actor_user_id"]}
    b_actors = {r["actor_user_id"] for r in rows_b if r["actor_user_id"]}
    assert b_user not in a_actors, "Org A's audit must NOT contain Org B's actor"
    assert a_user not in b_actors, "Org B's audit must NOT contain Org A's actor"
