"""Adversarial Jira scenarios for the agile reports, from the independent
reports audit. Each test asserts the number Jira would show, worked out by
hand (Clock back-dates the change log; see test_agile_reports_jira.py)."""
from __future__ import annotations

import csv
import io

from tests.test_agile_reports_jira import (
    Clock,
    _complete,
    _estimate,
    _issue,
    _project,
    _set_sprint_times,
    _sprint,
    _start,
    _status,
)


def _sr(c, sid):
    return c.get(f"/api/agile/reports/sprint/{sid}").json()


def _bd(c, sid):
    return c.get(f"/api/agile/reports/burndown?sprint_id={sid}").json()


def _vel(c, bid):
    return c.get(f"/api/agile/reports/velocity?board_id={bid}").json()


def _add(c, sid, *ids):
    r = c.post(f"/api/agile/sprints/{sid}/items", json={"item_ids": list(ids)})
    assert r.status_code == 200, r.text


def _remove(c, sid, iid):
    r = c.delete(f"/api/agile/sprints/{sid}/items/{iid}")
    assert r.status_code in (200, 204), r.text


# ---------------------------------------------------------------- S1
def test_s1_completed_outside_excluded_from_completed_and_velocity(admin_client):
    """A(3) committed, done Thu.  E(5) Done on Sun 1 Mar (before), added Wed 4.
    Jira: Completed issues = A, total 3; 'completed outside' = E (5) listed
    separately and NOT in the Completed total nor in velocity completed."""
    c = admin_client
    p, bid = _project(c, "S1")
    clock = Clock()
    a = _issue(c, p["id"], "Issue A", 3)
    e = _issue(c, p["id"], "Issue E", 5)
    s = _sprint(c, bid, "S1 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"])
    _status(c, e["id"], "Done")
    clock.at("2026-03-01T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T09:00:00.250000")
    clock.at("2026-03-02T09:00:00.250000")
    _add(c, s["id"], e["id"])
    clock.at("2026-03-04T11:00:00")
    _status(c, a["id"], "Done")
    clock.at("2026-03-05T11:00:00")
    _complete(c, s["id"])
    _set_sprint_times(s["id"], completed="2026-03-13T17:00:00")
    clock.at("2026-03-13T17:00:01")

    rep = _sr(c, s["id"])
    vel = _vel(c, bid)["points"]
    assert [r["id"] for r in rep["completed_outside"]] == [e["id"]]
    assert [r["id"] for r in rep["completed"]] == [a["id"]]
    # Jira: header total equals the Completed table total
    assert rep["completed_count"] == 1, rep["completed_count"]
    assert rep["completed_estimate"] == 3, rep["completed_estimate"]
    assert [(v["committed_estimate"], v["completed_estimate"]) for v in vel] == [(3, 3)]


# ---------------------------------------------------------------- S2
def test_s2_outside_issue_reopened_and_recompleted_counts_in_sprint(admin_client):
    """E(5) Done before; added Wed; reopened Thu; Done again Fri 6.
    Jira: E was completed during the sprint -> Completed issues [E], 5;
    nothing completed outside."""
    c = admin_client
    p, bid = _project(c, "S2")
    clock = Clock()
    a = _issue(c, p["id"], "Issue A", 3)
    e = _issue(c, p["id"], "Issue E", 5)
    s = _sprint(c, bid, "S2 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"])
    _status(c, e["id"], "Done")
    clock.at("2026-03-01T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T09:00:00")
    clock.at("2026-03-02T09:00:00")
    _add(c, s["id"], e["id"])
    clock.at("2026-03-04T11:00:00")
    _status(c, e["id"], "In Progress")
    clock.at("2026-03-05T11:00:00")
    _status(c, e["id"], "Done")
    clock.at("2026-03-06T11:00:00")
    _complete(c, s["id"])
    _set_sprint_times(s["id"], completed="2026-03-13T17:00:00")
    clock.at("2026-03-13T17:00:01")
    rep = _sr(c, s["id"])
    bd = _bd(c, s["id"])
    assert [r["id"] for r in rep["completed"]] == [e["id"]]
    assert rep["completed_outside"] == []
    # Added already done: no burn. Reopened: +5 of work. Done again: -5.
    assert [x["remaining"] for x in bd["points"]] == [3, 8, 3, 3]
    assert (bd["points"][-1]["scope"], bd["points"][-1]["completed"]) == (8, 5)


