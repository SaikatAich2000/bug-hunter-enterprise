"""Project-scoped access control tests (user_projects + app/access.py); admins unrestricted.
Covers every scoped surface: REST, reports engine, Sleuth read handlers, helpers, plumbing.
"""
from __future__ import annotations

from collections import namedtuple

BOOTSTRAP_EMAIL = "admin@test.local"
BOOTSTRAP_PASSWORD = "Admin1234"
_PW = "ProjAccess9X"

# A two-project world: a project + a bug in each.
_World = namedtuple("_World", "pa pb ba bb")


# Helpers — sequential logins on a single TestClient (each call replaces the cookie).
def _login(c, email, pw=_PW):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text


def _as_admin(c):
    _login(c, BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD)


def _mk_project(c, name):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_user(c, email, role="manager", project_ids=None, pw=_PW):
    body = {"name": email.split("@")[0], "email": email, "role": role, "password": pw}
    if project_ids is not None:
        body["project_ids"] = project_ids
    r = c.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(c, project_id, title="A bug", **extra):
    body = {"title": title, "project_id": project_id}
    body.update(extra)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _bug_ids(c, **params):
    r = c.get("/api/bugs", params=params)
    assert r.status_code == 200, r.text
    return {b["id"] for b in r.json()["items"]}, r.json()["total"]


def _chat(c, message):
    r = c.post("/api/chat/ask", json={"message": message})
    assert r.status_code == 200, r.text
    return r.json()


def _chat_table_rows(resp):
    for b in resp["blocks"]:
        if b["kind"] == "table":
            return b["payload"].get("rows", [])
    return []


def _two_project_world(c):
    """Create two projects with one bug each, as admin. Returns (pa, pb, ba, bb)."""
    _as_admin(c)
    pa = _mk_project(c, "Alpha")
    pb = _mk_project(c, "Beta")
    ba = _mk_bug(c, pa["id"], title="Alpha bug")
    bb = _mk_bug(c, pb["id"], title="Beta bug")
    return _World(pa, pb, ba, bb)


# access.py helpers (direct unit tests)
def test_access_helpers_direct(client):
    """Unit tests for accessible_project_ids, can_access_project, and membership helpers."""
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    mgr = _mk_user(client, "mgr@x.com", role="manager", project_ids=[pa["id"]])

    import app.access as access
    from app.database import SessionLocal
    from app.models import ROLE_ADMIN, User

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == BOOTSTRAP_EMAIL).one()
        manager = db.get(User, mgr["id"])

        # Admin sees every project of the organization; manager is limited to their assigned set.
        assert access.accessible_project_ids(db, admin) >= {pa["id"], pb["id"]}
        assert access.accessible_project_ids(db, manager) == {pa["id"]}

        # A None project_id is never in any scope.
        assert access.can_access_project({pa["id"]}, pa["id"]) is True
        assert access.can_access_project({pa["id"]}, pb["id"]) is False
        assert access.can_access_project({pa["id"]}, None) is False

        assert access.project_ids_for_user(db, manager.id) == [pa["id"]]

        # set_user_projects replaces (deduplicates); add_user_project is idempotent.
        access.set_user_projects(db, manager.id, [pa["id"], pb["id"], pa["id"]])
        db.commit()
        assert access.project_ids_for_user(db, manager.id) == sorted([pa["id"], pb["id"]])
        access.add_user_project(db, manager.id, pa["id"])  # already present → no-op
        db.commit()
        assert access.project_ids_for_user(db, manager.id) == sorted([pa["id"], pb["id"]])

        assert admin.role == ROLE_ADMIN
    finally:
        db.close()


# Users API — project_ids plumbing
def test_create_user_with_project_tags_roundtrips(client):
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    # Duplicates are de-duped server-side.
    u = _mk_user(client, "tagged@x.com", role="manager",
                 project_ids=[pa["id"], pb["id"], pa["id"]])
    assert sorted(u["project_ids"]) == sorted([pa["id"], pb["id"]])

    # Both get_user and list_users must include project_ids.
    got = client.get(f"/api/users/{u['id']}").json()
    assert sorted(got["project_ids"]) == sorted([pa["id"], pb["id"]])
    listed = {row["id"]: row for row in client.get("/api/users").json()}
    assert sorted(listed[u["id"]]["project_ids"]) == sorted([pa["id"], pb["id"]])


