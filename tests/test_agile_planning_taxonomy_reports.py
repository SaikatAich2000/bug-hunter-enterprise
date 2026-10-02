"""Basic sanity tests for Agile: board view/transition/WIP,
planning/capacity, epics/components/labels, and core reports.

"""
from __future__ import annotations


def _setup_project_with_board(admin_client, name="Agile Suite"):
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


# --- Board view + transitions + WIP ---

def test_board_view_shows_active_sprint_cards(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Board View Suite")
    item = _create_story(admin_client, project["id"], "Board card")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "S1"}).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})

    view = admin_client.get(f"/api/agile/boards/{board_id}/view").json()
    assert view["sprint_id"] == sprint["id"]
    all_card_ids = [c["id"] for col in view["columns"] for c in col["cards"]]
    assert item["id"] in all_card_ids


def test_transition_moves_item_and_bumps_version(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Transition Suite")
    item = _create_story(admin_client, project["id"], "Transition me")
    res = admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
        "to_status": "In Progress", "version": item["version"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "In Progress"
    assert res.json()["version"] == item["version"] + 1


def test_task_transition_to_blocked_repairs_missing_live_transition_state(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Task Blocked Transition Suite")
    task = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Task blocked transition",
        "description": "", "item_type": "Task",
    }).json()
    moved = admin_client.post(f"/api/agile/work-items/{task['id']}/transition", json={
        "to_status": "In Progress", "version": task["version"],
    })
    assert moved.status_code == 200, moved.text
    blocked = admin_client.post(f"/api/agile/work-items/{task['id']}/transition", json={
        "to_status": "Blocked", "version": moved.json()["version"],
    })
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "Blocked"


def test_daily_sprint_report_includes_each_item_snapshot(admin_client):
    from datetime import datetime, timedelta, timezone

    project, board_id = _setup_project_with_board(admin_client, "Daily Report Items Suite")
    item = _create_story(admin_client, project["id"], "Daily report item")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Daily S1"}).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    today = datetime.now(timezone.utc).date()  # the board's timezone is UTC
    report = admin_client.get(
        f"/api/agile/reports/daily?sprint_id={sprint['id']}&report_date={today.isoformat()}"
    )
    assert report.status_code == 200, report.text
    rows = report.json()["items"]
    assert rows == [{
        "work_item_id": item["id"], "display_id": f"USRSTR-{item['id']}",
        "title": "Daily report item", "item_type": "Story",
        "status": "New", "category": "todo", "column": "To Do", "story_points": 0,
    }]
    # The report is point-in-time: the day before the issue existed shows nothing.
    before = admin_client.get(
        f"/api/agile/reports/daily?sprint_id={sprint['id']}"
        f"&report_date={(today - timedelta(days=1)).isoformat()}"
    ).json()
    assert before["items"] == []


def test_done_column_target_resolves_for_legacy_item_types(admin_client):
    """With the universal status vocabulary, any status can be applied to any
    item type directly. A Task transitioned to 'Closed' stays 'Closed' (not
    remapped to 'Done') because every status is now canonical for every type.
    Cross-type status application no longer needs per-type resolution."""
    project, _ = _setup_project_with_board(admin_client, "Cross Type Status Suite")
    task = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Task transition",
        "description": "", "item_type": "Task",
    }).json()
    requirement = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Requirement transition",
        "description": "", "item_type": "Requirement",
    }).json()

    task_result = admin_client.post(
        f"/api/agile/work-items/{task['id']}/transition",
        json={"to_status": "Closed", "version": task["version"]},
    )
    requirement_result = admin_client.post(
        f"/api/agile/work-items/{requirement['id']}/transition",
        json={"to_status": "Closed", "version": requirement["version"]},
    )

    assert task_result.status_code == 200, task_result.text
    assert task_result.json()["status"] == "Closed"
    assert requirement_result.status_code == 200, requirement_result.text
    assert requirement_result.json()["status"] == "Closed"