# ---------------------------------------------------------------- S3
def test_s3_estimate_before_start_and_remove_readd(admin_client):
    """A 3->8 on Sun (before start).  B(5) committed.
    Tue 10:00 B removed (13->8), Tue 12:00 B 5->2 while out (no sprint event),
    Wed 10:00 B re-added (8->10).  Jira: committed 13 (A 8 + B 5),
    no removed issue, B not marked added (it was committed), remaining 10."""
    c = admin_client
    p, bid = _project(c, "S3")
    clock = Clock()
    a = _issue(c, p["id"], "Issue A", 3)
    b = _issue(c, p["id"], "Issue B", 5)
    s = _sprint(c, bid, "S3 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"], b["id"])
    clock.at("2026-02-27T09:00:00")
    _estimate(c, a["id"], 8)
    clock.at("2026-03-01T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T09:00:00")
    clock.at("2026-03-02T09:00:00")
    _remove(c, s["id"], b["id"])
    clock.at("2026-03-03T10:00:00")
    _estimate(c, b["id"], 2)
    clock.at("2026-03-03T12:00:00")
    _add(c, s["id"], b["id"])
    clock.at("2026-03-04T10:00:00")
    rep = _sr(c, s["id"])
    bd = _bd(c, s["id"])
    rem = [pt["remaining"] for pt in bd["points"]]
    assert rep["committed_estimate"] == 13
    assert rep["added_count"] == 0 and rep["removed_count"] == 0
    assert rep["estimate_change_count"] == 0
    assert rem[:3] == [13, 8, 10] and rem[-1] == 10
    assert rep["incomplete_estimate"] == 10


# ---------------------------------------------------------------- S4
def test_s4_moved_between_parallel_sprints(admin_client):
    """X: A(3),B(5); Y: C(2); both start Mon 09:00.  Tue B moved X->Y,
    Wed B done.  Complete both Fri.
    Jira X: committed 8, removed B(5), completed 0, not completed A 3.
    Jira Y: committed 2, added B, completed B 5, not completed C 2.
    Velocity: X (8,0), Y (2,5)."""
    c = admin_client
    project = c.post("/api/projects", json={"name": "S4", "color": "#c9764f"}).json()
    bid = c.post(f"/api/agile/projects/{project['id']}/enable",
                 json={"feature_flags": {"parallel_sprints": True}}).json()["board_id"]
    clock = Clock()
    a = _issue(c, project["id"], "Issue A", 3)
    b = _issue(c, project["id"], "Issue B", 5)
    cc = _issue(c, project["id"], "Issue C", 2)
    x = _sprint(c, bid, "SX", "2026-03-02", "2026-03-13")
    y = _sprint(c, bid, "SY", "2026-03-02", "2026-03-13")
    _add(c, x["id"], a["id"], b["id"])
    _add(c, y["id"], cc["id"])
    clock.at("2026-03-01T09:00:00")
    _start(c, x["id"])
    _start(c, y["id"])
    _set_sprint_times(x["id"], started="2026-03-02T09:00:00")
    _set_sprint_times(y["id"], started="2026-03-02T09:00:00")
    clock.at("2026-03-02T09:00:00")
    _add(c, y["id"], b["id"])
    clock.at("2026-03-03T10:00:00")
    _status(c, b["id"], "Done")
    clock.at("2026-03-04T10:00:00")
    _complete(c, x["id"])
    _set_sprint_times(x["id"], completed="2026-03-13T17:00:00")
    clock.at("2026-03-13T17:00:01")
    _complete(c, y["id"])
    _set_sprint_times(y["id"], completed="2026-03-13T17:30:00")
    clock.at("2026-03-13T17:30:01")
    rx, ry = _sr(c, x["id"]), _sr(c, y["id"])
    vel = [(v["sprint_name"], v["committed_estimate"], v["completed_estimate"]) for v in _vel(c, bid)["points"]]
    assert (rx["committed_estimate"], rx["completed_estimate"], rx["incomplete_estimate"]) == (8, 0, 3)
    assert (rx["removed_count"], rx["removed_estimate"]) == (1, 5)
    assert (ry["committed_estimate"], ry["completed_estimate"], ry["incomplete_estimate"]) == (2, 5, 2)
    assert (ry["added_count"], ry["added_estimate"]) == (1, 5)
    assert vel == [("SX", 8, 0), ("SY", 2, 5)]


