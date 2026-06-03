"""Branch-coverage tests for app/routes/bugs.py.

The goal of this file is to lift the bugs route module out of the 53%
range by exercising every list-filter, the update workflow, the bulk
endpoints, comments, attachments, activity, CSV export, and the
delete + cross-tenant gates. All tests use the shared fixtures
(``client``, ``two_orgs``, ``make_invite``) defined in tests/conftest.py
so we get a fresh SQLite DB per test, no real network, and no real
email backend.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


PASS = "TestPass1!"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _signup(client, org="Acme", name="Alice", email="alice@a.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Web"):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **overrides):
    body = {
        "project_id": project_id,
        "title": overrides.get("title", "A bug we can hit"),
        "description": overrides.get("description", "details"),
        "status": overrides.get("status", "New"),
        "priority": overrides.get("priority", "Medium"),
        "environment": overrides.get("environment", "DEV"),
    }
    for k, v in overrides.items():
        body[k] = v
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _bootstrap(client, org="Acme"):
    """Sign up admin and create one project. Returns (me, project)."""
    me = _signup(client, org=org)
    proj = _make_project(client)
    return me, proj


# ---------------------------------------------------------------------------
# list_bugs filter dispatch
# ---------------------------------------------------------------------------
class TestListBugs:
    def test_empty_when_no_projects(self, client):
        _signup(client)
        r = client.get("/api/bugs")
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["pages"] == 0

    def test_filter_status_priority_environment(self, client):
        _, p = _bootstrap(client)
        b1 = _make_bug(client, p["id"], title="alpha title", status="New",
                       priority="High", environment="DEV")
        b2 = _make_bug(client, p["id"], title="beta title", status="Closed",
                       priority="Low", environment="PROD")
        b3 = _make_bug(client, p["id"], title="gamma title", status="Resolved",
                       priority="Critical", environment="UAT")

        r = client.get("/api/bugs?status=New")
        ids = [b["id"] for b in r.json()["items"]]
        assert b1["id"] in ids and b2["id"] not in ids and b3["id"] not in ids

        r = client.get("/api/bugs?priority=Low")
        ids = [b["id"] for b in r.json()["items"]]
        assert ids == [b2["id"]]

        r = client.get("/api/bugs?environment=PROD&environment=UAT")
        ids = sorted(b["id"] for b in r.json()["items"])
        assert ids == sorted([b2["id"], b3["id"]])

    def test_filter_invalid_choice_returns_400(self, client):
        _bootstrap(client)
        r = client.get("/api/bugs?status=NotARealStatus")
        assert r.status_code == 400
        r = client.get("/api/bugs?priority=Bogus")
        assert r.status_code == 400
        r = client.get("/api/bugs?environment=NOPE")
        assert r.status_code == 400
        r = client.get("/api/bugs?item_type=Garbage")
        assert r.status_code == 400

    def test_filter_assignee_and_reporter(self, client, make_invite):
        admin = _signup(client)
        proj = _make_project(client)

        # Invite + accept a second user (member of org).
        tok = make_invite(client, "carol@a.test", role="member")
        from app.main import app
        with TestClient(app) as carol_c:
            carol_c.post("/api/invitations/accept", json={
                "token": tok, "name": "Carol", "password": PASS,
            })
            carol_id = carol_c.get("/api/auth/me").json()["id"]

        # Add Carol to the project as a member so she's accessible.
        r = client.post(f"/api/projects/{proj['id']}/members",
                        json={"user_id": carol_id, "project_role": "member"})
        assert r.status_code in (200, 201)

        # Bug assigned to Carol, reported by admin.
        b_assigned = _make_bug(client, proj["id"], title="assigned to carol",
                               assignee_ids=[carol_id])
        b_other = _make_bug(client, proj["id"], title="unassigned")

        r = client.get(f"/api/bugs?assignee_id={carol_id}")
        ids = [b["id"] for b in r.json()["items"]]
        assert b_assigned["id"] in ids
        assert b_other["id"] not in ids

        r = client.get(f"/api/bugs?reporter_id={admin['id']}")
        ids = [b["id"] for b in r.json()["items"]]
        # Both bugs were reported by admin
        assert b_assigned["id"] in ids and b_other["id"] in ids

    def test_filter_project_intersects_with_accessible(self, client):
        _, p1 = _bootstrap(client)
        p2 = _make_project(client, name="Mobile")
        b1 = _make_bug(client, p1["id"], title="p1 only")
        _make_bug(client, p2["id"], title="p2 only")

        r = client.get(f"/api/bugs?project_id={p1['id']}")
        ids = [b["id"] for b in r.json()["items"]]
        assert ids == [b1["id"]]

        # Asking for an inaccessible project ID -> nothing returned, not 403.
        r = client.get("/api/bugs?project_id=999999")
        assert r.status_code == 200
        assert r.json()["items"] == []
        assert r.json()["total"] == 0

    def test_search_q_textual_and_id(self, client):
        _, p = _bootstrap(client)
        b1 = _make_bug(client, p["id"], title="login bug")
        b2 = _make_bug(client, p["id"], title="logout regression")
        b3 = _make_bug(client, p["id"], title="dashboard tweak")

        # Text search on title
        r = client.get("/api/bugs?q=log")
        ids = sorted(b["id"] for b in r.json()["items"])
        assert b1["id"] in ids and b2["id"] in ids
        assert b3["id"] not in ids

        # Numeric search → exact id match
        r = client.get(f"/api/bugs?q={b1['id']}")
        ids = [b["id"] for b in r.json()["items"]]
        assert ids == [b1["id"]]

        # #-prefixed numeric search
        r = client.get(f"/api/bugs?q=%23{b2['id']}")
        ids = [b["id"] for b in r.json()["items"]]
        assert ids == [b2["id"]]

        # Trims to empty → no filter, all returned
        r = client.get("/api/bugs?q=%23")
        assert r.status_code == 200
        assert r.json()["total"] >= 3

        # SQL-like wildcard characters should be escaped, not treated as wildcards.
        r = client.get("/api/bugs?q=%25")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_pagination(self, client):
        _, p = _bootstrap(client)
        for i in range(7):
            _make_bug(client, p["id"], title=f"bug-{i:02d}")

        r = client.get("/api/bugs?page=1&page_size=3")
        body = r.json()
        assert body["page"] == 1 and body["page_size"] == 3
        assert body["total"] == 7
        assert body["pages"] == 3
        assert len(body["items"]) == 3

        r = client.get("/api/bugs?page=3&page_size=3")
        assert len(r.json()["items"]) == 1

    def test_pagination_invalid_params(self, client):
        _bootstrap(client)
        assert client.get("/api/bugs?page=0").status_code == 400
        assert client.get("/api/bugs?page_size=0").status_code == 400
        assert client.get("/api/bugs?page_size=999").status_code == 400

    def test_item_type_filter_and_event_filter(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"], title="just a bug", item_type="Bug")
        task = _make_bug(client, p["id"], title="a task to do", item_type="Task")

        # Create an event and attach a bug to it.
        r = client.post("/api/events", json={"name": "Launch"})
        assert r.status_code == 201, r.text
        event_id = r.json()["id"]
        ev_bug = _make_bug(client, p["id"], title="event-linked",
                           item_type="Bug", event_id=event_id)

        # Filter by item_type=Task
        r = client.get("/api/bugs?item_type=Task")
        ids = [b["id"] for b in r.json()["items"]]
        assert task["id"] in ids
        assert bug["id"] not in ids and ev_bug["id"] not in ids

        # event_id=<id> returns the linked one
        r = client.get(f"/api/bugs?event_id={event_id}")
        ids = [b["id"] for b in r.json()["items"]]
        assert ids == [ev_bug["id"]]

        # event_id=0 → items NOT attached to an event
        r = client.get("/api/bugs?event_id=0")
        ids = sorted(b["id"] for b in r.json()["items"])
        assert ev_bug["id"] not in ids
        assert bug["id"] in ids and task["id"] in ids


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
class TestBugDetail:
    def test_detail_missing_404(self, client):
        _bootstrap(client)
        r = client.get("/api/bugs/999999")
        assert r.status_code == 404

    def test_detail_cross_org_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_a = c_a.post("/api/projects", json={"name": "AlphaProj"}).json()
        bug = c_a.post("/api/bugs", json={
            "project_id": p_a["id"], "title": "secret to A",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        # B cannot see A's bug.
        assert c_b.get(f"/api/bugs/{bug['id']}").status_code == 404

    def test_detail_includes_comments_attachments_and_activity(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        # Add a comment
        rc = client.post(f"/api/bugs/{bug['id']}/comments",
                         json={"body": "first comment"})
        assert rc.status_code == 201, rc.text
        comment_id = rc.json()["id"]
        # Attach a file at the bug level
        files = {"file": ("note.txt", b"hello", "text/plain")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        assert r.status_code == 201, r.text
        # Attach a file scoped to the comment
        files = {"file": ("c.txt", b"hi", "text/plain")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments",
                        files=files, data={"comment_id": str(comment_id)})
        assert r.status_code == 201, r.text

        r = client.get(f"/api/bugs/{bug['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == bug["id"]
        # bug-level attachment is on the bug payload, comment-level is on the comment
        assert len(body["attachments"]) == 1
        assert any(c["id"] == comment_id and len(c["attachments"]) == 1
                   for c in body["comments"])
        # Activities list always populated (creation row exists)
        assert isinstance(body["activities"], list) and len(body["activities"]) >= 1


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
class TestCreateBug:
    def test_create_bad_project_400(self, client):
        _signup(client)
        r = client.post("/api/bugs", json={
            "project_id": 9999, "title": "missing project",
            "status": "New", "priority": "Low", "environment": "DEV",
        })
        assert r.status_code == 400

    def test_create_with_event_validates_org(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        c_a.post("/api/projects", json={"name": "AlphaProj"})
        ev_a = c_a.post("/api/events", json={"name": "Launch"}).json()
        # Org B has its own project but tries to attach to org A's event.
        p_b = c_b.post("/api/projects", json={"name": "BetaProj"}).json()
        r = c_b.post("/api/bugs", json={
            "project_id": p_b["id"], "title": "cross-tenant event",
            "status": "New", "priority": "Low", "environment": "DEV",
            "event_id": ev_a["id"],
        })
        assert r.status_code == 400

    def test_create_member_cannot_file_task(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "member@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as member_c:
            member_c.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            # Member CAN file a Bug
            r = member_c.post("/api/bugs", json={
                "project_id": proj["id"], "title": "member bug",
                "status": "New", "priority": "Low", "environment": "DEV",
                "item_type": "Bug",
            })
            assert r.status_code == 201
            # But cannot file a Task
            r = member_c.post("/api/bugs", json={
                "project_id": proj["id"], "title": "member task",
                "status": "New", "priority": "Low", "environment": "DEV",
                "item_type": "Task",
            })
            assert r.status_code == 403

    def test_member_cannot_set_other_reporter(self, client, make_invite):
        admin = _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            r = mc.post("/api/bugs", json={
                "project_id": proj["id"], "title": "bug with bad reporter",
                "status": "New", "priority": "Low", "environment": "DEV",
                "reporter_id": admin["id"],
            })
            assert r.status_code == 403

    def test_create_with_assignees_logs_audit(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        # Invite Carol so she's resolvable
        tok = make_invite(client, "carol@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as cc:
            cc.post("/api/invitations/accept", json={
                "token": tok, "name": "Carol", "password": PASS,
            })
            carol_id = cc.get("/api/auth/me").json()["id"]
        # Admin creates the bug assigning Carol
        bug = _make_bug(client, proj["id"], title="assigned bug",
                        assignee_ids=[carol_id])
        r = client.get(f"/api/bugs/{bug['id']}/activity")
        assert r.status_code == 200
        actions = {row["action"] for row in r.json()}
        assert "bug_created" in actions
        assert "assignees_added" in actions

    def test_unknown_assignee_400(self, client):
        _, p = _bootstrap(client)
        r = client.post("/api/bugs", json={
            "project_id": p["id"], "title": "bad assignee",
            "status": "New", "priority": "Low", "environment": "DEV",
            "assignee_ids": [99999],
        })
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------
class TestUpdateBug:
    def test_update_status_priority_environment(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        r = client.put(f"/api/bugs/{bug['id']}", json={
            "status": "In Progress", "priority": "Critical", "environment": "PROD",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "In Progress"
        assert body["priority"] == "Critical"
        assert body["environment"] == "PROD"
        # Audit rows were written.
        acts = client.get(f"/api/bugs/{bug['id']}/activity").json()
        actions = {a["action"] for a in acts}
        assert "status_changed" in actions
        assert "priority_changed" in actions
        assert "environment_changed" in actions

    def test_update_noop_does_not_change_updated_at(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        before = client.get(f"/api/bugs/{bug['id']}").json()["updated_at"]
        # Sending the same status is a no-op (no tracked change).
        r = client.put(f"/api/bugs/{bug['id']}", json={"status": bug["status"]})
        assert r.status_code == 200
        after = client.get(f"/api/bugs/{bug['id']}").json()["updated_at"]
        assert before == after

    def test_update_assignees_diff(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        # Invite Carol and Dave
        for email in ("carol@a.test", "dave@a.test"):
            tok = make_invite(client, email, role="member",
                              project_ids=[proj["id"]])
            from app.main import app
            with TestClient(app) as cc:
                cc.post("/api/invitations/accept", json={
                    "token": tok, "name": email.split("@")[0], "password": PASS,
                })

        # Resolve their ids from the org's user list
        users = client.get("/api/users").json()
        by_email = {u["email"]: u["id"] for u in users}
        carol_id = by_email["carol@a.test"]
        dave_id = by_email["dave@a.test"]

        bug = _make_bug(client, proj["id"], title="assignee dance",
                        assignee_ids=[carol_id])

        # Replace Carol with Dave.
        r = client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": [dave_id]})
        assert r.status_code == 200
        body = r.json()
        ids = sorted(a["id"] for a in body["assignees"])
        assert ids == [dave_id]
        # Activity log should mention the assignee change.
        acts = client.get(f"/api/bugs/{bug['id']}/activity").json()
        assert any(a["action"] == "assignees_changed" for a in acts)

        # Setting the same list again is a no-op.
        r2 = client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": [dave_id]})
        assert r2.status_code == 200
        ids_again = sorted(a["id"] for a in r2.json()["assignees"])
        assert ids_again == [dave_id]

    def test_update_project_move(self, client):
        _, p1 = _bootstrap(client)
        p2 = _make_project(client, name="Mobile")
        bug = _make_bug(client, p1["id"], title="movable")
        r = client.put(f"/api/bugs/{bug['id']}", json={"project_id": p2["id"]})
        assert r.status_code == 200
        assert r.json()["project_id"] == p2["id"]

    def test_update_to_unknown_project_400(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        r = client.put(f"/api/bugs/{bug['id']}", json={"project_id": 99999})
        assert r.status_code == 400

    def test_update_to_inaccessible_project_403(self, client, make_invite):
        # Admin creates two projects; member only has access to project 1.
        _signup(client)
        p1 = _make_project(client, name="Visible")
        p2 = _make_project(client, name="Hidden")
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[p1["id"]])
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            bug = _make_bug(mc, p1["id"], title="move me")
            r = mc.put(f"/api/bugs/{bug['id']}", json={"project_id": p2["id"]})
            assert r.status_code == 403

    def test_update_item_type_member_blocked(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            bug = _make_bug(mc, proj["id"], title="member bug",
                            item_type="Bug")
            # Member cannot promote to Task.
            r = mc.put(f"/api/bugs/{bug['id']}",
                       json={"item_type": "Task"})
            assert r.status_code == 403

    def test_update_event_id_validated(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        # A: project + bug.
        p_a = c_a.post("/api/projects", json={"name": "AlphaProj"}).json()
        bug = c_a.post("/api/bugs", json={
            "project_id": p_a["id"], "title": "A bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        # B: event.
        ev_b = c_b.post("/api/events", json={"name": "B-event"}).json()
        # A tries to attach to B's event.
        r = c_a.put(f"/api/bugs/{bug['id']}", json={"event_id": ev_b["id"]})
        assert r.status_code == 400

    def test_update_reporter_member_blocked(self, client, make_invite):
        admin = _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            me = mc.get("/api/auth/me").json()
            bug = _make_bug(mc, proj["id"], title="member bug")
            # Member tries to swap the reporter to the admin → 403.
            r = mc.put(f"/api/bugs/{bug['id']}",
                       json={"reporter_id": admin["id"]})
            assert r.status_code == 403
            # But echoing the existing reporter id is fine (no actual change).
            r = mc.put(f"/api/bugs/{bug['id']}",
                       json={"reporter_id": me["id"], "title": "new title"})
            assert r.status_code == 200

    def test_admin_can_change_reporter_and_clear_it(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "carol@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as cc:
            cc.post("/api/invitations/accept", json={
                "token": tok, "name": "Carol", "password": PASS,
            })
            carol_id = cc.get("/api/auth/me").json()["id"]

        bug = _make_bug(client, proj["id"], title="reporter swap")
        # Change reporter to Carol.
        r = client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": carol_id})
        assert r.status_code == 200
        assert r.json()["reporter"]["id"] == carol_id

        # Clear reporter explicitly with null.
        r = client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": None})
        assert r.status_code == 200
        assert r.json()["reporter"] is None

    def test_update_cross_org_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_a = c_a.post("/api/projects", json={"name": "AlphaProj"}).json()
        bug = c_a.post("/api/bugs", json={
            "project_id": p_a["id"], "title": "A only",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        # B trying to PUT it.
        r = c_b.put(f"/api/bugs/{bug['id']}", json={"status": "Closed"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
class TestDeleteBug:
    def test_admin_can_delete_and_audit_survives(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        r = client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 200
        # Audit endpoint should still mention the deleted bug.
        audit = client.get("/api/audit").json()
        rows = audit if isinstance(audit, list) else audit.get("items", [])
        assert any(row["action"] == "bug_deleted" for row in rows)

    def test_member_cannot_delete(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            bug = _make_bug(mc, proj["id"], title="cannot delete")
            r = mc.delete(f"/api/bugs/{bug['id']}")
            assert r.status_code == 403


# ---------------------------------------------------------------------------
# Bulk update / delete
# ---------------------------------------------------------------------------
class TestBulkPaths:
    def test_bulk_update_assignees_add_remove(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        # Invite Carol + Dave.
        for email in ("carol@a.test", "dave@a.test"):
            tok = make_invite(client, email, role="member",
                              project_ids=[proj["id"]])
            from app.main import app
            with TestClient(app) as cc:
                cc.post("/api/invitations/accept", json={
                    "token": tok, "name": email.split("@")[0], "password": PASS,
                })

        users = client.get("/api/users").json()
        by_email = {u["email"]: u["id"] for u in users}
        carol_id = by_email["carol@a.test"]
        dave_id = by_email["dave@a.test"]

        ids = [
            _make_bug(client, proj["id"], title="bulk-bug-one")["id"],
            _make_bug(client, proj["id"], title="bulk-bug-two")["id"],
        ]

        # Add Carol to both.
        r = client.post("/api/bugs/bulk-update", json={
            "bug_ids": ids, "add_assignee_ids": [carol_id],
        })
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == 2

        # Re-adding Carol is a no-op (passes gate but no diff).
        r = client.post("/api/bugs/bulk-update", json={
            "bug_ids": ids, "add_assignee_ids": [carol_id],
        })
        assert r.status_code == 200
        body = r.json()
        # No-op rows are counted as NEITHER updated NOR skipped.
        assert body["updated"] == 0 and body["skipped"] == 0

        # Remove Carol from both.
        r = client.post("/api/bugs/bulk-update", json={
            "bug_ids": ids, "remove_assignee_ids": [carol_id],
        })
        assert r.status_code == 200
        assert r.json()["updated"] == 2

        # Mixing add + remove on Dave who's not assigned: removing is a no-op.
        r = client.post("/api/bugs/bulk-update", json={
            "bug_ids": ids,
            "add_assignee_ids": [dave_id],
            "remove_assignee_ids": [carol_id],
        })
        assert r.status_code == 200
        assert r.json()["updated"] == 2

    def test_bulk_update_skips_inaccessible_bugs(self, client, make_invite):
        _signup(client)
        p1 = _make_project(client, name="Visible")
        p2 = _make_project(client, name="Hidden")
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[p1["id"]])
        bug_hidden = _make_bug(client, p2["id"], title="hidden")
        bug_visible = _make_bug(client, p1["id"], title="visible")
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            r = mc.post("/api/bugs/bulk-update", json={
                "bug_ids": [bug_visible["id"], bug_hidden["id"]],
                "status": "Closed",
            })
            assert r.status_code == 200
            body = r.json()
            # One updated, one skipped (not in accessible projects).
            assert body["updated"] == 1
            assert body["skipped"] == 1

    def test_bulk_delete_member_skips_silently(self, client, make_invite):
        _signup(client)
        proj = _make_project(client)
        bug = _make_bug(client, proj["id"])
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app
        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            r = mc.post("/api/bugs/bulk-delete", json={"bug_ids": [bug["id"]]})
            assert r.status_code == 200
            body = r.json()
            assert body["deleted"] == 0
            assert body["skipped"] == 1

    def test_bulk_update_no_diff_rollback(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"], status="New")
        # Updating status to current value yields zero updated bugs.
        r = client.post("/api/bugs/bulk-update", json={
            "bug_ids": [bug["id"]], "status": "New",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["updated"] == 0


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
class TestCSVExport:
    def test_export_when_empty(self, client):
        _signup(client)
        r = client.get("/api/bugs/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        # Header row always present.
        assert "id,project,project_key,title" in r.text

    def test_export_includes_bug_rows(self, client):
        _, p = _bootstrap(client)
        _make_bug(client, p["id"], title="One",
                  description="line1\nline2\rline3")
        _make_bug(client, p["id"], title="Two")
        r = client.get("/api/bugs/export.csv")
        assert r.status_code == 200
        text = r.text
        assert "One" in text and "Two" in text
        # Newlines in description must be collapsed for CSV safety.
        assert "line1\nline2" not in text


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
class TestComments:
    def test_list_and_add_comment(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])

        r = client.get(f"/api/bugs/{bug['id']}/comments")
        assert r.status_code == 200
        assert r.json() == []

        r = client.post(f"/api/bugs/{bug['id']}/comments",
                        json={"body": "first thoughts"})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]

        r = client.get(f"/api/bugs/{bug['id']}/comments")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["id"] == cid
        assert items[0]["body"] == "first thoughts"

    def test_comment_on_missing_bug_404(self, client):
        _bootstrap(client)
        r = client.post("/api/bugs/99999/comments", json={"body": "x"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
class TestAttachments:
    def test_upload_download_delete(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])

        files = {"file": ("hello.txt", b"hello world", "text/plain")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        assert r.status_code == 201, r.text
        att = r.json()
        assert att["filename"] == "hello.txt"
        assert att["size_bytes"] == 11

        # Download
        r = client.get(f"/api/bugs/{bug['id']}/attachments/{att['id']}/download")
        assert r.status_code == 200
        assert r.content == b"hello world"
        assert "filename=" in r.headers["content-disposition"]
        assert r.headers["x-content-type-options"] == "nosniff"

        # Delete (admin can)
        r = client.delete(f"/api/bugs/{bug['id']}/attachments/{att['id']}")
        assert r.status_code == 200

        # Missing attachment 404
        r = client.get(f"/api/bugs/{bug['id']}/attachments/9999/download")
        assert r.status_code == 404
        r = client.delete(f"/api/bugs/{bug['id']}/attachments/9999")
        assert r.status_code == 404

    def test_upload_empty_file_rejected(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        files = {"file": ("e.txt", b"", "text/plain")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        assert r.status_code == 400

    def test_upload_bad_comment_id_400(self, client):
        _, p = _bootstrap(client)
        bug1 = _make_bug(client, p["id"], title="Owner of comment")
        bug2 = _make_bug(client, p["id"], title="Other bug")
        # Comment under bug1
        rc = client.post(f"/api/bugs/{bug1['id']}/comments",
                         json={"body": "c on bug 1"})
        cid = rc.json()["id"]
        # Try to attach to bug2 with bug1's comment_id → 400.
        files = {"file": ("x.txt", b"x", "text/plain")}
        r = client.post(f"/api/bugs/{bug2['id']}/attachments",
                        files=files, data={"comment_id": str(cid)})
        assert r.status_code == 400

    def test_download_active_content_sanitized(self, client):
        """SVG / HTML uploads should be served as attachment+octet-stream
        so a browser never executes them inline."""
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        files = {"file": ("evil.svg",
                          b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                          "image/svg+xml")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        att_id = r.json()["id"]
        r = client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["content-type"].startswith("application/octet-stream")

    def test_download_filename_with_non_ascii(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        files = {"file": ("rëadme.txt", b"hi", "text/plain")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        att_id = r.json()["id"]
        r = client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert r.status_code == 200
        # ASCII-safe portion uses underscore for the non-ASCII char;
        # the RFC 5987 filename* uses percent-encoding.
        cd = r.headers["content-disposition"]
        assert "filename*=UTF-8''" in cd

    def test_member_can_delete_own_attachment_but_not_others(
        self, client, make_invite,
    ):
        _signup(client)
        proj = _make_project(client)
        tok = make_invite(client, "m@a.test", role="member",
                          project_ids=[proj["id"]])
        from app.main import app

        # Admin uploads an attachment.
        bug = _make_bug(client, proj["id"], title="shared bug")
        files = {"file": ("admin.txt", b"a", "text/plain")}
        r = client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        admin_att = r.json()["id"]

        with TestClient(app) as mc:
            mc.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            # Member uploads own attachment
            files = {"file": ("mem.txt", b"m", "text/plain")}
            r = mc.post(f"/api/bugs/{bug['id']}/attachments", files=files)
            assert r.status_code == 201
            mem_att = r.json()["id"]
            # Member cannot delete the admin's attachment
            r = mc.delete(f"/api/bugs/{bug['id']}/attachments/{admin_att}")
            assert r.status_code == 403
            # But CAN delete their own
            r = mc.delete(f"/api/bugs/{bug['id']}/attachments/{mem_att}")
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------
class TestActivity:
    def test_activity_endpoint(self, client):
        _, p = _bootstrap(client)
        bug = _make_bug(client, p["id"])
        # Trigger more rows.
        client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
        client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "hi"})

        r = client.get(f"/api/bugs/{bug['id']}/activity")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list) and len(body) >= 3
        actions = {row["action"] for row in body}
        assert "bug_created" in actions
        assert "status_changed" in actions
        assert "comment_added" in actions

    def test_activity_cross_org_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_a = c_a.post("/api/projects", json={"name": "AlphaProj"}).json()
        bug = c_a.post("/api/bugs", json={
            "project_id": p_a["id"], "title": "A bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        assert c_b.get(f"/api/bugs/{bug['id']}/activity").status_code == 404
