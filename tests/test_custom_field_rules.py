"""B.5: custom-field values follow the item's edit permissions, changes are audited / versioned /
announced, and required fields are enforced on create, update and import."""
from __future__ import annotations

import json

import pytest

from tests.test_saved_views_custom_fields import _add_user, _login, _project_id
from tests.conftest import sign_up

CSV_HEADER = "Title,Project,Tier\r\n"


@pytest.fixture
def outbox(monkeypatch):
    """Capture webhook deliveries (event, payload) instead of sending them."""
    import app.routes.bugs as bugs_routes
    import app.routes.custom_fields as cf_routes
    import app.routes.agile as agile_routes

    sent: list[tuple[str, dict]] = []

    def fake(org_id, event, payload, hook_id=None):
        sent.append((event, json.loads(json.dumps(payload, default=str))))

    for mod in (bugs_routes, cf_routes, agile_routes):
        if hasattr(mod, "deliver_event"):
            monkeypatch.setattr(mod, "deliver_event", fake)
    return sent


def _org(email="owner@acme.test", org="Acme Corp"):
    owner, _ = sign_up(org, email)
    return owner, _project_id(owner)


def _field(c, pid, **over):
    body = {"name": "Tier", "field_type": "select", "options": ["Gold", "Silver"], **over}
    res = c.post(f"/api/projects/{pid}/custom-fields", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _join(owner, pid, email, role="user"):
    uid = _add_user(owner, email, role)
    assert owner.post(f"/api/projects/{pid}/members", json={"user_id": uid}).status_code in (200, 201)
    return _login(email)


def test_a_plain_user_cannot_set_values_on_a_read_only_task(client):
    owner, pid = _org()
    fid = _field(owner, pid)
    task = owner.post("/api/bugs", json={"project_id": pid, "title": "Plan it", "item_type": "Task"})
    assert task.status_code == 201, task.text
    plain = _join(owner, pid, "plain@acme.test")
    url = f"/api/bugs/{task.json()['id']}/custom-values"
    assert plain.get(url).status_code == 200  # may read
    assert plain.put(url, json=[{"field_id": fid, "value": "Gold"}]).status_code == 403
    assert owner.get(url).json() == []  # nothing was written
    assert owner.put(url, json=[{"field_id": fid, "value": "Gold"}]).status_code == 200


def test_a_plain_user_can_set_values_on_a_bug_and_it_is_audited_versioned_and_announced(client, outbox):
    owner, pid = _org()
    fid = _field(owner, pid)
    plain = _join(owner, pid, "plain@acme.test")
    bug = owner.post("/api/bugs", json={"project_id": pid, "title": "Broken thing"}).json()
    before = owner.get(f"/api/bugs/{bug['id']}").json()["version"]
    outbox.clear()

    res = plain.put(f"/api/bugs/{bug['id']}/custom-values", json=[{"field_id": fid, "value": "Gold"}])
    assert res.status_code == 200, res.text
    assert owner.get(f"/api/bugs/{bug['id']}").json()["version"] == before + 1
    trail = [a for a in owner.get(f"/api/bugs/{bug['id']}/activity").json() if a["action"].startswith("custom_Tier")]
    assert len(trail) == 1 and "Gold" in trail[0]["detail"] and trail[0]["actor_name"] == "plain"
    assert [e for e, _ in outbox] == ["bug.updated"]
    assert outbox[0][1]["changes"] == [{"field": "custom_Tier", "old": "", "new": "Gold"}]

    outbox.clear()  # an identical write changes nothing: no version bump, no audit, no event
    plain.put(f"/api/bugs/{bug['id']}/custom-values", json=[{"field_id": fid, "value": "Gold"}])
    assert owner.get(f"/api/bugs/{bug['id']}").json()["version"] == before + 1
    assert outbox == []


def test_values_never_cross_organizations(client, outbox):
    a, pid_a = _org()
    b, pid_b = _org("owner@beta.test", "Beta Corp")
    fid_a = _field(a, pid_a)
    bug_a = a.post("/api/bugs", json={"project_id": pid_a, "title": "Acme bug"}).json()["id"]
    url = f"/api/bugs/{bug_a}/custom-values"
    assert b.put(url, json=[{"field_id": fid_a, "value": "Gold"}]).status_code == 404
    # B's own item: A's field id is ignored, never written
    bug_b = b.post("/api/bugs", json={"project_id": pid_b, "title": "Beta bug"}).json()["id"]
    b.put(f"/api/bugs/{bug_b}/custom-values", json=[{"field_id": fid_a, "value": "Gold"}])
    assert a.get(url).json() == [] and b.get(f"/api/bugs/{bug_b}/custom-values").json() == []
    # create/update through A's field id from B is ignored too
    res = b.post("/api/bugs", json={"project_id": pid_b, "title": "Beta two",
                                    "custom_values": [{"field_id": fid_a, "value": "Gold"}]})
    assert res.status_code == 201
    assert b.get(f"/api/bugs/{res.json()['id']}/custom-values").json() == []


def test_required_fields_are_enforced_on_create(client):
    owner, pid = _org()
    fid = _field(owner, pid, is_required=True)
    body = {"project_id": pid, "title": "Needs a tier"}
    res = owner.post("/api/bugs", json=body)
    assert res.status_code == 422 and "Tier" in res.json()["detail"]
    assert owner.get("/api/bugs").json()["total"] == 0  # nothing half-created
    assert owner.post("/api/bugs", json={**body, "custom_values": [{"field_id": fid, "value": "Bronze"}]}).status_code == 422
    ok = owner.post("/api/bugs", json={**body, "custom_values": [{"field_id": fid, "value": "Gold"}]})
    assert ok.status_code == 201, ok.text
    assert owner.get(f"/api/bugs/{ok.json()['id']}/custom-values").json() == [{"field_id": fid, "value": "Gold"}]


def test_required_fields_are_enforced_on_agile_create(client):
    owner, pid = _org()
    assert owner.post(f"/api/agile/projects/{pid}/enable", json={"feature_flags": {}}).status_code == 200
    fid = _field(owner, pid, is_required=True)
    body = {"project_id": pid, "title": "Story one", "item_type": "Story"}
    assert owner.post("/api/agile/work-items", json=body).status_code == 422
    ok = owner.post("/api/agile/work-items", json={**body, "custom_values": [{"field_id": fid, "value": "Gold"}]})
    assert ok.status_code == 201, ok.text


def test_required_fields_are_enforced_on_update_and_values_can_ride_along(client, outbox):
    owner, pid = _org()
    bug = owner.post("/api/bugs", json={"project_id": pid, "title": "Older item"}).json()
    fid = _field(owner, pid, is_required=True)  # added after the item exists
    res = owner.put(f"/api/bugs/{bug['id']}", json={"title": "Renamed item"})
    assert res.status_code == 422 and "Tier" in res.json()["detail"]
    assert owner.get(f"/api/bugs/{bug['id']}").json()["title"] == "Older item"
    outbox.clear()
    ok = owner.put(f"/api/bugs/{bug['id']}", json={"title": "Renamed item",
                                                  "custom_values": [{"field_id": fid, "value": "Silver"}]})
    assert ok.status_code == 200, ok.text
    assert owner.get(f"/api/bugs/{bug['id']}/custom-values").json() == [{"field_id": fid, "value": "Silver"}]
    fields_changed = {c["field"] for c in outbox[-1][1]["changes"]}
    assert fields_changed == {"title", "custom_Tier"}
    # now filled: plain edits keep working
    assert owner.put(f"/api/bugs/{bug['id']}", json={"priority": "High"}).status_code == 200
    # but a value cannot be blanked out through an update either
    assert owner.put(f"/api/bugs/{bug['id']}", json={"custom_values": []}).status_code == 422


def test_moving_an_item_into_a_project_with_required_fields_needs_values(client):
    owner, pid = _org()
    other = owner.post("/api/projects", json={"name": "Second", "description": ""})
    assert other.status_code == 201, other.text
    _field(owner, other.json()["id"], is_required=True)
    bug = owner.post("/api/bugs", json={"project_id": pid, "title": "Mover item"}).json()
    res = owner.put(f"/api/bugs/{bug['id']}", json={"project_id": other.json()["id"]})
    assert res.status_code == 422 and "Tier" in res.json()["detail"]


def test_required_fields_are_enforced_on_import_and_custom_columns_are_read(admin_client):
    c = admin_client
    pid = _project_id(c)
    name = c.get("/api/projects").json()[0]["name"]
    fid = _field(c, pid, is_required=True)

    def upload(text):
        return c.post("/api/bugs/import", files={"file": ("rows.csv", text.encode(), "text/csv")})

    refused = upload(f"Title,Project\r\nNo tier row,{name}\r\n")
    assert refused.status_code == 200 and refused.json()["created"] == 0
    assert "Tier" in refused.json()["errors"][0]["error"]
    bad = upload(f"{CSV_HEADER}Bad tier row,{name},Bronze\r\n")
    assert bad.json()["created"] == 0 and "Tier" in bad.json()["errors"][0]["error"]
    good = upload(f"{CSV_HEADER}Good row one,{name},Gold\r\nNo tier two,{name},\r\n")
    body = good.json()
    assert body["created"] == 1 and body["failed"] == 1, body  # the bad row did not block the good one
    made = body["created_ids"][0]
    assert c.get(f"/api/bugs/{made}/custom-values").json() == [{"field_id": fid, "value": "Gold"}]


def test_import_never_reads_another_orgs_custom_fields(client):
    a, pid_a = _org()
    b, pid_b = _org("owner@beta.test", "Beta Corp")
    _field(a, pid_a, is_required=True)  # Acme requires a Tier; Beta does not
    name_b = b.get("/api/projects").json()[0]["name"]
    res = b.post("/api/bugs/import", files={"file": ("r.csv", f"Title,Project\r\nBeta row,{name_b}\r\n".encode(), "text/csv")})
    assert res.json()["created"] == 1, res.text
