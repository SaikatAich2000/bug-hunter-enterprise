"""Board actions on assigned work items return the card, not a 500.

The transition, flag, estimate and taxonomy routes used to return raw dicts holding ORM users,
which failed to serialize as soon as an item had an assignee (the change was already committed).
"""
from __future__ import annotations


def _assigned_story(client) -> dict:
    me = client.get("/api/auth/me").json()
    pid = client.get("/api/projects").json()[0]["id"]
    assert client.post(f"/api/agile/projects/{pid}/enable", json={}).status_code == 200
    res = client.post("/api/agile/work-items", json={
        "project_id": pid, "title": "Assigned story", "item_type": "Story",
        "story_points": 2, "assignee_ids": [me["id"]],
    })
    assert res.status_code == 201, res.text
    return res.json()


def _version(client, item_id: int) -> int:
    return client.get(f"/api/bugs/{item_id}").json()["version"]


def test_flag_estimate_taxonomy_and_transition_on_an_assigned_item(admin_client):
    item = _assigned_story(admin_client)
    iid = item["id"]
    calls = [
        ("post", f"/api/agile/work-items/{iid}/flag", {"flagged": True}),
        ("put", f"/api/agile/work-items/{iid}/estimate", {"story_points": 5}),
        ("put", f"/api/agile/work-items/{iid}/taxonomy", {"label_ids": [], "component_ids": []}),
        ("post", f"/api/agile/work-items/{iid}/transition", {"to_status": "In Progress"}),
    ]
    for method, url, body in calls:
        res = getattr(admin_client, method)(url, json={**body, "version": _version(admin_client, iid)})
        assert res.status_code == 200, (url, res.status_code, res.text)
        card = res.json()
        assert card["id"] == iid
        assert [a["id"] for a in card["assignees"]] == [a["id"] for a in item["assignees"]]
        assert set(card["assignees"][0]) == {"id", "name", "email", "role"}
    final = admin_client.get(f"/api/bugs/{iid}").json()
    assert final["status"] == "In Progress"
    assert float(final["story_points"]) == 5
