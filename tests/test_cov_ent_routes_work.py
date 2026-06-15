"""Coverage-maximizing route tests for the work-item surfaces.

Targets the under-covered branches of five enterprise route modules:

  - app/routes/events.py        (event CRUD, manager validation, link/list,
                                  permission + not-found + cross-tenant)
  - app/routes/projects.py      (create/update/delete, key derivation,
                                  duplicate-key, integrity, not-found)
  - app/routes/custom_fields.py (field CRUD, bulk-set values, type/option
                                  validation, not-found, access gates)
  - app/routes/saved_views.py   (view CRUD, owner-only edit/delete, 404)
  - app/routes/sessions.py      (list/revoke, current-session guard,
                                  cross-tenant 404)

Every test names the endpoint + the specific branch it pins. All run on
the conftest fixtures (`client`, `admin_client`, `two_orgs`,
`make_invite`) against the in-memory-ish tmp SQLite DB; no network
egress (EMAIL_BACKEND=disabled, push/email are background no-ops here).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

PASS = "TestPass1!"


# ---------------------------------------------------------------------------
# Shared helpers (kept local so this file is additive + self-contained)
# ---------------------------------------------------------------------------
def _app():
    return __import__("app.main", fromlist=["app"]).app


def _signup_admin(client, org="Acme", name="Ada Admin",
                  email="ada@x.test", password=PASS):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _accept_user(client, make_invite, email, role="member",
                 project_ids=None, as_lead=False, name="Member Person"):
    """Invite + accept a second user. Returns a LIVE (entered) TestClient
    plus the accepted user's JSON. The client must be closed by the test
    (use within a `with` or rely on GC at test end — TestClient is cheap)."""
    tok = make_invite(client, email, role=role,
                      project_ids=project_ids or [], as_lead=as_lead)
    c = TestClient(_app())
    c.__enter__()
    me = c.post("/api/invitations/accept", json={
        "token": tok, "name": name, "password": PASS,
    }).json()
    return c, me


def _make_project(client, name="Proj", **extra):
    body = {"name": name, "color": "#334455"}
    body.update(extra)
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **extra):
    body = {
        "title": "Sample work item",
        "project_id": project_id,
        "item_type": "Bug",
        "priority": "Medium",
        "environment": "DEV",
        "description": "Test description body",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# events.py
# ===========================================================================
class TestEventsCreate:
    def test_create_minimal_event_admin(self, admin_client):
        """POST /api/events — happy path, no managers (skips notify branch)."""
        r = admin_client.post("/api/events", json={"name": "Daily Standup"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "Daily Standup"
        assert body["item_count"] == 0
        assert body["can_edit"] is True
        assert body["can_delete"] is True
        assert body["managers"] == []

    def test_create_event_with_scheduled_for(self, admin_client):
        """POST /api/events — scheduled_for set → _log appends the
        '(scheduled for ...)' suffix branch."""
        r = admin_client.post("/api/events", json={
            "name": "Sprint Planning", "scheduled_for": "2026-07-01",
        })
        assert r.status_code == 201, r.text
        assert r.json()["scheduled_for"] == "2026-07-01"

    def test_create_event_with_manager_fires_notify(self, admin_client):
        """POST /api/events with manager_ids → managers assigned and the
        notify/push background tasks branch (ev.managers truthy) runs."""
        me = admin_client.admin_me
        r = admin_client.post("/api/events", json={
            "name": "Managed Event", "manager_ids": [me["id"]],
        })
        assert r.status_code == 201, r.text
        managers = r.json()["managers"]
        assert [m["id"] for m in managers] == [me["id"]]

    def test_create_event_unknown_manager_id_400(self, admin_client):
        """_resolve_managers — unknown user id → 400."""
        r = admin_client.post("/api/events", json={
            "name": "Bad Mgr", "manager_ids": [999999],
        })
        assert r.status_code == 400
        assert "Unknown user ids" in r.json()["detail"]

    def test_create_event_member_manager_role_rejected_400(self, admin_client, make_invite):
        """_resolve_managers — a member-role user can't be an event
        manager → 400 (bad_roles branch)."""
        c, mem = _accept_user(admin_client, make_invite, "memmgr@x.test",
                              role="member", name="Mem Mgr")
        try:
            r = admin_client.post("/api/events", json={
                "name": "Role Check", "manager_ids": [mem["id"]],
            })
            assert r.status_code == 400
            assert "admin or manager" in r.json()["detail"]
        finally:
            c.__exit__(None, None, None)

    def test_create_event_dedupes_manager_ids(self, admin_client):
        """_resolve_managers — duplicate ids in manager_ids are deduped
        (seen-set branch); result lists the manager once."""
        me = admin_client.admin_me
        r = admin_client.post("/api/events", json={
            "name": "Dedupe Event",
            "manager_ids": [me["id"], me["id"]],
        })
        assert r.status_code == 201, r.text
        assert len(r.json()["managers"]) == 1

    def test_create_event_cross_org_manager_400(self, two_orgs):
        """_resolve_managers — manager id from another org → 400 (cross_org
        branch, same shape as unknown-id so existence isn't leaked)."""
        c_a, c_b, _me_a, me_b = two_orgs
        r = c_a.post("/api/events", json={
            "name": "X-Tenant Mgr", "manager_ids": [me_b["id"]],
        })
        assert r.status_code == 400
        assert "Unknown user ids" in r.json()["detail"]

    def test_member_cannot_create_event_403(self, admin_client, make_invite):
        """_require_edit — member role is read-only → 403."""
        c, _mem = _accept_user(admin_client, make_invite, "ro@x.test",
                               role="member", name="Read Only")
        try:
            r = c.post("/api/events", json={"name": "Sneaky"})
            assert r.status_code == 403
            assert "admins and managers" in r.json()["detail"]
        finally:
            c.__exit__(None, None, None)

    def test_manager_can_create_event(self, admin_client, make_invite):
        """_require_edit — manager role passes the gate (can_edit_event)."""
        c, _mgr = _accept_user(admin_client, make_invite, "mgr@x.test",
                               role="manager", name="Manager Mike")
        try:
            r = c.post("/api/events", json={"name": "Mgr Event"})
            assert r.status_code == 201, r.text
        finally:
            c.__exit__(None, None, None)

    def test_create_event_name_too_short_422(self, admin_client):
        """EventCreate validator — name < 2 chars → 422."""
        r = admin_client.post("/api/events", json={"name": "X"})
        assert r.status_code == 422


class TestEventsListAndGet:
    def test_list_events_empty_org(self, admin_client):
        """GET /api/events — fresh org with no events hits the
        `_item_counts_by_event([])` early `return {}` branch (no rows)."""
        assert admin_client.get("/api/events").json() == []

    def test_list_events_returns_created(self, admin_client):
        """GET /api/events — list returns org events with item counts."""
        admin_client.post("/api/events", json={"name": "Event A"})
        admin_client.post("/api/events", json={"name": "Event B"})
        rows = admin_client.get("/api/events").json()
        names = {e["name"] for e in rows}
        assert {"Event A", "Event B"}.issubset(names)

    def test_list_events_scheduled_for_filter(self, admin_client):
        """GET /api/events?scheduled_for=... — exercises the optional
        scheduled_for WHERE branch."""
        admin_client.post("/api/events", json={
            "name": "Dated", "scheduled_for": "2026-08-15",
        })
        admin_client.post("/api/events", json={
            "name": "Other", "scheduled_for": "2026-09-15",
        })
        rows = admin_client.get(
            "/api/events", params={"scheduled_for": "2026-08-15"}
        ).json()
        assert [e["name"] for e in rows] == ["Dated"]

    def test_get_event_detail_with_items_and_attachments(self, admin_client):
        """GET /api/events/{id} — detail includes linked items projected
        via _bug_to_event_item, with the attachment-count aggregate."""
        p = _make_project(admin_client, name="Detail Proj")
        ev = admin_client.post("/api/events", json={"name": "Detail Event"}).json()
        bug = _make_bug(admin_client, p["id"], event_id=ev["id"])
        # Add an attachment so the att_counts aggregate branch runs non-empty.
        import io
        admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("a.txt", io.BytesIO(b"x"), "text/plain")},
        )
        detail = admin_client.get(f"/api/events/{ev['id']}").json()
        assert detail["item_count"] == 1
        assert len(detail["items"]) == 1
        assert detail["items"][0]["attachment_count"] == 1
        assert detail["items"][0]["project_name"] == "Detail Proj"

    def test_get_event_detail_no_items(self, admin_client):
        """GET /api/events/{id} — empty event hits the `att_counts = {}`
        (no bug_ids) branch."""
        ev = admin_client.post("/api/events", json={"name": "Empty Event"}).json()
        detail = admin_client.get(f"/api/events/{ev['id']}").json()
        assert detail["items"] == []

    def test_get_event_not_found_404(self, admin_client):
        """_get_event_or_404 — missing id → 404."""
        assert admin_client.get("/api/events/999999").status_code == 404

    def test_get_event_cross_org_404(self, two_orgs):
        """_get_event_or_404 — org B event invisible to org A → 404."""
        c_a, c_b, _me_a, _me_b = two_orgs
        ev_b = c_b.post("/api/events", json={"name": "B Secret"}).json()
        assert c_a.get(f"/api/events/{ev_b['id']}").status_code == 404