def test_create_user_unknown_project_400(client):
    _as_admin(client)
    r = client.post("/api/users", json={
        "name": "Bad", "email": "bad@x.com", "role": "manager",
        "password": _PW, "project_ids": [999999],
    })
    assert r.status_code == 400
    assert "Unknown project" in r.json()["detail"]


def test_update_user_project_tags_replace_clear_unchanged(client):
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    u = _mk_user(client, "edit@x.com", role="manager", project_ids=[pa["id"]])

    # Replace tags entirely.
    r = client.put(f"/api/users/{u['id']}", json={"project_ids": [pb["id"]]})
    assert r.status_code == 200
    assert r.json()["project_ids"] == [pb["id"]]

    # Omitting project_ids in the payload must leave them unchanged.
    r = client.put(f"/api/users/{u['id']}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["project_ids"] == [pb["id"]]

    # Empty list clears all tags.
    r = client.put(f"/api/users/{u['id']}", json={"project_ids": []})
    assert r.status_code == 200
    assert r.json()["project_ids"] == []

    # Sending the same empty set again exercises the unchanged-set short-circuit.
    r = client.put(f"/api/users/{u['id']}", json={"project_ids": []})
    assert r.status_code == 200
    assert r.json()["project_ids"] == []


def test_manager_can_only_grant_own_projects(client):
    """A restricted manager may only assign projects they can access when creating or editing users."""
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    _mk_user(client, "mgr@x.com", role="manager", project_ids=[pa["id"]])

    _login(client, "mgr@x.com")
    r = client.post("/api/users", json={
        "name": "Sub", "email": "sub@x.com", "role": "user",
        "password": _PW, "project_ids": [pa["id"]],
    })
    assert r.status_code == 201, r.text
    sub_id = r.json()["id"]
    # Granting a project outside their scope is forbidden on create and update.
    r = client.post("/api/users", json={
        "name": "Sub2", "email": "sub2@x.com", "role": "user",
        "password": _PW, "project_ids": [pb["id"]],
    })
    assert r.status_code == 403
    r = client.put(f"/api/users/{sub_id}", json={"project_ids": [pb["id"]]})
    assert r.status_code == 403


# Bugs — list / detail / create / update / sub-resources / bulk
def test_bug_list_scoped_by_membership(client):
    w = _two_project_world(client)
    ids, total = _bug_ids(client)
    assert {w.ba["id"], w.bb["id"]} <= ids
    assert total >= 2

    # Manager tagged to Alpha only sees Alpha's bug.
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    ids, total = _bug_ids(client)
    assert ids == {w.ba["id"]}
    assert total == 1

    # No project memberships means no visible bugs.
    _as_admin(client)
    _mk_user(client, "none@x.com", role="user", project_ids=[])
    _login(client, "none@x.com")
    ids, total = _bug_ids(client)
    assert ids == set()
    assert total == 0


def test_bug_detail_and_subresources_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")

    assert client.get(f"/api/bugs/{w.ba['id']}").status_code == 200
    assert client.get(f"/api/bugs/{w.bb['id']}").status_code == 404

    # All sub-resources on an out-of-scope bug must 404, including writes.
    for path in ("comments", "activity", "links"):
        assert client.get(f"/api/bugs/{w.bb['id']}/{path}").status_code == 404
    assert client.post(f"/api/bugs/{w.bb['id']}/comments",
                       json={"body": "hi"}).status_code == 404
    assert client.post(f"/api/bugs/{w.ba['id']}/comments",
                       json={"body": "hello"}).status_code == 201


def test_bug_create_and_update_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")

    assert client.post("/api/bugs", json={"title": "Mine", "project_id": w.pa["id"]}).status_code == 201
    r = client.post("/api/bugs", json={"title": "Nope", "project_id": w.pb["id"]})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]

    # Out-of-scope bug is 404; moving an in-scope bug to an inaccessible project is rejected.
    assert client.put(f"/api/bugs/{w.bb['id']}", json={"priority": "High"}).status_code == 404
    r = client.put(f"/api/bugs/{w.ba['id']}", json={"project_id": w.pb["id"]})
    assert r.status_code == 400


