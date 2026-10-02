"""Regression tests for the independent audit of the Sprints feature: ranking
that never runs out, placement next to the right neighbour, deletes that keep
the hierarchy whole, backlog permissions on every path, and reopened issues
from closed sprints."""
from __future__ import annotations

import random

import pytest

from tests.test_agile_jira_rules import (
    _add,
    _backlog_ids,
    _bug,
    _complete,
    _issue,
    _planning,
    _project,
    _row,
    _set_status,
    _sprint,
    _start,
)

# --- rank tokens ---------------------------------------------------------------


def test_appending_and_prepending_keeps_tokens_short_and_ordered():
    from app.agile.ranking import WIDTH, rank_between

    last = None
    for _ in range(5000):
        token = rank_between(last, None)
        assert last is None or token > last
        last = token
    assert len(last) == WIDTH
    first = None
    for _ in range(5000):
        token = rank_between(None, first)
        assert first is None or token < first
        first = token
    assert len(first) == WIDTH


def test_random_inserts_stay_ordered_and_rebalance_when_a_gap_runs_out():
    from app.agile.ranking import MAX_LENGTH, RankExhausted, rank_between, spaced_ranks

    rng = random.Random(7)
    items: list[str] = []
    rebalanced = 0
    for n in range(4000):
        # Every fourth insert hammers the same spot to exhaust that gap.
        pos = 1 if n % 4 == 0 and len(items) > 1 else rng.randint(0, len(items))
        for _ in range(2):
            lo = items[pos - 1] if pos > 0 else None
            hi = items[pos] if pos < len(items) else None
            try:
                token = rank_between(lo, hi)
                break
            except RankExhausted:
                items = spaced_ranks(len(items))
                rebalanced += 1
        items.insert(pos, token)
    assert items == sorted(items)
    assert len(set(items)) == len(items)
    assert max(map(len, items)) <= MAX_LENGTH
    assert rebalanced > 0


def test_no_token_fits_between_a_token_and_its_zero_extension():
    from app.agile.ranking import RankExhausted, rank_between, ranks_between

    with pytest.raises(RankExhausted):
        rank_between("a", "a0")
    with pytest.raises(RankExhausted):
        rank_between("b", "a")
    batch = ranks_between("a", "b", 500)
    assert batch == sorted(batch) and "a" < batch[0] and batch[-1] < "b"


# --- placement in the database -------------------------------------------------


def _ranked(project_id):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Bug

    with SessionLocal() as db:
        rows = db.execute(
            select(Bug.id, Bug.rank).where(Bug.project_id == project_id, Bug.rank_scope == f"backlog:{project_id}")
            .order_by(Bug.rank, Bug.id)
        ).all()
    return [r.id for r in rows], [r.rank for r in rows]


def test_hundreds_of_issues_get_distinct_ranks_and_stay_draggable(admin_client):
    project, board_id = _project(admin_client, "Many Issues")
    created = [_bug(admin_client, project["id"], f"Issue {n}")["id"] for n in range(400)]
    order, ranks = _ranked(project["id"])
    assert order == created
    assert len(set(ranks)) == len(ranks)
    # Drag the last issue between the first two.
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={
        "item_ids": [created[-1]], "before_id": created[0], "after_id": created[1],
    })
    assert res.status_code == 200, res.text
    assert _ranked(project["id"])[0][:3] == [created[0], created[-1], created[1]]


def test_drop_below_an_issue_lands_right_after_it(admin_client):
    project, board_id = _project(admin_client, "Drop Below")
    a, b, c, x = (_bug(admin_client, project["id"], f"Issue {n}")["id"] for n in "ABCX")
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={"item_ids": [x], "before_id": a})
    assert res.status_code == 200, res.text
    assert _backlog_ids(admin_client, board_id) == [a, x, b, c]


