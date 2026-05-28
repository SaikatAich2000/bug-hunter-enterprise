"""Regression tests for the v2.4 enterprise feature port:

  1. Item types — Bug / Requirement / Task share a numbering sequence.
  2. Type-aware edit permissions — members can edit Bugs only; tasks
     and requirements are admin/manager-only. Backend 403 mirrors the
     SPA's read-only mode for restricted users.
  3. Events — CRUD scoped to org. Manager assignment validated against
     same-org + admin/manager role.
  4. Event delete is admin-only; edit/create are admin/manager.
  5. Task created inside an event does NOT email event managers — only
     its own assignees (per the OSS spec).
  6. Tab-aware stats: `?item_type=` scopes KPIs while by_type stays
     global.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PASS = "TestPass1!"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _signup(client, org="Acme", name="Alice", email="alice@acme.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Eng", key=None):
    body = {"name": name, "color": "#c9764f"}
    if key:
        body["key"] = key
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, item_type="Bug", **extra):
    body = {
        "title": f"Sample {item_type}-x",
        "project_id": project_id,
        "item_type": item_type,
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    return client.post("/api/bugs", json=body)


def _invite_and_join(client, make_invite, email, role="member", as_lead=False,
                     project_ids=None):
    tok = make_invite(client, email, role=role, project_ids=project_ids or [],
                      as_lead=as_lead)
    from fastapi.testclient import TestClient
    from app.main import app
    nc = TestClient(app)
    r = nc.post("/api/invitations/accept", json={
        "token": tok, "name": email.split("@")[0].capitalize(), "password": PASS,
    })
    assert r.status_code == 200, r.text
    return nc


# ---------------------------------------------------------------------------
# 1. Item types share numbering
# ---------------------------------------------------------------------------
def test_item_types_share_numbering_sequence(client):
    _signup(client)
    p = _make_project(client)
    r1 = _make_item(client, p["id"], item_type="Bug", title="First bug ever")
    assert r1.status_code == 201
    bug = r1.json()
    r2 = _make_item(client, p["id"], item_type="Task", title="Second item is task")
    assert r2.status_code == 201
    task = r2.json()
    r3 = _make_item(client, p["id"], item_type="Requirement", title="Third item is req")
    assert r3.status_code == 201
    req = r3.json()
    # Single numbering sequence — task = bug + 1, req = task + 1.
    assert task["id"] == bug["id"] + 1
    assert req["id"] == task["id"] + 1
    # Each carries the right type word.
    assert bug["item_type"] == "Bug"
    assert task["item_type"] == "Task"
    assert req["item_type"] == "Requirement"


def test_default_item_type_is_bug(client):
    """Existing payloads that don't send item_type get Bug for free."""
    _signup(client)
    p = _make_project(client)
    r = client.post("/api/bugs", json={
        "title": "Legacy create no item_type",
        "project_id": p["id"],
    })
    assert r.status_code == 201
    assert r.json()["item_type"] == "Bug"


# ---------------------------------------------------------------------------
# 2. Type-aware edit permissions
# ---------------------------------------------------------------------------
def test_member_can_edit_bug_but_not_task_or_requirement(client, make_invite):
    _signup(client)
    p = _make_project(client)
    bug  = _make_item(client, p["id"], item_type="Bug").json()
    task = _make_item(client, p["id"], item_type="Task").json()
    req  = _make_item(client, p["id"], item_type="Requirement").json()
    mc = _invite_and_join(client, make_invite, "member@a.test", role="member",
                          project_ids=[p["id"]])
    # Bug: editable.
    r = mc.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text
    # Task: forbidden.
    r = mc.put(f"/api/bugs/{task['id']}", json={"status": "In Progress"})
    assert r.status_code == 403
    assert "task" in r.json()["detail"].lower()
    # Requirement: forbidden.
    r = mc.put(f"/api/bugs/{req['id']}", json={"status": "In Progress"})
    assert r.status_code == 403
    assert "requirement" in r.json()["detail"].lower()


