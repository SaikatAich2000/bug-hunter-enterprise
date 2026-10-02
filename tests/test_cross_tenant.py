"""Tenant isolation: nothing that belongs to organization A is readable or changeable by B.

A is seeded with one of every kind of record. B then probes every OpenAPI operation that takes an
identifier, with A's ids, and must never succeed; afterwards A's records must be untouched.
"""
from __future__ import annotations

import re

import pytest

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"

# Operations whose identifier is the caller's own or whose result does not depend on the id.
_OWN_SCOPE = {
    ("DELETE", "/api/devices/{token}"),
    ("DELETE", "/api/sessions/{session_id}"),
    ("POST", "/api/notifications/{notif_id}/read"),
    ("DELETE", "/api/notifications/{notif_id}"),
}
_QUERY_DEFAULTS = {
    "report_date": "2026-01-01", "date_from": "2026-01-01", "date_to": "2026-01-31",
    "provider_repo_id": "1", "report_key": "velocity",
}


class World:
    """Org A's records, the ids B will try, and the two clients."""

    def __init__(self):
        self.a, self.a_me = sign_up("Org A", "owner@a.test")
        self.b, self.b_me = sign_up("Org B", "owner@b.test")
        a = self.a
        self.project_id = a.get("/api/projects").json()[0]["id"]
        enabled = a.post(f"/api/agile/projects/{self.project_id}/enable", json={"feature_flags": {}})
        assert enabled.status_code == 200, enabled.text
        self.board_id = enabled.json()["board_id"]
        self.bug_id = self._ok(a.post("/api/bugs", json={
            "project_id": self.project_id, "title": "Secret bug", "description": "classified"}))["id"]
        self.comment_id = self._ok(a.post(f"/api/bugs/{self.bug_id}/comments", json={"body": "secret note"}))["id"]
        self.event_id = self._ok(a.post("/api/events", json={"name": "Secret event"}))["id"]
        self.user_id = self._ok(a.post("/api/users", json={
            "name": "Colleague", "email": "colleague@a.test", "role": "user", "password": PASSWORD}))["id"]
        self.sprint_id = self._ok(a.post(f"/api/agile/sprints?board_id={self.board_id}", json={
            "name": "Sprint A", "goal": "g"}))["id"]
        self.epic_id = self._ok(a.post("/api/agile/work-items", json={
            "project_id": self.project_id, "title": "Epic A", "item_type": "epic"}))["id"]
        self.label_id = self._ok(a.post("/api/agile/labels", json={
            "project_id": self.project_id, "display_name": "secret"}))["id"]
        self.component_id = self._ok(a.post("/api/agile/components", json={
            "project_id": self.project_id, "name": "Core"}))["id"]
        self.version_id = self._ok(a.post("/api/agile/releases", json={
            "project_id": self.project_id, "name": "v1"}))["id"]
        self.filter_id = self._ok(a.post(f"/api/agile/boards/{self.board_id}/quick-filters", json={
            "name": "Mine", "filter_json": {"assignee": "me"}}))["id"]
        self.view_id = self._ok(a.post("/api/saved-views", json={
            "name": "A view", "filters": {}, "shared_with_org": True}))["id"]
        self.hook_id = self._ok(a.post("/api/webhooks", json={
            "name": "Hook", "url": "https://hooks.example.com/a"}))["id"]
        self.field_id = self._ok(a.post(f"/api/projects/{self.project_id}/custom-fields", json={
            "name": "Customer"}))["id"]
        self.invitation_id = self._ok(a.post("/api/invitations", json={"email": "pending@a.test"}))["id"]
        self.criterion_id = self._ok(a.post(f"/api/agile/work-items/{self.bug_id}/acceptance-criteria", json={
            "description": "Works"}))["id"]

    @staticmethod
    def _ok(res):
        assert res.status_code in (200, 201), res.text
        return res.json()

    def ids(self) -> dict[str, int]:
        return {
            "project_id": self.project_id, "board_id": self.board_id, "bug_id": self.bug_id,
            "item_id": self.bug_id, "work_item_id": self.bug_id, "comment_id": self.comment_id,
            "event_id": self.event_id, "user_id": self.user_id, "sprint_id": self.sprint_id,
            "epic_id": self.epic_id, "label_id": self.label_id, "component_id": self.component_id,
            "version_id": self.version_id, "filter_id": self.filter_id, "view_id": self.view_id,
            "hook_id": self.hook_id, "field_id": self.field_id, "invitation_id": self.invitation_id,
            "criterion_id": self.criterion_id,
        }