# ---------------------------------------------------------------- S5
def test_s5_board_timezone_dst_and_subsecond_start(admin_client):
    """Board America/New_York, Mon-Fri.  Sprint 2-13 Mar 2026, started
    2026-03-02T14:00:00.750Z (09:00:00.75 EST).  DST starts Sun 8 Mar.
    Jira: guideline ends at local midnight 14 Mar = 04:00Z; Sat 7 + Sun 8
    flat: value at 2026-03-07T05:00Z == value at 2026-03-09T04:00Z;
    non-working days ['2026-03-07','2026-03-08'].  A(10) done 2026-03-03T03:00Z
    (Mon 22:00 local) -> daily report for local Mon 2 Mar shows completed 10."""
    c = admin_client
    p, bid = _project(c, "S5")
    board = c.get(f"/api/agile/boards/{bid}").json()
    r = c.put(f"/api/agile/boards/{bid}", json={"timezone": "America/New_York", "version": board["version"]})
    assert r.status_code == 200, r.text
    clock = Clock()
    a = _issue(c, p["id"], "Issue A", 10)
    b = _issue(c, p["id"], "Issue B", 6)
    s = _sprint(c, bid, "S5 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"], b["id"])
    clock.at("2026-03-01T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T14:00:00.750000")
    clock.at("2026-03-02T14:00:00.750000")
    _status(c, a["id"], "Done")
    clock.at("2026-03-03T03:00:00")
    bd = _bd(c, s["id"])
    g = {x["at"][:19]: x["value"] for x in bd["guideline"]}
    assert bd["planned_end"].startswith("2026-03-14T04:00:00")
    assert bd["guideline"][-1]["at"].startswith("2026-03-14T04:00:00")
    assert g["2026-03-07T05:00:00"] == g["2026-03-09T04:00:00"]
    assert g["2026-03-06T05:00:00"] > g["2026-03-07T05:00:00"]
    assert bd["non_working_days"] == ["2026-03-07", "2026-03-08"]
    assert bd["points"][0]["remaining"] == 16
    d = c.get(f"/api/agile/reports/daily?sprint_id={s['id']}&report_date=2026-03-02").json()
    assert d["completed_estimate"] == 10 and d["completed_estimate_today"] == 10
    d3 = c.get(f"/api/agile/reports/daily?sprint_id={s['id']}&report_date=2026-03-03").json()
    assert d3["completed_estimate_today"] == 0


