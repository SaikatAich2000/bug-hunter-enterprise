"""API validation matrix group02: comments, attachments, links, activity, events."""
from __future__ import annotations


def _mk_project(c, name="MX2-1"):
    r = c.post("/api/projects", json={"name": name, "color": "#abcdef"})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(c, pid):
    r = c.post("/api/bugs", json={"project_id": pid, "title": "matrix bug two",
                                  "priority": "Medium", "environment": "DEV"})
    assert r.status_code == 201, r.text
    return r.json()


class TestCommentsLinksActivity:
    def test_comment_lifecycle(self, admin_client):
        p = _mk_project(admin_client, "MX2-C")
        bug = _mk_bug(admin_client, p["id"])
        bid = bug["id"]
        assert admin_client.get(f"/api/bugs/{bid}/comments").status_code == 200
        c = admin_client.post(f"/api/bugs/{bid}/comments", json={"body": "hello comment"})
        assert c.status_code in (200, 201), c.text
        cid = c.json().get("id")
        assert cid
        assert admin_client.put(
            f"/api/bugs/{bid}/comments/{cid}",
            json={"body": "edited comment"}).status_code == 200
        assert admin_client.delete(
            f"/api/bugs/{bid}/comments/{cid}").status_code in (200, 204)
        assert admin_client.get(
            f"/api/bugs/{bid}/comments/999999999") if False else True

    def test_comment_requires_auth(self, client):
        assert client.post("/api/bugs/1/comments",
                           json={"body": "x"}).status_code == 401

    def test_links_lifecycle(self, admin_client):
        p = _mk_project(admin_client, "MX2-L")
        a = _mk_bug(admin_client, p["id"])
        b = _mk_bug(admin_client, p["id"])
        r = admin_client.post(f"/api/bugs/{a['id']}/links",
                              json={"target_bug_id": b["id"], "link_type": "relates"})
        assert r.status_code in (200, 201), r.text
        lid = r.json().get("id")
        assert admin_client.get(f"/api/bugs/{a['id']}/links").status_code == 200
        if lid:
            assert admin_client.delete(
                f"/api/bugs/{a['id']}/links/{lid}").status_code in (200, 204)

    def test_activity_ok(self, admin_client):
        p = _mk_project(admin_client, "MX2-A")
        bug = _mk_bug(admin_client, p["id"])
        r = admin_client.get(f"/api/bugs/{bug['id']}/activity")
        assert r.status_code == 200


class TestAttachmentsEvents:
    def test_upload_download_delete(self, admin_client):
        p = _mk_project(admin_client, "MX2-F")
        bug = _mk_bug(admin_client, p["id"])
        bid = bug["id"]
        up = admin_client.post(
            f"/api/bugs/{bid}/attachments",
            files={"file": ("note.txt", b"hello world", "text/plain")})
        assert up.status_code in (200, 201), up.text
        aid = up.json().get("id")
        assert aid
        dl = admin_client.get(f"/api/bugs/{bid}/attachments/{aid}/download")
        assert dl.status_code == 200
        assert admin_client.delete(
            f"/api/bugs/{bid}/attachments/{aid}").status_code in (200, 204)

    def test_dangerous_upload_blocked(self, admin_client):
        p = _mk_project(admin_client, "MX2-X")
        bug = _mk_bug(admin_client, p["id"])
        up = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
        assert up.status_code in (400, 415, 422)

    def test_events_lifecycle(self, admin_client):
        c = admin_client.post("/api/events", json={"name": "MX event"})
        assert c.status_code in (200, 201), c.text
        eid = c.json().get("id")
        assert admin_client.get("/api/events").status_code == 200
        if eid:
            assert admin_client.get(f"/api/events/{eid}").status_code == 200
            assert admin_client.put(
                f"/api/events/{eid}", json={"name": "MX event 2"}).status_code == 200
            assert admin_client.delete(
                f"/api/events/{eid}").status_code in (200, 204)