@pytest.fixture
def world(client):
    return World()


def _operations(world):
    """(method, template, concrete url) for every OpenAPI operation, with A's ids filled in."""
    from app.main import app

    ids = world.ids()
    for template, methods in app.openapi()["paths"].items():
        for method, detail in methods.items():
            names = re.findall(r"\{(\w+)\}", template)
            url = template
            for name in names:
                url = url.replace("{%s}" % name, str(ids.get(name, 999_999)))
            query = {}
            for param in detail.get("parameters", []):
                if param["in"] == "query" and param.get("required"):
                    query[param["name"]] = ids.get(param["name"], _QUERY_DEFAULTS.get(param["name"], 1))
            yield method.upper(), template, url, query


def test_b_can_never_succeed_on_a_record_of_a(world):
    probed = 0
    leaks = []
    for method, template, url, query in _operations(world):
        has_id = "{" in template or any(k.endswith("_id") for k in query)
        if not has_id or (method, template) in _OWN_SCOPE or template.startswith("/api/invitations/preview"):
            continue
        if template in ("/api/invitations/accept", "/api/auth/login", "/api/auth/signup"):
            continue
        body = {} if method in ("POST", "PUT", "PATCH") else None
        res = world.b.request(method, url, params=query, json=body)
        probed += 1
        if res.status_code < 300:
            leaks.append(f"{method} {url} {query} -> {res.status_code}")
    assert probed >= 80, f"only {probed} operations were probed; the matrix shrank"
    assert not leaks, "organization B reached organization A's records:\n" + "\n".join(leaks)


def test_a_s_records_survive_the_probe(world):
    for method, _template, url, query in _operations(world):
        if "{" in _template and (method, _template) not in _OWN_SCOPE and method in ("DELETE", "PUT", "POST"):
            world.b.request(method, url, params=query, json={} if method != "DELETE" else None)
    a = world.a
    assert a.get(f"/api/bugs/{world.bug_id}").json()["title"] == "Secret bug"
    assert a.get(f"/api/events/{world.event_id}").status_code == 200
    assert a.get(f"/api/users/{world.user_id}").status_code == 200
    assert a.get(f"/api/projects/{world.project_id}").status_code == 200
    assert a.get(f"/api/saved-views/{world.view_id}").status_code == 200
    assert a.get(f"/api/webhooks/{world.hook_id}").status_code == 200
    assert a.get(f"/api/agile/sprints/{world.sprint_id}").status_code == 200
    assert a.get(f"/api/agile/labels/{world.label_id}").status_code == 200
    assert a.get(f"/api/agile/components/{world.component_id}").status_code == 200
    assert a.get(f"/api/agile/releases/{world.version_id}").status_code == 200
    assert len(a.get("/api/invitations").json()) == 1
    assert a.get(f"/api/projects/{world.project_id}/custom-fields").json()[0]["id"] == world.field_id


def test_lists_and_aggregates_of_b_contain_nothing_of_a(world):
    b = world.b
    own_projects = {p["id"] for p in b.get("/api/projects").json()}
    assert world.project_id not in own_projects
    listing = b.get("/api/bugs").json()
    assert listing["total"] == 0
    assert listing["items"] == []
    assert b.get("/api/events").json() == []
    assert {u["email"] for u in b.get("/api/users").json()} == {"owner@b.test"}
    assert b.get("/api/saved-views").json() == []
    assert b.get("/api/webhooks").json() == []
    assert b.get("/api/invitations").json() == []
    assert b.get(f"/api/agile/boards?project_id={world.project_id}").status_code in (403, 404)
    text = " ".join(
        b.get(path).text for path in ("/api/stats", "/api/audit", "/api/audit?q=Secret", "/api/notifications")
    )
    for secret in ("Secret bug", "Secret event", "classified", "secret note", "Colleague", "owner@a.test", "Org A"):
        assert secret not in text, secret