def test_board_categories_support_all_legacy_item_types(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "All Type Board Statuses")
    board = admin_client.get(f"/api/agile/boards/{board_id}").json()
    columns = {column["category"]: column for column in board["columns"]}
    item_statuses = {
        "Bug": ["New", "In Progress", "Testing", "Closed"],
        "Requirement": ["New", "In Review", "Testing", "Implemented"],
        "Task": ["New", "In Progress", "Testing", "Done"],
    }

    for item_type, expected_statuses in item_statuses.items():
        for category, expected_status in zip(("todo", "in_progress", "testing", "done"), expected_statuses):
            assert expected_status in columns[category]["statuses_by_type"][item_type]

        item = admin_client.post("/api/bugs", json={
            "project_id": project["id"], "title": f"{item_type} board status",
            "description": "", "item_type": item_type,
        }).json()
        for _category, expected_status in zip(("in_progress", "testing", "done"), expected_statuses[1:]):
            result = admin_client.post(
                f"/api/agile/work-items/{item['id']}/transition",
                json={"to_status": expected_status, "version": item["version"]},
            )
            assert result.status_code == 200, result.text
            item = result.json()


def test_transition_rejects_unknown_status(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Bad Transition Suite")
    item = _create_story(admin_client, project["id"], "Transition me badly")
    res = admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
        "to_status": "Nonexistent Status", "version": item["version"],
    })
    assert res.status_code == 422


def test_wip_limit_blocks_transition(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "WIP Suite")
    board = admin_client.get(f"/api/agile/boards/{board_id}").json()
    in_progress_col = next(c for c in board["columns"] if c["category"] == "in_progress")
    columns_payload = []
    for col in board["columns"]:
        entry = {
            "name": col["name"], "category": col["category"], "statuses": col["statuses"],
            "wip_enforcement": "block" if col["id"] == in_progress_col["id"] else "off",
        }
        if col["id"] == in_progress_col["id"]:
            entry["wip_limit"] = 1
        columns_payload.append(entry)
    replace_res = admin_client.put(f"/api/agile/boards/{board_id}/columns", json={
        "version": board["version"], "columns": columns_payload,
    })
    assert replace_res.status_code == 200, replace_res.text

    a = _create_story(admin_client, project["id"], "First in progress")
    b = _create_story(admin_client, project["id"], "Second in progress")
    r1 = admin_client.post(f"/api/agile/work-items/{a['id']}/transition", json={
        "to_status": "In Progress", "version": a["version"],
    })
    assert r1.status_code == 200, r1.text
    r2 = admin_client.post(f"/api/agile/work-items/{b['id']}/transition", json={
        "to_status": "In Progress", "version": b["version"],
    })
    assert r2.status_code == 400


def test_flag_and_estimate_update(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Flag Estimate Suite")
    item = _create_story(admin_client, project["id"], "Flag me")
    flag_res = admin_client.post(f"/api/agile/work-items/{item['id']}/flag", json={
        "flagged": True, "version": item["version"],
    })
    assert flag_res.status_code == 200
    assert flag_res.json()["flagged"] is True

    est_res = admin_client.put(f"/api/agile/work-items/{item['id']}/estimate", json={
        "story_points": 5, "version": flag_res.json()["version"],
    })
    assert est_res.status_code == 200, est_res.text
    assert est_res.json()["story_points"] == 5


def test_quick_filter_crud(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Quick Filter Suite")
    create_res = admin_client.post(f"/api/agile/boards/{board_id}/quick-filters", json={
        "name": "My Work", "filter_json": {"assignee": "me"},
    })
    assert create_res.status_code == 201, create_res.text
    qf = create_res.json()
    list_res = admin_client.get(f"/api/agile/boards/{board_id}/quick-filters")
    assert any(f["id"] == qf["id"] for f in list_res.json())
    del_res = admin_client.delete(f"/api/agile/quick-filters/{qf['id']}")
    assert del_res.status_code == 200


# --- Planning + capacity ---

def test_planning_readiness_and_capacity(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Planning Suite")
    item = _create_story(admin_client, project["id"], "Plan me", story_points=3)
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Planning Sprint", "goal": "Ship the thing",
        "start_date": "2026-01-01", "end_date": "2026-01-14",
    }).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})

    plan_res = admin_client.get(f"/api/agile/sprints/{sprint['id']}/planning")
    assert plan_res.status_code == 200, plan_res.text
    plan = plan_res.json()
    assert plan["item_count"] == 1
    assert plan["total_estimate"] == 3
    assert plan["ready_to_start"] is True

    me = admin_client.get("/api/auth/me").json()
    cap_res = admin_client.put(f"/api/agile/sprints/{sprint['id']}/capacity", json={
        "entries": [{"user_id": me["id"], "capacity_value": 10, "capacity_unit": "points"}],
    })
    assert cap_res.status_code == 200, cap_res.text
    assert len(cap_res.json()["capacity"]) == 1