class TestEventsUpdate:
    def test_update_event_fields(self, admin_client):
        """PUT /api/events/{id} — name/description/scheduled_for change →
        _compute_event_changes records them and persists."""
        ev = admin_client.post("/api/events", json={"name": "Before"}).json()
        r = admin_client.put(f"/api/events/{ev['id']}", json={
            "name": "After", "description": "now described",
            "scheduled_for": "2026-10-10",
        })
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "After"
        assert r.json()["scheduled_for"] == "2026-10-10"

    def test_update_event_no_changes_rolls_back(self, admin_client):
        """PUT /api/events/{id} — re-sending identical values yields no
        changes → _persist_event_update rollback branch (no audit row)."""
        ev = admin_client.post("/api/events", json={
            "name": "Stable", "description": "same",
        }).json()
        r = admin_client.put(f"/api/events/{ev['id']}", json={
            "name": "Stable", "description": "same",
        })
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Stable"

    def test_update_event_add_managers(self, admin_client):
        """_apply_event_manager_diff — old/new ids differ → managers set,
        a 'managers' change row appended, notify branch runs."""
        me = admin_client.admin_me
        ev = admin_client.post("/api/events", json={"name": "Add Mgr"}).json()
        r = admin_client.put(f"/api/events/{ev['id']}", json={
            "manager_ids": [me["id"]],
        })
        assert r.status_code == 200, r.text
        assert [m["id"] for m in r.json()["managers"]] == [me["id"]]

    def test_update_event_same_managers_no_diff(self, admin_client):
        """_apply_event_manager_diff — re-sending the same manager set is
        a no-op (old_ids == new_ids early return)."""
        me = admin_client.admin_me
        ev = admin_client.post("/api/events", json={
            "name": "Keep Mgr", "manager_ids": [me["id"]],
        }).json()
        r = admin_client.put(f"/api/events/{ev['id']}", json={
            "manager_ids": [me["id"]],
        })
        assert r.status_code == 200, r.text
        assert [m["id"] for m in r.json()["managers"]] == [me["id"]]

    def test_update_event_remove_all_managers(self, admin_client):
        """_apply_event_manager_diff — clearing managers ([]) records a
        '(none)' change and empties the M2M."""
        me = admin_client.admin_me
        ev = admin_client.post("/api/events", json={
            "name": "Drop Mgr", "manager_ids": [me["id"]],
        }).json()
        r = admin_client.put(f"/api/events/{ev['id']}", json={"manager_ids": []})
        assert r.status_code == 200, r.text
        assert r.json()["managers"] == []

    def test_update_event_member_forbidden_403(self, admin_client, make_invite):
        """PUT /api/events/{id} — _require_edit blocks members → 403."""
        ev = admin_client.post("/api/events", json={"name": "Locked"}).json()
        c, _mem = _accept_user(admin_client, make_invite, "rw@x.test",
                               role="member", name="Read Member")
        try:
            r = c.put(f"/api/events/{ev['id']}", json={"name": "Hacked"})
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_update_event_not_found_404(self, admin_client):
        """PUT /api/events/{id} — missing → 404 (after the edit gate)."""
        r = admin_client.put("/api/events/999999", json={"name": "Nope"})
        assert r.status_code == 404

    def test_update_event_cross_org_404(self, two_orgs):
        """PUT /api/events/{id} — org A can't touch org B's event → 404."""
        c_a, c_b, _me_a, _me_b = two_orgs
        ev_b = c_b.post("/api/events", json={"name": "B Event"}).json()
        r = c_a.put(f"/api/events/{ev_b['id']}", json={"name": "Pwn"})
        assert r.status_code == 404