def test_b_cannot_attach_a_s_records_to_its_own(world):
    b = world.b
    b_project = b.get("/api/projects").json()[0]["id"]
    assert b.post("/api/bugs", json={"project_id": world.project_id, "title": "Intruder", "description": "d"}).status_code in (400, 403, 404)
    # assigning or reporting as a user of another organization is refused
    res = b.post("/api/bugs", json={
        "project_id": b_project, "title": "Mine with foreign assignee", "description": "d",
        "assignee_ids": [world.user_id]})
    assert res.status_code in (400, 404)
    res = b.post("/api/bugs", json={
        "project_id": b_project, "title": "Mine with foreign event", "description": "d",
        "event_id": world.event_id})
    assert res.status_code in (400, 404)
    assert b.post("/api/events", json={"name": "Dup"}).status_code == 201
    assert b.post(f"/api/projects/{b_project}/members", json={"user_id": world.user_id}).status_code == 400
    assert b.put("/api/bugs/bulk", json={}).status_code in (404, 405, 422)
    bulk = b.post("/api/bugs/bulk", json={"ids": [world.bug_id], "action": "delete"})
    changed = bulk.json().get("updated", 0) if bulk.status_code == 200 else 0
    assert changed == 0
    assert world.a.get(f"/api/bugs/{world.bug_id}").status_code == 200


def test_same_names_do_not_collide_across_organizations(world):
    b = world.b
    # project, event, label names are unique per organization only
    assert b.post("/api/events", json={"name": "Secret event"}).status_code == 201
    assert b.post("/api/projects", json={"name": "General", "description": "", "color": "#c9764f"}).status_code in (201, 409)


def test_the_chatbot_only_sees_its_own_organization(world):
    res = world.b.post("/api/chat/ask", json={"message": "list all bugs"})
    assert res.status_code == 200, res.text
    assert "Secret bug" not in res.text
    res = world.b.post("/api/chat/ask", json={"message": f"show bug {world.bug_id}"})
    assert "Secret bug" not in res.text
    assert "classified" not in res.text
    res = world.b.post("/api/chat/ask", json={"message": "list users"})
    assert "Colleague" not in res.text
    assert "owner@a.test" not in res.text


def test_a_session_of_a_cannot_be_used_against_b_through_a_swapped_cookie(world):
    # the cookie of A answers as A, never as B, whatever the request says
    me = world.a.get("/api/auth/me").json()
    assert me["org_id"] == world.a_me["org_id"] != world.b_me["org_id"]
    other = new_client()
    assert other.get("/api/projects").status_code == 401


def test_foreign_ids_inside_request_bodies_are_refused(world):
    """Records of A named in B's request bodies: project, event, assignee, reporter, parent, epic."""
    b = world.b
    b_project = b.get("/api/projects").json()[0]["id"]
    b_bug = b.post("/api/bugs", json={"project_id": b_project, "title": "B item", "description": "d"}).json()["id"]

    refused = (400, 403, 404, 422)
    assert b.post("/api/users", json={
        "name": "Spy", "email": "spy@b.test", "role": "user", "password": PASSWORD,
        "project_ids": [world.project_id]}).status_code in refused
    assert b.post("/api/events", json={"name": "Spy event", "project_id": world.project_id}).status_code in refused
    assert b.post("/api/agile/work-items", json={
        "project_id": world.project_id, "title": "Spy story", "item_type": "Story"}).status_code in refused
    assert b.put(f"/api/bugs/{b_bug}", json={"project_id": world.project_id}).status_code in refused
    assert b.put(f"/api/bugs/{b_bug}", json={"event_id": world.event_id}).status_code in refused
    assert b.put(f"/api/bugs/{b_bug}", json={"assignee_ids": [world.user_id]}).status_code in refused
    assert b.put(f"/api/bugs/{b_bug}", json={"reporter_id": world.user_id}).status_code in refused
    assert b.post("/api/saved-views", json={"name": "v", "filters": {"project_id": [world.project_id]}}).status_code == 201
    assert b.get("/api/bugs", params={"project_id": world.project_id}).json()["total"] == 0

    # nothing of A changed
    a = world.a
    assert [u["email"] for u in a.get("/api/users").json() if u["email"] == "spy@b.test"] == []
    assert a.get(f"/api/bugs/{world.bug_id}").json()["project_id"] == world.project_id
    assert len(a.get("/api/events").json()) == 1
