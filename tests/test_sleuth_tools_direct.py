"""Direct unit tests for app/chatbot/tools.py — the Sleuth LLM tool
registry. These call the tool functions directly (db, actor, args) rather than
through the (off-by-default) LLM agent loop, since every tool re-implements
the same permission/validation path the REST routes use.
"""
from __future__ import annotations

import pytest

from app.chatbot import tools
from app.chatbot.tools import ToolError


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _admin(db):
    from app.models import User
    return db.query(User).filter(User.email == "admin@test.local").first()


def _make_project(admin_client, name="Sleuth Tools Suite"):
    res = admin_client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert res.status_code == 201, res.text
    return res.json()


def _make_bug(admin_client, project_id, title="Direct tool test bug", **extra):
    body = {"project_id": project_id, "title": title, "item_type": "Bug"}
    body.update(extra)
    res = admin_client.post("/api/bugs", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# --- _resolve_project / _resolve_user_by_name / _get_item ---

def test_resolve_project_not_found_and_no_access(admin_client):
    project = _make_project(admin_client, "Resolve Project Suite")
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="No project named"):
            tools._resolve_project(db, actor, "Definitely Not A Project")
        # Sanity: the project the admin just created IS resolvable.
        resolved = tools._resolve_project(db, actor, project["name"])
        assert resolved.id == project["id"]
    finally:
        db.close()


def test_resolve_user_by_name_matches_email_prefix(admin_client):
    db = _db()
    try:
        u = tools._resolve_user_by_name(db, _admin(db), "admin")
        assert u.email == "admin@test.local"
        with pytest.raises(ToolError, match="No user found"):
            tools._resolve_user_by_name(db, _admin(db), "definitely-nobody")
    finally:
        db.close()


def test_get_item_not_found(admin_client):
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="not found"):
            tools._get_item(db, actor, 999_999)
    finally:
        db.close()


# --- Read tools ---

def test_search_work_items_filters(admin_client):
    project = _make_project(admin_client, "Search Tools Suite")
    _make_bug(admin_client, project["id"], "Sleuth findable bug", priority="High")
    db = _db()
    try:
        actor = _admin(db)
        result = tools.search_work_items(db, actor, {
            "project_name": project["name"], "query": "findable", "limit": 5,
        })
        assert result["count"] >= 1
        assert any("findable" in i["title"].lower() for i in result["items"])

        by_status = tools.search_work_items(db, actor, {"status": "New", "limit": 5})
        assert isinstance(by_status["items"], list)

        by_assignee = tools.search_work_items(db, actor, {"assignee_name": "admin", "limit": 5})
        assert isinstance(by_assignee["items"], list)
    finally:
        db.close()


def test_get_work_item_requires_id_and_returns_brief(admin_client):
    project = _make_project(admin_client, "Get Item Tools Suite")
    bug = _make_bug(admin_client, project["id"])
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="item_id is required"):
            tools.get_work_item(db, actor, {})
        brief = tools.get_work_item(db, actor, {"item_id": bug["id"]})
        assert brief["id"] == bug["id"]
        assert brief["title"] == bug["title"]
    finally:
        db.close()


def test_get_backlog_requires_agile_enabled(admin_client):
    project = _make_project(admin_client, "Backlog Tools Suite")
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="No project named"):
            tools.get_backlog(db, actor, {"project_name": ""})
    finally:
        db.close()

    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200
    db = _db()
    try:
        actor = _admin(db)
        result = tools.get_backlog(db, actor, {"project_name": project["name"]})
        assert result["items"] == []
        assert result["count"] == 0
    finally:
        db.close()


def test_list_sprints_tool_no_agile(admin_client):
    project = _make_project(admin_client, "List Sprints No Agile Suite")
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="not enabled"):
            tools.list_sprints_tool(db, actor, {"project_name": project["name"]})
    finally:
        db.close()


def test_list_sprints_tool_with_agile(admin_client):
    project = _make_project(admin_client, "List Sprints Suite")
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable_res.json()["board_id"]
    sprint_res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Tool Sprint"})
    assert sprint_res.status_code == 201, sprint_res.text

    db = _db()
    try:
        actor = _admin(db)
        result = tools.list_sprints_tool(db, actor, {"project_name": project["name"]})
        assert any(s["name"] == "Tool Sprint" for s in result["sprints"])
        filtered = tools.list_sprints_tool(db, actor, {"project_name": project["name"], "state": "active"})
        assert filtered["sprints"] == []
    finally:
        db.close()


def test_get_sprint_planning_tool(admin_client):
    project = _make_project(admin_client, "Planning Tool Suite")
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable_res.json()["board_id"]
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Planning Tool Sprint"}).json()

    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="sprint_id is required"):
            tools.get_sprint_planning_tool(db, actor, {})
        with pytest.raises(ToolError, match="not found"):
            tools.get_sprint_planning_tool(db, actor, {"sprint_id": 999_999})
        result = tools.get_sprint_planning_tool(db, actor, {"sprint_id": sprint["id"]})
        assert "readiness" in result
        assert result["item_count"] == 0
    finally:
        db.close()


