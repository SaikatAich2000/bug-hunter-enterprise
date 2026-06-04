"""Regression tests for the v2.5 + v2.6 changes — enterprise port.

  1. Per-item-type status sets — "Not a Bug" / "Resolved" only on Bug;
     "Approved" / "Implemented" only on Requirement; "Done" / "Blocked"
     / "Cancelled" only on Task; "New" shared by every type.
  2. Comments — edit + delete admin-only (creation still open).
  3. Attachments — delete admin-only (upload still open to anyone
     who can edit the bug, including post-creation).
  4. /api/meta exposes statuses_by_type for the frontend.

These behaviors are enforced server-side (the SPA mirrors them but the
authoritative check is here). The enterprise port differs from the
internal OSS suite in three ways:
  - users are created via /api/invitations (multi-tenant) rather than
    /api/users; each role gets its own TestClient.
  - DELETE /api/bugs/{id}/comments/{cid} returns 204 (not 200).
  - DELETE /api/bugs/{id}/attachments/{aid} still returns 200 with a
    body that contains a 'message' field.
All endpoints are org-scoped via the same admin_client's organization.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PASS = "TestPass1!"


# ---------------------------------------------------------------------------
# Helpers — match the v2.4 style: one admin via /api/auth/signup, role
# users via /api/invitations + /api/invitations/accept on a fresh client.
# ---------------------------------------------------------------------------
def _signup(client, org="Acme", name="Alice", email="alice@acme.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Eng"):
    r = client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, item_type="Bug", **extra):
    body = {
        "title": f"Sample {item_type} v25",
        "project_id": project_id,
        "item_type": item_type,
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _invite_and_join(admin_client, make_invite, email, role="member",
                     project_ids=None, name=None):
    """Mirror of the v2.4 helper: create an invite via admin_client,
    accept it on a brand-new TestClient so the new user has their own
    cookie jar (multi-tenant tests rely on this isolation)."""
    tok = make_invite(admin_client, email, role=role,
                      project_ids=project_ids or [])
    from fastapi.testclient import TestClient
    from app.main import app
    nc = TestClient(app)
    display = name or email.split("@")[0].capitalize()
    r = nc.post("/api/invitations/accept", json={
        "token": tok, "name": display, "password": PASS,
    })
    assert r.status_code == 200, r.text
    return nc


# ---------------------------------------------------------------------------
# 1. Per-item-type status sets
# ---------------------------------------------------------------------------
def test_status_new_is_shared_by_every_type(client):
    _signup(client)
    p = _make_project(client)
    for itype in ("Bug", "Requirement", "Task"):
        body = {
            "title": f"{itype} test shared-new",
            "project_id": p["id"],
            "item_type": itype,
            "priority": "Medium",
            "environment": "DEV",
            "status": "New",
        }
        r = client.post("/api/bugs", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "New"
        assert r.json()["item_type"] == itype


def test_task_cannot_be_created_with_not_a_bug_status(client):
    _signup(client)
    p = _make_project(client)
    r = client.post("/api/bugs", json={
        "title": "Bad task with Not a Bug",
        "project_id": p["id"], "item_type": "Task",
        "priority": "Medium", "environment": "DEV",
        "status": "Not a Bug",
    })
    assert r.status_code == 422, r.text
    body = r.json()
    msgs = [d.get("msg", "") for d in body.get("detail", [])]
    joined = " ".join(msgs).lower()
    # The pydantic error must call out the type AND the bad status.
    assert "task" in joined, f"expected 'task' in error: {joined}"
    assert "not a bug" in joined, f"expected 'not a bug' in error: {joined}"


def test_requirement_cannot_move_to_resolved(client):
    _signup(client)
    p = _make_project(client)
    req = _make_item(client, p["id"], item_type="Requirement")
    r = client.put(f"/api/bugs/{req['id']}", json={"status": "Resolved"})
    assert r.status_code == 400, r.text
    assert "requirement" in r.json()["detail"].lower()


def test_bug_can_use_not_a_bug_status(client):
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    r = client.put(f"/api/bugs/{bug['id']}", json={"status": "Not a Bug"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Not a Bug"


def test_task_can_use_done_status(client):
    _signup(client)
    p = _make_project(client)
    task = _make_item(client, p["id"], item_type="Task")
    r = client.put(f"/api/bugs/{task['id']}", json={"status": "Done"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Done"


def test_requirement_can_use_approved_status(client):
    _signup(client)
    p = _make_project(client)
    req = _make_item(client, p["id"], item_type="Requirement")
    r = client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved"


def test_changing_type_validates_status_against_new_type(client):
    """If a bug is currently 'Not a Bug' and we try to flip it to a Task
    in the same PUT, the request must fail — Task can't carry that
    status. (Flipping the type WITHOUT changing the status is a quiet
    bug-data scenario we tolerate; this asserts the explicit reject
    path.)"""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    r = client.put(f"/api/bugs/{bug['id']}", json={"status": "Not a Bug"})
    assert r.status_code == 200
    # Now try to flip the type to Task while resetting status to
    # something invalid for Task → reject.
    r = client.put(f"/api/bugs/{bug['id']}", json={
        "item_type": "Task", "status": "Resolved",
    })
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert "task" in detail or "resolved" in detail, detail


def test_meta_endpoint_exposes_statuses_by_type(client):
    _signup(client)
    r = client.get("/api/meta")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "statuses_by_type" in body
    sbt = body["statuses_by_type"]
    assert "Bug" in sbt and "Requirement" in sbt and "Task" in sbt
    # Spec invariants.
    assert "New" in sbt["Bug"]
    assert "New" in sbt["Requirement"]
    assert "New" in sbt["Task"]
    assert "Not a Bug" in sbt["Bug"]
    assert "Not a Bug" not in sbt["Task"]
    assert "Not a Bug" not in sbt["Requirement"]
    assert "Done" in sbt["Task"]
    assert "Done" not in sbt["Bug"]
    assert "Approved" in sbt["Requirement"]
    assert "Approved" not in sbt["Task"]


# ---------------------------------------------------------------------------
# 2. Comments — admin-only edit + delete
# ---------------------------------------------------------------------------
def test_comment_delete_is_admin_only(client, make_invite):
    """Manager posts a comment, then tries to delete it — must 403.
    Admin (the original signup user) can delete and gets 204."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    # Manager joins the org with access to this project, files a comment.
    mc = _invite_and_join(client, make_invite, "mgr@a.test",
                          role="manager", project_ids=[p["id"]],
                          name="Mgr")
    r = mc.post(f"/api/bugs/{bug['id']}/comments", json={"body": "from manager"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Manager tries to delete their own comment → 403.
    r = mc.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 403, r.text
    assert "admin" in r.json()["detail"].lower()
    # Comment still exists.
    listing = client.get(f"/api/bugs/{bug['id']}/comments").json()
    assert any(c["id"] == cid for c in listing), \
        "manager delete must not have removed the comment"
    # Admin (original signup client) deletes — 204 (FastAPI No Content).
    r = client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 204, r.text
    # And it really is gone now.
    listing = client.get(f"/api/bugs/{bug['id']}/comments").json()
    assert not any(c["id"] == cid for c in listing), \
        "admin delete should have removed the comment"


def test_comment_edit_is_admin_only(client, make_invite):
    """Regular user files a comment, tries to edit their own → 403.
    Admin edits → 200 and the body actually changes in the listing."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    uc = _invite_and_join(client, make_invite, "user1@a.test",
                          role="member", project_ids=[p["id"]],
                          name="User1")
    r = uc.post(f"/api/bugs/{bug['id']}/comments", json={"body": "original body"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Author edit → 403.
    r = uc.put(f"/api/bugs/{bug['id']}/comments/{cid}", json={"body": "edited"})
    assert r.status_code == 403, r.text
    assert "admin" in r.json()["detail"].lower()
    # Body still the original on read-back.
    listing = client.get(f"/api/bugs/{bug['id']}/comments").json()
    row = next(c for c in listing if c["id"] == cid)
    assert row["body"] == "original body"
    # Admin edits and the response body reflects the new value.
    r = client.put(f"/api/bugs/{bug['id']}/comments/{cid}",
                   json={"body": "edited by admin"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "edited by admin"
    # And the listing now shows the new body too (round-trip persisted).
    listing = client.get(f"/api/bugs/{bug['id']}/comments").json()
    row = next(c for c in listing if c["id"] == cid)
    assert row["body"] == "edited by admin"


def test_comment_creation_still_open_for_everyone(client, make_invite):
    """v2.5 only restricts edit + delete — creating comments must still
    work for anyone authenticated, otherwise users can't add evidence."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    uc = _invite_and_join(client, make_invite, "user2@a.test",
                          role="member", project_ids=[p["id"]],
                          name="User2")
    r = uc.post(f"/api/bugs/{bug['id']}/comments", json={"body": "evidence"})
    assert r.status_code == 201, r.text
    # Comment came through with the member's name.
    assert r.json()["body"] == "evidence"
    assert r.json()["author_name"] == "User2"


# ---------------------------------------------------------------------------
# 3. Attachments — admin-only delete, upload still open
# ---------------------------------------------------------------------------
def test_attachment_delete_is_admin_only(client, make_invite):
    """Manager (used to be allowed in pre-v2.5) is now 403 on attachment
    delete; admin still gets 200."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    # Admin uploads the attachment.
    files = {"file": ("a.txt", io.BytesIO(b"contents"), "text/plain")}
    r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201, r.text
    att_id = r.json()["id"]
    # Manager joins, tries to delete → 403.
    mc = _invite_and_join(client, make_invite, "mgr2@a.test",
                          role="manager", project_ids=[p["id"]],
                          name="Mgr2")
    r = mc.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 403, r.text
    assert "admin" in r.json()["detail"].lower()
    # Admin can. Enterprise delete_attachment returns 200 with a message body.
    r = client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 200, r.text
    assert "message" in r.json()


def test_attachment_upload_open_post_creation(client, make_invite):
    """Users can still attach evidence after a bug is filed — the v2.5
    restriction only applies to delete."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    uc = _invite_and_join(client, make_invite, "user3@a.test",
                          role="member", project_ids=[p["id"]],
                          name="User3")
    # Bug-level upload after the bug exists.
    files = {"file": ("evidence.txt", io.BytesIO(b"after creation"), "text/plain")}
    r = uc.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 201, r.text
    assert r.json()["uploader_name"] == "User3"
    assert r.json()["filename"] == "evidence.txt"