class TestEventsDelete:
    def test_delete_event_no_managers(self, admin_client):
        """DELETE /api/events/{id} — happy path, no managers → snap is None,
        background notify skipped."""
        ev = admin_client.post("/api/events", json={"name": "ToDelete"}).json()
        r = admin_client.delete(f"/api/events/{ev['id']}")
        assert r.status_code == 200
        assert r.json()["message"] == "Event deleted"
        assert admin_client.get(f"/api/events/{ev['id']}").status_code == 404

    def test_delete_event_with_managers_and_items(self, admin_client):
        """DELETE /api/events/{id} — managers present → snap set + notify
        branch; linked bug's event_id is nulled (SET NULL parity), bug
        survives."""
        me = admin_client.admin_me
        p = _make_project(admin_client, name="Del Proj")
        ev = admin_client.post("/api/events", json={
            "name": "Big Delete", "manager_ids": [me["id"]],
        }).json()
        bug = _make_bug(admin_client, p["id"], event_id=ev["id"])
        r = admin_client.delete(f"/api/events/{ev['id']}")
        assert r.status_code == 200
        # Bug still exists but is detached from the event.
        got = admin_client.get(f"/api/bugs/{bug['id']}").json()
        assert got["event_id"] is None

    def test_delete_event_member_forbidden_403(self, admin_client, make_invite):
        """DELETE /api/events/{id} — manager (not admin) hits the
        can_delete_event=False branch → 403."""
        ev = admin_client.post("/api/events", json={"name": "MgrCantDelete"}).json()
        c, _mgr = _accept_user(admin_client, make_invite, "mgrdel@x.test",
                               role="manager", name="Mgr Del")
        try:
            r = c.delete(f"/api/events/{ev['id']}")
            assert r.status_code == 403
            assert "admins can delete" in r.json()["detail"]
        finally:
            c.__exit__(None, None, None)

    def test_delete_event_not_found_404(self, admin_client):
        """DELETE /api/events/{id} — missing → 404."""
        assert admin_client.delete("/api/events/999999").status_code == 404

    def test_delete_event_cross_org_404(self, two_orgs):
        """DELETE /api/events/{id} — org A can't delete org B's event."""
        c_a, c_b, _me_a, _me_b = two_orgs
        ev_b = c_b.post("/api/events", json={"name": "B Del"}).json()
        assert c_a.delete(f"/api/events/{ev_b['id']}").status_code == 404


# ===========================================================================
# projects.py
# ===========================================================================
class TestProjectsKeyDerivation:
    def test_single_word_alpha_key(self, admin_client):
        """_derive_key — single alpha word → first 6 chars upper."""
        p = _make_project(admin_client, name="Webportal")
        assert p["key"] == "WEBPOR"

    def test_single_word_numeric_prefixed_key(self, admin_client):
        """_derive_key — single token starting with a digit → 'P'+digits
        (not initials[0].isalpha() branch)."""
        p = _make_project(admin_client, name="1stProject")
        # "1STPROJECT" -> starts with digit -> "P" + first 5 = "P1STPR"
        assert p["key"] == "P1STPR"

    def test_multiword_initials_key(self, admin_client):
        """_derive_key — multi-word → initials of first 4 words."""
        p = _make_project(admin_client, name="Big Red Fox Jumps Over")
        assert p["key"] == "BRFJ"

    def test_multiword_numeric_initials_prefixed(self, admin_client):
        """_derive_key — multi-word whose initials start non-alpha →
        'P'+initials branch."""
        p = _make_project(admin_client, name="3 amigos ride")
        # initials "3AR" -> not alpha[0] -> "P3AR"
        assert p["key"] == "P3AR"

    def test_punctuation_only_name_falls_back_to_default_key(self, admin_client):
        """_derive_key — a single token that's all punctuation strips to
        empty → 'P' fallback (single-word `if not s` branch)."""
        p = _make_project(admin_client, name="!!!")
        assert p["key"] == "P"

    def test_multiword_initials_all_stripped_falls_back_to_default_key(self, admin_client):
        """_derive_key — multi-word whose initials are all non-alnum so
        they strip to empty → 'P' fallback (the multiword `if not initials`
        branch). e.g. "!a !b" → initials "!!" → "" → "P"."""
        p = _make_project(admin_client, name="!a !b")
        assert p["key"] == "P"

    def test_derived_key_collision_suffix(self, admin_client):
        """_unique_key — two names deriving the same base key get a numeric
        suffix on the second."""
        p1 = _make_project(admin_client, name="Marketing Site")
        p2 = _make_project(admin_client, name="Mobile Service")
        assert p1["key"] == "MS"
        assert p2["key"].startswith("MS") and p2["key"] != "MS"


