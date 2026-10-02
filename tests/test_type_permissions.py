"""Type-based permissions and event-manager notifications: type-restricted edits/deletes per role,
event-manager email delivery, manager-role validation, admin-only event deletion, and tab-aware /api/stats.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _make_user(client, name, role="user", email=None):
    email = email or f"{name.lower()}@x.test"
    body = {"name": name, "email": email, "role": role, "password": "User12345Aa"}
    # Tag into every project so the type check (403) is reached, not a scope 404.
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


def test_user_can_edit_bug_but_not_task_or_requirement(admin_client):
    # Admin creates the three flavors of items under their own session.
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    task = _make_item(admin_client, p["id"], item_type="Task")
    _make_user(admin_client, "Regular", role="user")
    _login(admin_client, "regular@x.test", "User12345Aa")

    # Bugs are editable by all roles.
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text

    # Requirements and tasks are manager/admin only.
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "In Progress"})
    assert r.status_code == 403, r.text
    assert "requirement" in r.json()["detail"].lower()

    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "In Progress"})
    assert r.status_code == 403, r.text
    assert "task" in r.json()["detail"].lower()


def test_user_cannot_convert_bug_to_task_via_item_type_overpost(admin_client):
    """Mass-assignment guard: a user editing a Bug can't reclassify it to Task/Requirement by over-posting item_type (403, row unchanged)."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    _make_user(admin_client, "Regular", role="user")
    _login(admin_client, "regular@x.test", "User12345Aa")

    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"item_type": "Task"})
    assert r.status_code == 403, r.text
    assert "convert" in r.json()["detail"].lower()

    # Guard must reject before any write, so the row stays unchanged.
    got = admin_client.get(f"/api/bugs/{bug['id']}")
    assert got.status_code == 200, got.text
    assert got.json()["item_type"] == "Bug"


def test_manager_can_edit_task_and_requirement(admin_client):
    p = _make_project(admin_client)
    task = _make_item(admin_client, p["id"], item_type="Task")
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    _make_user(admin_client, "Mgr", role="manager")
    _login(admin_client, "mgr@x.test", "User12345Aa")
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "In Progress"})
    assert r.status_code == 200, r.text
    # Requirements use "Approved" rather than bug-flavored "Resolved".
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200, r.text


