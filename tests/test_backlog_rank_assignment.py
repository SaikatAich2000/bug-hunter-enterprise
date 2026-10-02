"""Coverage for the backlog rank/rank_scope assignment fix on the legacy item
creation paths (POST /api/bugs and the bulk-import row insert). Before this
fix, Bug/Requirement/Task rows created through these two paths never got a
`rank`/`rank_scope`, so they silently never appeared in the Agile backlog
(app.agile.backlog.list_backlog filters on Bug.rank_scope) even after the
project enabled Agile. See app/routes/bugs.py::create_bug and
app/bulk_import.py::_create_validated_rows.
"""
from __future__ import annotations

import io

from openpyxl import Workbook

from app.agile.backlog import backlog_scope
from app.bulk_import import TEMPLATE_HEADERS


def _make_project(client, name="Rank Fix Proj"):
    r = client.post("/api/projects", json={"name": name, "color": "#336699"})
    assert r.status_code == 201, r.text
    return r.json()


def _enable_agile(client, project_id):
    r = client.post(f"/api/agile/projects/{project_id}/enable", json={"feature_flags": {}})
    assert r.status_code == 200, r.text
    return r.json()["board_id"]


def _get_bug_row(project_id, bug_id):
    from app.database import SessionLocal
    from app.models import Bug

    db = SessionLocal()
    try:
        return db.get(Bug, bug_id)
    finally:
        db.close()


def test_create_bug_gets_backlog_rank_scope(admin_client):
    """A freshly created Bug is immediately given a backlog rank_scope/rank,
    not left NULL for a later backfill."""
    project = _make_project(admin_client, "Rank Fix — Create Bug")
    res = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Unresponsive button",
        "item_type": "Bug", "priority": "Medium", "environment": "DEV",
    })
    assert res.status_code == 201, res.text
    bug_id = res.json()["id"]

    row = _get_bug_row(project["id"], bug_id)
    assert row.rank_scope == backlog_scope(project["id"])
    assert row.rank is not None


def test_create_bug_appears_in_backlog_after_agile_enabled_later(admin_client):
    """Items created before Agile is enabled for the project still show up in
    the backlog once Agile is turned on — no manual backfill required."""
    project = _make_project(admin_client, "Rank Fix — Late Enable")
    res = admin_client.post("/api/bugs", json={
        "project_id": project["id"], "title": "Pre-agile bug",
        "item_type": "Bug", "priority": "Medium", "environment": "DEV",
    })
    assert res.status_code == 201, res.text
    bug_id = res.json()["id"]

    board_id = _enable_agile(admin_client, project["id"])
    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    ids = [i["id"] for i in backlog]
    assert bug_id in ids


def test_create_bug_sequential_ranks_ordered_by_creation(admin_client):
    """Multiple items created back-to-back land in the backlog in creation
    order (each gets a rank strictly after the previous one's)."""
    project = _make_project(admin_client, "Rank Fix — Sequential")
    board_id = _enable_agile(admin_client, project["id"])

    ids = []
    for title in ("First", "Second", "Third"):
        res = admin_client.post("/api/bugs", json={
            "project_id": project["id"], "title": title,
            "item_type": "Task", "priority": "Medium", "environment": "DEV",
        })
        assert res.status_code == 201, res.text
        ids.append(res.json()["id"])

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    backlog_ids = [i["id"] for i in backlog]
    positions = [backlog_ids.index(i) for i in ids]
    assert positions == sorted(positions), "items must be ranked in creation order"


def test_create_bug_rank_scope_isolated_per_project(admin_client):
    """rank_scope is per-project — items in different projects never share
    a backlog ranking sequence."""
    project_a = _make_project(admin_client, "Rank Fix — Proj A")
    project_b = _make_project(admin_client, "Rank Fix — Proj B")

    res_a = admin_client.post("/api/bugs", json={
        "project_id": project_a["id"], "title": "A item",
        "item_type": "Bug", "priority": "Medium", "environment": "DEV",
    })
    res_b = admin_client.post("/api/bugs", json={
        "project_id": project_b["id"], "title": "B item",
        "item_type": "Bug", "priority": "Medium", "environment": "DEV",
    })
    assert res_a.status_code == 201
    assert res_b.status_code == 201

    row_a = _get_bug_row(project_a["id"], res_a.json()["id"])
    row_b = _get_bug_row(project_b["id"], res_b.json()["id"])
    assert row_a.rank_scope == backlog_scope(project_a["id"])
    assert row_b.rank_scope == backlog_scope(project_b["id"])
    assert row_a.rank_scope != row_b.rank_scope


# --- Bulk import: same fix, same guarantees ---

def _xlsx_bytes(rows: list[dict[str, str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.cell(row=1, column=1, value="Item Detail Export — banner row")
    for col, label in enumerate(TEMPLATE_HEADERS, start=1):
        ws.cell(row=2, column=col, value=label)
    for r_idx, row in enumerate(rows, start=3):
        for col, label in enumerate(TEMPLATE_HEADERS, start=1):
            ws.cell(row=r_idx, column=col, value=row.get(label, ""))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_bulk_import_assigns_backlog_rank_scope(admin_client):
    """Rows created via bulk import also get a backlog rank_scope/rank, and
    show up in the Agile backlog immediately."""
    project = _make_project(admin_client, "Rank Fix — Bulk")
    board_id = _enable_agile(admin_client, project["id"])

    rows = [
        {"Project": project["name"], "Title": "Bulk row 1", "Type": "Bug",
         "Status": "New", "Priority": "Medium", "Environment": "DEV"},
        {"Project": project["name"], "Title": "Bulk row 2", "Type": "Task",
         "Status": "New", "Priority": "Medium", "Environment": "DEV"},
    ]
    res = admin_client.post(
        "/api/bugs/import",
        files={"file": ("rows.xlsx", io.BytesIO(_xlsx_bytes(rows)), "application/octet-stream")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] == 2, body

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    titles = {i["title"] for i in backlog}
    assert {"Bulk row 1", "Bulk row 2"} <= titles


def test_bulk_import_sequential_ranks_within_one_batch(admin_client):
    """Two rows for the same project in a single import batch get distinct,
    ordered ranks (not colliding on the same backlog slot)."""
    project = _make_project(admin_client, "Rank Fix — Bulk Sequential")
    board_id = _enable_agile(admin_client, project["id"])

    rows = [
        {"Project": project["name"], "Title": f"Row {n}", "Type": "Bug",
         "Status": "New", "Priority": "Medium", "Environment": "DEV"}
        for n in range(1, 4)
    ]
    res = admin_client.post(
        "/api/bugs/import",
        files={"file": ("rows.xlsx", io.BytesIO(_xlsx_bytes(rows)), "application/octet-stream")},
    )
    assert res.status_code == 200, res.text

    backlog = admin_client.get(f"/api/agile/boards/{board_id}/backlog").json()
    ranks = [i["rank"] for i in backlog if i["title"] in {"Row 1", "Row 2", "Row 3"}]
    assert len(ranks) == 3
    assert len(set(ranks)) == 3, "each imported row must get a distinct rank"
    assert ranks == sorted(ranks), "ranks must be strictly increasing within the batch"
