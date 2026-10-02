"""Coverage tests for app.routes.events and app.routes.bugs.

Prefers the public API; a few helpers are called directly for branches HTTP can't reach.
app.* is imported inside each test because the client fixture re-imports it per test.
"""
from __future__ import annotations

import io

from tests.conftest import default_org_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_project(client, name="Eng"):
    r = client.post("/api/projects", json={"name": name, "color": "#c9764f"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_event(client, name="Standup 2026-05-28", **extra):
    body = {"name": name, "scheduled_for": "2026-05-28"}
    body.update(extra)
    r = client.post("/api/events", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_item(client, project_id, item_type="Bug", **extra):
    body = {
        "title": f"Sample {item_type}",
        "project_id": project_id,
        "item_type": item_type,
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_user(client, name, role="user", email=None, password="User12345Aa"):
    email = email or f"{name.lower()}@cov.test"
    r = client.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


# ===========================================================================
# events.py
# ===========================================================================

# --- list_events: filter by scheduled_for ---------------------------------
def test_cov_events_list_filter_by_scheduled_for(admin_client):
    _make_event(admin_client, name="On 28th", scheduled_for="2026-05-28")
    _make_event(admin_client, name="On 29th", scheduled_for="2026-05-29")
    rows = admin_client.get("/api/events?scheduled_for=2026-05-28").json()
    names = {r["name"] for r in rows}
    assert "On 28th" in names
    assert "On 29th" not in names


# --- _resolve_managers: unknown user ids → 400 ----------------------------
def test_cov_events_create_unknown_manager_id_400(admin_client):
    r = admin_client.post("/api/events", json={
        "name": "bad-mgr", "manager_ids": [999999],
    })
    assert r.status_code == 400, r.text
    assert "unknown user ids" in r.json()["detail"].lower()


# --- _apply_event_manager_diff: manager set changes ----------------------
def test_cov_events_update_changes_manager_set(admin_client):
    m1 = _make_user(admin_client, "MgrA", role="manager", email="mgra@cov.test")
    m2 = _make_user(admin_client, "MgrB", role="manager", email="mgrb@cov.test")
    ev = _make_event(admin_client, name="mgr-swap", manager_ids=[m1["id"]])
    assert [m["id"] for m in ev["managers"]] == [m1["id"]]
    # old != new triggers the diff branch
    r = admin_client.put(f"/api/events/{ev['id']}", json={"manager_ids": [m2["id"]]})
    assert r.status_code == 200, r.text
    assert {m["id"] for m in r.json()["managers"]} == {m2["id"]}
    audit = admin_client.get("/api/audit").json()
    assert any(a["action"] == "event_managers_changed" for a in audit), audit


def test_cov_events_update_same_manager_set_is_noop(admin_client):
    """Re-sending the same manager set in different order is a no-op (early-return in _apply_event_manager_diff)."""
    m1 = _make_user(admin_client, "MgrC", role="manager", email="mgrc@cov.test")
    m2 = _make_user(admin_client, "MgrD", role="manager", email="mgrd@cov.test")
    ev = _make_event(admin_client, name="mgr-same", manager_ids=[m1["id"], m2["id"]])
    r = admin_client.put(f"/api/events/{ev['id']}", json={
        "manager_ids": [m2["id"], m1["id"]],  # reversed order, same set
    })
    assert r.status_code == 200, r.text
    assert {m["id"] for m in r.json()["managers"]} == {m1["id"], m2["id"]}


# --- _persist_event_update: rolls back when nothing changed ---------------
def test_cov_events_update_no_change_rolls_back(admin_client):
    ev = _make_event(admin_client, name="No-op event")
    audit_before = admin_client.get("/api/audit").json()
    n_before = sum(1 for a in audit_before if a.get("entity_type") == "event")
    # No real change → _persist_event_update rolls back without an audit row.
    r = admin_client.put(f"/api/events/{ev['id']}", json={"name": "No-op event"})
    assert r.status_code == 200, r.text
    audit_after = admin_client.get("/api/audit").json()
    n_after = sum(1 for a in audit_after if a.get("entity_type") == "event")
    assert n_after == n_before


# --- update_event: 404 when missing ---------------------------------------
def test_cov_events_update_missing_404(admin_client):
    r = admin_client.put("/api/events/999999", json={"name": "ghost"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Event not found"


# --- delete_event: 404 when missing ----------------------------------------
def test_cov_events_delete_missing_404(admin_client):
    r = admin_client.delete("/api/events/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Event not found"


# --- _event_brief: null creator -------------------------------------------
def test_cov_events_brief_with_null_creator(admin_client):
    """Event with NULL created_by_user_id still renders (false branch of _event_brief's creator check)."""
    from app.database import SessionLocal
    from app.models import Event

    db = SessionLocal()
    try:
        ev = Event(org_id=default_org_id(), name="Orphan event", description="", scheduled_for=None,
                   created_by_user_id=None)
        db.add(ev)
        db.commit()
        db.refresh(ev)
        ev_id = ev.id
    finally:
        db.close()

    # Null creator → created_by_name stays None in both the detail and list views.
    detail = admin_client.get(f"/api/events/{ev_id}").json()
    assert detail["id"] == ev_id
    assert detail["created_by_user_id"] is None
    assert detail["created_by_name"] is None
    rows = admin_client.get("/api/events").json()
    assert any(r["id"] == ev_id and r["created_by_name"] is None for r in rows)


# ===========================================================================
# bugs.py
# ===========================================================================

# --- _check_user_rate: cap eviction and timestamp expiry ------------------
def test_cov_bugs_rate_guard_cap_eviction_and_expiry(client):
    """Two inner branches of the sliding-window rate guard: at-capacity eviction, and stale-timestamp pop."""
    import threading
    import time

    import app.routes.bugs as bugs

    # Pre-fill to cap with one user, then a second user must evict it.
    buckets: dict = {}
    lock = threading.Lock()
    bugs._check_user_rate(
        buckets, lock, user_id=1, max_req=5, window=60,
        detail="x", cap=1,
    )
    assert 1 in buckets
    assert len(buckets) == 1
    # At cap: user 1 is evicted when user 2 is inserted.
    bugs._check_user_rate(
        buckets, lock, user_id=2, max_req=5, window=60,
        detail="x", cap=1,
    )
    assert 2 in buckets
    assert 1 not in buckets  # evicted

    # Seed a bucket with a stale timestamp; the next call must pop it.
    from collections import deque
    buckets2: dict = {7: deque([time.monotonic() - 120.0])}
    bugs._check_user_rate(
        buckets2, lock, user_id=7, max_req=5, window=60,
        detail="x", cap=100,
    )
    assert len(buckets2[7]) == 1  # stale entry popped, one fresh entry remains


# --- _resolve_user: 400 for a non-existent reporter -----------------------
def test_cov_bugs_update_unknown_reporter_400(admin_client):
    p = _make_project(admin_client, name="ReporterProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Admin clears the reporter-change gate; _resolve_user(999999) then raises 400.
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": 999999})
    assert r.status_code == 400, r.text
    assert "does not exist" in r.json()["detail"].lower()


# --- _apply_q_filter: early-return when cleaned query is empty ------------
def test_cov_bugs_list_q_only_hash_returns_all(admin_client):
    p = _make_project(admin_client, name="QProj")
    _make_item(admin_client, p["id"], item_type="Bug", title="alpha")
    _make_item(admin_client, p["id"], item_type="Bug", title="beta")
    # "#" strips to "" (not a digit, not truthy) so the filter is a no-op.
    rows = admin_client.get("/api/bugs?q=%23").json()
    assert rows["total"] >= 2


# --- list bugs: filter by reporter_id -------------------------------------
def test_cov_bugs_list_filter_by_reporter_id(admin_client):
    me = admin_client.get("/api/auth/me").json()
    u = _make_user(admin_client, "Reporter1", role="manager", email="rep1@cov.test")
    p = _make_project(admin_client, name="RepFilterProj")
    _make_item(admin_client, p["id"], item_type="Bug", title="admins")
    _make_item(admin_client, p["id"], item_type="Bug", title="theirs",
               reporter_id=u["id"])
    rows = admin_client.get(f"/api/bugs?reporter_id={u['id']}").json()
    assert rows["total"] == 1
    assert rows["items"][0]["reporter"]["id"] == u["id"]
    mine = admin_client.get(f"/api/bugs?reporter_id={me['id']}").json()
    assert all(it["reporter"]["id"] == me["id"] for it in mine["items"])
    assert mine["total"] >= 1  # sanity


# --- _validate_update_payload: unknown project_id → 400 ------------------
def test_cov_bugs_update_unknown_project_400(admin_client):
    p = _make_project(admin_client, name="UpdProjA")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"project_id": 999999})
    assert r.status_code == 400, r.text
    assert "project does not exist" in r.json()["detail"].lower()


# --- update_bug: 404 when missing -----------------------------------------
def test_cov_bugs_update_missing_404(admin_client):
    r = admin_client.put("/api/bugs/999999", json={"title": "ghost title"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# --- delete_bug: 404 when missing -----------------------------------------
def test_cov_bugs_delete_missing_404(admin_client):
    r = admin_client.delete("/api/bugs/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# --- list_comments: comment with an attachment ----------------------------
def test_cov_bugs_list_comments_with_attachment(admin_client):
    p = _make_project(admin_client, name="CommentsProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    # Two comments so the grouping loop runs over multiple rows.
    c1 = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "first"})
    assert c1.status_code == 201, c1.text
    c2 = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "second"})
    assert c2.status_code == 201, c2.text
    # Attach to the second comment so the by_cid grouping has a non-empty entry.
    files = {"file": ("note.txt", io.BytesIO(b"attached bytes"), "text/plain")}
    r = admin_client.post(
        f"/api/bugs/{bug['id']}/attachments",
        files=files, data={"comment_id": str(c2.json()["id"])},
    )
    assert r.status_code == 201, r.text

    listing = admin_client.get(f"/api/bugs/{bug['id']}/comments")
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert len(rows) == 2
    by_id = {c["id"]: c for c in rows}
    assert len(by_id[c2.json()["id"]]["attachments"]) == 1
    assert by_id[c2.json()["id"]]["attachments"][0]["filename"] == "note.txt"
    assert by_id[c1.json()["id"]]["attachments"] == []


# --- _read_upload_with_limit: oversized upload returns 413 ----------------
def test_cov_bugs_upload_too_large_413(admin_client, monkeypatch):
    """Shrink MAX_FILE_BYTES (read at call time) so a small upload trips the 413 guard."""
    import app.routes.bugs as bugs
    monkeypatch.setattr(bugs, "MAX_FILE_BYTES", 4)

    p = _make_project(admin_client, name="BigFileProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    files = {"file": ("big.bin", io.BytesIO(b"way too many bytes"), "application/octet-stream")}
    r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
    assert r.status_code == 413, r.text
    assert "too large" in r.json()["detail"].lower()


# --- upload_attachment: 404 when bug doesn't exist ------------------------
def test_cov_bugs_upload_missing_bug_404(admin_client):
    files = {"file": ("x.txt", io.BytesIO(b"hi"), "text/plain")}
    r = admin_client.post("/api/bugs/999999/attachments", files=files)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# --- delete_attachment: 404 when missing ----------------------------------
def test_cov_bugs_delete_attachment_missing_404(admin_client):
    p = _make_project(admin_client, name="DelAttProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Attachment not found"


# --- update_comment: 404 when comment absent ------------------------------
def test_cov_bugs_update_comment_missing_404(admin_client):
    p = _make_project(admin_client, name="UpdCmtProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.put(
        f"/api/bugs/{bug['id']}/comments/999999", json={"body": "edited"},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Comment not found"


# --- delete_comment: 404 when comment absent ------------------------------
def test_cov_bugs_delete_comment_missing_404(admin_client):
    p = _make_project(admin_client, name="DelCmtProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    r = admin_client.delete(f"/api/bugs/{bug['id']}/comments/999999")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Comment not found"


# --- list_activity: 404 when bug doesn't exist ----------------------------
def test_cov_bugs_list_activity_missing_bug_404(admin_client):
    r = admin_client.get("/api/bugs/999999/activity")
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Bug not found"


# statuses_for_type across all three item flavors, plus cross-type rejection.
def test_cov_bugs_status_transitions_per_item_type(admin_client):
    p = _make_project(admin_client, name="StatusProj")
    bug = _make_item(admin_client, p["id"], item_type="Bug")
    task = _make_item(admin_client, p["id"], item_type="Task")
    req = _make_item(admin_client, p["id"], item_type="Requirement")

    r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Resolved"})
    assert r.status_code == 200
    assert r.json()["status"] == "Resolved"
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "Done"})
    assert r.status_code == 200
    assert r.json()["status"] == "Done"
    r = admin_client.put(f"/api/bugs/{req['id']}", json={"status": "Approved"})
    assert r.status_code == 200
    assert r.json()["status"] == "Approved"

    # Universal status vocabulary: every configured status is valid for every
    # item type, so Task may also carry "Resolved" (and Bug may reach "Done").
    r = admin_client.put(f"/api/bugs/{task['id']}", json={"status": "Resolved"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Resolved"

    # A genuinely unknown status is rejected by schema validation (422).
    r = admin_client.put(
        f"/api/bugs/{task['id']}", json={"status": "Bogus Status"},
    )
    assert r.status_code == 422, r.text
    assert "invalid status" in str(r.json()["detail"]).lower()