class TestProjectsListGet:
    def test_list_projects_with_counts(self, admin_client):
        """GET /api/projects — list path: returns visible projects with
        per-project member counts and can_manage=True for the admin."""
        _make_project(admin_client, name="List One")
        _make_project(admin_client, name="List Two")
        rows = admin_client.get("/api/projects").json()
        names = {p["name"] for p in rows}
        assert {"List One", "List Two"}.issubset(names)
        for p in rows:
            assert p["member_count"] >= 1
            assert p["can_manage"] is True

    def test_list_projects_empty_for_member(self, admin_client, make_invite):
        """GET /api/projects — a member with no project memberships hits the
        `if not ids: return []` short-circuit."""
        c, _mem = _accept_user(admin_client, make_invite, "ple@x.test",
                               role="member", name="Empty List Member")
        try:
            assert c.get("/api/projects").json() == []
        finally:
            c.__exit__(None, None, None)

    def test_get_one_project_member_count(self, admin_client):
        """GET /api/projects/{id} — single-project read returns member_count
        and can_manage (the get_project handler)."""
        p = _make_project(admin_client, name="Single Get")
        r = admin_client.get(f"/api/projects/{p['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["member_count"] == 1
        assert r.json()["can_manage"] is True

    def test_get_one_non_member_404(self, admin_client, make_invite):
        """GET /api/projects/{id} — same org but caller isn't a member →
        can_access_project False → 404 (not 403, to avoid leaking)."""
        p = _make_project(admin_client, name="Hidden Get")
        c, _mem = _accept_user(admin_client, make_invite, "gnm@x.test",
                               role="member", name="Get NonMember")
        try:
            assert c.get(f"/api/projects/{p['id']}").status_code == 404
        finally:
            c.__exit__(None, None, None)

    def test_get_one_cross_org_404(self, two_orgs):
        """GET /api/projects/{id} — project in another org → 404."""
        c_a, c_b, _me_a, _me_b = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "B Get"}).json()
        assert c_a.get(f"/api/projects/{p_b['id']}").status_code == 404


class TestProjectsIntegrityErrors:
    def test_create_duplicate_name_integrity_409(self, admin_client):
        """POST /api/projects — two projects with the SAME name (distinct
        derived keys) trip the uq_projects_org_name UniqueConstraint at
        flush → IntegrityError handler → 409."""
        _make_project(admin_client, name="Twins", key="TWNA")
        r = admin_client.post("/api/projects", json={
            "name": "Twins", "key": "TWNB", "color": "#222222",
        })
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]

    def test_update_duplicate_name_integrity_409(self, admin_client):
        """PUT /api/projects/{id} — renaming a project to a name already
        used by another project in the org trips uq_projects_org_name →
        IntegrityError handler in update → 409."""
        _make_project(admin_client, name="Alpha Name", key="ALN")
        p2 = _make_project(admin_client, name="Beta Name", key="BTN")
        r = admin_client.put(f"/api/projects/{p2['id']}", json={
            "name": "Alpha Name", "color": p2["color"],
        })
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"]


class TestProjectsCreatePermissions:
    def test_member_cannot_create_403(self, admin_client, make_invite):
        """POST /api/projects — can_create_project False for member → 403."""
        c, _mem = _accept_user(admin_client, make_invite, "pm@x.test",
                               role="member", name="Plain Member")
        try:
            r = c.post("/api/projects", json={"name": "NoCanDo"})
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_explicit_key_duplicate_409(self, admin_client):
        """POST /api/projects — explicit key already used in org → 409."""
        _make_project(admin_client, name="Alpha", key="DUP")
        r = admin_client.post("/api/projects", json={"name": "Beta", "key": "DUP"})
        assert r.status_code == 409
        assert "already in use" in r.json()["detail"]

    def test_explicit_key_honored(self, admin_client):
        """POST /api/projects — explicit unique key kept verbatim (no
        auto-suffix branch)."""
        p = _make_project(admin_client, name="Whatever", key="WTV")
        assert p["key"] == "WTV"


class TestProjectsUpdateBranches:
    def test_update_name_and_description(self, admin_client):
        """PUT /api/projects/{id} — field changes recorded + audited."""
        p = _make_project(admin_client, name="Old")
        r = admin_client.put(f"/api/projects/{p['id']}", json={
            "name": "New Name", "description": "fresh desc", "color": "#abcdef",
        })
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "New Name"

    def test_update_no_changes(self, admin_client):
        """PUT /api/projects/{id} — identical payload → empty changes list
        (skips the audit branch) but still 200."""
        p = _make_project(admin_client, name="Same", description="d")
        r = admin_client.put(f"/api/projects/{p['id']}", json={
            "name": "Same", "description": "d", "color": p["color"],
        })
        assert r.status_code == 200, r.text

    def test_update_new_key(self, admin_client):
        """PUT /api/projects/{id} — new unique key sets the key-change branch."""
        p = _make_project(admin_client, name="Keyed")
        r = admin_client.put(f"/api/projects/{p['id']}", json={
            "name": p["name"], "key": "FRESH",
        })
        assert r.status_code == 200, r.text
        assert r.json()["key"] == "FRESH"

    def test_update_conflicting_key_409(self, admin_client):
        """PUT /api/projects/{id} — switching to a key owned by another
        project in the org → 409."""
        _make_project(admin_client, name="One", key="AK")
        p2 = _make_project(admin_client, name="Two", key="BK")
        r = admin_client.put(f"/api/projects/{p2['id']}", json={
            "name": p2["name"], "key": "AK",
        })
        assert r.status_code == 409

    def test_update_member_forbidden_403(self, admin_client, make_invite):
        """PUT /api/projects/{id} — non-lead member → 403."""
        p = _make_project(admin_client, name="Guarded")
        c, _mem = _accept_user(admin_client, make_invite, "gm@x.test",
                               role="member", project_ids=[p["id"]],
                               name="Guard Member")
        try:
            r = c.put(f"/api/projects/{p['id']}", json={"name": "Hax"})
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_update_not_found_404(self, admin_client):
        """PUT /api/projects/{id} — missing → 404 (valid name so the body
        passes validation and the 404 guard is what fires, not a 422)."""
        assert admin_client.put(
            "/api/projects/999999", json={"name": "Ghost Project"}
        ).status_code == 404