def test_bug_links_scoped_to_both_endpoints(client):
    w = _two_project_world(client)
    _as_admin(client)
    ba2 = _mk_bug(client, w.pa["id"], title="Alpha bug 2")
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")

    r = client.post(f"/api/bugs/{w.ba['id']}/links",
                    json={"target_bug_id": ba2["id"], "link_type": "relates"})
    assert r.status_code == 201, r.text
    # Linking to an out-of-scope bug must be rejected.
    r = client.post(f"/api/bugs/{w.ba['id']}/links",
                    json={"target_bug_id": w.bb["id"], "link_type": "relates"})
    assert r.status_code == 400


def test_bulk_action_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    r = client.post("/api/bugs/bulk", json={
        "action": "set_priority", "value": "High", "ids": [w.ba["id"], w.bb["id"]],
    })
    assert r.status_code == 200
    body = r.json()
    # The Alpha bug updates; the Beta bug is out-of-scope so it counts as failed.
    assert body["updated"] == 1
    assert body["failed"] == 1


def test_bug_create_and_update_with_out_of_scope_event_rejected(client):
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    ev_b = client.post("/api/events", json={"name": "Beta ev", "project_id": pb["id"]}).json()
    own = _mk_bug(client, pa["id"], title="own")
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[pa["id"]])
    _login(client, "alpha@x.com")
    # An out-of-scope event_id must be treated as if it doesn't exist.
    r = client.post("/api/bugs", json={
        "title": "linking test bug", "project_id": pa["id"], "event_id": ev_b["id"],
    })
    assert r.status_code == 400
    assert "Event does not exist" in r.json()["detail"]
    r = client.put(f"/api/bugs/{own['id']}", json={"event_id": ev_b["id"]})
    assert r.status_code == 400


def test_attachment_download_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    files = {"file": ("a.txt", b"hi there", "text/plain")}
    att = client.post(f"/api/bugs/{w.bb['id']}/attachments", files=files).json()
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    # Downloading an attachment on an out-of-scope bug must return 404.
    r = client.get(f"/api/bugs/{w.bb['id']}/attachments/{att['id']}/download")
    assert r.status_code == 404


def test_attachment_download_in_scope_succeeds(client):
    # Counterpart to the 404 case above: a scoped (non-admin) user downloading
    # an attachment on a bug within their own accessible project must succeed.
    w = _two_project_world(client)
    _as_admin(client)
    files = {"file": ("a.txt", b"hi there", "text/plain")}
    att = client.post(f"/api/bugs/{w.ba['id']}/attachments", files=files).json()
    _mk_user(client, "alpha2@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha2@x.com")
    r = client.get(f"/api/bugs/{w.ba['id']}/attachments/{att['id']}/download")
    assert r.status_code == 200
    assert r.content == b"hi there"


# Events — require/validate project + scoped visibility
def test_event_project_scoping(client):
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    ev_a = client.post("/api/events", json={"name": "Alpha sprint", "project_id": pa["id"]}).json()
    ev_b = client.post("/api/events", json={"name": "Beta sprint", "project_id": pb["id"]}).json()
    assert ev_a["project_id"] == pa["id"]
    assert ev_a["project_name"] == "Alpha"
    # A project-less event is only visible to admins.
    ev_none = client.post("/api/events", json={"name": "No project"}).json()
    assert ev_none["project_id"] is None

    _mk_user(client, "alpha@x.com", role="manager", project_ids=[pa["id"]])
    _login(client, "alpha@x.com")

    ids = {e["id"] for e in client.get("/api/events").json()}
    assert ids == {ev_a["id"]}
    # Out-of-scope and project-less events both appear as 404.
    assert client.get(f"/api/events/{ev_a['id']}").status_code == 200
    assert client.get(f"/api/events/{ev_b['id']}").status_code == 404
    assert client.get(f"/api/events/{ev_none['id']}").status_code == 404


