"""Flush-time hierarchy rules and the work-item change log (app/agile/integrity.py).

These run against the ORM directly, so they hold for every write path that
ends in a flush, not just the routes that happen to be tested elsewhere.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.conftest import default_org_id


@pytest.fixture
def world(db_session):
    from app.models import Board, Bug, Project, Sprint

    db = db_session
    project = Project(org_id=default_org_id(), name="Integrity", description="")
    other = Project(org_id=default_org_id(), name="Elsewhere", description="")
    db.add_all([project, other])
    db.flush()
    board = Board(project_id=project.id, name="B", name_normalized="b", is_default=True)
    db.add(board)
    db.flush()
    sprint_a = Sprint(board_id=board.id, project_id=project.id, name="A", name_normalized="a")
    sprint_b = Sprint(board_id=board.id, project_id=project.id, name="B", name_normalized="b",
                      sequence_number=2)
    epic_1 = Bug(project_id=project.id, title="Epic one", item_type="Epic")
    epic_2 = Bug(project_id=project.id, title="Epic two", item_type="Epic")
    db.add_all([sprint_a, sprint_b, epic_1, epic_2])
    db.flush()
    story = Bug(project_id=project.id, title="Story", item_type="Story",
                epic_id=epic_1.id, sprint_id=sprint_a.id, story_points=3)
    db.add(story)
    db.flush()
    subtasks = [
        Bug(project_id=project.id, title=f"Sub {n}", item_type="Sub-task", parent_id=story.id)
        for n in (1, 2)
    ]
    db.add_all(subtasks)
    db.commit()
    return {
        "db": db, "project": project, "other": other, "sprint_a": sprint_a,
        "sprint_b": sprint_b, "epic_1": epic_1, "epic_2": epic_2, "story": story,
        "subtasks": subtasks,
    }


def _changes(db, item_id, field=None):
    from app.models import WorkItemChange

    stmt = select(WorkItemChange).where(WorkItemChange.work_item_id == item_id)
    if field:
        stmt = stmt.where(WorkItemChange.field == field)
    return list(db.scalars(stmt.order_by(WorkItemChange.id)).all())


def test_new_subtask_takes_parent_epic_and_sprint(world):
    for sub in world["subtasks"]:
        assert sub.epic_id == world["epic_1"].id
        assert sub.sprint_id == world["sprint_a"].id


def test_moving_parent_moves_its_subtasks(world):
    db, story = world["db"], world["story"]
    story.sprint_id = world["sprint_b"].id
    story.epic_id = world["epic_2"].id
    db.commit()
    for sub in world["subtasks"]:
        db.refresh(sub)
        assert sub.sprint_id == world["sprint_b"].id
        assert sub.epic_id == world["epic_2"].id
        # The cascade is history too: reports see the sub-task leave sprint A.
        moves = _changes(db, sub.id, "sprint")
        assert [(c.old_value, c.new_value) for c in moves] == [
            (str(world["sprint_a"].id), str(world["sprint_b"].id)),
        ]


def test_parent_leaving_sprint_takes_subtasks_to_backlog(world):
    db, story = world["db"], world["story"]
    story.sprint_id = None
    db.commit()
    for sub in world["subtasks"]:
        db.refresh(sub)
        assert sub.sprint_id is None


def test_subtask_cannot_hold_its_own_sprint(world):
    db, sub = world["db"], world["subtasks"][0]
    sub.sprint_id = world["sprint_b"].id
    db.commit()
    db.refresh(sub)
    assert sub.sprint_id == world["sprint_a"].id


def test_reparenting_a_subtask_follows_the_new_parent(world):
    from app.models import Bug

    db = world["db"]
    other_story = Bug(project_id=world["project"].id, title="Other", item_type="Task",
                      epic_id=world["epic_2"].id, sprint_id=world["sprint_b"].id)
    db.add(other_story)
    db.commit()
    sub = world["subtasks"][0]
    sub.parent_id = other_story.id
    db.commit()
    db.refresh(sub)
    assert (sub.epic_id, sub.sprint_id) == (world["epic_2"].id, world["sprint_b"].id)


def test_epic_never_sits_in_a_sprint_or_under_a_parent(world):
    db, epic = world["db"], world["epic_1"]
    epic.sprint_id = world["sprint_a"].id
    epic.epic_id = world["epic_2"].id
    db.commit()
    db.refresh(epic)
    assert epic.sprint_id is None and epic.epic_id is None


def test_standard_issue_never_has_a_parent(world):
    db, story = world["db"], world["story"]
    story.parent_id = world["subtasks"][0].id
    db.commit()
    db.refresh(story)
    assert story.parent_id is None


def test_every_tracked_change_is_logged_once_with_old_and_new_values(world):
    from app.agile.integrity import set_actor
    from app.models import User

    db, story = world["db"], world["story"]
    admin = User(org_id=default_org_id(), name="Ada", email="ada@example.test", role="admin", password_hash="x")
    db.add(admin)
    db.commit()
    set_actor(db, admin.id)
    story.status = "In Progress"
    story.story_points = 5
    db.commit()
    story.status = "Done"
    db.commit()

    statuses = _changes(db, story.id, "status")
    assert [(c.old_value, c.new_value) for c in statuses] == [
        ("New", "In Progress"), ("In Progress", "Done"),
    ]
    assert all(c.actor_id == admin.id for c in statuses)
    points = _changes(db, story.id, "story_points")
    assert [(c.old_value, c.new_value) for c in points] == [("3", "5")]
    created = _changes(db, story.id, "created")
    assert len(created) == 1 and created[0].new_value == "Story"


def test_unchanged_assignment_is_not_logged(world):
    db, story = world["db"], world["story"]
    story.status = story.status
    story.story_points = 3.0  # same value, different Python type
    db.commit()
    assert _changes(db, story.id, "status") == []
    assert _changes(db, story.id, "story_points") == []


def test_expired_objects_still_log_the_true_old_value(world):
    db, story = world["db"], world["story"]
    db.expire_all()
    story.status = "Testing"
    db.commit()
    assert [(c.old_value, c.new_value) for c in _changes(db, story.id, "status")] == [
        ("New", "Testing"),
    ]


def test_change_reason_tags_side_effects(world):
    from app.agile.integrity import change_reason

    db, story = world["db"], world["story"]
    with change_reason(db, "sprint_completed"):
        story.sprint_id = None
    db.commit()
    rows = _changes(db, story.id, "sprint")
    assert rows and rows[0].reason == "sprint_completed"
    assert db.info.get("change_reason") is None
    story.sprint_id = world["sprint_b"].id
    db.commit()
    assert _changes(db, story.id, "sprint")[-1].reason is None


def test_changes_are_written_with_sub_second_precision_in_order(world):
    db, story = world["db"], world["story"]
    for status in ("In Progress", "Testing", "Done"):
        story.status = status
        db.commit()
    rows = _changes(db, story.id, "status")
    times = [c.changed_at for c in rows]
    assert times == sorted(times)
    assert any(t.microsecond for t in times) or len(set(times)) == len(times)


def test_deleting_an_item_removes_its_history(world):
    from app.models import WorkItemChange

    db, sub = world["db"], world["subtasks"][1]
    sub_id = sub.id
    db.delete(sub)
    db.commit()
    assert db.scalars(select(WorkItemChange).where(WorkItemChange.work_item_id == sub_id)).all() == []