# ---------------------------------------------------------------- S6
def test_s6_control_chart_reopened_issue_is_one_dot(admin_client):
    """X: In Progress Mon 2 10:00, Done Tue 3 10:00, In Progress Wed 4 10:00,
    Done Fri 6 10:00.  Jira control chart: ONE dot for X, completed at the last
    entry into Done (6 Mar); cycle = first In Progress -> last Done = 4.0 d
    (time-in-columns variant: 3.0 d)."""
    c = admin_client
    p, bid = _project(c, "S6")
    clock = Clock()
    x = _issue(c, p["id"], "Issue X")
    clock.at("2026-03-01T09:00:00")
    _status(c, x["id"], "In Progress")
    clock.at("2026-03-02T10:00:00")
    _status(c, x["id"], "Done")
    clock.at("2026-03-03T10:00:00")
    _status(c, x["id"], "In Progress")
    clock.at("2026-03-04T10:00:00")
    _status(c, x["id"], "Done")
    clock.at("2026-03-06T10:00:00")
    ch = c.get(f"/api/agile/reports/control-chart?board_id={bid}&date_from=2026-03-01&date_to=2026-03-31").json()
    got = [(e["completed_at"][:10], e["cycle_time_days"]) for e in ch["entries"]]
    assert ch["count"] == 1
    assert got[0][0] == "2026-03-06" and got[0][1] in (4.0, 3.0)


# ---------------------------------------------------------------- S7
def test_s7_cfd_csv_export_matches_json(admin_client):
    c = admin_client
    p, bid = _project(c, "S7")
    _issue(c, p["id"], "Issue X")
    q = f"board_id={bid}&date_from=2026-01-01&date_to=2026-01-03"
    r = c.post(f"/api/agile/reports/export?report_key=cumulative-flow&{q}")
    assert r.status_code == 200, r.text
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][0] == "date", rows[0]
    assert len(rows) - 1 == 3


# ---------------------------------------------------------------- S8
def test_s8_sprint_csv_export_has_all_outcomes(admin_client):
    """Sprint with 1 completed + 1 not completed: the CSV should carry both
    (it only carries the first non-empty list)."""
    c = admin_client
    p, bid = _project(c, "S8")
    a = _issue(c, p["id"], "Issue A", 3)
    b = _issue(c, p["id"], "Issue B", 5)
    s = _sprint(c, bid, "S8 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"], b["id"])
    _start(c, s["id"])
    _status(c, a["id"], "Done")
    r = c.post(f"/api/agile/reports/export?report_key=sprint&sprint_id={s['id']}")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert sorted(int(x["id"]) for x in rows) == sorted([a["id"], b["id"]])


# ---------------------------------------------------------------- S9
def test_s9_parent_done_with_open_subtask_burndown_vs_sprint_report(admin_client):
    """P(5) done, its sub-task open, sprint completed.  Sprint report: P not
    completed (Jira sub-task rule).  Burndown/burnup end should agree:
    remaining 5, completed 0."""
    c = admin_client
    p, bid = _project(c, "S9")
    par = _issue(c, p["id"], "Issue P", 5)
    _issue(c, p["id"], "Child task", item_type="Sub-task", parent_id=par["id"])
    s = _sprint(c, bid, "S9 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], par["id"])
    _start(c, s["id"])
    _status(c, par["id"], "Done")
    _complete(c, s["id"])
    rep, bd = _sr(c, s["id"]), _bd(c, s["id"])
    assert rep["incomplete_estimate"] == 5
    assert bd["remaining_estimate"] == rep["incomplete_estimate"]
    assert bd["completed_estimate"] == rep["completed_estimate"]


# ---------------------------------------------------------------- S10
def test_s10_closed_sprint_is_immutable(admin_client):
    """After closing, reopen the completed issue, re-estimate it and change
    the other issue's type.  Closed sprint report/velocity must not move."""
    c = admin_client
    p, bid = _project(c, "S10")
    a = _issue(c, p["id"], "Issue A", 3)
    b = _issue(c, p["id"], "Issue B", 5)
    s = _sprint(c, bid, "S10 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"], b["id"])
    _start(c, s["id"])
    _status(c, a["id"], "Done")
    _complete(c, s["id"])
    before = _sr(c, s["id"]), _bd(c, s["id"]), _vel(c, bid)
    _status(c, a["id"], "In Progress")
    _estimate(c, a["id"], 13)
    _estimate(c, b["id"], 1)
    after = _sr(c, s["id"]), _bd(c, s["id"]), _vel(c, bid)
    keys = ("committed_estimate", "completed_estimate", "incomplete_estimate")
    for k in keys:
        assert before[0][k] == after[0][k], k
    assert [p_["remaining"] for p_ in before[1]["points"]] == [p_["remaining"] for p_ in after[1]["points"]]
    assert before[2]["points"] == after[2]["points"]