def test_member_cannot_create_task_or_requirement(client, make_invite):
    _signup(client)
    p = _make_project(client)
    mc = _invite_and_join(client, make_invite, "member2@a.test", role="member",
                          project_ids=[p["id"]])
    # Bug: allowed.
    r = mc.post("/api/bugs", json={
        "title": "Member bug filing", "project_id": p["id"], "item_type": "Bug",
    })
    assert r.status_code == 201, r.text
    # Task: forbidden.
    r = mc.post("/api/bugs", json={
        "title": "Member task filing", "project_id": p["id"], "item_type": "Task",
    })
    assert r.status_code == 403
    # Requirement: forbidden.
    r = mc.post("/api/bugs", json={
        "title": "Member req filing", "project_id": p["id"], "item_type": "Requirement",
    })
    assert r.status_code == 403


def test_manager_can_edit_task_and_requirement(client, make_invite):
    _signup(client)
    p = _make_project(client)
    task = _make_item(client, p["id"], item_type="Task").json()
    req  = _make_item(client, p["id"], item_type="Requirement").json()
    mc = _invite_and_join(client, make_invite, "mgr@a.test", role="manager",
                          project_ids=[p["id"]])
    r = mc.put(f"/api/bugs/{task['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text
    r = mc.put(f"/api/bugs/{req['id']}", json={"status": "Resolved"})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 3. Events CRUD + org scoping
# ---------------------------------------------------------------------------
def test_admin_can_create_edit_delete_event(client):
    _signup(client)
    r = client.post("/api/events", json={"name": "Daily standup"})
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["name"] == "Daily standup"
    assert ev["managers"] == []
    # Edit.
    r = client.put(f"/api/events/{ev['id']}", json={"name": "Daily standup (renamed)"})
    assert r.status_code == 200
    assert r.json()["name"] == "Daily standup (renamed)"
    # Delete.
    r = client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200
    # Listing it again returns nothing.
    assert client.get("/api/events").json() == []


def test_member_cannot_create_event(client, make_invite):
    _signup(client)
    p = _make_project(client)
    mc = _invite_and_join(client, make_invite, "ev_member@a.test", role="member",
                          project_ids=[p["id"]])
    r = mc.post("/api/events", json={"name": "should-fail"})
    assert r.status_code == 403


def test_manager_can_create_but_not_delete_event(client, make_invite):
    _signup(client)
    p = _make_project(client)
    mc = _invite_and_join(client, make_invite, "ev_mgr@a.test", role="manager",
                          project_ids=[p["id"]])
    r = mc.post("/api/events", json={"name": "Sprint planning"})
    assert r.status_code == 201
    ev = r.json()
    # Manager edit allowed.
    r = mc.put(f"/api/events/{ev['id']}", json={"name": "Sprint planning v2"})
    assert r.status_code == 200
    # Manager delete forbidden.
    r = mc.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 403


def test_event_managers_must_be_admin_or_manager(client, make_invite):
    me = _signup(client)
    p = _make_project(client)
    member_client = _invite_and_join(client, make_invite, "regular@a.test",
                                      role="member", project_ids=[p["id"]])
    # Get the member's user id via /api/auth/me.
    member_id = member_client.get("/api/auth/me").json()["id"]
    # Admin tries to add the member as event manager — should 400.
    r = client.post("/api/events", json={
        "name": "bad-managers", "manager_ids": [member_id],
    })
    assert r.status_code == 400, r.text
    assert "manager" in r.json()["detail"].lower()


def test_events_org_scoped(client):
    """Events are invisible to other orgs."""
    from fastapi.testclient import TestClient
    from app.main import app
    a = client
    _signup(a, org="OrgA", name="Alice", email="alice@a.test")
    a.post("/api/events", json={"name": "OrgA event"})
    # New independent client → new signup → new org.
    b = TestClient(app)
    _signup(b, org="OrgB", name="Bob", email="bob@b.test")
    assert b.get("/api/events").json() == []
    # And the OrgA event can't be opened by OrgB.
    a_ev = a.get("/api/events").json()
    assert len(a_ev) == 1
    a_ev_id = a_ev[0]["id"]
    r = b.get(f"/api/events/{a_ev_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. Task created inside an event does NOT email event managers
# ---------------------------------------------------------------------------
def test_task_inside_event_does_not_email_event_managers(client, make_invite, monkeypatch):
    _signup(client)
    p = _make_project(client)
    # Invite a manager — they can be an event manager.
    mc = _invite_and_join(client, make_invite, "evmgr@a.test", role="manager",
                          project_ids=[p["id"]])
    mgr_id = mc.get("/api/auth/me").json()["id"]
    # Also invite a regular worker to assign tasks to.
    wc = _invite_and_join(client, make_invite, "worker@a.test", role="member",
                          project_ids=[p["id"]])
    worker_id = wc.get("/api/auth/me").json()["id"]

    # Capture every email delivered after we set up.
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body: sent.append((subject, sorted(to), body)),
    )
    # Create the event WITH the manager assigned.
    ev = client.post("/api/events", json={
        "name": "Standup", "manager_ids": [mgr_id],
    }).json()
    sent.clear()
    # File a Task inside that event, assigned to the worker only.
    r = client.post("/api/bugs", json={
        "title": "Do the thing task",
        "project_id": p["id"],
        "item_type": "Task",
        "event_id": ev["id"],
        "assignee_ids": [worker_id],
    })
    assert r.status_code == 201, r.text

    all_addresses = {addr for _, to, _ in sent for addr in to}
    assert "worker@a.test" in all_addresses, \
        "Assignee should be notified via per-task email channel"
    assert "evmgr@a.test" not in all_addresses, \
        "Event manager must NOT be cc'd on task-created emails"
    # Sanity: at least one subject mentions 'task' (type-aware subjects).
    assert any("task" in s.lower() for s, _, _ in sent)


# ---------------------------------------------------------------------------
# 5. Event delete preserves items
# ---------------------------------------------------------------------------
def test_event_delete_preserves_items(client):
    _signup(client)
    p = _make_project(client)
    ev = client.post("/api/events", json={"name": "Delete-me"}).json()
    task = client.post("/api/bugs", json={
        "title": "Survives the event delete",
        "project_id": p["id"], "item_type": "Task", "event_id": ev["id"],
    }).json()
    # Confirm linked.
    assert task["event_id"] == ev["id"]
    # Delete event.
    r = client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200
    # Task still exists but with event_id NULL.
    after = client.get(f"/api/bugs/{task['id']}").json()
    assert after["event_id"] is None
    assert after["title"] == "Survives the event delete"


# ---------------------------------------------------------------------------
# 6. Tab-aware stats endpoint
# ---------------------------------------------------------------------------
def test_stats_global_includes_by_type(client):
    _signup(client)
    p = _make_project(client)
    for i in range(3):
        _make_item(client, p["id"], item_type="Bug", title=f"Bug-{i}-here")
    _make_item(client, p["id"], item_type="Requirement", title="Req-A here")
    for i in range(2):
        _make_item(client, p["id"], item_type="Task", title=f"Task-{i}-here")
    # Also one event so by_type includes the Event key.
    client.post("/api/events", json={"name": "An event"})

    s = client.get("/api/stats").json()
    assert s["bugs"] == 6     # all non-excluded statuses across types
    assert s["by_type"]["Bug"] == 3
    assert s["by_type"]["Requirement"] == 1
    assert s["by_type"]["Task"] == 2
    assert s["by_type"]["Event"] == 1


def test_stats_filtered_by_item_type(client):
    _signup(client)
    p = _make_project(client)
    for i in range(3):
        _make_item(client, p["id"], item_type="Bug", priority="High", title=f"Bg-{i}-1")
    _make_item(client, p["id"], item_type="Task", priority="Low", title="Tk-0-1")

    bug_s = client.get("/api/stats?item_type=Bug").json()
    assert bug_s["bugs"] == 3
    assert bug_s["by_priority"].get("High") == 3
    assert "Low" not in bug_s["by_priority"]
    # by_type stays GLOBAL.
    assert bug_s["by_type"]["Bug"] == 3
    assert bug_s["by_type"]["Task"] == 1

    task_s = client.get("/api/stats?item_type=Task").json()
    assert task_s["bugs"] == 1
    assert task_s["by_priority"].get("Low") == 1


def test_stats_rejects_unknown_item_type(client):
    _signup(client)
    r = client.get("/api/stats?item_type=Bogus")
    assert r.status_code == 400
    assert "item_type" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 7. Audit trail — type-aware delete message
# ---------------------------------------------------------------------------
def test_audit_delete_uses_item_type_word(client):
    _signup(client)
    p = _make_project(client)
    task = _make_item(client, p["id"], item_type="Task",
                      title="Doomed task title").json()
    client.delete(f"/api/bugs/{task['id']}")
    rows = client.get("/api/audit").json()
    deleted = [r for r in rows if r["action"] == "bug_deleted" and r["entity_id"] == task["id"]]
    assert deleted, rows
    assert "task" in deleted[0]["detail"].lower(), deleted[0]
