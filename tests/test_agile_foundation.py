"""Basic sanity tests for the Agile foundation: project activation,
default board provisioning, global workflow-status seeding, and idempotent
re-activation.
"""
from __future__ import annotations


def _make_project(admin_client, name="Apollo"):
    res = admin_client.post("/api/projects", json={"name": name, "description": "", "color": "#c9764f"})
    assert res.status_code == 201, res.text
    return res.json()


def test_agile_settings_default_disabled(admin_client):
    project = _make_project(admin_client)
    res = admin_client.get(f"/api/agile/projects/{project['id']}/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["agile_enabled"] is False
    assert body["board_id"] is None


def test_enable_agile_creates_default_board(admin_client):
    project = _make_project(admin_client, "Board Test")
    res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agile_enabled"] is True
    assert body["board_id"] is not None

    board_res = admin_client.get(f"/api/agile/boards/{body['board_id']}")
    assert board_res.status_code == 200
    board = board_res.json()
    assert board["is_default"] is True
    categories = {c["category"] for c in board["columns"]}
    # Simplified-Jira contract mandates Testing between In Progress and Done.
    assert categories == {"todo", "in_progress", "testing", "done"}
    # Every column has at least one mapped status.
    for col in board["columns"]:
        assert col["statuses"], f"column {col['name']} has no mapped statuses"


def test_enable_agile_is_idempotent(admin_client):
    project = _make_project(admin_client, "Idempotent Test")
    r1 = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    r2 = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["board_id"] == r2.json()["board_id"]

    boards_res = admin_client.get(f"/api/agile/boards?project_id={project['id']}")
    assert boards_res.status_code == 200
    assert len(boards_res.json()) == 1


def test_enable_agile_requires_permission(user_client):
    # Regular users cannot see /api/projects create (manager/admin only) — use
    # an existing accessible project instead by checking 403 on settings write.
    res = user_client.get("/api/projects")
    assert res.status_code == 200
    # No projects visible to a fresh user with no memberships; nothing to
    # enable — assert the enable endpoint 404s (no access) rather than 200.
    enable_res = user_client.post("/api/agile/projects/1/enable", json={"feature_flags": {}})
    assert enable_res.status_code in (403, 404)


def test_disable_agile_blocked_while_sprint_active(admin_client):
    project = _make_project(admin_client, "Disable Guard")
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable_res.json()["board_id"]

    item_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Story One", "item_type": "story",
    })
    assert item_res.status_code == 201, item_res.text
    item_id = item_res.json()["id"]

    sprint_res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint 1"})
    assert sprint_res.status_code == 201, sprint_res.text
    sprint_id = sprint_res.json()["id"]

    add_res = admin_client.post(f"/api/agile/sprints/{sprint_id}/items", json={"item_ids": [item_id]})
    assert add_res.status_code == 200, add_res.text

    start_res = admin_client.post(f"/api/agile/sprints/{sprint_id}/start", json={"version": 1})
    assert start_res.status_code == 200, start_res.text

    disable_res = admin_client.post(f"/api/agile/projects/{project['id']}/disable")
    assert disable_res.status_code == 409


def test_disabled_project_404s_agile_data_endpoints(admin_client):
    """disabling Agile makes Board/Sprint/Backlog/Taxonomy data
    endpoints 404, while the settings endpoint itself stays reachable and data
    is retained (not deleted) for a later re-enable."""
    project = _make_project(admin_client, "Feature Flag Guard")
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable_res.json()["board_id"]

    disable_res = admin_client.post(f"/api/agile/projects/{project['id']}/disable")
    assert disable_res.status_code == 200, disable_res.text

    # Settings endpoint stays reachable (it's the activation entry point).
    settings_res = admin_client.get(f"/api/agile/projects/{project['id']}/settings")
    assert settings_res.status_code == 200
    assert settings_res.json()["agile_enabled"] is False

    # Real Agile data endpoints 404 while disabled.
    assert admin_client.get(f"/api/agile/boards/{board_id}").status_code == 404
    assert admin_client.get(f"/api/agile/boards?project_id={project['id']}").status_code == 404
    assert admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Should 404", "item_type": "story",
    }).status_code == 404
    assert admin_client.get(f"/api/agile/epics?project_id={project['id']}").status_code == 404

    # Re-enabling reuses the existing board (data was retained, not deleted).
    re_enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert re_enable_res.status_code == 200, re_enable_res.text
    assert re_enable_res.json()["board_id"] == board_id
    assert admin_client.get(f"/api/agile/boards/{board_id}").status_code == 200