# ---------------------------------------------------------------- S11
def test_s11_empty_sprint_all_removed(admin_client):
    c = admin_client
    p, bid = _project(c, "S11")
    a = _issue(c, p["id"], "Issue A", 3)
    s = _sprint(c, bid, "S11 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"])
    _start(c, s["id"])
    _remove(c, s["id"], a["id"])
    _complete(c, s["id"])
    rep, bd = _sr(c, s["id"]), _bd(c, s["id"])
    assert rep["committed_estimate"] == 3 and rep["removed_count"] == 1
    assert bd["remaining_estimate"] == 0
    for key in ("sprint", "burndown", "scope-change"):
        r = c.post(f"/api/agile/reports/export?report_key={key}&sprint_id={s['id']}")
        assert r.status_code == 200, (key, r.text)
    r = c.post(f"/api/agile/reports/export?report_key=control-chart&board_id={bid}&date_from=2026-01-01&date_to=2026-01-02")
    assert r.status_code == 200


# ---------------------------------------------------------------- S12
def test_s12_cfd_json_and_csv_honour_include_subtasks(admin_client):
    c = admin_client
    p, bid = _project(c, "S12")
    par = _issue(c, p["id"], "Issue P", 5)
    _issue(c, p["id"], "Child task", item_type="Sub-task", parent_id=par["id"])
    from datetime import date
    today = date.today().isoformat()
    j = c.get(f"/api/agile/reports/cumulative-flow?board_id={bid}&date_from={today}&date_to={today}"
              f"&include_subtasks=false").json()
    assert sum(j["points"][-1]["counts"].values()) == 1
    for flag, expected in (("false", 1), ("true", 2)):
        res = c.post(f"/api/agile/reports/export?report_key=cumulative-flow&board_id={bid}"
                     f"&date_from={today}&date_to={today}&include_subtasks={flag}")
        assert res.status_code == 200, res.text
        rows = list(csv.DictReader(io.StringIO(res.text)))
        assert rows and list(rows[0])[0] == "date"
        assert sum(int(v) for k, v in rows[-1].items() if k != "date") == expected


# ---------------------------------------------------------------- S13
def test_s13_issue_done_before_start_and_created_in_sprint(admin_client):
    """Z(2) Done on Sun, committed at Mon start; A(3) committed open.
    Tue: N(4) created directly into the active sprint.
    Jira lists N as added (*) and Z as completed outside the sprint."""
    c = admin_client
    p, bid = _project(c, "S13")
    clock = Clock()
    z = _issue(c, p["id"], "Issue Z", 2)
    a = _issue(c, p["id"], "Issue A", 3)
    s = _sprint(c, bid, "S13 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], z["id"], a["id"])
    _status(c, z["id"], "Done")
    clock.at("2026-03-01T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T09:00:00")
    clock.at("2026-03-02T09:00:00")
    n = _issue(c, p["id"], "Issue N", 4, sprint_id=s["id"])
    clock.at("2026-03-03T10:00:00")
    rep, bd = _sr(c, s["id"]), _bd(c, s["id"])
    assert rep["added_count"] == 1 and rep["added_estimate"] == 4
    # Z was done before the sprint started: completed outside it, not part
    # of the commitment, and the guideline starts at what is left (A).
    assert rep["committed_estimate"] == 3
    assert [r["id"] for r in rep["completed_outside"]] == [z["id"]]
    assert rep["completed_count"] == 0
    assert bd["guideline"][0]["value"] == 3
    assert c.get(f"/api/bugs/{n['id']}").json()["sprint_id"] == s["id"]


# ---------------------------------------------------------------- S14
def test_s14_daily_completed_today_with_same_day_reopen(admin_client):
    """A(3) done Tue 10:00, reopened Tue 15:00.  Jira-like daily: net 0
    completed that day (A is not done at end of day)."""
    c = admin_client
    p, bid = _project(c, "S14")
    clock = Clock()
    a = _issue(c, p["id"], "Issue A", 3)
    s = _sprint(c, bid, "S14 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"])
    clock.at("2026-03-01T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T09:00:00")
    clock.at("2026-03-02T09:00:00")
    _status(c, a["id"], "Done")
    clock.at("2026-03-03T10:00:00")
    _status(c, a["id"], "In Progress")
    clock.at("2026-03-03T15:00:00")
    d = c.get(f"/api/agile/reports/daily?sprint_id={s['id']}&report_date=2026-03-03").json()
    assert d["completed_estimate"] == 0 and d["remaining_estimate"] == 3
    assert d["completed_estimate_today"] == 0


