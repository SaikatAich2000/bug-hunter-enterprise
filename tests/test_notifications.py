"""Tests for per-user in-app notifications.
Triggers, per-user isolation, unread count, read/delete lifecycle, auth, additive schema safety.
"""
from __future__ import annotations

import sys

from fastapi.testclient import TestClient

_PW = "User12345"
_NOTIFS = "/api/notifications"
_BOOTSTRAP_EMAIL = "admin@test.local"
_BOOTSTRAP_PW = "Admin1234"


# Helpers
def _admin(c: TestClient) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": _BOOTSTRAP_EMAIL, "password": _BOOTSTRAP_PW})
    assert r.status_code == 200, r.text


def _new_client() -> TestClient:
    """Return a second TestClient sharing the already-imported app and DB."""
    from app.main import app
    return TestClient(app)


def _login(c: TestClient, email: str, password: str = _PW) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _mk_user(admin: TestClient, name: str, email: str, role: str = "user") -> dict:
    body = {"name": name, "email": email, "role": role, "password": _PW}
    # Grant all projects so the user can comment/edit the bugs used here.
    pids = [p["id"] for p in admin.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = admin.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_project(admin: TestClient, name: str = "Notif Proj") -> dict:
    r = admin.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(admin: TestClient, project_id: int, **extra) -> dict:
    body = {"project_id": project_id, "title": "Notify me", "priority": "Medium", "environment": "DEV"}
    body.update(extra)
    r = admin.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# Auth
def test_notifications_require_auth(client):
    assert client.get(_NOTIFS).status_code == 401
    assert client.get(f"{_NOTIFS}/unread_count").status_code == 401
    assert client.post(f"{_NOTIFS}/read-all").status_code == 401


# Triggers
def test_assignment_creates_notification_for_assignee(client):
    _admin(client)
    proj = _mk_project(client)
    bob = _mk_user(client, "Bob", "bob@notif.test")
    bug = _mk_bug(client, proj["id"], title="Assigned bug", assignee_ids=[bob["id"]])

    bobc = _new_client()
    _login(bobc, "bob@notif.test")
    notifs = bobc.get(_NOTIFS).json()
    assert any(n["kind"] == "assigned" and n["bug_id"] == bug["id"] for n in notifs), notifs
    assert bobc.get(f"{_NOTIFS}/unread_count").json()["unread"] >= 1


def test_assignee_only_update_skips_updated_notification(client):
    """A PUT that only adds an assignee (no other tracked field changes) must
    fire the 'assigned' notification but no 'updated' one, since `changes` is
    empty for everyone except the newly-assigned user."""
    _admin(client)
    proj = _mk_project(client, "AssigneeOnlyProj")
    bob = _mk_user(client, "Bob", "bob.assignonly@notif.test")
    bug = _mk_bug(client, proj["id"], title="No assignees yet")

    r = client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": [bob["id"]]})
    assert r.status_code == 200, r.text

    bobc = _new_client()
    _login(bobc, "bob.assignonly@notif.test")
    bob_notifs = bobc.get(_NOTIFS).json()
    assert any(n["kind"] == "assigned" and n["bug_id"] == bug["id"] for n in bob_notifs), bob_notifs
    assert not any(n["kind"] == "updated" and n["bug_id"] == bug["id"] for n in bob_notifs), bob_notifs


def test_self_assignment_notifies_actor(client):
    """Self-assignment still produces a notification for the actor."""
    _admin(client)
    proj = _mk_project(client, "Self Proj")
    me = client.get("/api/auth/me").json()
    bug = _mk_bug(client, proj["id"], title="Self bug", assignee_ids=[me["id"]])
    notifs = client.get(_NOTIFS).json()
    assert any(n["bug_id"] == bug["id"] and n["kind"] == "assigned"
               for n in notifs), notifs


def test_actor_is_not_notified_of_own_update(client):
    """Non-assignment changes don't notify the actor; only the initial assignment does."""
    _admin(client)
    proj = _mk_project(client, "Own Update Proj")
    me = client.get("/api/auth/me").json()
    bug = _mk_bug(client, proj["id"], title="Own update", assignee_ids=[me["id"]])
    client.get(f"{_NOTIFS}/read-all")  # clear the self-assignment notification
    r = client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text
    own = [n for n in client.get(_NOTIFS).json()
           if n["bug_id"] == bug["id"] and n["kind"] == "updated"]
    assert not own, own


def test_comment_notifies_reporter_not_author(client):
    _admin(client)
    proj = _mk_project(client, "Comment Proj")
    bob = _mk_user(client, "Bob", "bob2@notif.test")
    # Admin is the reporter, Bob is the assignee.
    bug = _mk_bug(client, proj["id"], title="Comment bug", assignee_ids=[bob["id"]])

    bobc = _new_client()
    _login(bobc, "bob2@notif.test")
    bobc.get(f"{_NOTIFS}/read-all")  # clear Bob's "assigned" notification so the comment is isolated
    r = bobc.post(f"/api/bugs/{bug['id']}/comments", json={"body": "<p>Looking into it.</p>"})
    assert r.status_code == 201, r.text

    # The reporter (admin) gets notified about the comment...
    admin_notifs = client.get(_NOTIFS).json()
    assert any(n["kind"] == "comment" and n["bug_id"] == bug["id"] for n in admin_notifs), admin_notifs
    # ...but the comment author (Bob) is not.
    bob_notifs = bobc.get(_NOTIFS).json()
    assert not any(n["kind"] == "comment" and n["bug_id"] == bug["id"] for n in bob_notifs), bob_notifs


def test_status_update_notifies_assignee(client):
    _admin(client)
    proj = _mk_project(client, "Update Proj")
    bob = _mk_user(client, "Bob", "bob3@notif.test")
    bug = _mk_bug(client, proj["id"], title="Update bug", assignee_ids=[bob["id"]])

    bobc = _new_client()
    _login(bobc, "bob3@notif.test")
    bobc.get(f"{_NOTIFS}/read-all")  # clear the assignment notification before the update

    r = client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text

    bob_notifs = bobc.get(_NOTIFS).json()
    assert any(n["kind"] == "updated" and n["bug_id"] == bug["id"] for n in bob_notifs), bob_notifs


def _mk_manager(admin: TestClient, name: str, email: str) -> dict:
    return _mk_user(admin, name, email, role="manager")


def _mk_event(admin: TestClient, name: str, manager_ids: list[int]) -> dict:
    r = admin.post("/api/events", json={"name": name, "manager_ids": manager_ids})
    assert r.status_code == 201, r.text
    return r.json()


def test_event_create_notifies_managers_not_actor(client):
    """Creating an event notifies its managers, but not the admin who created it."""
    _admin(client)
    meg = _mk_manager(client, "Meg", "meg@notif.test")
    ev = _mk_event(client, "Sprint Standup", [meg["id"]])

    megc = _new_client()
    _login(megc, "meg@notif.test")
    notifs = megc.get(_NOTIFS).json()
    assert any(n["kind"] == "event" and n["event_id"] == ev["id"] for n in notifs), notifs
    # The creating admin should not receive their own event notification.
    assert not any(n["event_id"] == ev["id"] for n in client.get(_NOTIFS).json())


def test_event_create_notifies_self_when_self_managed(client):
    """Self-managed event still notifies, mirroring bug self-assignment."""
    _admin(client)
    me = client.get("/api/auth/me").json()
    ev = _mk_event(client, "My Own Standup", [me["id"]])
    notifs = client.get(_NOTIFS).json()
    assert any(n["kind"] == "event" and n["event_id"] == ev["id"] for n in notifs), notifs


def test_event_update_notifies_managers(client):
    _admin(client)
    meg = _mk_manager(client, "Meg", "meg2@notif.test")
    ev = _mk_event(client, "Planning", [meg["id"]])

    megc = _new_client()
    _login(megc, "meg2@notif.test")
    megc.get(f"{_NOTIFS}/read-all")  # clear the creation notification
    r = client.put(f"/api/events/{ev['id']}", json={"name": "Planning (revised)"})
    assert r.status_code == 200, r.text

    notifs = megc.get(_NOTIFS).json()
    assert any(n["kind"] == "event" and n["event_id"] == ev["id"] for n in notifs), notifs


def test_event_delete_notifies_managers_without_dangling_fk(client):
    """Event delete notifies managers; the row survives with event_id NULL (no cascade erase)."""
    _admin(client)
    meg = _mk_manager(client, "Meg", "meg3@notif.test")
    ev = _mk_event(client, "Retro", [meg["id"]])

    megc = _new_client()
    _login(megc, "meg3@notif.test")
    megc.get(f"{_NOTIFS}/read-all")
    assert client.delete(f"/api/events/{ev['id']}").status_code == 200

    notifs = megc.get(_NOTIFS).json()
    deleted = [n for n in notifs if n["kind"] == "event" and "deleted" in n["title"].lower()]
    assert deleted, notifs
    assert deleted[0]["event_id"] is None  # nulled out after the event is removed


# Per-user isolation
def test_user_cannot_see_or_mutate_another_users_notifications(client):
    _admin(client)
    proj = _mk_project(client, "Isolation Proj")
    bob = _mk_user(client, "Bob", "bob4@notif.test")
    carol = _mk_user(client, "Carol", "carol@notif.test")  # noqa: F841 (created so she can log in)
    bug = _mk_bug(client, proj["id"], title="Bob's bug", assignee_ids=[bob["id"]])

    bobc = _new_client()
    _login(bobc, "bob4@notif.test")
    bob_notifs = bobc.get(_NOTIFS).json()
    assert bob_notifs, "Bob should have at least one notification"
    bob_notif_id = bob_notifs[0]["id"]

    carolc = _new_client()
    _login(carolc, "carol@notif.test")
    # Carol has no stake in the bug, so her list is empty.
    assert carolc.get(_NOTIFS).json() == []
    assert carolc.get(f"{_NOTIFS}/unread_count").json()["unread"] == 0
    # 404 (not 403) prevents leaking that the notification exists at all.
    assert carolc.post(f"{_NOTIFS}/{bob_notif_id}/read").status_code == 404
    assert carolc.delete(f"{_NOTIFS}/{bob_notif_id}").status_code == 404
    # Bob's notification must be untouched.
    assert bobc.get(f"{_NOTIFS}/unread_count").json()["unread"] >= 1
    _ = bug  # silence "unused" — keeps the assignment intent explicit


# Read / unread / delete lifecycle
def test_mark_read_and_unread_count(client):
    _admin(client)
    proj = _mk_project(client, "Read Proj")
    bob = _mk_user(client, "Bob", "bob5@notif.test")
    _mk_bug(client, proj["id"], assignee_ids=[bob["id"]])

    bobc = _new_client()
    _login(bobc, "bob5@notif.test")
    before = bobc.get(f"{_NOTIFS}/unread_count").json()["unread"]
    assert before >= 1
    nid = bobc.get(_NOTIFS).json()[0]["id"]

    assert bobc.post(f"{_NOTIFS}/{nid}/read").status_code == 200
    assert bobc.get(f"{_NOTIFS}/unread_count").json()["unread"] == before - 1
    # The row is still returned by the default listing, with read_at populated.
    row = next(n for n in bobc.get(_NOTIFS).json() if n["id"] == nid)
    assert row["read_at"] is not None
    # With unread_only=true it should be absent.
    assert all(n["id"] != nid for n in bobc.get(f"{_NOTIFS}?unread_only=true").json())


def test_read_all_clears_unread(client):
    _admin(client)
    proj = _mk_project(client, "ReadAll Proj")
    bob = _mk_user(client, "Bob", "bob6@notif.test")
    # Two triggers: assignment + status update, so Bob gets at least two notifications.
    b1 = _mk_bug(client, proj["id"], title="One", assignee_ids=[bob["id"]])
    client.put(f"/api/bugs/{b1['id']}", json={"status": "In Progress"})

    bobc = _new_client()
    _login(bobc, "bob6@notif.test")
    assert bobc.get(f"{_NOTIFS}/unread_count").json()["unread"] >= 2
    assert bobc.post(f"{_NOTIFS}/read-all").status_code == 200
    assert bobc.get(f"{_NOTIFS}/unread_count").json()["unread"] == 0


def test_delete_notification(client):
    _admin(client)
    proj = _mk_project(client, "Delete Proj")
    bob = _mk_user(client, "Bob", "bob7@notif.test")
    _mk_bug(client, proj["id"], assignee_ids=[bob["id"]])

    bobc = _new_client()
    _login(bobc, "bob7@notif.test")
    nid = bobc.get(_NOTIFS).json()[0]["id"]
    assert bobc.delete(f"{_NOTIFS}/{nid}").status_code == 200
    assert all(n["id"] != nid for n in bobc.get(_NOTIFS).json())
    # Second delete returns 404, not 500.
    assert bobc.delete(f"{_NOTIFS}/{nid}").status_code == 404


# Additive schema safety
def test_notifications_table_is_additive_and_idempotent(tmp_path, monkeypatch):
    """Running init_db twice must leave the notifications table intact."""
    db_file = tmp_path / "notif_schema.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "a@a.local")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", _BOOTSTRAP_PW)
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from sqlalchemy import inspect

    from app.database import engine, init_db

    init_db()
    assert "notifications" in inspect(engine).get_table_names()
    init_db()  # must not error or drop the table
    insp = inspect(engine)
    assert "notifications" in insp.get_table_names()
    idx = {i["name"] for i in insp.get_indexes("notifications")}
    assert "idx_notifications_user_read" in idx
