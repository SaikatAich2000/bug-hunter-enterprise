"""Basic sanity tests for backlog ranking and the sprint lifecycle
(create/start/complete/cancel), plus hierarchy assignment.
"""
from __future__ import annotations


def _setup_project_with_board(admin_client, name="Sprint Suite"):
    res = admin_client.post("/api/projects", json={"name": name, "description": "", "color": "#c9764f"})
    assert res.status_code == 201, res.text
    project = res.json()
    enable_res = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable_res.status_code == 200, enable_res.text
    return project, enable_res.json()["board_id"]


def _create_story(admin_client, project_id, title):
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project_id, "title": title, "item_type": "story",
    })
    assert res.status_code == 201, res.text
    return res.json()


def test_backlog_lists_new_items_ranked(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Backlog Suite")
    a = _create_story(admin_client, project["id"], "Story A")
    b = _create_story(admin_client, project["id"], "Story B")

    res = admin_client.get(f"/api/agile/boards/{board_id}/backlog")
    assert res.status_code == 200
    ids = [i["id"] for i in res.json()]
    assert a["id"] in ids
    assert b["id"] in ids
    # Creation order preserved by rank.
    assert ids.index(a["id"]) < ids.index(b["id"])


def test_backlog_rank_reorder(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Rank Suite")
    a = _create_story(admin_client, project["id"], "Story A")
    b = _create_story(admin_client, project["id"], "Story B")

    # Move B before A.
    res = admin_client.post(f"/api/agile/boards/{board_id}/rank", json={
        "item_id": b["id"], "before_id": None, "after_id": a["id"],
    })
    assert res.status_code == 200, res.text

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    ids = [i["id"] for i in backlog]
    assert ids.index(b["id"]) < ids.index(a["id"])


def test_sprint_full_lifecycle_start_complete(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Lifecycle Suite")
    item = _create_story(admin_client, project["id"], "Do the thing")

    sprint_res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint 1", "goal": "Ship it",
    })
    assert sprint_res.status_code == 201, sprint_res.text
    sprint = sprint_res.json()
    assert sprint["state"] == "future"

    add_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    assert add_res.status_code == 200, add_res.text

    start_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    assert start_res.status_code == 200, start_res.text
    started = start_res.json()
    assert started["state"] == "active"

    history_res = admin_client.get(f"/api/agile/sprints/{sprint['id']}/history")
    assert history_res.status_code == 200
    assert any(h["event_type"] == "committed" for h in history_res.json())

    complete_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/complete", json={
        "closing_note": "done for now", "version": started["version"],
        "default_destination": "backlog",
    })
    assert complete_res.status_code == 200, complete_res.text
    assert complete_res.json()["state"] == "closed"


def test_sprint_cannot_start_empty(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Empty Sprint Suite")
    sprint_res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Empty Sprint"})
    sprint = sprint_res.json()
    start_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]})
    assert start_res.status_code == 409


def test_sprint_cancel_returns_items_to_backlog(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Cancel Suite")
    item = _create_story(admin_client, project["id"], "Cancel me maybe")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint X"}).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})

    cancel_res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/cancel", json={
        "cancellation_note": "changed plans", "version": sprint["version"],
        "default_destination": "backlog",
    })
    assert cancel_res.status_code == 200, cancel_res.text
    assert cancel_res.json()["state"] == "cancelled"

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    assert any(i["id"] == item["id"] for i in backlog)


def test_start_sprint_idempotency_key_replays(admin_client):
    project, board_id = _setup_project_with_board(admin_client, "Idempotent Sprint Suite")
    item = _create_story(admin_client, project["id"], "Idempotent item")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint Idem"}).json()
    admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})

    headers = {"Idempotency-Key": "test-key-123"}
    r1 = admin_client.post(
        f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]}, headers=headers,
    )
    assert r1.status_code == 200
    r2 = admin_client.post(
        f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]}, headers=headers,
    )
    assert r2.status_code == 200
    assert r1.json() == r2.json()