def test_manager_cannot_delete_anything(admin_client):
    """Managers can edit, never delete — every item type AND events."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    req = _make_item(admin_client, p["id"], item_type="Requirement")
    ev = admin_client.post("/api/events", json={
        "name": "for-delete-test",
    }).json()
    _make_user(admin_client, "Mgr2", role="manager")
    _login(admin_client, "mgr2@x.test", "User12345Aa")

    for bid in (bug["id"], task["id"], req["id"]):
        r = admin_client.delete(f"/api/bugs/{bid}")
        assert r.status_code == 403, f"Manager should NOT be able to delete bug #{bid}, got {r.status_code}"
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 403, "Manager should not be able to delete events"


def test_admin_can_delete_everything(admin_client):
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    ev = admin_client.post("/api/events", json={"name": "delete-me"}).json()
    for bid in (bug["id"], task["id"]):
        r = admin_client.delete(f"/api/bugs/{bid}")
        assert r.status_code == 200, r.text
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200, r.text


# Event managers — emails, role validation.
def test_event_create_emails_only_managers(admin_client, monkeypatch):
    """Creating an event emails its managers only — not item assignees or random users."""
    m1 = _make_user(admin_client, "Mgr-a", role="manager", email="mgra@x.test")
    m2 = _make_user(admin_client, "Mgr-b", role="manager", email="mgrb@x.test")
    _make_user(admin_client, "Worker", role="user", email="worker@x.test")

    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body, from_address=None: sent.append((subject, sorted(to), body)),
    )

    sent.clear()
    ev = admin_client.post("/api/events", json={
        "name": "Sprint kickoff",
        "manager_ids": [m1["id"], m2["id"]],
    }).json()
    # Admin is the actor, so only the two managers are recipients.
    assert sent, "Expected an event-created email"
    subj, to, body = sent[-1]
    assert "New event" in subj
    assert "Sprint kickoff" in subj
    assert to == ["mgra@x.test", "mgrb@x.test"], to
    assert "Managers:" in body
    assert ev["managers"]
    assert len(ev["managers"]) == 2


def test_event_update_emails_managers(admin_client, monkeypatch):
    mgr = _make_user(admin_client, "Mgr3", role="manager", email="m3@x.test")
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body, from_address=None: sent.append((subject, sorted(to), body)),
    )
    ev = admin_client.post("/api/events", json={
        "name": "Daily", "manager_ids": [mgr["id"]],
    }).json()
    sent.clear()
    r = admin_client.put(f"/api/events/{ev['id']}", json={"name": "Daily (renamed)"})
    assert r.status_code == 200
    assert sent, "Expected an event-updated email"
    subj, to, body = sent[-1]
    assert "updated" in subj.lower()
    assert "m3@x.test" in to
    assert "name" in body  # change line is present


def test_event_delete_emails_managers(admin_client, monkeypatch):
    mgr = _make_user(admin_client, "Mgr4", role="manager", email="m4@x.test")
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body, from_address=None: sent.append((subject, sorted(to), body)),
    )
    ev = admin_client.post("/api/events", json={
        "name": "doomed", "manager_ids": [mgr["id"]],
    }).json()
    sent.clear()
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 200
    assert sent
    subj, to, body = sent[-1]
    assert "deleted" in subj.lower()
    assert "m4@x.test" in to


def test_task_creation_does_NOT_email_event_managers(admin_client, monkeypatch):
    """A task dropped inside an event notifies only the task's assignees, not the event's managers."""
    mgr = _make_user(admin_client, "EvMgr", role="manager", email="evmgr@x.test")
    worker = _make_user(admin_client, "Wkr", role="user", email="wkr@x.test")
    p = _make_project(admin_client)
    ev = admin_client.post("/api/events", json={
        "name": "Standup", "manager_ids": [mgr["id"]],
    }).json()
    sent = []
    monkeypatch.setattr(
        "app.email_service.deliver",
        lambda subject, to, body, from_address=None: sent.append((subject, sorted(to), body)),
    )
    r = admin_client.post("/api/bugs", json={
        "title": "Do the thing",
        "project_id": p["id"], "item_type": "Task",
        "event_id": ev["id"], "assignee_ids": [worker["id"]],
    })
    assert r.status_code == 201, r.text
    # Only the assignee gets a task-created email; the event manager must not.
    " ".join(b for _, _, b in sent) + " " + " ".join(s for s, _, _ in sent)
    all_to = [addr for _, to, _ in sent for addr in to]
    assert "wkr@x.test" in all_to, "Assignee should be notified"
    assert "evmgr@x.test" not in all_to, \
        "Event manager must NOT be cc'd on task-created emails"
    assert any("task" in s.lower() for s, _, _ in sent)


def test_event_managers_must_be_admin_or_manager(admin_client):
    """Regular users can't be event managers. Returns 400 with a helpful detail."""
    regular = _make_user(admin_client, "Plain", role="user", email="plain@x.test")
    r = admin_client.post("/api/events", json={
        "name": "bad-managers", "manager_ids": [regular["id"]],
    })
    assert r.status_code == 400, r.text
    assert "manager" in r.json()["detail"].lower()


# Event delete is admin-only (also exercised in test_manager_cannot_delete_anything).
def test_regular_user_cannot_delete_event(admin_client):
    ev = admin_client.post("/api/events", json={"name": "user-delete-test"}).json()
    _make_user(admin_client, "User9", role="user", email="u9@x.test")
    _login(admin_client, "u9@x.test", "User12345Aa")
    r = admin_client.delete(f"/api/events/{ev['id']}")
    assert r.status_code == 403


def test_regular_user_cannot_create_event(admin_client):
    _make_user(admin_client, "User10", role="user", email="u10@x.test")
    _login(admin_client, "u10@x.test", "User12345Aa")
    r = admin_client.post("/api/events", json={"name": "should-fail"})
    assert r.status_code == 403


def test_event_out_includes_managers(admin_client):
    mgr = _make_user(admin_client, "Mgr5", role="manager", email="m5@x.test")
    ev = admin_client.post("/api/events", json={
        "name": "with-managers", "manager_ids": [mgr["id"]],
    }).json()
    assert len(ev["managers"]) == 1
    assert ev["managers"][0]["name"] == "Mgr5"
    # Verify the detail endpoint returns the same data.
    detail = admin_client.get(f"/api/events/{ev['id']}").json()
    assert len(detail["managers"]) == 1
    assert detail["managers"][0]["email"] == "m5@x.test"