def test_event_create_update_restricted_to_scope(client):
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[pa["id"]])
    _login(client, "alpha@x.com")

    ev = client.post("/api/events", json={"name": "Mine", "project_id": pa["id"]})
    assert ev.status_code == 201, ev.text
    ev_id = ev.json()["id"]
    r = client.post("/api/events", json={"name": "Nope", "project_id": pb["id"]})
    assert r.status_code == 400
    # Moving the event out of their scope must also be rejected.
    r = client.put(f"/api/events/{ev_id}", json={"project_id": pb["id"]})
    assert r.status_code == 400


def test_event_project_move_within_scope_succeeds(client):
    """Moving an event between two accessible projects updates project_id correctly."""
    _as_admin(client)  # admin can access both projects
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    ev = client.post("/api/events", json={"name": "Movable", "project_id": pa["id"]}).json()
    r = client.put(f"/api/events/{ev['id']}", json={"project_id": pb["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == pb["id"]
    assert r.json()["project_name"] == "Beta"


def test_event_detail_items_filtered_to_scope(client):
    """A restricted viewer only sees event items belonging to their accessible projects."""
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    ev = client.post("/api/events", json={"name": "Cross", "project_id": pa["id"]}).json()
    in_scope = _mk_bug(client, pa["id"], title="seen", item_type="Task", event_id=ev["id"])
    _mk_bug(client, pb["id"], title="hidden", item_type="Task", event_id=ev["id"])

    _mk_user(client, "alpha@x.com", role="manager", project_ids=[pa["id"]])
    _login(client, "alpha@x.com")
    detail = client.get(f"/api/events/{ev['id']}").json()
    item_ids = {it["id"] for it in detail["items"]}
    assert item_ids == {in_scope["id"]}


# Stats — scoped aggregations
def test_stats_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _mk_user(client, "none@x.com", role="user", project_ids=[])

    _login(client, "alpha@x.com")
    s = client.get("/api/stats").json()
    assert s["bugs"] == 1
    proj_names = {row["name"] for row in s["by_project"]}
    assert proj_names == {"Alpha"}

    # No memberships means all aggregates are zero/empty.
    _login(client, "none@x.com")
    s = client.get("/api/stats").json()
    assert s["bugs"] == 0
    assert s["by_project"] == []
    assert s["by_type"].get("Event", 0) == 0


# Audit — scoped to a manager's projects
def test_audit_scoped_for_manager(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    rows = client.get("/api/audit").json()
    bug_ids = {r["bug_id"] for r in rows if r["bug_id"] is not None}
    assert w.bb["id"] not in bug_ids
    assert w.ba["id"] in bug_ids


def test_audit_empty_for_untagged_manager(client):
    _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "empty@x.com", role="manager", project_ids=[])
    _login(client, "empty@x.com")
    assert client.get("/api/audit").json() == []


# Reports — scoped, and not wideable via the payload
def test_reports_scoped_and_not_wideable(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")

    r = client.post("/api/reports/run", json={"report_key": "item_detail", "filters": {}})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["rows"]}
    assert ids == {w.ba["id"]}

    # Passing Beta's project_id in the filter payload must not widen the scope.
    r = client.post("/api/reports/run", json={
        "report_key": "item_detail", "filters": {"project_ids": [w.pb["id"]]},
    })
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_reports_empty_for_untagged_manager(client):
    _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "empty@x.com", role="manager", project_ids=[])
    _login(client, "empty@x.com")
    r = client.post("/api/reports/run", json={"report_key": "item_detail", "filters": {}})
    assert r.status_code == 200
    assert r.json()["rows"] == []


def test_reports_throughput_scoped(client):
    """Audit-log-based reports respect project scope; a restricted manager never sees other projects' data."""
    w = _two_project_world(client)
    _as_admin(client)
    client.put(f"/api/bugs/{w.bb['id']}", json={"status": "Resolved"})
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    r = client.post("/api/reports/run", json={"report_key": "throughput", "filters": {}})
    assert r.status_code == 200, r.text
    assert "Beta bug" not in str(r.json()["rows"])


# Projects — scoped list/detail + auto-enroll the manager creator
def test_projects_scoped_and_creator_autoenrolled(client):
    _as_admin(client)
    pa = _mk_project(client, "Alpha")
    pb = _mk_project(client, "Beta")
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[pa["id"]])
    _login(client, "alpha@x.com")

    names = {p["name"] for p in client.get("/api/projects").json()}
    assert names == {"Alpha"}
    assert client.get(f"/api/projects/{pa['id']}").status_code == 200
    assert client.get(f"/api/projects/{pb['id']}").status_code == 404

    # A manager creating a project is auto-enrolled so they can immediately see it.
    created = client.post("/api/projects", json={"name": "Gamma"})
    assert created.status_code == 201, created.text
    names = {p["name"] for p in client.get("/api/projects").json()}
    assert names == {"Alpha", "Gamma"}


def test_admin_sees_all_projects(client):
    _as_admin(client)
    _mk_project(client, "Alpha")
    _mk_project(client, "Beta")
    names = {p["name"] for p in client.get("/api/projects").json()}
    # Includes the bootstrap "General" project plus the two new ones.
    assert {"Alpha", "Beta"} <= names


# Sleuth chatbot — read handlers scoped
def test_sleuth_list_bugs_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    resp = _chat(client, "list all bugs")
    rows = _chat_table_rows(resp)
    flat = " ".join(str(cell) for row in rows for cell in row)
    assert f"#{w.ba['id']}" in flat
    assert f"#{w.bb['id']}" not in flat


def test_sleuth_bug_detail_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    resp = _chat(client, f"bug {w.ba['id']}")
    assert resp["intent"] == "bug_detail"
    assert "Not found" not in resp["summary"]
    # Out-of-scope bug must surface as not found through the chat path as well.
    resp = _chat(client, f"bug {w.bb['id']}")
    assert resp["summary"] == "Not found"


def test_sleuth_projects_and_stats_scoped(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")

    resp = _chat(client, "list projects")
    flat = " ".join(str(cell) for row in _chat_table_rows(resp) for cell in row)
    assert "Alpha" in flat
    assert "Beta" not in flat

    resp = _chat(client, "summary")
    text = " ".join(b["payload"].get("text", "") for b in resp["blocks"] if b["kind"] == "text")
    assert "1" in resp["summary"] or "Total" in text


def test_sleuth_untagged_user_sees_nothing(client):
    _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "none@x.com", role="user", project_ids=[])
    _login(client, "none@x.com")
    resp = _chat(client, "list all bugs")
    assert _chat_table_rows(resp) == []


def test_sleuth_cloud_data_path_scoped_for_manager(client, monkeypatch):
    """Regression: cloud _route_data_query must scope like the deterministic path (was accessible=None, admin-level)."""
    from app.chatbot import cloud_llm
    from app.config import get_settings
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")

    s = get_settings()
    monkeypatch.setattr(s, "SLEUTH_CLOUD_ENABLED", True)
    monkeypatch.setattr(s, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(cloud_llm, "_cooldown_until", 0.0, raising=False)
    # Simulate model routing a free-form ask to a canonical query; unscoped would show both projects.
    monkeypatch.setattr(
        cloud_llm, "_call_groq",
        lambda system, user, **kw: '{"mode":"data","canonical_query":"list all bugs"}',
    )

    resp = _chat(client, "show me everything still outstanding")
    assert resp["intent"].startswith("cloud_data:")
    flat = " ".join(str(cell) for row in _chat_table_rows(resp) for cell in row)
    assert f"#{w.ba['id']}" in flat      # Alpha bug (in scope) shown
    assert f"#{w.bb['id']}" not in flat  # Beta bug (out of scope) withheld


def test_sleuth_recent_activity_scoped_for_manager(client):
    w = _two_project_world(client)
    _as_admin(client)
    _mk_user(client, "alpha@x.com", role="manager", project_ids=[w.pa["id"]])
    _login(client, "alpha@x.com")
    resp = _chat(client, "recent activity")
    flat = " ".join(
        str(cell) for row in _chat_table_rows(resp) for cell in row
    )
    assert "Beta bug" not in flat