# ---------------------------------------------------------------- S15
def test_s15_legacy_sprint_without_history(admin_client):
    """Closed sprint whose started_at is NULL (pre-history): committed A3,B5;
    B removed, D(2) added, A done.  Expected: committed 8, completed 3,
    not completed D 2, removed B 5, history_complete False, burndown
    end remaining 2, export works."""
    c = admin_client
    p, bid = _project(c, "S15")
    a = _issue(c, p["id"], "Issue A", 3)
    b = _issue(c, p["id"], "Issue B", 5)
    d = _issue(c, p["id"], "Issue D", 2)
    s = _sprint(c, bid, "S15 Sprint", "2026-03-02", "2026-03-13")
    _add(c, s["id"], a["id"], b["id"])
    _start(c, s["id"])
    _remove(c, s["id"], b["id"])
    _add(c, s["id"], d["id"])
    _status(c, a["id"], "Done")
    _complete(c, s["id"])
    from app.database import SessionLocal
    from app.models import Sprint
    with SessionLocal() as db:
        db.get(Sprint, s["id"]).started_at = None
        db.commit()
    rep, bd = _sr(c, s["id"]), _bd(c, s["id"])
    assert rep["history_complete"] is False
    assert (rep["committed_estimate"], rep["completed_estimate"], rep["incomplete_estimate"],
            rep["removed_estimate"], rep["added_count"]) == (8, 3, 2, 5, 1)
    assert bd["remaining_estimate"] == 2 and bd["scope_estimate"] == 5


def test_s16_burndown_days_are_the_boards_days_as_instants(admin_client):
    """A board in Asia/Kolkata: each chart day starts at the board's midnight
    (18:30 UTC the day before), and weekends are flagged non-working, so a
    viewer anywhere shades the same instants the guideline is flat over."""
    c = admin_client
    p, bid = _project(c, "S16")
    board = c.get(f"/api/agile/boards/{bid}").json()
    res = c.put(f"/api/agile/boards/{bid}", json={"timezone": "Asia/Kolkata", "version": board["version"]})
    assert res.status_code == 200, res.text
    a = _issue(c, p["id"], "Issue A", 3)
    s = _sprint(c, bid, "S16 Sprint", "2026-03-02", "2026-03-08")
    _add(c, s["id"], a["id"])
    Clock().at("2026-03-02T09:00:00")
    _start(c, s["id"])
    _set_sprint_times(s["id"], started="2026-03-02T04:00:00")
    bd = _bd(c, s["id"])
    assert bd["timezone"] == "Asia/Kolkata"
    days = {d["date"]: d for d in bd["days"]}
    assert list(days)[:7] == [f"2026-03-0{n}" for n in range(2, 9)]
    assert days["2026-03-02"]["start"].startswith("2026-03-01T18:30:00")
    assert days["2026-03-02"]["end"].startswith("2026-03-02T18:30:00")
    # The sprint is still running, so the chart reaches today; its planned
    # week has the weekend off.
    assert [d for d, v in days.items() if not v["working"] and d <= "2026-03-08"] == ["2026-03-07", "2026-03-08"]