# Tab-aware /api/stats: every KPI filters by item_type, except by_type,
# which stays global so the tab badges stay correct.
def test_stats_filters_kpis_by_item_type(admin_client):
    p = _make_project(admin_client)
    for i in range(3):
        _make_item(admin_client, p["id"], item_type="Bug", title=f"Bug-{i}-name")
    _make_item(admin_client, p["id"], item_type="Requirement", title="Req-0-name")
    for i in range(2):
        _make_item(admin_client, p["id"], item_type="Task", title=f"Task-{i}-name")

    global_s = admin_client.get("/api/stats").json()
    assert global_s["bugs"] == 6  # all non-excluded statuses
    assert global_s["by_type"]["Bug"] == 3
    assert global_s["by_type"]["Requirement"] == 1
    assert global_s["by_type"]["Task"] == 2

    bug_s = admin_client.get("/api/stats?item_type=Bug").json()
    assert bug_s["bugs"] == 3
    assert bug_s["open"] == 3        # default status is "New"
    # by_type stays global under an item_type filter so tab badges show the full picture.
    assert bug_s["by_type"]["Bug"] == 3
    assert bug_s["by_type"]["Requirement"] == 1
    assert bug_s["by_type"]["Task"] == 2

    task_s = admin_client.get("/api/stats?item_type=Task").json()
    assert task_s["bugs"] == 2
    assert task_s["open"] == 2

    req_s = admin_client.get("/api/stats?item_type=Requirement").json()
    assert req_s["bugs"] == 1


def test_stats_filters_breakdowns_by_item_type(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], item_type="Bug",         priority="High")
    _make_item(admin_client, p["id"], item_type="Bug",         priority="High")
    _make_item(admin_client, p["id"], item_type="Task",        priority="Low")
    _make_item(admin_client, p["id"], item_type="Requirement", priority="Medium")

    bug_s = admin_client.get("/api/stats?item_type=Bug").json()
    assert bug_s["by_priority"].get("High") == 2
    assert "Low" not in bug_s["by_priority"]    # task's Low excluded
    assert "Medium" not in bug_s["by_priority"]  # req's Medium excluded

    task_s = admin_client.get("/api/stats?item_type=Task").json()
    assert task_s["by_priority"].get("Low") == 1
    assert "High" not in task_s["by_priority"]


def test_stats_rejects_unknown_item_type(admin_client):
    r = admin_client.get("/api/stats?item_type=Bogus")
    assert r.status_code == 400
    assert "item_type" in r.json()["detail"].lower()


def test_audit_history_survives_bug_delete(admin_client):
    """Regression: rows are detached (bug_id set NULL) before a bug delete, so the global audit trail survives."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug", title="Login broken thing")
    admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
    admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "Investigating"})
    audit_before = admin_client.get("/api/audit").json()
    rows_for_bug_before = [
        r for r in audit_before
        if (r["entity_type"] == "bug" and r["entity_id"] == bug["id"])
        or f"#{bug['id']}" in (r["detail"] or "")
    ]
    assert len(rows_for_bug_before) >= 3, rows_for_bug_before  # create + status + comment

    r = admin_client.delete(f"/api/bugs/{bug['id']}")
    assert r.status_code == 200, r.text

    audit_after = admin_client.get("/api/audit").json()
    rows_for_bug_after = [
        r for r in audit_after
        if (r["entity_type"] == "bug" and r["entity_id"] == bug["id"])
        or f"#{bug['id']}" in (r["detail"] or "")
    ]
    # Full history should remain, plus the new "bug_deleted" row.
    actions = [r["action"] for r in rows_for_bug_after]
    assert "bug_created" in actions, actions
    assert "comment_added" in actions, actions
    assert "status_changed" in actions, actions
    assert "bug_deleted" in actions, actions


def test_audit_search_by_bug_title(admin_client):
    """Audit search by bug title matches via the live bug.title (LEFT JOIN) and the title baked into the detail string."""
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug", title="Payment gateway timeout")
    admin_client.put(f"/api/bugs/{bug['id']}", json={"priority": "Critical"})

    r = admin_client.get("/api/audit?q=Payment+gateway")
    assert r.status_code == 200
    rows = r.json()
    assert any(r["entity_id"] == bug["id"] for r in rows), \
        "Searching by bug title should find audit rows for that bug"


def test_audit_search_by_item_type_word(admin_client):
    """Searching 'task' narrows to task-related audit events via the joined Bug.item_type and the detail string."""
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], item_type="Task", title="Write the spec")
    _make_item(admin_client, p["id"], item_type="Bug",  title="Crash on submit")
    r = admin_client.get("/api/audit?q=task")
    rows = r.json()
    assert any(
        "task" in (row["detail"] or "").lower() or
        "task" in (row["action"] or "").lower()
        for row in rows
    ), rows


def test_audit_search_by_assignee_name(admin_client):
    """Assignee names are baked into the audit detail, so searching by name surfaces the assignment event."""
    user = _make_user(admin_client, "Sandra", role="user", email="sandra@x.test")
    p = _make_project(admin_client)
    bug = _make_item(admin_client, p["id"], item_type="Bug",
                     title="Crash on submit form", assignee_ids=[user["id"]])
    r = admin_client.get("/api/audit?q=Sandra")
    rows = r.json()
    assert any(r["entity_id"] == bug["id"] for r in rows), \
        f"Searching audit by assignee name 'Sandra' should hit bug #{bug['id']}: got {rows[:3]}"