def test_planning_not_ready_when_empty(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Empty Planning Suite")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Empty"}).json()
    plan_res = admin_client.get(f"/api/agile/sprints/{sprint['id']}/planning")
    assert plan_res.status_code == 200
    assert plan_res.json()["ready_to_start"] is False


# --- Epics, components, labels, collections ---

def test_epic_update_and_progress(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Epic Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Big Epic", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()

    child = _create_story(admin_client, project["id"], "Epic child", epic_id=epic["id"], story_points=5)
    assert child["epic_id"] == epic["id"]

    get_res = admin_client.get(f"/api/agile/epics/{epic['id']}")
    assert get_res.status_code == 200
    assert get_res.json()["progress"]["child_count"] == 1
    assert get_res.json()["progress"]["total_estimate"] == 5

    upd_res = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "health": "at_risk", "version": get_res.json()["version"],
    })
    assert upd_res.status_code == 200, upd_res.text
    assert upd_res.json()["health"] == "at_risk"

    archive_res = admin_client.post(f"/api/agile/epics/{epic['id']}/archive")
    assert archive_res.status_code == 200
    assert archive_res.json()["archived"] is True


def test_component_crud_and_archive(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Component Suite")
    create_res = admin_client.post("/api/agile/components", json={
        "project_id": project["id"], "name": "Backend",
    })
    assert create_res.status_code == 201, create_res.text
    comp = create_res.json()
    dup_res = admin_client.post("/api/agile/components", json={
        "project_id": project["id"], "name": "backend",
    })
    assert dup_res.status_code == 409

    archive_res = admin_client.post(f"/api/agile/components/{comp['id']}/archive")
    assert archive_res.status_code == 200
    assert archive_res.json()["archived"] is True


def test_label_merge_safety_and_taxonomy_assignment(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Label Suite")
    item = _create_story(admin_client, project["id"], "Label me")
    label_res = admin_client.post("/api/agile/labels", json={
        "project_id": project["id"], "display_name": "Accessibility",
    })
    assert label_res.status_code == 201, label_res.text
    label = label_res.json()

    tax_res = admin_client.put(f"/api/agile/work-items/{item['id']}/taxonomy", json={
        "label_ids": [label["id"]], "version": item["version"],
    })
    assert tax_res.status_code == 200, tax_res.text

    used_label = admin_client.get(f"/api/agile/labels/{label['id']}").json()
    assert used_label["usage_count"] == 1

    del_res = admin_client.delete(f"/api/agile/labels/{label['id']}")
    assert del_res.status_code == 409  # in use, must merge instead of delete


def test_retired_collection_and_feature_routes_are_gone(admin_client):
    """Collections and Features are not Jira levels; grouping uses labels."""
    project, _board_id = _setup_project_with_board(admin_client, "Collection Suite")
    for path in ("/api/agile/collections", "/api/agile/features"):
        assert admin_client.get(f"{path}?project_id={project['id']}").status_code == 404
        assert admin_client.post(path, json={"project_id": project["id"], "name": "x"}).status_code in (404, 405)


def test_release_version_crud(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Release Suite")
    create_res = admin_client.post("/api/agile/releases", json={
        "project_id": project["id"], "name": "v1.0",
    })
    assert create_res.status_code == 201, create_res.text
    version = create_res.json()
    upd_res = admin_client.put(f"/api/agile/releases/{version['id']}", json={
        "status": "released", "version": version["version"],
    })
    assert upd_res.status_code == 200, upd_res.text
    assert upd_res.json()["status"] == "released"


# --- Core reports ---

def _sprint_dates(days=7):
    from datetime import datetime, timedelta, timezone

    start = datetime.now(timezone.utc).date()
    return start.isoformat(), (start + timedelta(days=days - 1)).isoformat()


def test_sprint_report_and_burndown(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Report Suite")
    item = _create_story(admin_client, project["id"], "Reported item", story_points=8)
    start, end = _sprint_dates()
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Report Sprint", "start_date": start, "end_date": end,
    }).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})

    report_res = admin_client.get(f"/api/agile/reports/sprint/{sprint['id']}")
    assert report_res.status_code == 200, report_res.text
    report = report_res.json()
    assert report["committed_count"] == 1
    assert report["committed_estimate"] == 8
    assert [r["id"] for r in report["not_completed"]] == [item["id"]]

    burndown_res = admin_client.get(f"/api/agile/reports/burndown?sprint_id={sprint['id']}")
    assert burndown_res.status_code == 200, burndown_res.text
    burndown = burndown_res.json()
    first = burndown["points"][0]
    assert (first["remaining"], first["scope"], first["completed"]) == (8, 8, 0)
    guide = burndown["guideline"]
    assert guide[0]["value"] == 8 and guide[-1]["value"] == 0
    assert all(a["value"] >= b["value"] for a, b in zip(guide, guide[1:]))

    burnup_res = admin_client.get(f"/api/agile/reports/burnup?sprint_id={sprint['id']}")
    assert burnup_res.status_code == 200

    workload_res = admin_client.get(f"/api/agile/reports/workload?sprint_id={sprint['id']}")
    assert workload_res.status_code == 200

    scope_res = admin_client.get(f"/api/agile/reports/scope-change?sprint_id={sprint['id']}")
    assert scope_res.status_code == 200