def test_comment_attachment_delete_is_admin_only(client, make_invite):
    """Attachments on a comment also drop into the admin-only delete
    rule (the route is the same DELETE /attachments/{id} endpoint)."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    # User adds a comment and a comment-scoped attachment.
    uc = _invite_and_join(client, make_invite, "user4@a.test",
                          role="member", project_ids=[p["id"]],
                          name="User4")
    r = uc.post(f"/api/bugs/{bug['id']}/comments", json={"body": "see file"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    files = {"file": ("log.txt", io.BytesIO(b"stuff"), "text/plain")}
    r = uc.post(
        f"/api/bugs/{bug['id']}/attachments",
        files=files,
        data={"comment_id": str(cid)},
    )
    assert r.status_code == 201, r.text
    att_id = r.json()["id"]
    # Author CAN'T delete their own attachment.
    r = uc.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 403, r.text
    assert "admin" in r.json()["detail"].lower()
    # Admin can.
    r = client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
    assert r.status_code == 200, r.text


def test_audit_log_records_admin_comment_actions(client):
    """Admin edit + delete of comments should be auditable.

    Uses the dedicated /api/audit?entity_type=comment&limit=50 endpoint
    so we don't have to walk the full activity log."""
    _signup(client)
    p = _make_project(client)
    bug = _make_item(client, p["id"], item_type="Bug")
    r = client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "initial"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = client.put(f"/api/bugs/{bug['id']}/comments/{cid}",
                   json={"body": "edited"})
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
    assert r.status_code == 204, r.text
    # Audit feed scoped to entity_type=comment should contain both rows.
    r = client.get("/api/audit?entity_type=comment&limit=50")
    assert r.status_code == 200, r.text
    rows = r.json()
    actions = {row["action"] for row in rows}
    assert "comment_edited" in actions, f"missing comment_edited: {actions}"
    assert "comment_deleted" in actions, f"missing comment_deleted: {actions}"
    # The rows for our comment carry the correct entity_id pointer.
    comment_rows = [r for r in rows if r.get("entity_id") == cid]
    assert any(r["action"] == "comment_edited" for r in comment_rows), \
        comment_rows
    assert any(r["action"] == "comment_deleted" for r in comment_rows), \
        comment_rows