def test_hierarchy_subtask_requires_level2_parent(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Hierarchy Suite")
    story = _create_story(admin_client, project["id"], "Parent story")

    sub_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Sub work", "item_type": "sub-task",
        "parent_id": story["id"],
    })
    assert sub_res.status_code == 201, sub_res.text
    assert sub_res.json()["id"] is not None

    bad_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Orphan subtask", "item_type": "sub-task",
    })
    assert bad_res.status_code == 422


def test_legacy_bugs_endpoint_rejects_agile_types(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Legacy Guard Suite")
    res = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Should be rejected", "item_type": "Epic",
    })
    assert res.status_code == 422


def test_bulk_sprint_generation_weekly_one_month(admin_client):
    """A one-month range with weekly cadence must create the correct number of
    sprints (preview count == created count), and the response shape must be
    {"created": [...]} — not a bare SprintOut dict."""
    project, board_id = _setup_project_with_board(admin_client, "Bulk Weekly Suite")
    periods = [
        {"start_date": "2026-01-01", "end_date": "2026-01-07"},
        {"start_date": "2026-01-08", "end_date": "2026-01-14"},
        {"start_date": "2026-01-15", "end_date": "2026-01-21"},
        {"start_date": "2026-01-22", "end_date": "2026-01-28"},
        {"start_date": "2026-01-29", "end_date": "2026-01-31"},
    ]
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint", "goal": "Bulk", "cadence": "weekly",
        "generate": True, "periods": periods,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert "created" in body, f"expected bulk response envelope, got: {body}"
    assert len(body["created"]) == len(periods)
    # Each created sprint carries the right period dates.
    for i, created in enumerate(body["created"]):
        assert created["start_date"] == periods[i]["start_date"]
        assert created["end_date"] == periods[i]["end_date"]
        assert created["state"] == "future"


def test_bulk_sprint_generation_atomic_rollback(admin_client):
    """A duplicate name in the middle of a bulk batch must roll back the whole
    series — no partial sprints left behind."""
    project, board_id = _setup_project_with_board(admin_client, "Atomic Suite")
    # Pre-create a sprint whose name collides with the 2nd generated one.
    admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Atomic 2", "goal": "pre-existing",
    })
    periods = [
        {"start_date": "2026-02-01", "end_date": "2026-02-07"},
        {"start_date": "2026-02-08", "end_date": "2026-02-14"},
    ]
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Atomic", "goal": "Bulk", "cadence": "weekly",
        "generate": True, "periods": periods,
    })
    assert res.status_code == 409, res.text
    # Only the pre-existing sprint should remain — none from the failed batch.
    sprints = admin_client.get(f"/api/agile/sprints?board_id={board_id}").json()
    assert len(sprints) == 1
    assert sprints[0]["name"] == "Atomic 2"


def test_single_sprint_response_unchanged(admin_client):
    """Single-sprint mode (no generate flag) must still return a bare SprintOut
    dict — backward compatibility for existing callers."""
    project, board_id = _setup_project_with_board(admin_client, "Single Suite")
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint One", "goal": "Single", "start_date": "2026-03-01", "end_date": "2026-03-07",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert "id" in body
    assert "created" not in body
    assert body["name"] == "Sprint One"


def test_sprint_generate_intervals_weekly_one_month():
    """Static interval calculation: a one-month inclusive range with weekly
    cadence must yield the correct number of periods."""
    from datetime import datetime as dt

    from app.schemas import SprintCreateIn

    start = dt(2026, 1, 1)
    end = dt(2026, 1, 31)
    intervals = SprintCreateIn.generate_intervals(start, end, "weekly", 5)
    assert len(intervals) == 5
    # First interval starts on the range start.
    assert intervals[0][0] == start
    # Last interval ends exactly on the range end.
    assert intervals[-1][1] == end
    # Intervals are contiguous (no gaps, no overlaps beyond shared boundary).
    for i in range(1, len(intervals)):
        assert intervals[i][0] >= intervals[i - 1][1]


