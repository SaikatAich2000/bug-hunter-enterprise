"""Sleuth bulk actions + admin-only write policy: one confirmable plan per bulk write;
non-admin writes (single or bulk) get action_denied while reads keep working (temp-SQLite).
"""
from __future__ import annotations

import os as _os
import sys as _sys

from tests.conftest import default_org_id

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["SESSION_SECRET"] = "test-secret-bulk-only"
os.environ["SLEUTH_CLOUD_ENABLED"] = "0"

# Purge app.* so this file gets a fresh import bound to its own DB (avoids "no such table").
import sys as _sys_purge

for _m in list(_sys_purge.modules):
    if _m == "app" or _m.startswith("app."):
        del _sys_purge.modules[_m]

from app import models
from app.auth import hash_password
from app.chatbot import actions, executor
from app.chatbot.memory import store as memstore
from app.database import Base, SessionLocal, engine


def seed():
    """Drop and recreate all tables, seed 3 New/Medium/DEV bugs in General, return a dict of ids."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    memstore._clear_all_for_test()
    db = SessionLocal()
    try:
        admin = models.User(org_id=default_org_id(), name="Saikat Aich", email="admin@bulk.test",
                            role="admin", password_hash=hash_password("AdminPass1!"),
                            is_active=True)
        mgr = models.User(org_id=default_org_id(), name="Mark Manager", email="mgr@bulk.test",
                          role="manager", password_hash=hash_password("MgrPass1!"),
                          is_active=True)
        usr = models.User(org_id=default_org_id(), name="Uma User", email="usr@bulk.test",
                          role="user", password_hash=hash_password("UsrPass1!"),
                          is_active=True)
        db.add_all([admin, mgr, usr])
        db.commit()
        proj = models.Project(org_id=default_org_id(), name="General", description="d")
        db.add(proj)
        db.commit()
        ids = []
        for i in range(3):
            b = models.Bug(title=f"Bug {i}", description="d", status="New",
                           priority="Medium", environment="DEV",
                           project_id=proj.id, reporter_id=admin.id)
            db.add(b)
            db.commit()
            ids.append(b.id)
        return {"admin": admin.id, "mgr": mgr.id, "usr": usr.id, "bugs": ids}
    finally:
        db.close()


def _user(db, uid):
    return db.get(models.User, uid)


# --------------------------------------------------------------------------
# Bulk writes (admin)
# --------------------------------------------------------------------------
def test_bulk_assign_all_admin():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        staged = executor.execute("assign all the bugs to Saikat Aich", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        assert "3" in staged.blocks[0].payload["text"]

        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        assert "3" in done.blocks[0].payload["text"]

        db.expire_all()
        assigned = sum(1 for b in db.query(models.Bug).all()
                       if any(a.id == ids["admin"] for a in b.assignees))
        assert assigned == 3, assigned
        # Bulk ops emit one aggregate notification (to the actor), not one per item.
        notifs = db.query(models.Notification).filter_by(user_id=ids["admin"]).all()
        assert len(notifs) == 1, len(notifs)
        assert notifs[0].kind == "assigned"
    finally:
        db.close()


def test_bulk_assign_all_to_me():
    """"assign all the bugs to me" resolves "me" to the actor and assigns all."""
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        staged = executor.execute("assign all the bugs to me", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        assert "3" in staged.blocks[0].payload["text"]
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        assigned = sum(1 for b in db.query(models.Bug).all()
                       if any(a.id == ids["admin"] for a in b.assignees))
        assert assigned == 3, assigned
    finally:
        db.close()


def test_bulk_assign_all_to_me_no_noun():
    """"assign all to me" (no item noun) still stages a bulk self-assign."""
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        staged = executor.execute("assign all to me", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
    finally:
        db.close()


def test_single_assign_to_me():
    """"assign bug N to me" resolves "me" to the actor for a single item."""
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        bug_id = ids["bugs"][0]
        staged = executor.execute(f"assign bug {bug_id} to me", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        bug = db.get(models.Bug, bug_id)
        assert any(a.id == ids["admin"] for a in bug.assignees)
    finally:
        db.close()


def test_bulk_close_all_admin():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        staged = executor.execute("close all bugs", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        closed = sum(1 for b in db.query(models.Bug).all() if b.status == "Closed")
        assert closed == 3, closed
    finally:
        db.close()


def test_bulk_unassign_all_admin():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        # Assign first, then bulk-unassign to verify the result is zero.
        executor.execute("assign all the bugs to Saikat Aich", db, admin)
        executor.execute("yes", db, admin)
        staged = executor.execute("unassign Saikat Aich from all the bugs", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        still = sum(1 for b in db.query(models.Bug).all()
                    if any(a.id == ids["admin"] for a in b.assignees))
        assert still == 0, still
    finally:
        db.close()


def test_clear_all_assignees_by_status_filter():
    """'remove assignees from CLOSED bugs' clears every assignee on the closed
    set only, behind a Yes/Cancel confirm — the reported 'make it smart' case."""
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        # Assign the 3 seeded (New) bugs, then add 2 Closed bugs with assignees.
        executor.execute("assign all the bugs to Saikat Aich", db, admin)
        executor.execute("yes", db, admin)
        proj = db.query(models.Project).first()
        closed_ids = []
        for i in range(2):
            b = models.Bug(title=f"Closed {i}", description="d", status="Closed",
                           priority="Medium", environment="DEV",
                           project_id=proj.id, reporter_id=ids["admin"])
            db.add(b)
            db.commit()
            b.assignees = [_user(db, ids["admin"]), _user(db, ids["usr"])]
            db.commit()
            closed_ids.append(b.id)

        staged = executor.execute("remove assignees from 'CLOSED' bugs", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        text = staged.blocks[0].payload["text"].lower()
        assert "remove all assignees" in text
        assert "2" in text  # only the 2 closed bugs, not the 3 New ones

        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent

        db.expire_all()
        for cid in closed_ids:
            assert db.get(models.Bug, cid).assignees == []
        new_assigned = sum(1 for b in db.query(models.Bug).filter_by(status="New").all()
                           if b.assignees)
        assert new_assigned == 3, new_assigned  # untouched
    finally:
        db.close()


def test_clear_all_assignees_single_bug():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        bug_id = ids["bugs"][0]
        executor.execute(f"assign bug {bug_id} to Saikat Aich", db, admin)
        executor.execute("yes", db, admin)
        staged = executor.execute(f"remove all assignees from bug {bug_id}", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        assert db.get(models.Bug, bug_id).assignees == []
    finally:
        db.close()


def test_clear_all_assignees_needs_a_scope():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("remove the assignees", db, admin)
        assert r.intent == "action_invalid", r.intent
        assert "which items" in r.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_clear_all_assignees_admin_only():
    ids = seed()
    db = SessionLocal()
    try:
        usr = _user(db, ids["usr"])
        r = executor.execute("remove assignees from all the bugs", db, usr)
        assert r.intent == "action_denied", r.intent
    finally:
        db.close()


def test_clear_all_assignees_no_matching_items():
    # A valid scope phrase with a status filter matching nothing (all seeded
    # bugs are "New") must report no matching items, not stage a plan.
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("remove assignees from all closed bugs", db, admin)
        assert r.intent == "action_invalid", r.intent
        assert "couldn't find" in r.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_clear_all_assignees_over_cap_refused(monkeypatch):
    # 3 seeded bugs exceed a cap of 1: refuse rather than stage a runaway plan.
    ids = seed()
    monkeypatch.setattr(executor, "_BULK_ACTION_CAP", 1)
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("remove assignees from all the bugs", db, admin)
        assert r.intent == "action_invalid", r.intent
        assert "too many" in r.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_clear_assignees_with_named_user_is_not_a_clear_all():
    # "remove Bob as assignee..." names a specific user: this is a targeted
    # unassign, not a clear-all, so _maybe_handle_clear_assignees must defer
    # (return None) and let the normal single/bulk-unassign flow handle it.
    from app.chatbot.nlu import ParsedQuery
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        pq = ParsedQuery()
        pq.assignee_ids = [ids["usr"]]
        res = executor._maybe_handle_clear_assignees(
            "remove Uma as assignee from all bugs", db, admin, pq)
        assert res is None
    finally:
        db.close()


def test_bulk_assign_needs_a_user():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("assign all the bugs", db, admin)
        assert r.intent == "action_invalid", r.intent
        assert "user" in r.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_bulk_assign_unknown_user():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        r = executor.execute("assign all the bugs to Nobody McGhost", db, admin)
        assert r.intent == "action_invalid", r.intent
    finally:
        db.close()


def test_bulk_priority_all_admin():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        staged = executor.execute("set all bugs to high priority", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        assert all(b.priority == "High" for b in db.query(models.Bug).all())
    finally:
        db.close()


def test_bulk_assign_with_status_filter():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        # Move one bug to In Progress so only the remaining two match the New filter.
        b = db.get(models.Bug, ids["bugs"][0])
        b.status = "In Progress"
        db.commit()
        staged = executor.execute("assign all new bugs to Saikat Aich", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        assert "2" in staged.blocks[0].payload["text"]
        executor.execute("yes", db, admin)
        db.expire_all()
        assigned = sum(1 for b in db.query(models.Bug).all()
                       if any(a.id == ids["admin"] for a in b.assignees))
        assert assigned == 2, assigned
    finally:
        db.close()


def _add_mixed_types(db, proj_id, admin_id):
    """Add one Requirement and one Task alongside the seeded bugs. Returns (requirement_id, task_id)."""
    req = models.Bug(title="Req A", description="d", status="New", priority="Medium",
                     environment="DEV", project_id=proj_id, reporter_id=admin_id,
                     item_type="Requirement")
    task = models.Bug(title="Task A", description="d", status="New", priority="Medium",
                      environment="DEV", project_id=proj_id, reporter_id=admin_id,
                      item_type="Task")
    db.add_all([req, task])
    db.commit()
    return req.id, task.id


def test_bulk_assign_all_bugs_excludes_requirements_and_tasks():
    """"assign all the bugs" touches only Bugs, not the Requirements/Tasks sharing the table."""
    ids = seed()
    db = SessionLocal()
    try:
        proj_id = db.query(models.Project).first().id
        req_id, task_id = _add_mixed_types(db, proj_id, ids["admin"])

        admin = _user(db, ids["admin"])
        staged = executor.execute("assign all the bugs to Saikat Aich", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        # Confirmation names the typed scope so the user sees exactly what's touched.
        text = staged.blocks[0].payload["text"]
        assert "3 Bug" in text, text

        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent

        db.expire_all()
        req2 = db.get(models.Bug, req_id)
        task2 = db.get(models.Bug, task_id)
        assert all(a.id != ids["admin"] for a in req2.assignees), "requirement wrongly assigned"
        assert all(a.id != ids["admin"] for a in task2.assignees), "task wrongly assigned"
        bugs_assigned = sum(
            1 for b in db.query(models.Bug).filter_by(item_type="Bug").all()
            if any(a.id == ids["admin"] for a in b.assignees)
        )
        assert bugs_assigned == 3, bugs_assigned
    finally:
        db.close()


def test_bulk_assign_all_items_covers_every_type_with_breakdown():
    """"assign all items" sweeps every type; the confirmation spells out the Bug/Requirement/Task breakdown."""
    ids = seed()
    db = SessionLocal()
    try:
        proj_id = db.query(models.Project).first().id
        _add_mixed_types(db, proj_id, ids["admin"])

        admin = _user(db, ids["admin"])
        staged = executor.execute("assign all items to Saikat Aich", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        text = staged.blocks[0].payload["text"]
        assert "5" in text, text
        assert "Bug" in text, text
        assert "Requirement" in text, text
        assert "Task" in text, text

        done = executor.execute("yes", db, admin)
        assert done.intent == "action_done", done.intent
        db.expire_all()
        assigned = sum(1 for b in db.query(models.Bug).all()
                       if any(a.id == ids["admin"] for a in b.assignees))
        assert assigned == 5, assigned
    finally:
        db.close()


def test_bulk_close_all_tasks_only_targets_tasks():
    """Type scoping applies to status changes: "close all tasks" leaves Bugs and Requirements alone."""
    ids = seed()
    db = SessionLocal()
    try:
        proj_id = db.query(models.Project).first().id
        _req_id, task_id = _add_mixed_types(db, proj_id, ids["admin"])

        admin = _user(db, ids["admin"])
        staged = executor.execute("close all tasks", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        assert "1 Task" in staged.blocks[0].payload["text"], staged.blocks[0].payload["text"]
        executor.execute("yes", db, admin)

        db.expire_all()
        assert db.get(models.Bug, task_id).status == "Closed"
        # The 3 bugs must remain New.
        assert all(b.status == "New"
                   for b in db.query(models.Bug).filter_by(item_type="Bug").all())
    finally:
        db.close()


def test_bulk_assign_with_project_filter():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        staged = executor.execute(
            "assign all bugs in project General to Saikat Aich", db, admin)
        assert staged.intent == "confirm_action", staged.intent
        assert "3" in staged.blocks[0].payload["text"]
    finally:
        db.close()


def test_bulk_no_matching_items():
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        # No Critical bugs were seeded, so the filter matches nothing.
        r = executor.execute("assign all critical bugs to Saikat Aich", db, admin)
        assert r.intent == "action_invalid", r.intent
        assert "matching" in r.blocks[0].payload["text"].lower()
    finally:
        db.close()


def test_bulk_skips_missing_ids():
    """_apply_bulk counts a vanished id as skipped, not a crash."""
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])
        plan = actions.ActionPlan(
            kind="set_status", actor_user_id=admin.id,
            bug_ids=[ids["bugs"][0], 999999], new_value="Closed",
            summary_human="Set status of all 2 items to Closed",
        )
        resp = actions.execute_plan(plan, db, admin)
        assert resp.intent == "action_done", resp.intent
        assert "1 skipped" in resp.blocks[0].payload["text"]
    finally:
        db.close()


def test_every_sleuth_op_notifies():
    """Every Sleuth write (status, comment, create bug, create project) surfaces in the bell, not just assignment."""
    ids = seed()
    db = SessionLocal()
    try:
        admin = _user(db, ids["admin"])

        def ncount():
            db.expire_all()
            return db.query(models.Notification).filter_by(user_id=ids["admin"]).count()

        base = ncount()
        executor.execute(f"close bug {ids['bugs'][0]}", db, admin)
        executor.execute("yes", db, admin)
        assert ncount() == base + 1

        executor.execute(f"comment on #{ids['bugs'][0]}: investigating", db, admin)
        executor.execute("yes", db, admin)
        assert ncount() == base + 2

        executor.execute('create a bug titled "Brand new"', db, admin)
        executor.execute("yes", db, admin)
        assert ncount() == base + 3

        executor.execute("create project Mercury", db, admin)
        executor.execute("yes", db, admin)
        assert ncount() == base + 4
    finally:
        db.close()


# --------------------------------------------------------------------------
# Role policy — managers / users are read-only via Sleuth
# --------------------------------------------------------------------------
def test_sleuth_write_denied_helper():
    ids = seed()
    db = SessionLocal()
    try:
        assert actions.sleuth_write_denied(_user(db, ids["admin"])) is None
        assert actions.sleuth_write_denied(_user(db, ids["mgr"])) is not None
        assert actions.sleuth_write_denied(_user(db, ids["usr"])) is not None
    finally:
        db.close()


def test_manager_single_write_denied():
    ids = seed()
    db = SessionLocal()
    try:
        mgr = _user(db, ids["mgr"])
        r = executor.execute(f"assign bug {ids['bugs'][0]} to Mark Manager", db, mgr)
        assert r.intent == "action_denied", r.intent
        # Nothing should have been staged.
        sess = memstore.get(ids["mgr"])
        assert sess is None or sess.pending_action is None
    finally:
        db.close()


def test_user_bulk_write_denied():
    ids = seed()
    db = SessionLocal()
    try:
        usr = _user(db, ids["usr"])
        r = executor.execute("assign all the bugs to Uma User", db, usr)
        assert r.intent == "action_denied", r.intent
        db.expire_all()
        assigned = sum(1 for b in db.query(models.Bug).all() if b.assignees)
        assert assigned == 0
    finally:
        db.close()


def test_manager_write_never_stages_then_confirm_is_idle():
    """A blocked non-admin write stages nothing, so a follow-up 'yes' can't smuggle the change through."""
    ids = seed()
    db = SessionLocal()
    try:
        mgr = _user(db, ids["mgr"])
        denied = executor.execute("close all bugs", db, mgr)
        assert denied.intent == "action_denied", denied.intent
        follow = executor.execute("yes", db, mgr)
        assert follow.intent == "confirm_idle", follow.intent
        db.expire_all()
        assert all(b.status == "New" for b in db.query(models.Bug).all())
    finally:
        db.close()


def test_manager_read_lookup_still_works():
    ids = seed()
    db = SessionLocal()
    try:
        mgr = _user(db, ids["mgr"])
        r = executor.execute("show all bugs", db, mgr)
        assert r.intent in ("list_bugs", "count_bugs"), r.intent
        r2 = executor.execute("how many bugs are there", db, mgr)
        assert r2.intent in ("list_bugs", "count_bugs"), r2.intent
    finally:
        db.close()