def test_rank_endpoint_places_next_to_the_given_neighbour(admin_client):
    project, board_id = _project(admin_client, "Rank Endpoint")
    a, b, c, d = (_bug(admin_client, project["id"], f"Issue {n}")["id"] for n in "ABCD")

    def rank(item_id, **where):
        return admin_client.post(f"/api/agile/boards/{board_id}/rank", json={"item_id": item_id, **where})

    assert rank(d, before_id=a).status_code == 200
    assert _backlog_ids(admin_client, board_id) == [a, d, b, c]
    assert rank(a, after_id=c).status_code == 200
    assert _backlog_ids(admin_client, board_id) == [d, b, a, c]
    assert rank(c).status_code == 200
    assert _backlog_ids(admin_client, board_id) == [c, d, b, a]
    # Neighbours given the wrong way round are refused, not silently misplaced.
    res = rank(b, before_id=a, after_id=c)
    assert res.status_code == 422, res.text
    assert _backlog_ids(admin_client, board_id) == [c, d, b, a]
    _, ranks = _ranked(project["id"])
    assert len(set(ranks)) == 4


def test_duplicate_legacy_ranks_are_respaced_on_the_next_drop(admin_client):
    from sqlalchemy import update

    from app.database import SessionLocal
    from app.models import Bug

    project, board_id = _project(admin_client, "Duplicate Ranks")
    a, b, c = (_bug(admin_client, project["id"], f"Issue {n}")["id"] for n in "ABC")
    with SessionLocal() as db:
        db.execute(update(Bug).where(Bug.id.in_([a, b])).values(rank="m"))
        db.commit()
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={
        "item_ids": [c], "before_id": a, "after_id": b,
    })
    assert res.status_code == 200, res.text
    order, ranks = _ranked(project["id"])
    assert order == [a, c, b]
    assert len(set(ranks)) == 3


# --- deletes keep the hierarchy whole ------------------------------------------


def test_bulk_delete_removes_sub_tasks_with_their_parent(admin_client):
    project, _ = _project(admin_client, "Bulk Delete")
    story = _issue(admin_client, project["id"], "Parent story")
    sub = _issue(admin_client, project["id"], "Child", item_type="Sub-task", parent_id=story["id"])
    lone = _bug(admin_client, project["id"], "Unrelated")
    # The selection holds the Sub-task too, after its parent.
    res = admin_client.post("/api/bugs/bulk", json={"action": "delete", "ids": [story["id"], sub["id"], lone["id"]]})
    assert res.status_code == 200, res.text
    for item_id in (story["id"], sub["id"], lone["id"]):
        assert admin_client.get(f"/api/bugs/{item_id}").status_code == 404


def test_deleting_an_epic_unlinks_its_issues_and_logs_it(admin_client):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import WorkItemChange

    project, _ = _project(admin_client, "Epic Delete")
    epic = _issue(admin_client, project["id"], "Epic", item_type="Epic")
    story = _issue(admin_client, project["id"], "Story", epic_id=epic["id"])
    sub = _issue(admin_client, project["id"], "Sub", item_type="Sub-task", parent_id=story["id"])
    assert admin_client.delete(f"/api/bugs/{epic['id']}").status_code == 200
    assert _row(story["id"]).epic_id is None
    assert _row(sub["id"]).epic_id is None
    with SessionLocal() as db:
        fields = set(db.scalars(select(WorkItemChange.field).where(WorkItemChange.work_item_id == story["id"])).all())
    assert "epic" in fields


# --- backlog permissions on every path -----------------------------------------


def _login_member(client, project_id):
    res = client.post("/api/users", json={
        "name": "Member", "email": "member2@test.local", "role": "user",
        "password": "Member12345", "project_ids": [project_id],
    })
    assert res.status_code == 201, res.text
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={
        "email": "member2@test.local", "password": "Member12345",
    }).status_code == 200


