"""Agile reports against hand-computed Jira numbers.

Each scenario performs real API actions and then pins every recorded change
to a known instant (the change log is the reports' only input), so the
expected series can be worked out by hand in the docstrings.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class Clock:
    """Back-dates everything recorded since the previous call."""

    def __init__(self) -> None:
        self.seen = self._max_id()

    @staticmethod
    def _max_id() -> int:
        from sqlalchemy import func

        from app.database import SessionLocal
        from app.models import WorkItemChange

        with SessionLocal() as db:
            return db.query(func.max(WorkItemChange.id)).scalar() or 0

    def at(self, when: str) -> None:
        from app.database import SessionLocal
        from app.models import Bug, WorkItemChange

        instant = _ts(when)
        with SessionLocal() as db:
            rows = db.query(WorkItemChange).filter(WorkItemChange.id > self.seen).all()
            for row in rows:
                row.changed_at = instant
                if row.field == "created":
                    db.get(Bug, row.work_item_id).created_at = instant
            db.commit()
        self.seen = self._max_id()


def _set_sprint_times(sprint_id, started=None, completed=None):
    from app.database import SessionLocal
    from app.models import Sprint

    with SessionLocal() as db:
        sprint = db.get(Sprint, sprint_id)
        if started:
            sprint.started_at = _ts(started)
        if completed:
            sprint.completed_at = _ts(completed)
        db.commit()


def _project(client, name):
    project = client.post("/api/projects", json={"name": name, "color": "#c9764f"}).json()
    board_id = client.post(f"/api/agile/projects/{project['id']}/enable", json={"feature_flags": {}}).json()["board_id"]
    return project, board_id


def _issue(client, project_id, title, points=None, item_type="Story", **extra):
    body = {"project_id": project_id, "title": title, "item_type": item_type, **extra}
    if points is not None:
        body["story_points"] = points
    res = client.post("/api/agile/work-items", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _status(client, item_id, status):
    item = client.get(f"/api/bugs/{item_id}").json()
    res = client.post(f"/api/agile/work-items/{item_id}/transition",
                      json={"to_status": status, "version": item["version"]})
    assert res.status_code == 200, res.text


def _estimate(client, item_id, points):
    item = client.get(f"/api/bugs/{item_id}").json()
    res = client.put(f"/api/agile/work-items/{item_id}/estimate",
                     json={"story_points": points, "version": item["version"]})
    assert res.status_code == 200, res.text


def _sprint(client, board_id, name, start, end):
    res = client.post(f"/api/agile/sprints?board_id={board_id}",
                      json={"name": name, "start_date": start, "end_date": end})
    assert res.status_code == 201, res.text
    return res.json()


def _start(client, sprint_id):
    sprint = client.get(f"/api/agile/sprints/{sprint_id}").json()
    res = client.post(f"/api/agile/sprints/{sprint_id}/start", json={"version": sprint["version"]})
    assert res.status_code == 200, res.text


def _complete(client, sprint_id):
    sprint = client.get(f"/api/agile/sprints/{sprint_id}").json()
    res = client.post(f"/api/agile/sprints/{sprint_id}/complete", json={"version": sprint["version"]})
    assert res.status_code == 200, res.text


@pytest.fixture
def two_week_sprint(admin_client):
    """Sprint 1, Mon 2 Mar - Fri 13 Mar 2026, board in UTC, Mon-Fri working.

    Committed at start: A (3), B (5), C (8)                   remaining 16
      Tue 3  10:00  A done                                    13  (-3)
      Wed 4  11:00  D (2) added                               15  (+2, scope)
      Thu 5  12:00  B removed                                 10  (-5, scope)
      Fri 6  13:00  C re-estimated 8 -> 13                    15  (+5, scope)
      Mon 9  09:00  A reopened                                18  (+3)
      Tue 10 09:00  A done again                              15  (-3)
      Wed 11 15:00  C done                                     2  (-13)
      Fri 13 17:00  sprint completed, D carried to the backlog
    """
    client = admin_client
    project, board_id = _project(client, "Report Math Suite")
    clock = Clock()
    a = _issue(client, project["id"], "Story A", 3)
    b = _issue(client, project["id"], "Story B", 5)
    c = _issue(client, project["id"], "Story C", 8)
    d = _issue(client, project["id"], "Story D", 2)
    sprint = _sprint(client, board_id, "Sprint 1", "2026-03-02", "2026-03-13")
    client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [a["id"], b["id"], c["id"]]})
    clock.at("2026-03-01T09:00:00")
    _start(client, sprint["id"])
    _set_sprint_times(sprint["id"], started="2026-03-02T09:00:00")
    clock.at("2026-03-02T09:00:00")
    _status(client, a["id"], "Done")
    clock.at("2026-03-03T10:00:00")
    client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [d["id"]]})
    clock.at("2026-03-04T11:00:00")
    client.delete(f"/api/agile/sprints/{sprint['id']}/items/{b['id']}")
    clock.at("2026-03-05T12:00:00")
    _estimate(client, c["id"], 13)
    clock.at("2026-03-06T13:00:00")
    _status(client, a["id"], "In Progress")
    clock.at("2026-03-09T09:00:00")
    _status(client, a["id"], "Done")
    clock.at("2026-03-10T09:00:00")
    _status(client, c["id"], "Done")
    clock.at("2026-03-11T15:00:00")
    _complete(client, sprint["id"])
    _set_sprint_times(sprint["id"], completed="2026-03-13T17:00:00")
    clock.at("2026-03-13T17:00:01")
    return {"client": client, "board_id": board_id, "sprint": sprint, "project": project,
            "a": a, "b": b, "c": c, "d": d}


def test_burndown_series_matches_jira(two_week_sprint):
    s = two_week_sprint
    report = s["client"].get(f"/api/agile/reports/burndown?sprint_id={s['sprint']['id']}").json()
    remaining = [p["remaining"] for p in report["points"]]
    assert remaining == [16, 13, 15, 10, 15, 18, 15, 2, 2]
    scope = [p["scope"] for p in report["points"]]
    assert scope == [16, 16, 18, 13, 18, 18, 18, 18, 18]
    completed = [p["completed"] for p in report["points"]]
    assert completed == [0, 3, 3, 3, 3, 0, 3, 16, 16]
    assert report["points"][0]["at"].startswith("2026-03-02T09:00:00")
    assert report["points"][-1]["at"].startswith("2026-03-13T17:00:00")
    events = [(e["event"], e["change"]) for e in report["events"]]
    assert events == [
        ("Issue completed", -3), ("Issue added to sprint", 2), ("Issue removed from sprint", -5),
        ("Estimate changed", 5), ("Issue reopened", 3), ("Issue completed", -3), ("Issue completed", -13),
    ]
    assert [e["scope_change"] for e in report["events"]] == [False, True, True, True, False, False, False]
    assert report["committed_estimate"] == 16 and report["remaining_estimate"] == 2


def test_guideline_is_flat_over_the_weekend(two_week_sprint):
    s = two_week_sprint
    report = s["client"].get(f"/api/agile/reports/burndown?sprint_id={s['sprint']['id']}").json()
    guide = {g["at"][:19]: g["value"] for g in report["guideline"]}
    assert report["guideline"][0]["value"] == 16 and report["guideline"][-1]["value"] == 0
    assert report["guideline"][-1]["at"].startswith("2026-03-14T00:00:00")
    assert guide["2026-03-07T00:00:00"] == guide["2026-03-09T00:00:00"]    # Sat + Sun flat
    assert guide["2026-03-06T00:00:00"] > guide["2026-03-07T00:00:00"]     # Friday burns
    assert report["non_working_days"] == ["2026-03-07", "2026-03-08"]
    # 9 full working days + the 15 working hours left on day one, burned evenly.
    per_day = 16 / (9 + 15 / 24)
    assert guide["2026-03-03T00:00:00"] == pytest.approx(16 - per_day * 15 / 24, abs=0.01)


def test_sprint_report_lists_match_jira(two_week_sprint):
    s = two_week_sprint
    report = s["client"].get(f"/api/agile/reports/sprint/{s['sprint']['id']}").json()
    assert (report["committed_count"], report["committed_estimate"]) == (3, 16)
    assert [r["id"] for r in report["completed"]] == [s["a"]["id"], s["c"]["id"]]
    assert report["completed_estimate"] == 16  # A 3 + C 13 (its estimate when completed)
    assert [(r["id"], r["added_during_sprint"]) for r in report["not_completed"]] == [(s["d"]["id"], True)]
    assert report["incomplete_estimate"] == 2
    assert [(r["id"], r["estimate"]) for r in report["removed"]] == [(s["b"]["id"], 5)]
    assert (report["added_count"], report["added_estimate"]) == (1, 2)
    assert report["estimate_change_count"] == 1
    c_row = next(r for r in report["completed"] if r["id"] == s["c"]["id"])
    assert (c_row["estimate_at_start"], c_row["estimate"]) == (8, 13)
    assert report["history_complete"] is True


def test_velocity_uses_commitment_at_start_and_completion_at_close(two_week_sprint):
    s = two_week_sprint
    client = s["client"]
    report = client.get(f"/api/agile/reports/velocity?board_id={s['board_id']}").json()
    assert [(p["sprint_name"], p["committed_estimate"], p["completed_estimate"]) for p in report["points"]] == [
        ("Sprint 1", 16, 16),
    ]
    assert report["average_completed"] == 16
    assert report["statistic_label"] == "Story points"


def test_scope_change_report(two_week_sprint):
    s = two_week_sprint
    entries = s["client"].get(f"/api/agile/reports/scope-change?sprint_id={s['sprint']['id']}").json()["entries"]
    assert [(e["event_type"], e["work_item_id"], e["change"]) for e in entries] == [
        ("added", s["d"]["id"], 2), ("removed", s["b"]["id"], -5), ("estimate_changed", s["c"]["id"], 5),
    ]
    assert all(e["actor_name"] == "Test Admin" for e in entries)


def test_daily_report_is_the_state_at_the_end_of_that_day(two_week_sprint):
    s = two_week_sprint
    daily = s["client"].get(
        f"/api/agile/reports/daily?sprint_id={s['sprint']['id']}&report_date=2026-03-05"
    ).json()
    # End of Thu 5 Mar: A done, C and D open, B already removed.
    assert sorted(r["work_item_id"] for r in daily["items"]) == sorted([s["a"]["id"], s["c"]["id"], s["d"]["id"]])
    assert (daily["done_count"], daily["todo_count"]) == (1, 2)
    assert (daily["completed_estimate"], daily["remaining_estimate"]) == (3, 10)
    assert daily["scope_removed_today"] == 1 and daily["scope_added_today"] == 0
    tuesday = s["client"].get(
        f"/api/agile/reports/daily?sprint_id={s['sprint']['id']}&report_date=2026-03-03"
    ).json()
    assert tuesday["completed_estimate_today"] == 3
    assert any(t["detail"] == "New → Done" for t in tuesday["transitions"])


def test_cumulative_flow_and_control_chart(admin_client):
    """X: created Sun 1 Mar 09:00, In Progress Mon 10:00, Testing Tue 10:00,
    Done Thu 10:00 -> 3 days in working columns, 4 days 1 hour lead time.
    Y: created Sun 1 Mar, never started."""
    client = admin_client
    project, board_id = _project(client, "Flow Suite")
    clock = Clock()
    x = _issue(client, project["id"], "Flow X")
    y = _issue(client, project["id"], "Flow Y")
    clock.at("2026-03-01T09:00:00")
    _status(client, x["id"], "In Progress")
    clock.at("2026-03-02T10:00:00")
    _status(client, x["id"], "Testing")
    clock.at("2026-03-03T10:00:00")
    _status(client, x["id"], "Done")
    clock.at("2026-03-05T10:00:00")

    cfd = client.get(
        f"/api/agile/reports/cumulative-flow?board_id={board_id}&date_from=2026-02-28&date_to=2026-03-05"
    ).json()
    names = {str(c["id"]): c["name"] for c in cfd["columns"]}
    by_day = {p["date"]: {names[k]: v for k, v in p["counts"].items() if v} for p in cfd["points"]}
    assert by_day["2026-02-28"] == {}
    assert by_day["2026-03-01"] == {"To Do": 2}
    assert by_day["2026-03-02"] == {"To Do": 1, "In Progress": 1}
    assert by_day["2026-03-03"] == {"To Do": 1, "Testing": 1}
    assert by_day["2026-03-04"] == {"To Do": 1, "Testing": 1}
    assert by_day["2026-03-05"] == {"To Do": 1, "Done": 1}

    chart = client.get(
        f"/api/agile/reports/control-chart?board_id={board_id}&date_from=2026-03-01&date_to=2026-03-31"
    ).json()
    assert chart["work_columns"] == ["In Progress", "Testing"]
    assert [(e["work_item_id"], e["cycle_time_days"], e["lead_time_days"]) for e in chart["entries"]] == [
        (x["id"], 3.0, 4.04),
    ]
    assert chart["average_days"] == 3.0 and chart["count"] == 1
    assert y["id"] not in [e["work_item_id"] for e in chart["entries"]]


def test_epic_report_shows_progress_per_sprint(admin_client):
    client = admin_client
    project, board_id = _project(client, "Epic Report Suite")
    epic = _issue(client, project["id"], "Checkout epic", item_type="Epic")
    one = _issue(client, project["id"], "Pay", 5, epic_id=epic["id"])
    two = _issue(client, project["id"], "Refund", 3, epic_id=epic["id"])
    _issue(client, project["id"], "Unrelated", 8)
    sprint = _sprint(client, board_id, "Epic Sprint", "2026-04-06", "2026-04-17")
    client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [one["id"], two["id"]]})
    _start(client, sprint["id"])
    _status(client, one["id"], "Done")
    _complete(client, sprint["id"])
    report = client.get(f"/api/agile/reports/epic/{epic['id']}").json()
    assert report["progress"]["child_count"] == 2 and report["progress"]["completed_child_count"] == 1
    assert (report["progress"]["total_estimate"], report["progress"]["completed_estimate"]) == (8, 5)
    assert [r["id"] for r in report["done"]] == [one["id"]]
    assert [r["id"] for r in report["todo"]] == [two["id"]]
    assert [(r["sprint_name"], r["completed_estimate"], r["scope_estimate"], r["remaining_estimate"])
            for r in report["sprints"]] == [("Epic Sprint", 5, 8, 3)]


def test_time_statistic_counts_original_estimate_hours(admin_client):
    client = admin_client
    project, board_id = _project(client, "Time Stat Suite")
    board = client.get(f"/api/agile/boards/{board_id}").json()
    res = client.put(f"/api/agile/boards/{board_id}", json={"estimation_mode": "time", "version": board["version"]})
    assert res.status_code == 200, res.text
    item = _issue(client, project["id"], "Timed")
    res = client.put(f"/api/agile/work-items/{item['id']}/estimate",
                     json={"original_estimate_minutes": 150, "version": item["version"]})
    assert res.status_code == 200, res.text
    sprint = _sprint(client, board_id, "Time Sprint", "2026-04-06", "2026-04-17")
    client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [item["id"]]})
    _start(client, sprint["id"])
    report = client.get(f"/api/agile/reports/sprint/{sprint['id']}").json()
    assert report["committed_estimate"] == 2.5
    assert report["statistic_label"] == "Original time estimate (hours)"


def test_subtasks_never_count_towards_sprint_estimates(admin_client):
    client = admin_client
    project, board_id = _project(client, "Subtask Stat Suite")
    parent = _issue(client, project["id"], "Parent", 5)
    # The API refuses points on a Sub-task; data from before that rule may
    # still carry some, and the reports must ignore them.
    res = client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Child", "item_type": "Sub-task",
        "parent_id": parent["id"], "story_points": 3,
    })
    assert res.status_code == 422, res.text
    child = _issue(client, project["id"], "Child", item_type="Sub-task", parent_id=parent["id"])
    from sqlalchemy import update

    from app.database import SessionLocal
    from app.models import Bug

    with SessionLocal() as db:
        db.execute(update(Bug).where(Bug.id == child["id"]).values(story_points=3))
        db.commit()
    sprint = _sprint(client, board_id, "Stat Sprint", "2026-04-06", "2026-04-17")
    client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [parent["id"]]})
    _start(client, sprint["id"])
    report = client.get(f"/api/agile/reports/sprint/{sprint['id']}").json()
    assert (report["committed_count"], report["committed_estimate"]) == (1, 5)