def test_disabled_project_create_reports_agile_not_enabled(admin_client):
    """An Agile-only create in a project with Agile off must explain itself.

    The status stays 404 (inaccessible projects remain indistinguishable), but a
    project the caller can list must not be reported as "Project not found" —
    that was the misleading error users hit when creating an Epic/Story/Sub-task.
    Legacy /api/bugs creation keeps working in the same project.
    """
    project = _make_project(admin_client, "Agile Off Create Guard")

    create = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Blocked Story", "item_type": "story",
    })
    assert create.status_code == 404
    assert create.json()["detail"] == "Agile is not enabled for this project"

    # The same project still accepts legacy items.
    legacy = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Legacy still fine", "item_type": "Bug",
    })
    assert legacy.status_code == 201, legacy.text

    # Enabling Agile makes the previously refused create succeed unchanged.
    assert admin_client.post(
        f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}},
    ).status_code == 200
    retry = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Now allowed", "item_type": "story",
    })
    assert retry.status_code == 201, retry.text


def test_nonexistent_project_still_reports_project_not_found(admin_client):
    """The existence mask is unchanged for projects the caller cannot reach."""
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": 999999, "title": "Ghost", "item_type": "story",
    })
    assert res.status_code == 404
    assert res.json()["detail"] == "Project not found"


def test_create_project_with_agile_enabled_provisions_board(admin_client):
    """An Agile-on create returns a project that is immediately usable.

    The board and status mappings must exist in the same request, so a caller
    can create a Story right away without a second activation call.
    """
    res = admin_client.post("/api/projects", json={
        "name": "Agile At Creation", "color": "#c9764f", "agile_enabled": True,
    })
    assert res.status_code == 201, res.text
    project = res.json()

    settings = admin_client.get(f"/api/agile/projects/{project['id']}/settings")
    assert settings.status_code == 200
    body = settings.json()
    assert body["agile_enabled"] is True
    assert body["board_id"] is not None

    board = admin_client.get(f"/api/agile/boards/{body['board_id']}")
    assert board.status_code == 200
    assert board.json()["is_default"] is True
    assert {c["category"] for c in board.json()["columns"]} == {
        "todo", "in_progress", "testing", "done",
    }

    # The point of the flag: an Agile item can be created straight away.
    item = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "First Story", "item_type": "story",
    })
    assert item.status_code == 201, item.text


def test_create_project_defaults_to_agile_disabled(admin_client):
    """Omitting the field keeps the historical opt-out default."""
    res = admin_client.post("/api/projects", json={"name": "Plain Tracker", "color": "#123456"})
    assert res.status_code == 201, res.text
    project = res.json()

    settings = admin_client.get(f"/api/agile/projects/{project['id']}/settings").json()
    assert settings["agile_enabled"] is False
    assert settings["board_id"] is None


def test_project_update_cannot_toggle_agile_without_a_board(admin_client):
    """PUT ignores agile_enabled: only the Agile routes may flip it.

    Otherwise a client could set agile_enabled=True and leave the project with
    no default board, which every Agile endpoint depends on.
    """
    project = admin_client.post(
        "/api/projects", json={"name": "Update Guard", "color": "#c9764f"},
    ).json()
    # An extra body property is ignored by the schema; the send cannot fail.
    res = admin_client.put(f"/api/projects/{project['id']}", json={
        "name": "Update Guard", "color": "#c9764f", "agile_enabled": True,
    })
    assert res.status_code == 200, res.text

    settings = admin_client.get(f"/api/agile/projects/{project['id']}/settings").json()
    assert settings["agile_enabled"] is False
    assert settings["board_id"] is None

    # The supported path provisions the board as well.
    enable = admin_client.post(
        f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}},
    )
    assert enable.status_code == 200
    assert enable.json()["board_id"] is not None