def test_members_cannot_change_sprint_scope_through_any_path(admin_client):
    project, board_id = _project(admin_client, "Scope Permissions")
    sprint = _sprint(admin_client, board_id)
    planned = _issue(admin_client, project["id"], "Planned story")
    _add(admin_client, sprint["id"], planned["id"])
    _start(admin_client, sprint["id"])
    _login_member(admin_client, project["id"])

    mine = _bug(admin_client, project["id"], "Member's bug")
    res = admin_client.put(f"/api/bugs/{mine['id']}", json={"sprint_id": sprint["id"]})
    assert res.status_code == 403, res.text
    res = admin_client.put(f"/api/bugs/{planned['id']}", json={"sprint_id": None})
    assert res.status_code == 403, res.text
    res = admin_client.post("/api/agile/work-items", json={
        "project_id": project["id"], "title": "Sneaky", "item_type": "Story", "sprint_id": sprint["id"],
    })
    assert res.status_code == 403, res.text
    res = admin_client.post(f"/api/agile/sprints/{sprint['id']}/items", json={"item_ids": [mine["id"]]})
    assert res.status_code == 403, res.text
    # Other edits still work, and resending the current sprint is not a change.
    res = admin_client.put(f"/api/bugs/{mine['id']}", json={"title": "Member's bug, renamed", "sprint_id": None})
    assert res.status_code == 200, res.text
    assert _row(planned["id"]).sprint_id == sprint["id"]


# --- reopened issues from closed sprints ---------------------------------------


def test_reopening_an_issue_from_a_closed_sprint_returns_it_to_the_backlog(admin_client):
    project, board_id = _project(admin_client, "Reopen After Close")
    s1 = _sprint(admin_client, board_id, "Sprint 1")
    story = _issue(admin_client, project["id"], "Shipped story", story_points=5)
    sub = _issue(admin_client, project["id"], "Its sub-task", item_type="Sub-task", parent_id=story["id"])
    _add(admin_client, s1["id"], story["id"])
    _start(admin_client, s1["id"])
    _set_status(admin_client, sub["id"], "Done")
    _set_status(admin_client, story["id"], "Done")
    assert _complete(admin_client, s1["id"]).status_code == 200
    before = admin_client.get(f"/api/agile/reports/sprint/{s1['id']}").json()

    _set_status(admin_client, story["id"], "In Progress")
    assert story["id"] in _backlog_ids(admin_client, board_id)
    assert _row(sub["id"]).sprint_id is None
    s2 = _sprint(admin_client, board_id, "Sprint 2")
    res = admin_client.post(f"/api/agile/boards/{board_id}/move", json={"item_ids": [story["id"]], "sprint_id": s2["id"]})
    assert res.status_code == 200, res.text
    assert (_row(story["id"]).sprint_id, _row(sub["id"]).sprint_id) == (s2["id"], s2["id"])
    # The closed sprint's report is history: it does not change.
    after = admin_client.get(f"/api/agile/reports/sprint/{s1['id']}").json()
    assert after["completed_estimate"] == before["completed_estimate"] == 5
    assert [i["id"] for i in after["completed"]] == [story["id"]]


def test_an_issue_still_done_stays_with_its_closed_sprint(admin_client):
    project, board_id = _project(admin_client, "Done Stays")
    s1 = _sprint(admin_client, board_id, "Sprint 1")
    story = _issue(admin_client, project["id"], "Finished story")
    _add(admin_client, s1["id"], story["id"])
    _start(admin_client, s1["id"])
    _set_status(admin_client, story["id"], "Done")
    assert _complete(admin_client, s1["id"]).status_code == 200
    _set_status(admin_client, story["id"], "Closed")  # another done status
    assert _row(story["id"]).sprint_id == s1["id"]
    assert story["id"] not in _backlog_ids(admin_client, board_id)
    assert _planning(admin_client, board_id)["sprints"] == []


def test_moving_a_finished_issue_to_another_project_keeps_the_closed_sprint_report(admin_client):
    project, board_id = _project(admin_client, "Move Source")
    other, _ = _project(admin_client, "Move Target")
    s1 = _sprint(admin_client, board_id, "Sprint 1")
    story = _issue(admin_client, project["id"], "Finished story", story_points=3)
    _add(admin_client, s1["id"], story["id"])
    _start(admin_client, s1["id"])
    _set_status(admin_client, story["id"], "Done")
    assert _complete(admin_client, s1["id"]).status_code == 200
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"project_id": other["id"]})
    assert res.status_code == 200, res.text
    report = admin_client.get(f"/api/agile/reports/sprint/{s1['id']}").json()
    assert [i["id"] for i in report["completed"]] == [story["id"]]
    assert report["completed_estimate"] == 3