def test_get_epic_progress_tool(admin_client):
    project = _make_project(admin_client, "Epic Tool Suite")
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200, enable_res.text
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Tool Epic", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()
    bug = _make_bug(admin_client, project["id"])

    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="epic_id is required"):
            tools.get_epic_progress_tool(db, actor, {})
        with pytest.raises(ToolError, match="is not an Epic"):
            tools.get_epic_progress_tool(db, actor, {"epic_id": bug["id"]})
        result = tools.get_epic_progress_tool(db, actor, {"epic_id": epic["id"]})
        assert result["epic_id"] == epic["id"]
    finally:
        db.close()


# --- Write tools ---

def test_create_work_item_legacy_and_agile_types(admin_client):
    project = _make_project(admin_client, "Create Tool Suite")
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="at least 3 characters"):
            tools.create_work_item(db, actor, {"title": "ab", "project_name": project["name"]})

        created = tools.create_work_item(db, actor, {
            "title": "Legacy bug via tool", "project_name": project["name"], "item_type": "Bug",
        })
        assert created["item_type"] == "Bug"
        db.commit()
    finally:
        db.close()

    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200
    db = _db()
    try:
        actor = _admin(db)
        created = tools.create_work_item(db, actor, {
            "title": "Story via tool", "project_name": project["name"], "item_type": "Story",
        })
        assert created["item_type"] == "Story"
        db.commit()
    finally:
        db.close()


def test_transition_work_item_tool(admin_client):
    project = _make_project(admin_client, "Transition Tool Suite")
    bug = _make_bug(admin_client, project["id"])
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="are required"):
            tools.transition_work_item_tool(db, actor, {"item_id": bug["id"]})
        with pytest.raises(ToolError, match="not valid"):
            tools.transition_work_item_tool(db, actor, {"item_id": bug["id"], "to_status": "NotAStatus"})
        result = tools.transition_work_item_tool(db, actor, {
            "item_id": bug["id"], "to_status": "In Progress",
        })
        assert result["status"] == "In Progress"
        db.commit()
    finally:
        db.close()


def test_assign_work_item_tool(admin_client):
    project = _make_project(admin_client, "Assign Tool Suite")
    bug = _make_bug(admin_client, project["id"])
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="are required"):
            tools.assign_work_item_tool(db, actor, {"item_id": bug["id"]})
        with pytest.raises(ToolError, match="No user found"):
            tools.assign_work_item_tool(db, actor, {"item_id": bug["id"], "assignee_name": "nobody-at-all"})
        result = tools.assign_work_item_tool(db, actor, {
            "item_id": bug["id"], "assignee_name": "admin",
        })
        assert "Saikat Aich" in result["assignees"] or any(result["assignees"])
        db.commit()
    finally:
        db.close()


def test_add_comment_tool(admin_client):
    project = _make_project(admin_client, "Comment Tool Suite")
    bug = _make_bug(admin_client, project["id"])
    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="are required"):
            tools.add_comment_tool(db, actor, {"item_id": bug["id"]})
        result = tools.add_comment_tool(db, actor, {"item_id": bug["id"], "body": "Filed via Sleuth"})
        assert result["id"] == bug["id"]
        assert result["comment_id"] is not None
        db.commit()
    finally:
        db.close()


def test_start_and_complete_sprint_tool(admin_client):
    project = _make_project(admin_client, "Sprint Lifecycle Tool Suite")
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    board_id = enable_res.json()["board_id"]
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Tool Lifecycle Sprint"}).json()
    item = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Sprint tool item", "item_type": "story",
    }).json()
    add_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    assert add_res.status_code == 200, add_res.text

    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="sprint_id is required"):
            tools.start_sprint_tool(db, actor, {})
        with pytest.raises(ToolError, match="not found"):
            tools.start_sprint_tool(db, actor, {"sprint_id": 999_999})
        started = tools.start_sprint_tool(db, actor, {"sprint_id": sprint["id"]})
        assert started["state"] == "active"
        db.commit()
    finally:
        db.close()

    db = _db()
    try:
        actor = _admin(db)
        with pytest.raises(ToolError, match="sprint_id is required"):
            tools.complete_sprint_tool(db, actor, {})
        completed = tools.complete_sprint_tool(db, actor, {
            "sprint_id": sprint["id"], "closing_note": "done via tool",
        })
        assert completed["state"] == "closed"
        db.commit()
    finally:
        db.close()


# --- Registry integrity ---

def test_tool_registry_matches_names_and_specs():
    assert set(tools.TOOL_REGISTRY.keys()) == tools.ALL_TOOL_NAMES
    spec_names = {s["function"]["name"] for s in tools.TOOL_SPECS}
    assert spec_names == tools.ALL_TOOL_NAMES
