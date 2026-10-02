"""Focused tests for Sprint multiple-assignee support."""
from __future__ import annotations


def _make_project(admin_client, name="Apollo"):
    res = admin_client.post("/api/projects", json={"name": name, "description": "", "color": "#c9764f"})
    assert res.status_code == 201, res.text
    return res.json()


def _make_user(admin_client, name, email, role="user"):
    res = admin_client.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": "Pass12345",
    })
    assert res.status_code == 201, res.text
    return res.json()


def test_sprint_create_with_assignees(admin_client):
    project = _make_project(admin_client, "Sprint Assignees")
    enable = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable.json()["board_id"]

    user1 = _make_user(admin_client, "Alice", "alice@test.local")
    user2 = _make_user(admin_client, "Bob", "bob@test.local")

    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint With Assignees",
        "assignee_ids": [user1["id"], user2["id"]],
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["display_id"] == f"SPRINT-{body['id']}"
    assignee_ids = [a["id"] for a in body["assignees"]]
    assert set(assignee_ids) == {user1["id"], user2["id"]}


def test_sprint_update_add_assignee(admin_client):
    project = _make_project(admin_client, "Sprint Update Assignees")
    enable = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable.json()["board_id"]

    user1 = _make_user(admin_client, "Carol", "carol@test.local")
    user2 = _make_user(admin_client, "Dave", "dave@test.local")

    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint Update Test",
        "assignee_ids": [user1["id"]],
    })
    sprint_id = res.json()["id"]
    version = res.json()["version"]

    # Add second assignee
    res = admin_client.put(f"/api/agile/sprints/{sprint_id}", json={
        "assignee_ids": [user1["id"], user2["id"]],
        "version": version,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assignee_ids = [a["id"] for a in body["assignees"]]
    assert set(assignee_ids) == {user1["id"], user2["id"]}


def test_sprint_update_remove_assignee(admin_client):
    project = _make_project(admin_client, "Sprint Remove Assignees")
    enable = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable.json()["board_id"]

    user1 = _make_user(admin_client, "Eve", "eve@test.local")
    user2 = _make_user(admin_client, "Frank", "frank@test.local")

    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint Remove Test",
        "assignee_ids": [user1["id"], user2["id"]],
    })
    sprint_id = res.json()["id"]
    version = res.json()["version"]

    # Remove one assignee
    res = admin_client.put(f"/api/agile/sprints/{sprint_id}", json={
        "assignee_ids": [user1["id"]],
        "version": version,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assignee_ids = [a["id"] for a in body["assignees"]]
    assert assignee_ids == [user1["id"]]


def test_sprint_without_assignees(admin_client):
    project = _make_project(admin_client, "Sprint No Assignees")
    enable = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable.json()["board_id"]

    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Empty Sprint",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["assignees"] == []
    assert body["display_id"] == f"SPRINT-{body['id']}"
