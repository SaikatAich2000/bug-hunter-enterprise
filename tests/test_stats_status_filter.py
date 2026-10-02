"""GET /api/stats?status=... scopes chart breakdowns while headline KPI counts stay global (stable KPI strip)."""
from __future__ import annotations

_BOOTSTRAP_EMAIL = "admin@test.local"
_BOOTSTRAP_PW = "Admin1234"


def _admin(c) -> None:
    c.post("/api/auth/logout")
    assert c.post("/api/auth/login",
                  json={"email": _BOOTSTRAP_EMAIL, "password": _BOOTSTRAP_PW}).status_code == 200


def _project(c) -> int:
    return c.post("/api/projects", json={"name": "Stats Filter Proj"}).json()["id"]


def _bug(c, pid, status, priority="Medium") -> dict:
    r = c.post("/api/bugs", json={
        "project_id": pid, "title": f"{status} bug",
        "priority": priority, "environment": "DEV",
    })
    assert r.status_code == 201, r.text
    bug = r.json()
    if status != "New":
        u = c.put(f"/api/bugs/{bug['id']}", json={"status": status})
        assert u.status_code == 200, u.text
    return bug


def test_status_filter_scopes_charts_but_not_kpis(client):
    _admin(client)
    pid = _project(client)
    # 2 open (New) + 1 resolved
    _bug(client, pid, "New", priority="High")
    _bug(client, pid, "New", priority="Low")
    _bug(client, pid, "Resolved", priority="High")

    glob = client.get("/api/stats").json()
    # Baseline: global KPIs cover all three bugs.
    assert glob["bugs"] >= 3
    assert glob["open"] >= 2
    assert glob["resolved"] >= 1

    # Filter charts to open statuses only.
    filt = client.get("/api/stats", params={"status": ["New", "In Progress", "Reopened"]}).json()

    # KPI counts must be unchanged regardless of the filter.
    assert filt["bugs"] == glob["bugs"]
    assert filt["open"] == glob["open"]
    assert filt["resolved"] == glob["resolved"]

    # The by_status chart should only include the filtered statuses.
    assert "Resolved" not in filt["by_status"], filt["by_status"]
    assert filt["by_status"].get("New", 0) >= 2

    # The excluded High-priority Resolved bug drops High by exactly one vs the global view.
    assert filt["by_priority"].get("Low", 0) >= 1
    assert filt["by_priority"].get("High", 0) == glob["by_priority"].get("High", 0) - 1


def test_status_filter_empty_is_same_as_unfiltered(client):
    _admin(client)
    pid = _project(client)
    _bug(client, pid, "New")
    base = client.get("/api/stats").json()
    # A blank status value is normalized away, so the result matches global.
    blanked = client.get("/api/stats", params={"status": [""]}).json()
    assert blanked["by_status"] == base["by_status"]