def test_sprint_generate_intervals_daily():
    """Daily cadence over a 4-day range must yield exactly 4 one-day intervals."""
    from datetime import datetime as dt
    from datetime import timedelta

    from app.schemas import SprintCreateIn

    start = dt(2026, 6, 1)
    end = dt(2026, 6, 5)  # 4-day span (Jun 5 - Jun 1 = 4 days)
    intervals = SprintCreateIn.generate_intervals(start, end, "daily", 4)
    assert len(intervals) == 4
    assert intervals[0][0] == start
    assert intervals[-1][1] == end
    # Each interval spans roughly one day (within a few seconds of rounding).
    for lo, hi in intervals:
        assert timedelta(hours=23, minutes=59) <= (hi - lo) <= timedelta(days=1, seconds=1)


def test_sprint_generate_intervals_invalid_inputs():
    """Invalid inputs must raise ValueError, not silently produce garbage."""
    from datetime import datetime as dt

    import pytest

    from app.schemas import SprintCreateIn

    bad_inputs = [
        (None, dt(2026, 1, 31), "weekly", 4),
        (dt(2026, 1, 1), None, "weekly", 4),
        (dt(2026, 1, 1), dt(2026, 1, 31), "weekly", 0),
        (dt(2026, 1, 31), dt(2026, 1, 1), "weekly", 4),
    ]
    for start, end, cadence, periods in bad_inputs:
        with pytest.raises(ValueError):
            SprintCreateIn.generate_intervals(start, end, cadence, periods)


def test_bulk_sprint_validation_requires_periods(admin_client):
    """generate=True without periods must return 422."""
    project, board_id = _setup_project_with_board(admin_client, "Validation Suite")
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint", "goal": "Bulk", "cadence": "weekly", "generate": True,
    })
    assert res.status_code == 422, res.text


def test_bulk_sprint_validation_rejects_bad_period_dates(admin_client):
    """Periods with end_date before start_date must return 422."""
    project, board_id = _setup_project_with_board(admin_client, "Bad Period Suite")
    res = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={
        "name": "Sprint", "goal": "Bulk", "cadence": "weekly",
        "generate": True,
        "periods": [{"start_date": "2026-01-07", "end_date": "2026-01-01"}],
    })
    assert res.status_code == 422, res.text


def test_epic_update_includes_target_date(admin_client):
    """Epic update must persist target_date and start_date so the edit form
    can round-trip values back to the API."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Date Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Epic With Dates", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()

    update_res = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "title": "Epic Updated", "description": "Updated",
        "start_date": "2026-04-01", "target_date": "2026-04-30",
        "version": epic["version"],
    })
    assert update_res.status_code == 200, update_res.text
    updated = update_res.json()
    assert updated["title"] == "Epic Updated"
    assert updated["start_date"] == "2026-04-01"
    assert updated["target_date"] == "2026-04-30"
    assert updated["display_id"] == f"EPIC-{epic['id']}"

    backwards = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "start_date": "2026-05-01", "target_date": "2026-04-01", "version": updated["version"],
    })
    assert backwards.status_code == 422, backwards.text


def test_epics_are_never_planned_into_sprints(admin_client):
    """Jira plans issues, not Epics: an Epic cannot be created in, added to,
    or updated into a sprint, whatever the route."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Sprint Suite")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board_id}", json={"name": "Sprint A"}).json()

    created = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Epic in sprint", "item_type": "epic",
        "sprint_id": sprint["id"],
    })
    assert created.status_code == 422, created.text

    epic = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Plain epic", "item_type": "epic",
    }).json()
    added = admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [epic["id"]]})
    assert added.status_code == 409, added.text
    assert "Epics are not planned" in added.json()["detail"]

    # A sprint_id sent to the epic update is not part of the contract and is ignored.
    updated = admin_client.put(f"/api/agile/epics/{epic['id']}", json={
        "sprint_id": sprint["id"], "version": 1,
    })
    assert updated.status_code == 200, updated.text
    item = admin_client.get(f"/api/bugs/{epic['id']}").json()
    assert item["sprint_id"] is None


def test_retired_hierarchy_transition_routes_are_gone(admin_client):
    project, _board_id = _setup_project_with_board(admin_client, "Retired Transition Suite")
    for path in ("/api/agile/collections/1/transition", "/api/agile/features/1/transition"):
        res = admin_client.post(path, json={"to_status": "In Progress", "version": 1})
        assert res.status_code in (404, 405), res.text


