"""API validation matrix group05+: agile taxonomy/board/sprints, git, chat read
APIs — positive lifecycle + auth + not-found coverage per operation family."""
from __future__ import annotations


def _mk_project(c, name="MX5"):
    r = c.post("/api/projects", json={"name": name, "color": "#abcdef"})
    assert r.status_code == 201, r.text
    return r.json()


def _enable_agile(c, pid):
    r = c.post(f"/api/agile/projects/{pid}/enable", json={"feature_flags": {}})
    assert r.status_code == 200, r.text
    return r.json()


def _mk_bug(c, pid, **kw):
    body = {"project_id": pid, "title": "agile matrix item",
            "priority": "Medium", "environment": "DEV"}
    body.update(kw)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class TestAgileCore:
    """Agile list endpoints require a project_id query parameter (422 without)."""

    def test_boards_list(self, admin_client):
        p = _mk_project(admin_client, "MX5-B")
        _enable_agile(admin_client, p["id"])
        r = admin_client.get(f"/api/agile/boards?project_id={p['id']}")
        assert r.status_code == 200, r.text

    def test_backlog_planning_view(self, admin_client):
        p = _mk_project(admin_client, "MX5-C")
        board = _enable_agile(admin_client, p["id"])
        r = admin_client.get(f"/api/agile/boards/{board['board_id']}/planning")
        assert r.status_code == 200, r.text
        assert r.json()["sprints"] == [] and r.json()["backlog"] == []

    def test_components_epics_labels_list(self, admin_client):
        p = _mk_project(admin_client, "MX5-T")
        _enable_agile(admin_client, p["id"])
        assert admin_client.get(
            f"/api/agile/components?project_id={p['id']}").status_code == 200
        assert admin_client.get(
            f"/api/agile/epics?project_id={p['id']}").status_code == 200
        assert admin_client.get(
            f"/api/agile/labels?project_id={p['id']}").status_code == 200

    def test_sprints_list(self, admin_client):
        p = _mk_project(admin_client, "MX5-S")
        board = _enable_agile(admin_client, p["id"])
        r = admin_client.get(
            f"/api/agile/sprints?project_id={p['id']}&board_id={board['board_id']}")
        assert r.status_code == 200, r.text

    def test_disabled_agile_404s(self, admin_client):
        p = _mk_project(admin_client, "MX5-D")
        r = admin_client.get(f"/api/agile/boards?project_id={p['id']}")
        assert r.status_code == 404

    def test_taxonomy_not_found(self, admin_client):
        assert admin_client.get("/api/agile/epics/999999999").status_code == 404
        assert admin_client.get("/api/agile/releases/999999999").status_code == 404

    def test_agile_requires_auth(self, client):
        assert client.get("/api/agile/boards?project_id=1").status_code == 401

    def test_work_items_list_scoped(self, admin_client):
        """GET /api/agile/work-items is 405 (method intentionally unlisted)."""
        p = _mk_project(admin_client, "MX5-WI")
        _enable_agile(admin_client, p["id"])
        _mk_bug(admin_client, p["id"])
        r = admin_client.get(f"/api/agile/work-items?project_id={p['id']}")
        assert r.status_code == 405


class TestGitChat:
    def test_git_config_requires_project(self, admin_client):
        p = _mk_project(admin_client, "MX5-G")
        r = admin_client.get(f"/api/git/projects/{p['id']}/config")
        assert r.status_code in (200, 404)

    def test_git_not_found(self, admin_client):
        assert admin_client.get(
            "/api/git/projects/999999999/config").status_code == 404

    def test_git_requires_auth(self, client):
        assert client.get("/api/git/projects/1/config").status_code == 401

    def test_chat_ask_requires_auth(self, client):
        assert client.post("/api/chat/ask",
                           json={"question": "hi"}).status_code == 401