class TestProjectsDeleteBranches:
    def test_delete_empty_ok(self, admin_client):
        """DELETE /api/projects/{id} — no bugs → deleted."""
        p = _make_project(admin_client, name="Disposable")
        assert admin_client.delete(f"/api/projects/{p['id']}").status_code == 200

    def test_delete_with_bugs_409(self, admin_client):
        """DELETE /api/projects/{id} — bug_count > 0 → 409 guard."""
        p = _make_project(admin_client, name="HasBugs")
        _make_bug(admin_client, p["id"])
        r = admin_client.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 409
        assert "belong to this project" in r.json()["detail"]

    def test_delete_member_forbidden_403(self, admin_client, make_invite):
        """DELETE /api/projects/{id} — lead is still not allowed (admin
        only) → 403."""
        p = _make_project(admin_client, name="LeadProj")
        c, _lead = _accept_user(admin_client, make_invite, "ld@x.test",
                                role="member", project_ids=[p["id"]],
                                as_lead=True, name="Lead Person")
        try:
            r = c.delete(f"/api/projects/{p['id']}")
            assert r.status_code == 403
            assert "admins" in r.json()["detail"]
        finally:
            c.__exit__(None, None, None)

    def test_delete_not_found_404(self, admin_client):
        """DELETE /api/projects/{id} — missing → 404."""
        assert admin_client.delete("/api/projects/999999").status_code == 404


# ===========================================================================
# custom_fields.py
# ===========================================================================
class TestCustomFieldsManage:
    def test_create_field_admin(self, admin_client):
        """POST custom-fields — admin/lead creates a select field with
        options joined by '|'."""
        p = _make_project(admin_client, name="CF Proj")
        r = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Severity", "field_type": "select",
            "options": ["Low", "High", "  "], "is_required": True, "position": 1,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["field_type"] == "select"
        # Blank option stripped out.
        assert body["options"] == ["Low", "High"]
        assert body["is_required"] is True

    def test_list_fields(self, admin_client):
        """GET custom-fields — list returns created fields ordered by pos."""
        p = _make_project(admin_client, name="CF List")
        admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Field A", "field_type": "text",
        })
        rows = admin_client.get(f"/api/projects/{p['id']}/custom-fields").json()
        assert any(f["name"] == "Field A" for f in rows)

    def test_create_field_invalid_type_422(self, admin_client):
        """CustomFieldIn validator — unknown field_type → 422."""
        p = _make_project(admin_client, name="CF Bad")
        r = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Weird", "field_type": "rainbow",
        })
        assert r.status_code == 422

    def test_create_field_member_forbidden_403(self, admin_client, make_invite):
        """POST custom-fields — member can't manage → 403."""
        p = _make_project(admin_client, name="CF Guard")
        c, _mem = _accept_user(admin_client, make_invite, "cfm@x.test",
                               role="member", project_ids=[p["id"]],
                               name="CF Member")
        try:
            r = c.post(f"/api/projects/{p['id']}/custom-fields", json={
                "name": "Nope", "field_type": "text",
            })
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_list_fields_no_access_403(self, admin_client, make_invite):
        """GET custom-fields — non-member of the project → 403."""
        p = _make_project(admin_client, name="CF Private")
        c, _mem = _accept_user(admin_client, make_invite, "cfn@x.test",
                               role="member", name="No Access")
        try:
            r = c.get(f"/api/projects/{p['id']}/custom-fields")
            # member is not on the project AND project exists in org → 404
            # from get_org_project_or_404? No: same org so project resolves,
            # then can_access_project False → 403.
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_create_field_cross_org_404(self, two_orgs):
        """POST custom-fields — project in another org → 404."""
        c_a, c_b, _me_a, _me_b = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "B Proj"}).json()
        r = c_a.post(f"/api/projects/{p_b['id']}/custom-fields", json={
            "name": "X", "field_type": "text",
        })
        assert r.status_code == 404

    def test_update_field(self, admin_client):
        """PUT custom-fields/{id} — rename + change options + type."""
        p = _make_project(admin_client, name="CF Upd")
        f = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Orig", "field_type": "text",
        }).json()
        r = admin_client.put(
            f"/api/projects/{p['id']}/custom-fields/{f['id']}", json={
                "name": "Renamed", "field_type": "select",
                "options": ["a", "b"], "is_required": True, "position": 5,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Renamed"
        assert body["field_type"] == "select"
        assert body["options"] == ["a", "b"]

    def test_update_field_invalid_type_400(self, admin_client):
        """PUT custom-fields/{id} — invalid field_type in body → 400
        (the in-handler validation, distinct from the create validator)."""
        p = _make_project(admin_client, name="CF Upd Bad")
        f = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "F", "field_type": "text",
        }).json()
        r = admin_client.put(
            f"/api/projects/{p['id']}/custom-fields/{f['id']}",
            json={"field_type": "bogus"},
        )
        assert r.status_code == 400
        assert "invalid field_type" in r.json()["detail"]

    def test_update_field_not_found_404(self, admin_client):
        """PUT custom-fields/{id} — field id not in this project → 404."""
        p = _make_project(admin_client, name="CF Upd Missing")
        r = admin_client.put(
            f"/api/projects/{p['id']}/custom-fields/999999",
            json={"name": "Ghost"},
        )
        assert r.status_code == 404

    def test_update_field_member_forbidden_403(self, admin_client, make_invite):
        """PUT custom-fields/{id} — member can't manage → 403."""
        p = _make_project(admin_client, name="CF Upd Guard")
        f = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Z", "field_type": "text",
        }).json()
        c, _mem = _accept_user(admin_client, make_invite, "cfu@x.test",
                               role="member", project_ids=[p["id"]],
                               name="CF Upd Member")
        try:
            r = c.put(
                f"/api/projects/{p['id']}/custom-fields/{f['id']}",
                json={"name": "Hax"},
            )
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_delete_field(self, admin_client):
        """DELETE custom-fields/{id} — removes the field (204)."""
        p = _make_project(admin_client, name="CF Del")
        f = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Gone", "field_type": "text",
        }).json()
        r = admin_client.delete(f"/api/projects/{p['id']}/custom-fields/{f['id']}")
        assert r.status_code == 204
        rows = admin_client.get(f"/api/projects/{p['id']}/custom-fields").json()
        assert all(x["id"] != f["id"] for x in rows)

    def test_delete_field_not_found_404(self, admin_client):
        """DELETE custom-fields/{id} — wrong id → 404."""
        p = _make_project(admin_client, name="CF Del Missing")
        r = admin_client.delete(f"/api/projects/{p['id']}/custom-fields/999999")
        assert r.status_code == 404

    def test_delete_field_member_forbidden_403(self, admin_client, make_invite):
        """DELETE custom-fields/{id} — member can't manage → 403."""
        p = _make_project(admin_client, name="CF Del Guard")
        f = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Y", "field_type": "text",
        }).json()
        c, _mem = _accept_user(admin_client, make_invite, "cfd@x.test",
                               role="member", project_ids=[p["id"]],
                               name="CF Del Member")
        try:
            r = c.delete(f"/api/projects/{p['id']}/custom-fields/{f['id']}")
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)


