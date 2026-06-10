"""Tests for the Reports feature (enterprise port of v2.9).

Enterprise differs from the internal Bug Hunter:
  - No `bootstrap admin` — every test uses signup to create an org admin.
  - All queries are scoped by `actor.org_id`; cross-org isolation is the
    most important invariant to verify.
  - User creation goes through the invitations flow, not direct POST.

The full coverage matrix from the internal `test_reports.py` lives there;
this file ships the smoke + critical-path tests that map cleanly onto
enterprise's signup-based fixtures.
"""
from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook


_TEST_SECRET = ("TestPass1!",)   # noqa: S105 — hermetic test password


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_project(client, name="ReportProj"):
    r = client.post("/api/projects", json={"name": name, "color": "#000000"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, **extra):
    body = {
        "title": "reports smoke item",
        "project_id": project_id,
        "priority": "High",
        "environment": "DEV",
        "item_type": "Bug",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _change_status(client, bug_id, new_status):
    r = client.put(f"/api/bugs/{bug_id}", json={"status": new_status})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
def test_reports_unauthenticated_401(client):
    """No session → 401 on every reports endpoint."""
    r = client.get("/api/reports/types")
    assert r.status_code == 401
    r = client.post("/api/reports/run", json={"report_key": "item_detail"})
    assert r.status_code == 401


def test_admin_can_list_report_types(admin_client):
    r = admin_client.get("/api/reports/types")
    assert r.status_code == 200
    body = r.json()
    keys = {t["key"] for t in body["types"]}
    expected = {
        "item_detail", "throughput", "pending_snapshot",
        "status_distribution", "priority_distribution",
        "project_breakdown", "aging", "timeline", "time_to_resolution",
    }
    assert expected.issubset(keys)


# ---------------------------------------------------------------------------
# Item Detail
# ---------------------------------------------------------------------------
def test_item_detail_returns_admin_org_items(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="alpha")
    _make_item(admin_client, p["id"], title="beta")
    r = admin_client.post("/api/reports/run", json={
        "report_key": "item_detail", "filters": {},
    })
    assert r.status_code == 200
    body = r.json()
    titles = {row["title"] for row in body["rows"]}
    assert {"alpha", "beta"}.issubset(titles)


# ---------------------------------------------------------------------------
# Throughput from activity_log
# ---------------------------------------------------------------------------
def test_throughput_counts_resolution_events(admin_client):
    p = _make_project(admin_client)
    b = _make_item(admin_client, p["id"], title="resolvable-one")
    _change_status(admin_client, b["id"], "Resolved")
    r = admin_client.post("/api/reports/run", json={
        "report_key": "throughput", "filters": {},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["total_resolved"] >= 1


# ---------------------------------------------------------------------------
# Cross-org isolation — the critical enterprise invariant
# ---------------------------------------------------------------------------
def test_reports_isolate_across_orgs(two_orgs):
    """Org B's admin must NOT see Org A's items in their Reports run."""
    c_a, c_b, _me_a, _me_b = two_orgs
    # Create a project + bug in Org A.
    p_a = c_a.post("/api/projects", json={"name": "Secret A", "color": "#000000"}).json()
    c_a.post("/api/bugs", json={
        "project_id": p_a["id"], "title": "ORG A SECRET BUG",
        "priority": "High", "environment": "DEV", "item_type": "Bug",
    })
    # Org B runs a report — should see ZERO items.
    r = c_b.post("/api/reports/run", json={"report_key": "item_detail", "filters": {}})
    assert r.status_code == 200
    body = r.json()
    titles = {row["title"] for row in body["rows"]}
    assert "ORG A SECRET BUG" not in titles
    assert body["total"] == 0


# ---------------------------------------------------------------------------
# XLSX export — round-trips and is org-scoped
# ---------------------------------------------------------------------------
def test_xlsx_export_returns_valid_workbook(admin_client):
    p = _make_project(admin_client)
    _make_item(admin_client, p["id"], title="export-target")
    r = admin_client.post("/api/reports/export.xlsx", json={
        "report_key": "item_detail", "filters": {"label": "Smoke"},
    })
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    assert "Filters Applied" in wb.sheetnames


def test_xlsx_export_isolates_across_orgs(two_orgs):
    c_a, c_b, *_ = two_orgs
    p_a = c_a.post("/api/projects", json={"name": "Iso A", "color": "#000000"}).json()
    c_a.post("/api/bugs", json={
        "project_id": p_a["id"], "title": "ONLY-IN-A",
        "priority": "High", "environment": "DEV", "item_type": "Bug",
    })
    r = c_b.post("/api/reports/export.xlsx", json={"report_key": "item_detail", "filters": {}})
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content), read_only=True)
    text = " ".join(
        str(v) for row in wb[wb.sheetnames[0]].iter_rows(values_only=True)
        for v in row if v is not None
    )
    assert "ONLY-IN-A" not in text


# ---------------------------------------------------------------------------
# Sleuth report intent
# ---------------------------------------------------------------------------
def test_sleuth_report_intent_returns_report_for_admin(admin_client):
    p = _make_project(admin_client)
    b = _make_item(admin_client, p["id"], title="for-sleuth")
    _change_status(admin_client, b["id"], "Resolved")
    r = admin_client.post("/api/chat/ask", json={
        "message": "report of who resolved how many bugs",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "report", body


# ---------------------------------------------------------------------------
# Legacy CSV endpoint must be gone
# ---------------------------------------------------------------------------
def test_legacy_csv_endpoint_removed(admin_client):
    r = admin_client.get("/api/bugs/export.csv")
    # 404 (no handler) or 422 (catchall /{bug_id} treats "export.csv" as
    # not-an-int) both mean the legacy CSV is gone.
    assert r.status_code in (404, 422), r.text
