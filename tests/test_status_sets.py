"""Per-item-type status sets and admin-only comment/attachment edits (server-side authoritative):
status sets scoped by item type, admin-only comment edit/delete + attachment delete, and /api/meta statuses_by_type.
"""
from __future__ import annotations

import io


# Shared helpers
def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _make_user(client, name, role="user", email=None, password="User12345Aa"):
    email = email or f"{name.lower()}@x.test"
    body = {"name": name, "email": email, "role": role, "password": password}
    # Tag into every project so non-admins can see all items.
    pids = [p["id"] for p in client.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = client.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Eng"):
    r = client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, item_type="Bug", **extra):
    body = {
        "title": f"Sample {item_type}",
        "project_id": project_id,
        "item_type": item_type,
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# 1. Per-item-type status sets
def test_status_new_is_shared_by_every_type(admin_client):
    p = _make_project(admin_client)
    for itype in ("Bug", "Requirement", "Task"):
        body = {
            "title": f"{itype} test",
            "project_id": p["id"],
            "item_type": itype,
            "priority": "Medium",
            "environment": "DEV",
            "status": "New",
        }
        r = admin_client.post("/api/bugs", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "New"


def test_task_can_be_created_with_not_a_bug_status(admin_client):
    # Universal status vocabulary: Not a Bug is a canonical status, valid for Task.
    p = _make_project(admin_client)
    r = admin_client.post("/api/bugs", json={
        "title": "Task with shared status", "project_id": p["id"], "item_type": "Task",
        "priority": "Medium", "environment": "DEV", "status": "Not a Bug",
    })
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "Not a Bug"


def test_requirement_can_move_to_resolved(admin_client):
    # Universal status vocabulary: Resolved is valid for Requirement.
    p = _make_project(admin_client)
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Resolved"


def test_bug_can_use_not_a_bug_status(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Not a Bug"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Not a Bug"


def test_task_can_use_done_status(admin_client):
    p = _make_project(admin_client)
    task = _make_item(admin_client, p["id"], item_type="Task")
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "Done"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Done"


def test_requirement_can_use_approved_status(admin_client):
    p = _make_project(admin_client)
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved"


def test_changing_type_accepts_universal_status_vocabulary(admin_client):
    """A PUT changing item_type keeps the universal canonical vocabulary: any
    canonical status is valid for the new type; unknown statuses are rejected."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Not a Bug"})
    assert r.status_code == 200
    # Universal vocabulary: "Resolved" is canonical for Task too, so the flip succeeds.
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={
        "item_type": "Task", "status": "Resolved",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Resolved"
    # An unknown status is still rejected (schema validation -> 422).
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Not A Real Status"})
    assert r.status_code == 422


def test_meta_endpoint_exposes_statuses_by_type(admin_client):
    r = admin_client.get("/api/meta")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "statuses_by_type" in body
    sbt = body["statuses_by_type"]
    assert "Bug" in sbt
    assert "Requirement" in sbt
    assert "Task" in sbt
    # Universal vocabulary: every type exposes the full canonical status set.
    for itype in ("Bug", "Requirement", "Task"):
        for status in ("New", "Not a Bug", "Done", "Approved", "Resolved"):
            assert status in sbt[itype], f"{status} missing for {itype}"
    from app.schemas import CANONICAL_STATUSES
    for itype, statuses in sbt.items():
        assert sorted(statuses) == sorted(CANONICAL_STATUSES), itype

def test_comment_delete_is_admin_only(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "Mgr", role="manager")
    _login(admin_client, "mgr@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "from manager"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Manager trying to delete their own comment should be refused.
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 200


def test_comment_edit_is_admin_only(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "User1", role="user", email="user1@x.test")
    _login(admin_client, "user1@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "original body"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Author trying to edit their own comment should be refused.
    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited"})
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited by admin"})
    assert r.status_code == 200
    assert r.json()["body"] == "edited by admin"


def test_comment_creation_still_open_for_everyone(admin_client):
    """Comment creation stays open to any authenticated user; only edit/delete are admin-only."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "User2", role="user", email="user2@x.test")
    _login(admin_client, "user2@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "evidence"})
    assert r.status_code == 201


# 3. Attachments — admin-only delete, upload still open
def test_attachment_delete_is_admin_only(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    files = {"file": ("a.txt", io.BytesIO(b"contents"), "text/plain")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201
    att_id = r.json()["id"]
    # Managers were previously allowed to delete; that permission was removed.
    _make_user(admin_client, "Mgr2", role="manager", email="mgr2@x.test")
    _login(admin_client, "mgr2@x.test", "User12345Aa")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 200


def test_attachment_upload_open_post_creation(admin_client):
    """Post-creation attachment uploads stay open to all users; the admin restriction covers delete only."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "User3", role="user", email="user3@x.test")
    _login(admin_client, "user3@x.test", "User12345Aa")
    # Upload after creation.
    files = {"file": ("evidence.txt", io.BytesIO(b"after creation"), "text/plain")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201
    assert r.json()["uploader_name"] == "User3"


def test_comment_attachment_delete_is_admin_only(admin_client):
    """Comment attachments share the DELETE endpoint and the same admin-only rule as bug attachments."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "User4", role="user", email="user4@x.test")
    _login(admin_client, "user4@x.test", "User12345Aa")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "see file"})
    cid = r.json()["id"]
    files = {"file": ("log.txt", io.BytesIO(b"stuff"), "text/plain")}
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files=files,
        data={"comment_id": str(cid)},
    )
    assert r.status_code == 201
    att_id = r.json()["id"]
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 403
    _login(admin_client, "admin@test.local", "Admin1234")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 200


def test_audit_log_records_admin_comment_actions(admin_client):
    """Admin edits and deletes of comments must appear in the audit log."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "initial"})
    cid = r.json()["id"]
    r = admin_client.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited"})
    assert r.status_code == 200
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 200
    r = admin_client.get("/api/audit?entity_type=comment&limit=50")
    assert r.status_code == 200
    actions = {row["action"] for row in r.json()}
    assert "comment_edited" in actions
    assert "comment_deleted" in actions