class TestCustomFieldValues:
    def test_set_and_list_values(self, admin_client):
        """PUT then GET bugs/{id}/custom-values — bulk-set persists only
        field ids belonging to the bug's project; readback matches."""
        p = _make_project(admin_client, name="CV Proj")
        f1 = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Owner", "field_type": "text",
        }).json()
        f2 = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "Sprint", "field_type": "text",
        }).json()
        bug = _make_bug(admin_client, p["id"])
        # Include a bogus field id that's NOT in the project — it must be
        # filtered out by the project_field_ids guard.
        r = admin_client.put(f"/api/bugs/{bug['id']}/custom-values", json=[
            {"field_id": f1["id"], "value": "Alice"},
            {"field_id": f2["id"], "value": "S1"},
            {"field_id": 999999, "value": "ignored"},
        ])
        assert r.status_code == 200, r.text
        got = {v["field_id"]: v["value"] for v in r.json()}
        assert got == {f1["id"]: "Alice", f2["id"]: "S1"}
        # Readback via GET.
        listed = admin_client.get(f"/api/bugs/{bug['id']}/custom-values").json()
        assert {v["field_id"] for v in listed} == {f1["id"], f2["id"]}

    def test_update_existing_value_and_remove_dropped(self, admin_client):
        """PUT bugs/{id}/custom-values — second call updates an existing
        row (existing[fid] branch) and deletes the row whose field is no
        longer in the payload."""
        p = _make_project(admin_client, name="CV Upd")
        f1 = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "A", "field_type": "text",
        }).json()
        f2 = admin_client.post(f"/api/projects/{p['id']}/custom-fields", json={
            "name": "B", "field_type": "text",
        }).json()
        bug = _make_bug(admin_client, p["id"])
        admin_client.put(f"/api/bugs/{bug['id']}/custom-values", json=[
            {"field_id": f1["id"], "value": "one"},
            {"field_id": f2["id"], "value": "two"},
        ])
        # Now resend with f1 updated and f2 dropped entirely.
        r = admin_client.put(f"/api/bugs/{bug['id']}/custom-values", json=[
            {"field_id": f1["id"], "value": "one-updated"},
        ])
        assert r.status_code == 200, r.text
        got = {v["field_id"]: v["value"] for v in r.json()}
        assert got == {f1["id"]: "one-updated"}

    def test_set_values_bug_not_found_404(self, admin_client):
        """PUT bugs/{id}/custom-values — missing bug → 404."""
        r = admin_client.put("/api/bugs/999999/custom-values", json=[])
        assert r.status_code == 404
        assert r.json()["detail"] == "Bug not found"

    def test_list_values_bug_not_found_404(self, admin_client):
        """GET bugs/{id}/custom-values — missing bug → 404."""
        assert admin_client.get(
            "/api/bugs/999999/custom-values"
        ).status_code == 404

    def test_values_cross_org_404(self, two_orgs):
        """GET/PUT bugs/{id}/custom-values — bug in another org → 404
        (project.org_id != user.org_id branch)."""
        c_a, c_b, _me_a, _me_b = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "B CV"}).json()
        bug_b = c_b.post("/api/bugs", json={
            "project_id": p_b["id"], "title": "B Bug Title",
            "description": "Test description",
        }).json()
        assert c_a.get(f"/api/bugs/{bug_b['id']}/custom-values").status_code == 404
        assert c_a.put(
            f"/api/bugs/{bug_b['id']}/custom-values", json=[]
        ).status_code == 404

    def test_values_no_project_access_404(self, admin_client, make_invite):
        """GET + PUT bugs/{id}/custom-values — same org but caller lacks
        project access → 404 on BOTH (can_access_project False branch in
        list_values AND set_values)."""
        p = _make_project(admin_client, name="CV NoAccess")
        bug = _make_bug(admin_client, p["id"])
        c, _mem = _accept_user(admin_client, make_invite, "cvn@x.test",
                               role="member", name="CV NoAccess Member")
        try:
            assert c.get(
                f"/api/bugs/{bug['id']}/custom-values"
            ).status_code == 404
            # set_values has its own identical access gate (covers the
            # 404 branch in the PUT handler).
            assert c.put(
                f"/api/bugs/{bug['id']}/custom-values", json=[],
            ).status_code == 404
        finally:
            c.__exit__(None, None, None)


