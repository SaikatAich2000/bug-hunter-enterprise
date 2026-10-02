"""Saved filter views and per-project custom fields."""
from __future__ import annotations

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"


def _add_user(c, email, role="user"):
    res = c.post("/api/users", json={"name": email.split("@")[0], "email": email, "role": role, "password": PASSWORD})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _login(email):
    c = new_client()
    assert c.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return c


# --- saved views ---------------------------------------------------------------------------------


def test_views_are_private_until_an_admin_or_manager_shares_them(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    _add_user(owner, "plain@acme.test")
    plain = _login("plain@acme.test")

    mine = plain.post("/api/saved-views", json={"name": "My open", "filters": {"status": "New"}})
    assert mine.status_code == 201, mine.text
    assert mine.json()["is_mine"] is True
    # a plain user cannot publish a view to the organization
    pushy = plain.post("/api/saved-views", json={"name": "For all", "filters": {}, "shared_with_org": True}).json()
    assert pushy["shared_with_org"] is False

    shared = owner.post("/api/saved-views", json={"name": "Team view", "filters": {"priority": "High"},
                                                   "shared_with_org": True}).json()
    assert shared["shared_with_org"] is True

    assert {v["name"] for v in plain.get("/api/saved-views").json()} == {"My open", "For all", "Team view"}
    # the owner sees the shared view plus their own, but never the plain user's private ones
    assert {v["name"] for v in owner.get("/api/saved-views").json()} == {"Team view"}
    assert owner.get(f"/api/saved-views/{mine.json()['id']}").status_code == 404


def test_only_the_creator_changes_or_deletes_a_view(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    _add_user(owner, "plain@acme.test")
    plain = _login("plain@acme.test")
    view = owner.post("/api/saved-views", json={"name": "Team view", "filters": {}, "shared_with_org": True}).json()
    vid = view["id"]

    assert plain.get(f"/api/saved-views/{vid}").status_code == 200
    assert plain.put(f"/api/saved-views/{vid}", json={"name": "Mine now"}).status_code == 403
    assert plain.delete(f"/api/saved-views/{vid}").status_code == 403

    updated = owner.put(f"/api/saved-views/{vid}", json={"name": "Renamed", "filters": {"status": "Closed"}})
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["filters"] == {"status": "Closed"}
    assert owner.delete(f"/api/saved-views/{vid}").status_code == 204
    assert owner.get(f"/api/saved-views/{vid}").status_code == 404


def test_view_input_is_validated(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    assert owner.post("/api/saved-views", json={"name": "", "filters": {}}).status_code == 422
    assert owner.post("/api/saved-views", json={"name": "x", "filters": {"q": "a" * 9000}}).status_code == 422


def test_views_never_cross_organizations(client):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")
    view = a.post("/api/saved-views", json={"name": "Shared", "filters": {}, "shared_with_org": True}).json()
    assert b.get("/api/saved-views").json() == []
    assert b.get(f"/api/saved-views/{view['id']}").status_code == 404
    assert b.put(f"/api/saved-views/{view['id']}", json={"name": "x"}).status_code == 404
    assert b.delete(f"/api/saved-views/{view['id']}").status_code == 404


# --- custom fields -------------------------------------------------------------------------------


def _project_id(c):
    return c.get("/api/projects").json()[0]["id"]


def _bug(c, project_id, title="Item"):
    res = c.post("/api/bugs", json={"project_id": project_id, "title": title, "description": "d"})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_fields_are_managed_by_admins_and_project_leads_only(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    pid = _project_id(owner)
    lead_id = _add_user(owner, "lead@acme.test")
    member_id = _add_user(owner, "member@acme.test")
    owner.post(f"/api/projects/{pid}/members", json={"user_id": lead_id, "role": "lead"})
    owner.post(f"/api/projects/{pid}/members", json={"user_id": member_id})
    base = f"/api/projects/{pid}/custom-fields"

    member = _login("member@acme.test")
    assert member.get(base).status_code == 200
    assert member.post(base, json={"name": "Nope"}).status_code == 403

    lead = _login("lead@acme.test")
    made = lead.post(base, json={"name": "Customer", "field_type": "text"})
    assert made.status_code == 201, made.text
    fid = made.json()["id"]
    assert lead.post(base, json={"name": "Customer"}).status_code == 409
    assert lead.post(base, json={"name": "Bad", "field_type": "blob"}).status_code == 422
    assert lead.put(f"{base}/{fid}", json={"name": "Client"}).json()["name"] == "Client"
    assert member.delete(f"{base}/{fid}").status_code == 403
    assert lead.delete(f"{base}/{fid}").status_code == 204
    assert lead.delete(f"{base}/{fid}").status_code == 404


def test_values_are_type_checked_and_required_fields_enforced(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    pid = _project_id(owner)
    base = f"/api/projects/{pid}/custom-fields"
    number = owner.post(base, json={"name": "Cost", "field_type": "number"}).json()["id"]
    when = owner.post(base, json={"name": "Due", "field_type": "date"}).json()["id"]
    pick = owner.post(base, json={"name": "Tier", "field_type": "select", "options": ["Gold", "Silver"],
                                  "is_required": True}).json()
    assert pick["options"] == ["Gold", "Silver"]
    bug_id = _bug(owner, pid)
    url = f"/api/bugs/{bug_id}/custom-values"

    assert owner.put(url, json=[{"field_id": number, "value": "12.5"}]).status_code == 422  # Tier missing
    assert owner.put(url, json=[{"field_id": pick["id"], "value": "Bronze"}]).status_code == 422
    assert owner.put(url, json=[{"field_id": pick["id"], "value": "Gold"},
                                {"field_id": number, "value": "abc"}]).status_code == 422
    assert owner.put(url, json=[{"field_id": pick["id"], "value": "Gold"},
                                {"field_id": when, "value": "31/12/2026"}]).status_code == 422

    ok = owner.put(url, json=[{"field_id": pick["id"], "value": "Gold"},
                              {"field_id": number, "value": "12.5"},
                              {"field_id": when, "value": "2026-12-31"}])
    assert ok.status_code == 200, ok.text
    assert {v["field_id"]: v["value"] for v in owner.get(url).json()} == {
        pick["id"]: "Gold", number: "12.5", when: "2026-12-31"}
    # blank clears an optional value; replacing keeps the rest
    owner.put(url, json=[{"field_id": pick["id"], "value": "Silver"}, {"field_id": number, "value": ""}])
    assert {v["field_id"]: v["value"] for v in owner.get(url).json()} == {pick["id"]: "Silver"}


def test_values_for_foreign_fields_are_ignored_and_foreign_items_are_hidden(client):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")
    pid_a, pid_b = _project_id(a), _project_id(b)
    foreign_field = b.post(f"/api/projects/{pid_b}/custom-fields", json={"name": "Secret"}).json()["id"]
    bug_a = _bug(a, pid_a)

    assert a.put(f"/api/bugs/{bug_a}/custom-values", json=[{"field_id": foreign_field, "value": "x"}]).json() == []
    assert b.get(f"/api/bugs/{bug_a}/custom-values").status_code == 404
    assert b.put(f"/api/bugs/{bug_a}/custom-values", json=[]).status_code == 404
    assert b.get(f"/api/projects/{pid_a}/custom-fields").status_code == 404
    assert b.post(f"/api/projects/{pid_a}/custom-fields", json={"name": "Hack"}).status_code == 404


def test_deleting_a_field_removes_its_values(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    pid = _project_id(owner)
    fid = owner.post(f"/api/projects/{pid}/custom-fields", json={"name": "Note"}).json()["id"]
    bug_id = _bug(owner, pid)
    owner.put(f"/api/bugs/{bug_id}/custom-values", json=[{"field_id": fid, "value": "hello"}])
    assert owner.delete(f"/api/projects/{pid}/custom-fields/{fid}").status_code == 204
    assert owner.get(f"/api/bugs/{bug_id}/custom-values").json() == []
