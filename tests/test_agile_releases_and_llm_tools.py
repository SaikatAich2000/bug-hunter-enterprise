"""Basic sanity tests for release readiness/roadmap, reporting follow-on and
the Sleuth LLM-driven tool-calling agent.
"""
from __future__ import annotations


def _setup_project_with_board(admin_client, name="Release Suite"):
    res = admin_client.post("/api/projects", json={"name": name, "description": "", "color": "#c9764f"})
    assert res.status_code == 201, res.text
    project = res.json()
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200, enable_res.text
    return project, enable_res.json()["board_id"]


def _create_story(admin_client, project_id, title, **extra):
    body = {"project_id": project_id, "title": title, "item_type": "story"}
    body.update(extra)
    res = admin_client.post("/api/agile/work-items", json=body)
    assert res.status_code == 201, res.text
    return res.json()


# --- Release readiness / roadmap ---

def test_release_action_and_double_release_rejected(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Release Gate Suite")

    v_res = admin_client.post("/api/agile/releases", json={"project_id": project["id"], "name": "v1.0"})
    assert v_res.status_code == 201, v_res.text
    version = v_res.json()

    # No work items associated with this release — readiness has only the
    # missing-release-date warning (not a hard error), so it should succeed.
    release_res = admin_client.post(f"/api/agile/releases/{version['id']}/release", json={
        "version": version["version"],
    })
    assert release_res.status_code == 200, release_res.text
    released = release_res.json()
    assert released["status"] == "released"

    # Releasing an already-released version must be rejected.
    second_res = admin_client.post(f"/api/agile/releases/{version['id']}/release", json={
        "version": released["version"],
    })
    assert second_res.status_code == 409


def test_release_360_and_roadmap(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Roadmap Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Roadmap Epic", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()
    admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "start_date": "2026-01-01", "target_date": "2026-03-01", "version": 1,
    })

    v_res = admin_client.post("/api/agile/releases", json={
        "project_id": project["id"], "name": "v2.0",
        "start_date": "2026-01-01", "release_date": "2026-02-01",
    })
    version = v_res.json()

    r360_res = admin_client.get(f"/api/agile/releases/{version['id']}/360")
    assert r360_res.status_code == 200, r360_res.text
    assert "readiness" in r360_res.json()

    roadmap_res = admin_client.get(f"/api/agile/roadmap?project_id={project['id']}")
    assert roadmap_res.status_code == 200, roadmap_res.text
    roadmap = roadmap_res.json()
    assert any(e["id"] == epic["id"] for e in roadmap["epics"])
    assert any(r["id"] == version["id"] for r in roadmap["releases"])


def test_reporting_follow_on_smoke(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Follow-on Reports Suite")
    item = _create_story(admin_client, project["id"], "Reported story", story_points=3)
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Closed Sprint", "start_date": "2026-01-01", "end_date": "2026-01-07",
    }).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
        "to_status": "Done", "version": item["version"],
    })
    complete_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/complete", json={
        "closing_note": "wrapped up", "version": sprint["version"] + 1,
        "default_destination": "backlog",
    })
    assert complete_res.status_code == 200, complete_res.text

    velocity_res = admin_client.get(f"/api/agile/reports/velocity?board_id={board_id}&sprint_count=5")
    assert velocity_res.status_code == 200, velocity_res.text
    assert len(velocity_res.json()["points"]) == 1

    cfd_res = admin_client.get(
        f"/api/agile/reports/cumulative-flow?board_id={board_id}&date_from=2026-01-01&date_to=2026-01-07"
    )
    assert cfd_res.status_code == 200, cfd_res.text
    assert len(cfd_res.json()["points"]) == 7

    cc_res = admin_client.get(
        f"/api/agile/reports/control-chart?board_id={board_id}&date_from=2026-01-01&date_to=2026-01-07"
    )
    assert cc_res.status_code == 200, cc_res.text


# --- Sleuth LLM-driven tool-calling agent ---

def test_llm_tools_agent_disabled_by_default():
    from app.chatbot import llm_tools_agent
    assert llm_tools_agent.is_available() is False


def test_no_delete_tool_exists():
    from app.chatbot import tools
    assert not any("delete" in name for name in tools.ALL_TOOL_NAMES)
    for spec in tools.TOOL_SPECS:
        assert "delete" not in spec["function"]["name"]


def test_llm_tools_rejects_disallowed_tool_name(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", lambda messages: {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "1", "function": {"name": "delete_everything", "arguments": "{}"}}],
    })

    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        actor = db.query(User).first()
        resp = llm_tools_agent.run("please help", db, actor)
    finally:
        db.close()
    assert resp.intent == "llm_tools_blocked"
    assert "blocked" in resp.blocks[0].payload["text"].lower()


def test_llm_tools_write_tool_stages_confirmation(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent
    from app.chatbot.memory import store as mem_store

    project_res = admin_client.post("/api/projects", json={"name": "LLM Tools Suite", "color": "#c9764f"})
    project = project_res.json()

    call_count = {"n": 0}

    def fake_call(messages):
        call_count["n"] += 1
        return {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "1", "function": {
                "name": "create_work_item",
                "arguments": f'{{"project_name": "{project["name"]}", "title": "New bug from Sleuth"}}',
            }}],
        }

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", fake_call)

    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        actor = db.query(User).filter(User.email == "admin@test.local").first()
        resp = llm_tools_agent.run("create a bug titled New bug from Sleuth", db, actor)
        assert resp.intent == "confirm_action"
        assert call_count["n"] == 1  # write tool call stops the loop immediately

        pending = mem_store.take_pending(actor.id)
        assert pending is not None
        assert pending["kind"] == "llm_tool_call"
        assert pending["tool"] == "create_work_item"

        confirm_resp = llm_tools_agent.confirm_pending_tool_call(db, actor, pending)
        assert confirm_resp.intent == "action_done"
    finally:
        db.close()


def test_llm_tools_provider_failure_returns_clear_error(monkeypatch, admin_client):
    from app.chatbot import llm_tools_agent

    monkeypatch.setattr(llm_tools_agent, "call_llm_with_tools", lambda messages: None)

    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        actor = db.query(User).first()
        resp = llm_tools_agent.run("hello", db, actor)
    finally:
        db.close()
    assert resp.intent == "llm_tools_unavailable"
    assert "unavailable" in resp.blocks[0].payload["text"].lower()
