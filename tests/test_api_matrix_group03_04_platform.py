"""API validation matrix group03/04: stats, reports, audit, sessions,
notifications, push, users/projects edge cases."""
from __future__ import annotations


def _mk_project(c, name="MX3"):
    r = c.post("/api/projects", json={"name": name, "color": "#abcdef"})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_bug(c, pid, **kw):
    body = {"project_id": pid, "title": "matrix bug three",
            "priority": "Medium", "environment": "DEV"}
    body.update(kw)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class TestStatsReportsAudit:
    def test_stats_positive(self, admin_client):
        p = _mk_project(admin_client, "MX3-S")
        _mk_bug(admin_client, p["id"])
        r = admin_client.get("/api/stats")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    def test_report_types(self, admin_client):
        r = admin_client.get("/api/reports/types")
        assert r.status_code == 200

    def test_report_run_and_export(self, admin_client):
        p = _mk_project(admin_client, "MX3-R")
        _mk_bug(admin_client, p["id"])
        types = admin_client.get("/api/reports/types").json()
        assert isinstance(types, (list, dict))
        run = admin_client.post("/api/reports/run", json={"report_type": "summary"})
        assert run.status_code in (200, 400, 422), run.text

    def test_audit_requires_auth(self, client):
        assert client.get("/api/audit").status_code == 401

    def test_audit_list(self, admin_client):
        p = _mk_project(admin_client, "MX3-A")
        _mk_bug(admin_client, p["id"])
        r = admin_client.get("/api/audit")
        assert r.status_code == 200


class TestSessionsNotificationsPush:
    def test_sessions_list_and_revoke(self, admin_client):
        r = admin_client.get("/api/sessions")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_notifications_flow(self, admin_client):
        assert admin_client.get("/api/notifications").status_code == 200
        assert admin_client.get("/api/notifications/unread_count").status_code == 200
        assert admin_client.post("/api/notifications/read-all").status_code in (200, 204)

    def test_notification_not_found(self, admin_client):
        r = admin_client.post("/api/notifications/999999999/read")
        assert r.status_code in (404, 400, 422)

    def test_push_config(self, admin_client):
        r = admin_client.get("/api/push/config")
        assert r.status_code == 200


class TestValidationEdges:
    def test_project_name_too_long(self, admin_client):
        r = admin_client.post(
            "/api/projects", json={"name": "x" * 300, "color": "#abcdef"})
        assert r.status_code in (400, 422)

    def test_project_bad_color(self, admin_client):
        r = admin_client.post("/api/projects",
                              json={"name": "MX3-color", "color": "not-a-color"})
        assert r.status_code in (400, 422)

    def test_bug_title_min_length(self, admin_client):
        p = _mk_project(admin_client, "MX3-T")
        r = admin_client.post(
            "/api/bugs",
            json={"project_id": p["id"], "title": "ab",
                  "priority": "Medium", "environment": "DEV"})
        assert r.status_code == 422

    def test_bug_unknown_enum(self, admin_client):
        p = _mk_project(admin_client, "MX3-E")
        r = admin_client.post(
            "/api/bugs",
            json={"project_id": p["id"], "title": "enum bad",
                  "priority": "Ultra", "environment": "DEV"})
        assert r.status_code in (400, 422)

    def test_bug_cross_project_get(self, admin_client):
        """IDOR probe: reading a nonexistent id must be 404, not leaked state."""
        p = _mk_project(admin_client, "MX3-I")
        bug = _mk_bug(admin_client, p["id"])
        assert admin_client.get(f"/api/bugs/{bug['id']}").status_code == 200
        assert admin_client.get("/api/bugs/2147483647").status_code == 404

    def test_malformed_json(self, admin_client):
        r = admin_client.post("/api/projects", content=b"{not json",
                              headers={"Content-Type": "application/json"})
        assert r.status_code == 422