def test_epic_transition_via_work_items_endpoint(admin_client):
    """Epic (stored in Bug table) must transition via the work-items endpoint."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Transition Suite")
    epic_res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Drag Epic", "item_type": "epic",
    })
    assert epic_res.status_code == 201, epic_res.text
    epic = epic_res.json()

    # Epic is a Bug row, so it uses the work-items endpoint
    res = admin_client.post(f"/api/agile/work-items/{epic['id']}/transition", json={
        "to_status": "In Progress", "version": epic["version"],
    })
    assert res.status_code == 200, res.text
    updated = res.json()
    assert updated["id"] == epic["id"]


# ---------------------------------------------------------------------------
# Focused regression tests for the four defects:
#   1. Single Task Type   2. Universal Status Support
#   3. False Concurrency  4. Unrestricted Status Movement
# ---------------------------------------------------------------------------


def _create_item(client, project_id, title, item_type, **extra):
    """Create a work item. Agile hierarchy types (Epic/Story/Sub-task) go
    through /api/agile/work-items; legacy types (Bug/Requirement/Task) go
    through /api/bugs. Returns the created item dict."""
    # Normalize to canonical case for the agile endpoint (which validates
    # against AGILE_ITEM_TYPES exactly); the bugs endpoint is case-insensitive.
    canonical = {"epic": "Epic", "story": "Story", "sub-task": "Sub-task"}.get(
        item_type.lower(), item_type,
    )
    payload = {"project_id": project_id, "title": title, "item_type": canonical}
    payload.update(extra)
    if canonical in ("Epic", "Story", "Sub-task"):
        res = client.post("/api/agile/work-items", json=payload)
    else:
        res = client.post("/api/bugs", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


def test_task_and_sub_task_each_use_their_own_workflow(admin_client):
    """Task (a standard issue) and Sub-task are different types: each resolves
    to its own workflow statuses, and a Task moves on the board."""
    from app.agile.workflow import canonical_work_item_type, get_status_by_value
    from app.database import SessionLocal

    assert canonical_work_item_type("task") == "Task"
    assert canonical_work_item_type("Sub-task") == "Sub-task"
    with SessionLocal() as db:
        task_row = get_status_by_value(db, "Task", "In Progress")
        sub_row = get_status_by_value(db, "Sub-task", "In Progress")
        assert task_row is not None and sub_row is not None
        assert (task_row.work_item_type, sub_row.work_item_type) == ("Task", "Sub-task")
    project, board_id = _setup_project_with_board(admin_client, "Task Workflow Suite")
    task = _create_item(admin_client, project["id"], "A task", "Task")
    res = admin_client.post(f"/api/agile/work-items/{task['id']}/transition", json={
        "to_status": "In Progress", "version": task["version"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["item_type"] == "Task"


def test_single_task_type_status_vocabularies_match(admin_client):
    """Defect 1: 'Task' and 'Sub-task' must share one canonical status set."""
    from app.schemas import STATUSES_BY_TYPE
    assert STATUSES_BY_TYPE["Task"] == STATUSES_BY_TYPE["Sub-task"]


def test_universal_status_epic_can_reach_testing(admin_client):
    """Defect 2: Epic must accept 'Testing' (previously raised TransitionError)."""
    project, board_id = _setup_project_with_board(admin_client, "Epic Testing Suite")
    epic = _create_item(admin_client, project["id"], "Epic Under Test", "Epic")

    res = admin_client.post(f"/api/agile/work-items/{epic['id']}/transition", json={
        "to_status": "Testing", "version": epic["version"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "Testing"


def test_universal_status_matrix(admin_client):
    """Defect 2: every configured status must be reachable for every item type."""
    from app.schemas import CANONICAL_STATUSES, STATUSES_BY_TYPE
    for item_type, statuses in STATUSES_BY_TYPE.items():
        for status in CANONICAL_STATUSES:
            assert status in statuses, f"{status} missing for {item_type}"


def test_unrestricted_status_movement_requirement_out_of_done(admin_client):
    """Defect 4: a Requirement in Done must be movable to every other status."""
    project, board_id = _setup_project_with_board(admin_client, "Requirement Move Suite")
    req = _create_item(admin_client, project["id"], "Movable Requirement", "requirement")

    # Move Requirement to Done first.
    res = admin_client.post(f"/api/agile/work-items/{req['id']}/transition", json={
        "to_status": "Done", "version": req["version"],
    })
    assert res.status_code == 200, res.text
    done_version = res.json()["version"]

    # Now move it out of Done to several other statuses.
    for target in ("In Progress", "Testing", "New", "Blocked"):
        res = admin_client.post(f"/api/agile/work-items/{req['id']}/transition", json={
            "to_status": target, "version": done_version,
        })
        assert res.status_code == 200, f"Done -> {target} failed: {res.text}"
        done_version = res.json()["version"]


def test_unrestricted_status_movement_all_types_leave_done(admin_client):
    """Defect 4: every item type must be able to move out of Done.

    All work-item types are stored in the bugs table, so they share the
    work-items transition endpoint regardless of which create path was used."""
    project, board_id = _setup_project_with_board(admin_client, "All Types Move Suite")
    item_types = ["story", "bug", "epic", "requirement"]
    for item_type in item_types:
        item = _create_item(admin_client, project["id"], f"Move {item_type}", item_type)
        res = admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
            "to_status": "Done", "version": item["version"],
        })
        assert res.status_code == 200, f"{item_type} -> Done failed: {res.text}"
        done_version = res.json()["version"]

        res = admin_client.post(f"/api/agile/work-items/{item['id']}/transition", json={
            "to_status": "In Progress", "version": done_version,
        })
        assert res.status_code == 200, f"{item_type} Done -> In Progress failed: {res.text}"


def test_concurrency_rapid_repeated_moves(admin_client):
    """Defect 3: rapid sequential moves must not raise a false 409."""
    project, board_id = _setup_project_with_board(admin_client, "Rapid Move Suite")
    story = _create_item(admin_client, project["id"], "Rapid Story", "story")
    version = story["version"]

    # Issue three transitions back-to-back using the version returned by each
    # response. This simulates fast drag-and-drop without waiting for a full
    # page refresh between moves.
    path = ["In Progress", "Testing", "Done"]
    for target in path:
        res = admin_client.post(f"/api/agile/work-items/{story['id']}/transition", json={
            "to_status": target, "version": version,
        })
        assert res.status_code == 200, f"Move to {target} failed: {res.text}"
        version = res.json()["version"]

    # Final state must be Done and persisted.
    final = admin_client.get(f"/api/bugs/{story['id']}").json()
    assert final["status"] == "Done"


def test_settings_self_heals_agile_enabled_without_board(admin_client):
    """Regression: a project flagged agile_enabled=True with no default board
    (e.g. after a database reset that dropped board rows) made the Sprint Board
    and Sprint Items tabs render nothing at all — the settings gate
    (`agile_enabled && board_id`) failed and the panels returned null. The
    settings endpoint must report a consistent, recoverable state instead.
    """
    from sqlalchemy import delete, select

    from app.database import SessionLocal
    from app.models import Board, BoardColumn, BoardColumnStatus

    project, board_id = _setup_project_with_board(admin_client, "Heal Suite")

    # Simulate the corrupted state directly: board rows gone, flag still set.
    with SessionLocal() as db:
        column_ids = select(BoardColumn.id).where(BoardColumn.board_id == board_id)
        db.execute(delete(BoardColumnStatus).where(BoardColumnStatus.board_column_id.in_(column_ids)))
        db.execute(delete(BoardColumn).where(BoardColumn.board_id == board_id))
        db.execute(delete(Board).where(Board.id == board_id))
        db.commit()

    res = admin_client.get(f"/api/agile/projects/{project['id']}/settings")
    assert res.status_code == 200, res.text
    body = res.json()
    # The impossible combination must be healed to a recoverable one, so the
    # UI shows the "Agile is not enabled / Enable Agile" panel instead of blank.
    assert body["agile_enabled"] is False
    assert body["board_id"] is None

    # And the project can be re-enabled cleanly afterwards.
    enable = admin_client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}})
    assert enable.status_code == 200, enable.text
    assert enable.json()["agile_enabled"] is True
    assert enable.json()["board_id"] is not None