# --- second audit round ----------------------------------------------------------


def test_moving_an_issue_out_of_an_open_sprint_by_changing_project_needs_backlog_rights(admin_client):
    """A member may edit their bug, but moving it to another project would
    take it out of the running sprint: that needs the backlog permission."""
    project, board_id = _project(admin_client, "Move Rights Source")
    other, _ = _project(admin_client, "Move Rights Target")
    member = admin_client.post("/api/users", json={
        "name": "Member", "email": "member3@test.local", "role": "user",
        "password": "Member12345", "project_ids": [project["id"], other["id"]],
    }).json()
    sprint = _sprint(admin_client, board_id)
    bug = _bug(admin_client, project["id"], "Member's bug", assignee_ids=[member["id"]])
    _add(admin_client, sprint["id"], bug["id"])
    _start(admin_client, sprint["id"])
    admin_client.post("/api/auth/logout")
    assert admin_client.post("/api/auth/login", json={
        "email": "member3@test.local", "password": "Member12345",
    }).status_code == 200
    assert admin_client.put(f"/api/bugs/{bug['id']}", json={"title": "Member's bug, edited"}).status_code == 200
    res = admin_client.put(f"/api/bugs/{bug['id']}", json={"project_id": other["id"]})
    assert res.status_code == 403, res.text
    assert "manage_backlog" in res.json()["detail"]
    row = _row(bug["id"])
    assert (row.project_id, row.sprint_id) == (project["id"], sprint["id"])


def test_a_project_move_leaves_the_old_projects_labels_and_components_behind(admin_client):
    project, _ = _project(admin_client, "Taxonomy Source")
    other, _ = _project(admin_client, "Taxonomy Target")
    component = admin_client.post("/api/agile/components", json={"project_id": project["id"], "name": "Backend"}).json()
    label = admin_client.post("/api/agile/labels", json={"project_id": project["id"], "display_name": "urgent"}).json()
    story = _issue(admin_client, project["id"], "Labelled story")
    sub = _issue(admin_client, project["id"], "Labelled sub-task", item_type="Sub-task", parent_id=story["id"])
    for item in (story, sub):
        fresh = admin_client.get(f"/api/bugs/{item['id']}").json()
        res = admin_client.put(f"/api/agile/work-items/{item['id']}/taxonomy", json={
            "component_ids": [component["id"]], "label_ids": [label["id"]], "version": fresh["version"],
        })
        assert res.status_code == 200, res.text
    res = admin_client.put(f"/api/bugs/{story['id']}", json={"project_id": other["id"]})
    assert res.status_code == 200, res.text
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Label, WorkItemComponent, WorkItemLabel

    with SessionLocal() as db:
        ids = [story["id"], sub["id"]]
        assert db.scalars(select(WorkItemComponent).where(WorkItemComponent.work_item_id.in_(ids))).all() == []
        assert db.scalars(select(WorkItemLabel).where(WorkItemLabel.work_item_id.in_(ids))).all() == []
        assert db.get(Label, label["id"]).usage_count == 0
    assert _row(sub["id"]).project_id == other["id"]


def test_issues_in_a_closed_sprint_cannot_be_reranked(admin_client):
    project, board_id = _project(admin_client, "Closed Ranks")
    sprint = _sprint(admin_client, board_id)
    a, b = (_issue(admin_client, project["id"], f"Issue {n}") for n in "AB")
    _add(admin_client, sprint["id"], a["id"], b["id"])
    _start(admin_client, sprint["id"])
    _set_status(admin_client, a["id"], "Done")
    _set_status(admin_client, b["id"], "Done")
    assert _complete(admin_client, sprint["id"]).status_code == 200
    before = _row(b["id"]).rank
    res = admin_client.post(f"/api/agile/boards/{board_id}/rank", json={"item_id": b["id"], "after_id": a["id"]})
    assert res.status_code == 422, res.text
    assert _row(b["id"]).rank == before
