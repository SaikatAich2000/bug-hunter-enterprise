"""Tests for POST /api/bugs/bulk.
Covers each action, tallies, per-type status validity, permission skips, validation, notifications.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

_PW = "User12345"
_BULK = "/api/bugs/bulk"
_NOTIFS = "/api/notifications"


def _new_client() -> TestClient:
    from app.main import app
    return TestClient(app)


def _login(c: TestClient, email: str, password: str = _PW) -> None:
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _mk_user(admin: TestClient, name: str, email: str, role: str = "user") -> dict:
    body = {"name": name, "email": email, "role": role, "password": _PW}
    # Tag the user to every project; per-type rules only apply after visibility.
    pids = [p["id"] for p in admin.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = admin.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_project(admin: TestClient, name: str = "Bulk Proj") -> dict:
    r = admin.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(admin: TestClient, project_id: int, title: str, **extra) -> dict:
    body = {"project_id": project_id, "title": title}
    body.update(extra)
    r = admin.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# Auth + validation
def test_bulk_requires_auth(client):
    assert client.post(_BULK, json={"action": "set_status", "ids": [1], "value": "Closed"}).status_code == 401


def test_bulk_invalid_action_422(admin_client):
    proj = _mk_project(admin_client)
    bug = _mk_bug(admin_client, proj["id"], "item x here")
    assert admin_client.post(_BULK, json={"action": "explode", "ids": [bug["id"]]}).status_code == 422


def test_bulk_empty_ids_422(admin_client):
    assert admin_client.post(_BULK, json={"action": "set_priority", "ids": [], "value": "High"}).status_code == 422


def test_bulk_invalid_value_400(admin_client):
    proj = _mk_project(admin_client, "BadVal")
    bug = _mk_bug(admin_client, proj["id"], "item x here")
    assert admin_client.post(_BULK, json={"action": "set_priority", "ids": [bug["id"]], "value": "Wat"}).status_code == 400


# set_* actions
def test_bulk_set_priority(admin_client):
    proj = _mk_project(admin_client, "SetPrio")
    ids = [_mk_bug(admin_client, proj["id"], f"bug {i} here")["id"] for i in range(3)]
    r = admin_client.post(_BULK, json={"action": "set_priority", "ids": ids, "value": "Critical"})
    assert r.status_code == 200
    assert r.json()["updated"] == 3
    for bid in ids:
        assert admin_client.get(f"/api/bugs/{bid}").json()["priority"] == "Critical"


def test_bulk_set_environment(admin_client):
    proj = _mk_project(admin_client, "SetEnv")
    ids = [_mk_bug(admin_client, proj["id"], f"env {i} item")["id"] for i in range(2)]
    r = admin_client.post(_BULK, json={"action": "set_environment", "ids": ids, "value": "PROD"})
    assert r.json()["updated"] == 2


def test_bulk_set_status_universal_vocabulary_updates_all_types(admin_client):
    proj = _mk_project(admin_client, "SetStatus")
    bug = _mk_bug(admin_client, proj["id"], "a bug")           # Bug
    task = _mk_bug(admin_client, proj["id"], "a task", item_type="Task")  # Task
    # Universal status vocabulary: "Resolved" is a canonical status valid for
    # every shared work-item type, so both items are updated (no per-type skip).
    r = admin_client.post(_BULK, json={"action": "set_status", "ids": [bug["id"], task["id"]], "value": "Resolved"})
    body = r.json()
    assert body["updated"] == 2
    assert body["skipped"] == 0


def test_bulk_set_status_noop_is_skipped(admin_client):
    proj = _mk_project(admin_client, "Noop")
    bug = _mk_bug(admin_client, proj["id"], "already new")  # status defaults to New
    r = admin_client.post(_BULK, json={"action": "set_status", "ids": [bug["id"]], "value": "New"})
    assert r.json()["skipped"] == 1
    assert r.json()["updated"] == 0


def test_bulk_set_field_noop_is_skipped(admin_client):
    # Same-value set hits the generic no-op path (distinct from the status one).
    proj = _mk_project(admin_client, "FieldNoop")
    bug = _mk_bug(admin_client, proj["id"], "already high", priority="High")
    r = admin_client.post(_BULK, json={"action": "set_priority", "ids": [bug["id"]], "value": "High"})
    assert r.json()["skipped"] == 1
    assert r.json()["updated"] == 0


def test_bulk_not_found_counts_as_failed(admin_client):
    proj = _mk_project(admin_client, "NotFound")
    bug = _mk_bug(admin_client, proj["id"], "real")
    r = admin_client.post(_BULK, json={"action": "set_priority", "ids": [bug["id"], 99999], "value": "Low"})
    body = r.json()
    assert body["updated"] == 1
    assert body["failed"] == 1
    assert "not found" in body["message"]


# Permissions
def test_bulk_skips_items_the_user_cannot_edit(admin_client):
    proj = _mk_project(admin_client, "PermSkip")
    _mk_user(admin_client, "Reg", "reg.bulk@test.local")
    bug = _mk_bug(admin_client, proj["id"], "editable bug")
    task = _mk_bug(admin_client, proj["id"], "locked task", item_type="Task")
    userc = _new_client()
    _login(userc, "reg.bulk@test.local")
    # The user can edit Bugs but not Tasks, so the Task is skipped.
    r = userc.post(_BULK, json={"action": "set_priority", "ids": [bug["id"], task["id"]], "value": "High"})
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped"] == 1


def test_bulk_delete_is_admin_only(admin_client):
    proj = _mk_project(admin_client, "BulkDelete")
    _mk_user(admin_client, "Reg", "reg.del@test.local")
    ids = [_mk_bug(admin_client, proj["id"], f"del {i} item")["id"] for i in range(2)]
    userc = _new_client()
    _login(userc, "reg.del@test.local")
    # Regular user: all skipped, items survive.
    r = userc.post(_BULK, json={"action": "delete", "ids": ids})
    assert r.json()["skipped"] == 2
    assert admin_client.get(f"/api/bugs/{ids[0]}").status_code == 200
    r = admin_client.post(_BULK, json={"action": "delete", "ids": ids})
    assert r.json()["updated"] == 2
    assert admin_client.get(f"/api/bugs/{ids[0]}").status_code == 404


# Notifications
def test_bulk_status_change_notifies_assignee(admin_client):
    proj = _mk_project(admin_client, "BulkNotify")
    bob = _mk_user(admin_client, "Bob", "bob.bulk@test.local")
    bug = _mk_bug(admin_client, proj["id"], "assigned", assignee_ids=[bob["id"]])
    bobc = _new_client()
    _login(bobc, "bob.bulk@test.local")
    bobc.post(f"{_NOTIFS}/read-all")
    admin_client.post(_BULK, json={"action": "set_status", "ids": [bug["id"]], "value": "In Progress"})
    notifs = bobc.get(_NOTIFS).json()
    assert any(n["kind"] == "updated" and n["bug_id"] == bug["id"] for n in notifs), notifs