def test_report_export_streams_csv(admin_client):
    """report export must stream CSV for every report type."""
    project, board_id = _setup_project_with_board(admin_client, "Export Suite")
    item = _create_story(admin_client, project["id"], "Exported item", story_points=5)
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Export Sprint", "start_date": "2026-02-01", "end_date": "2026-02-07",
    }).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})

    burndown_res = admin_client.post(
        f"/api/agile/reports/export?report_key=burndown&sprint_id={sprint['id']}"
    )
    assert burndown_res.status_code == 200, burndown_res.text
    assert burndown_res.headers["content-type"].startswith("text/csv")
    assert "attachment" in burndown_res.headers["content-disposition"]
    body = burndown_res.text.splitlines()
    assert body[0].split(",") == ["at", "remaining", "scope", "completed"]
    points = admin_client.get(f"/api/agile/reports/burndown?sprint_id={sprint['id']}").json()["points"]
    assert len(body) == 1 + len(points)

    velocity_res = admin_client.post(
        f"/api/agile/reports/export?report_key=velocity&board_id={board_id}"
    )
    assert velocity_res.status_code == 200

    bad_key_res = admin_client.post("/api/agile/reports/export?report_key=not-a-report")
    assert bad_key_res.status_code == 400

    missing_param_res = admin_client.post("/api/agile/reports/export?report_key=burndown")
    assert missing_param_res.status_code == 400