# ===========================================================================
# saved_views.py
# ===========================================================================
class TestSavedViews:
    def test_create_and_get_view(self, admin_client):
        """POST + GET /api/saved-views — create a personal view, read back."""
        r = admin_client.post("/api/saved-views", json={
            "name": "My Open Bugs", "filters": {"status": "New"},
        })
        assert r.status_code == 201, r.text
        vid = r.json()["id"]
        assert r.json()["is_mine"] is True
        assert r.json()["shared_with_org"] is False
        got = admin_client.get(f"/api/saved-views/{vid}")
        assert got.status_code == 200
        assert got.json()["filters"] == {"status": "New"}

    def test_admin_can_share_view(self, admin_client):
        """POST /api/saved-views — admin sets shared_with_org=True and the
        can_invite gate allows it (shared stays True)."""
        r = admin_client.post("/api/saved-views", json={
            "name": "Team Queue", "filters": {}, "shared_with_org": True,
        })
        assert r.status_code == 201, r.text
        assert r.json()["shared_with_org"] is True

    def test_member_share_request_downgraded(self, admin_client, make_invite):
        """POST /api/saved-views — a member asking for shared_with_org=True
        is silently downgraded (can_invite False → shared=False)."""
        c, _mem = _accept_user(admin_client, make_invite, "svm@x.test",
                               role="member", name="SV Member")
        try:
            r = c.post("/api/saved-views", json={
                "name": "Wannabe Shared", "filters": {}, "shared_with_org": True,
            })
            assert r.status_code == 201, r.text
            assert r.json()["shared_with_org"] is False
        finally:
            c.__exit__(None, None, None)

    def test_list_views_includes_shared(self, admin_client, make_invite):
        """GET /api/saved-views — a member sees their own views plus the
        org-shared one created by the admin."""
        admin_client.post("/api/saved-views", json={
            "name": "Shared One", "filters": {}, "shared_with_org": True,
        })
        c, _mem = _accept_user(admin_client, make_invite, "svl@x.test",
                               role="member", name="SV List Member")
        try:
            c.post("/api/saved-views", json={"name": "Mine", "filters": {}})
            rows = c.get("/api/saved-views").json()
            names = {v["name"] for v in rows}
            assert {"Shared One", "Mine"}.issubset(names)
        finally:
            c.__exit__(None, None, None)

    def test_update_view_owner(self, admin_client):
        """PUT /api/saved-views/{id} — owner edits name/filters/share."""
        v = admin_client.post("/api/saved-views", json={
            "name": "Editable", "filters": {"a": 1},
        }).json()
        r = admin_client.put(f"/api/saved-views/{v['id']}", json={
            "name": "Edited", "filters": {"b": 2}, "shared_with_org": True,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "Edited"
        assert body["filters"] == {"b": 2}
        assert body["shared_with_org"] is True

    def test_update_view_not_found_404(self, admin_client):
        """PUT /api/saved-views/{id} — missing → 404 (_get_view_or_404)."""
        assert admin_client.put(
            "/api/saved-views/999999", json={"name": "Ghost"}
        ).status_code == 404

    def test_update_view_non_owner_403(self, admin_client, make_invite):
        """PUT /api/saved-views/{id} — a non-owner viewing a SHARED view
        still can't edit it → 403 (owner-only branch)."""
        shared = admin_client.post("/api/saved-views", json={
            "name": "Owned By Admin", "filters": {}, "shared_with_org": True,
        }).json()
        c, _mem = _accept_user(admin_client, make_invite, "svu@x.test",
                               role="member", name="SV Upd Member")
        try:
            # Member can SEE the shared view (visibility ok) but editing 403s.
            r = c.put(f"/api/saved-views/{shared['id']}", json={"name": "Hax"})
            assert r.status_code == 403
            assert "creator" in r.json()["detail"]
        finally:
            c.__exit__(None, None, None)

    def test_get_view_private_other_user_404(self, admin_client, make_invite):
        """GET /api/saved-views/{id} — a private (non-shared) view owned by
        someone else is invisible → 404 (_get_view_or_404 visibility branch)."""
        priv = admin_client.post("/api/saved-views", json={
            "name": "Admin Private", "filters": {},
        }).json()
        c, _mem = _accept_user(admin_client, make_invite, "svp@x.test",
                               role="member", name="SV Priv Member")
        try:
            assert c.get(f"/api/saved-views/{priv['id']}").status_code == 404
        finally:
            c.__exit__(None, None, None)

    def test_get_view_cross_org_404(self, two_orgs):
        """GET /api/saved-views/{id} — view from another org → 404."""
        c_a, c_b, _me_a, _me_b = two_orgs
        v_b = c_b.post("/api/saved-views", json={
            "name": "B View", "filters": {}, "shared_with_org": True,
        }).json()
        assert c_a.get(f"/api/saved-views/{v_b['id']}").status_code == 404

    def test_delete_view_owner(self, admin_client):
        """DELETE /api/saved-views/{id} — owner deletes (204)."""
        v = admin_client.post("/api/saved-views", json={
            "name": "Deletable", "filters": {},
        }).json()
        assert admin_client.delete(f"/api/saved-views/{v['id']}").status_code == 204
        assert admin_client.get(f"/api/saved-views/{v['id']}").status_code == 404

    def test_delete_view_non_owner_403(self, admin_client, make_invite):
        """DELETE /api/saved-views/{id} — non-owner of a shared view → 403."""
        shared = admin_client.post("/api/saved-views", json={
            "name": "Shared To Delete", "filters": {}, "shared_with_org": True,
        }).json()
        c, _mem = _accept_user(admin_client, make_invite, "svd@x.test",
                               role="member", name="SV Del Member")
        try:
            r = c.delete(f"/api/saved-views/{shared['id']}")
            assert r.status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_delete_view_not_found_404(self, admin_client):
        """DELETE /api/saved-views/{id} — missing → 404 (_get_view_or_404
        fires before the owner check / 204 path)."""
        assert admin_client.delete("/api/saved-views/999999").status_code == 404

    def test_update_view_name_only_keeps_filters(self, admin_client):
        """PUT /api/saved-views/{id} — partial update (name only) leaves
        filters untouched (exercises the per-field `if in fields` branches
        independently)."""
        v = admin_client.post("/api/saved-views", json={
            "name": "Partial", "filters": {"keep": True},
        }).json()
        r = admin_client.put(f"/api/saved-views/{v['id']}", json={"name": "Renamed"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Renamed"
        assert r.json()["filters"] == {"keep": True}

    def test_update_view_filters_only_keeps_name(self, admin_client):
        """PUT /api/saved-views/{id} — filters-only update takes the
        `name not in fields` path (the false arm of the name branch) while
        replacing filters_json."""
        v = admin_client.post("/api/saved-views", json={
            "name": "KeepName", "filters": {"old": 1},
        }).json()
        r = admin_client.put(f"/api/saved-views/{v['id']}", json={
            "filters": {"new": 2},
        })
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "KeepName"
        assert r.json()["filters"] == {"new": 2}

    def test_get_view_with_corrupt_filters_json_falls_back(self, admin_client, db_path):
        """SavedViewOut.from_row — a row whose filters_json is not valid
        JSON (corrupted at rest) must not 500: the JSONDecodeError fallback
        yields an empty filters dict."""
        v = admin_client.post("/api/saved-views", json={
            "name": "Corrupt", "filters": {"x": 1},
        }).json()
        from sqlalchemy import create_engine, text
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE saved_views SET filters_json = :j WHERE id = :i"),
                {"j": "{not valid json", "i": v["id"]},
            )
        engine.dispose()
        r = admin_client.get(f"/api/saved-views/{v['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["filters"] == {}


# ===========================================================================
# sessions.py
# ===========================================================================
class TestSessionsCoverage:
    def test_list_sessions_marks_current(self, admin_client):
        """GET /api/sessions — admin's own session flagged is_current=True;
        exercises _is_current True branch + user_map population."""
        rows = admin_client.get("/api/sessions").json()
        assert any(r["is_current"] for r in rows)
        mine = next(r for r in rows if r["is_current"])
        assert mine["user_email"] == "admin@acme.test"

    def test_list_sessions_member_forbidden_403(self, admin_client, make_invite):
        """GET /api/sessions — require_admin blocks a member → 403."""
        c, _mem = _accept_user(admin_client, make_invite, "se@x.test",
                               role="member", name="Sess Member")
        try:
            assert c.get("/api/sessions").status_code == 403
        finally:
            c.__exit__(None, None, None)

    def test_list_sessions_sweeps_expired(self, admin_client, db_path):
        """GET /api/sessions — a row with expires_at < now is swept by
        _sweep_expired_sessions and absent from the response."""
        from sqlalchemy import create_engine, text
        from datetime import datetime, timezone, timedelta
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            uid = conn.execute(
                text("SELECT id FROM users WHERE email = :e"),
                {"e": "admin@acme.test"},
            ).scalar()
            past = datetime.now(timezone.utc) - timedelta(days=2)
            conn.execute(
                text(
                    "INSERT INTO sessions (user_id, jti, ip_address, "
                    "user_agent, created_at, last_seen_at, expires_at) "
                    "VALUES (:u, :j, :ip, :ua, :c, :l, :e)"
                ),
                {"u": uid, "j": "swept-jti", "ip": "9.9.9.9",
                 "ua": "ExpiredAgent/9", "c": past, "l": past, "e": past},
            )
        engine.dispose()
        rows = admin_client.get("/api/sessions").json()
        assert all(r["user_agent"] != "ExpiredAgent/9" for r in rows)

    def test_revoke_other_session(self, admin_client, make_invite):
        """DELETE /api/sessions/{id} — admin revokes another user's session
        (the happy path: not current, same org)."""
        c, _mem = _accept_user(admin_client, make_invite, "sr@x.test",
                               role="member", name="Sess Revoke Member")
        try:
            assert c.get("/api/auth/me").status_code == 200
            sessions = admin_client.get("/api/sessions").json()
            target = next(s for s in sessions if s["user_email"] == "sr@x.test")
            r = admin_client.delete(f"/api/sessions/{target['id']}")
            assert r.status_code == 200
            assert r.json()["message"] == "Session revoked"
            # Their cookie no longer resolves.
            assert c.get("/api/auth/me").status_code == 401
        finally:
            c.__exit__(None, None, None)

    def test_revoke_own_current_session_400(self, admin_client):
        """DELETE /api/sessions/{id} — revoking your own current session is
        blocked → 400 (_is_current True guard)."""
        sessions = admin_client.get("/api/sessions").json()
        mine = next(s for s in sessions if s["is_current"])
        r = admin_client.delete(f"/api/sessions/{mine['id']}")
        assert r.status_code == 400
        assert "Log out" in r.json()["detail"]

    def test_revoke_missing_session_404(self, admin_client):
        """DELETE /api/sessions/{id} — no such row → 404."""
        assert admin_client.delete("/api/sessions/999999").status_code == 404

    def test_revoke_cross_org_session_404(self, two_orgs):
        """DELETE /api/sessions/{id} — target user belongs to another org →
        404 (target.org_id != actor.org_id branch)."""
        c_a, c_b, _me_a, _me_b = two_orgs
        sessions_b = c_b.get("/api/sessions").json()
        assert sessions_b
        bob_sess = sessions_b[0]
        assert c_a.delete(f"/api/sessions/{bob_sess['id']}").status_code == 404

    def test_revoke_session_user_deleted_404(self, admin_client, make_invite, db_path):
        """DELETE /api/sessions/{id} — session row exists but its user row
        is gone → target is None → 404 (the `target is None` arm of the
        org-isolation guard)."""
        c, mem = _accept_user(admin_client, make_invite, "sx@x.test",
                              role="member", name="Sess Gone Member")
        try:
            assert c.get("/api/auth/me").status_code == 200
            sessions = admin_client.get("/api/sessions").json()
            target = next(s for s in sessions if s["user_email"] == "sx@x.test")
            sess_id = target["id"]
        finally:
            c.__exit__(None, None, None)
        # Orphan the session: delete the user row directly, leaving the
        # session row pointing at a now-missing user_id.
        from sqlalchemy import create_engine, text
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM users WHERE id = :i"), {"i": mem["id"]},
            )
        engine.dispose()
        r = admin_client.delete(f"/api/sessions/{sess_id}")
        assert r.status_code == 404
