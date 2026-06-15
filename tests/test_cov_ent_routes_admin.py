"""Coverage-driven tests for the org/admin route surface.

Targets every if/elif/else, permission gate, not-found guard and
validation branch in:

  - app/routes/invitations.py  (create / list / revoke / preview / accept,
                                project scoping, as_lead, lifecycle guards,
                                email-collision + member branches)
  - app/routes/dsar.py         (data-export + account-deletion, last-admin
                                guard, wrong-password guard)
  - app/routes/audit.py        (list + CSV, every filter, admin-only gate,
                                pagination)
  - app/routes/users.py        (list/create/get/update/delete, last-admin
                                guard, self-edit guard, not-found, perms)
  - app/routes/memberships.py  (add/update/remove edge branches)
  - app/routes/organizations.py(rename + no-change + not-found)
  - app/routes/branding.py     (colour/logo/email validation, not-found)

Conventions: each test docstring names the endpoint + branch it pins.
Deterministic; the only external I/O (invitation email) is already a
no-op under EMAIL_BACKEND=disabled. Token/secret values are obviously
fake one-tuples re-stamped via the conftest DB helper.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

PASS = "TestPass1!"


# ---------------------------------------------------------------------------
# Small helpers shared across the suite.
# ---------------------------------------------------------------------------
def _signup(client, org="Acme Co", name="Admin", email="admin@acme.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _accept(token, name="Member", password=PASS):
    """Accept an invite on a brand-new client and return (client, me)."""
    from app.main import app
    c = TestClient(app)
    c.__enter__()
    r = c.post("/api/invitations/accept", json={
        "token": token, "name": name, "password": password,
    })
    assert r.status_code == 200, r.text
    return c, r.json()


def _second_user(admin_client, db_path, email, role="member",
                 name="Second User", project_ids=None, as_lead=False):
    """Create a real second user in the admin's org and return their /me.

    Re-implements the make_invite re-stamp dance inline so we can pick the
    role + project scoping per test.
    """
    from app.auth import generate_random_token
    r = admin_client.post("/api/invitations", json={
        "email": email, "role": role,
        "project_ids": project_ids or [], "as_lead": as_lead,
    })
    assert r.status_code == 201, r.text
    inv_id = r.json()["id"]
    raw, h = generate_random_token()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv_id))
    conn.commit()
    conn.close()
    _c, me = _accept(raw, name=name)
    return me, raw


def _invite_and_accept(admin_client, db_path, email, role="member",
                       name="Invitee", project_ids=None, as_lead=False):
    """Like _second_user but also hands back the new user's authed client."""
    from app.auth import generate_random_token
    r = admin_client.post("/api/invitations", json={
        "email": email, "role": role,
        "project_ids": project_ids or [], "as_lead": as_lead,
    })
    assert r.status_code == 201, r.text
    inv_id = r.json()["id"]
    raw, h = generate_random_token()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv_id))
    conn.commit()
    conn.close()
    client, me = _accept(raw, name=name)
    return client, me


def _expire_invitation(db_path, email):
    """Push an invitation's expires_at into the past (lifecycle guard)."""
    conn = sqlite3.connect(str(db_path))
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute("UPDATE invitations SET expires_at=? WHERE email=?", (past, email))
    conn.commit()
    conn.close()


# ===========================================================================
# app/routes/invitations.py
# ===========================================================================
class TestInvitationCreate:
    def test_member_cannot_invite_403(self, admin_client, db_path):
        """POST /api/invitations — can_invite False → 403."""
        member_client, _ = self_member(admin_client, db_path)
        resp = member_client.post("/api/invitations", json={
            "email": "x@acme.test", "role": "member",
        })
        assert resp.status_code == 403

    def test_manager_cannot_invite_as_admin_403(self, admin_client, db_path):
        """POST /api/invitations — non-admin inviting role=admin → 403."""
        from app.auth import generate_random_token
        r = admin_client.post("/api/invitations", json={
            "email": "mgr@acme.test", "role": "manager",
        })
        inv_id = r.json()["id"]
        raw, h = generate_random_token()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv_id))
        conn.commit()
        conn.close()
        mgr_client, _ = _accept(raw, name="Manager Mike")
        resp = mgr_client.post("/api/invitations", json={
            "email": "evil@acme.test", "role": "admin",
        })
        assert resp.status_code == 403

    def test_invalid_role_422(self, admin_client):
        """POST /api/invitations — bogus role rejected by schema → 422."""
        r = admin_client.post("/api/invitations", json={
            "email": "who@acme.test", "role": "wizard",
        })
        assert r.status_code == 422

    def test_duplicate_email_same_org_409(self, admin_client):
        """POST /api/invitations — email already a member of MY org → 409."""
        r = admin_client.post("/api/invitations", json={
            "email": admin_client.admin_me["email"], "role": "member",
        })
        assert r.status_code == 409
        assert "member of your organization" in r.json()["detail"]

    def test_duplicate_email_other_org_409(self, two_orgs):
        """POST /api/invitations — email registered in ANOTHER org → 409."""
        c_a, _c_b, _, _ = two_orgs
        r = c_a.post("/api/invitations", json={
            "email": "bob@b.test", "role": "member",
        })
        assert r.status_code == 409
        assert "another organization" in r.json()["detail"]

    def test_unknown_project_id_400(self, admin_client):
        """POST /api/invitations — project_id not in org → 400."""
        r = admin_client.post("/api/invitations", json={
            "email": "p@acme.test", "role": "member", "project_ids": [99999],
        })
        assert r.status_code == 400
        assert "Unknown project id" in r.json()["detail"]

    def test_cross_org_project_id_400(self, two_orgs):
        """POST /api/invitations — project belongs to other org → 400."""
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "BProj", "color": "#000000"}).json()
        r = c_a.post("/api/invitations", json={
            "email": "p@a.test", "role": "member", "project_ids": [p_b["id"]],
        })
        assert r.status_code == 400

    def test_manager_cannot_attach_unled_project_403(self, admin_client, db_path):
        """POST /api/invitations — manager attaching a project they don't
        lead → 403 (the non-admin lead check)."""
        # Admin makes a project the manager will NOT lead.
        p = admin_client.post("/api/projects", json={"name": "AdminProj", "color": "#000000"}).json()
        from app.auth import generate_random_token
        r = admin_client.post("/api/invitations", json={
            "email": "mgr2@acme.test", "role": "manager",
        })
        inv_id = r.json()["id"]
        raw, h = generate_random_token()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv_id))
        conn.commit()
        conn.close()
        mgr_client, _ = _accept(raw, name="Manager Two")
        resp = mgr_client.post("/api/invitations", json={
            "email": "victim@acme.test", "role": "member",
            "project_ids": [p["id"]],
        })
        assert resp.status_code == 403
        assert "not a lead" in resp.json()["detail"]

    def test_admin_attaches_project_and_as_lead(self, admin_client, db_path):
        """POST /api/invitations — admin attaches a project with as_lead=True
        → initial_project_ids uses the L: marker; accept seeds a lead row."""
        p = admin_client.post("/api/projects", json={"name": "LeadProj", "color": "#000000"}).json()
        r = admin_client.post("/api/invitations", json={
            "email": "newlead@acme.test", "role": "member",
            "project_ids": [p["id"]], "as_lead": True,
        })
        assert r.status_code == 201
        assert r.json()["initial_project_ids"] == f"L:{p['id']}"
        # Now accept and confirm the membership row is a lead.
        from app.auth import generate_random_token
        raw, h = generate_random_token()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, r.json()["id"]))
        conn.commit()
        conn.close()
        _c, me = _accept(raw, name="New Lead")
        members = admin_client.get(f"/api/projects/{p['id']}/members").json()
        roles = {m["user_id"]: m["project_role"] for m in members}
        assert roles.get(me["id"]) == "lead"

    def test_manager_can_attach_led_project(self, admin_client, db_path):
        """POST /api/invitations — a manager who LEADS the project may attach
        it (the non-admin lead-check pass branch, pm is not None)."""
        p = admin_client.post("/api/projects", json={"name": "MgrLed", "color": "#000000"}).json()
        # Invite the manager as a lead of that project, then accept.
        mgr_client, _mgr = _invite_and_accept(
            admin_client, db_path, "leadmgr@acme.test", role="manager",
            name="Lead Manager", project_ids=[p["id"]], as_lead=True,
        )
        resp = mgr_client.post("/api/invitations", json={
            "email": "recruit@acme.test", "role": "member",
            "project_ids": [p["id"]],
        })
        assert resp.status_code == 201
        assert resp.json()["initial_project_ids"] == str(p["id"])

    def test_reinvite_revokes_prior_pending(self, admin_client, db_path):
        """POST /api/invitations — a second invite to the same email revokes
        the earlier still-pending one (the existing_pending loop)."""
        first = admin_client.post("/api/invitations", json={
            "email": "dup@acme.test", "role": "member",
        }).json()
        second = admin_client.post("/api/invitations", json={
            "email": "dup@acme.test", "role": "member",
        }).json()
        rows = admin_client.get("/api/invitations").json()
        by_id = {i["id"]: i for i in rows}
        assert by_id[first["id"]]["revoked_at"] is not None
        assert by_id[second["id"]]["revoked_at"] is None


class TestInvitationListRevoke:
    def test_list_returns_org_invites(self, admin_client):
        """GET /api/invitations — admin sees their org's invites."""
        admin_client.post("/api/invitations", json={"email": "a@acme.test", "role": "member"})
        rows = admin_client.get("/api/invitations").json()
        assert any(i["email"] == "a@acme.test" for i in rows)

    def test_list_member_forbidden_403(self, admin_client, db_path):
        """GET /api/invitations — member can't view invites → 403."""
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.get("/api/invitations").status_code == 403

    def test_revoke_not_found_404(self, admin_client):
        """DELETE /api/invitations/{id} — unknown id → 404."""
        assert admin_client.delete("/api/invitations/99999").status_code == 404

    def test_revoke_cross_org_404(self, two_orgs):
        """DELETE /api/invitations/{id} — other org's invite → 404."""
        c_a, c_b, _, _ = two_orgs
        inv = c_b.post("/api/invitations", json={"email": "z@b.test", "role": "member"}).json()
        assert c_a.delete(f"/api/invitations/{inv['id']}").status_code == 404

    def test_revoke_member_forbidden_403(self, admin_client, db_path):
        """DELETE /api/invitations/{id} — member lacks can_invite → 403."""
        inv = admin_client.post("/api/invitations", json={
            "email": "target@acme.test", "role": "member",
        }).json()
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.delete(f"/api/invitations/{inv['id']}").status_code == 403

    def test_revoke_already_accepted_400(self, admin_client, db_path):
        """DELETE /api/invitations/{id} — already-accepted invite → 400."""
        from app.auth import generate_random_token
        r = admin_client.post("/api/invitations", json={
            "email": "acc@acme.test", "role": "member",
        })
        inv_id = r.json()["id"]
        raw, h = generate_random_token()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv_id))
        conn.commit()
        conn.close()
        _accept(raw, name="Acceptor")
        assert admin_client.delete(f"/api/invitations/{inv_id}").status_code == 400

    def test_revoke_then_revoke_again_idempotent(self, admin_client):
        """DELETE /api/invitations/{id} — twice → second returns 'Already
        revoked' (revoked_at-not-None branch)."""
        inv = admin_client.post("/api/invitations", json={
            "email": "rev@acme.test", "role": "member",
        }).json()
        assert admin_client.delete(f"/api/invitations/{inv['id']}").status_code == 200
        r2 = admin_client.delete(f"/api/invitations/{inv['id']}")
        assert r2.status_code == 200
        assert r2.json()["message"] == "Already revoked"


class TestInvitationPreview:
    def test_preview_unknown_token_404(self, admin_client):
        """GET /api/invitations/preview/{token} — bad token → 404."""
        assert admin_client.get("/api/invitations/preview/nope-not-real").status_code == 404

    def test_preview_revoked_400(self, admin_client, make_invite):
        """GET /api/invitations/preview/{token} — revoked invite → 400."""
        tok = make_invite(admin_client, "prev-rev@acme.test")
        # Revoke via DB.
        # Token was re-stamped; find its row by email and revoke it.
        from app.database import SessionLocal
        from app.models import Invitation
        db = SessionLocal()
        try:
            inv = db.query(Invitation).filter(Invitation.email == "prev-rev@acme.test").one()
            inv.revoked_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
        r = admin_client.get(f"/api/invitations/preview/{tok}")
        assert r.status_code == 400
        assert "revoked" in r.json()["detail"].lower()

    def test_preview_expired_400(self, admin_client, make_invite, db_path):
        """GET /api/invitations/preview/{token} — expired invite → 400."""
        tok = make_invite(admin_client, "prev-exp@acme.test")
        _expire_invitation(db_path, "prev-exp@acme.test")
        r = admin_client.get(f"/api/invitations/preview/{tok}")
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    def test_preview_used_400(self, admin_client, make_invite):
        """GET /api/invitations/preview/{token} — already-accepted → 400."""
        tok = make_invite(admin_client, "prev-used@acme.test")
        _accept(tok, name="Used Already")
        r = admin_client.get(f"/api/invitations/preview/{tok}")
        assert r.status_code == 400
        assert "already been used" in r.json()["detail"].lower()

    def test_preview_valid_returns_org_metadata(self, admin_client, make_invite):
        """GET /api/invitations/preview/{token} — happy path returns the org
        name + invited-by (the org lookup + success return)."""
        tok = make_invite(admin_client, "prev-ok@acme.test", role="manager")
        r = admin_client.get(f"/api/invitations/preview/{tok}")
        assert r.status_code == 200
        body = r.json()
        assert body["organization_name"] == "Acme Co"
        assert body["email"] == "prev-ok@acme.test"
        assert body["role"] == "manager"
        assert body["invited_by_name"] == "Admin"


class TestInvitationAccept:
    def test_accept_valid_creates_user(self, admin_client, make_invite):
        """POST /api/invitations/accept — valid token → user + session."""
        tok = make_invite(admin_client, "ok@acme.test", role="member")
        c, me = _accept(tok, name="Okay Person")
        assert me["role"] == "member"
        assert c.get("/api/auth/me").status_code == 200

    def test_accept_unknown_token_400(self, admin_client):
        """POST /api/invitations/accept — token not in DB → 400 (load guard)."""
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/invitations/accept", json={
                "token": "totally-fake-token", "name": "Nobody", "password": PASS,
            })
            assert r.status_code == 400

    def test_accept_expired_token_400(self, admin_client, make_invite, db_path):
        """POST /api/invitations/accept — expired invite → 400."""
        tok = make_invite(admin_client, "acc-exp@acme.test")
        _expire_invitation(db_path, "acc-exp@acme.test")
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/invitations/accept", json={
                "token": tok, "name": "Late Comer", "password": PASS,
            })
            assert r.status_code == 400
            assert "expired" in r.json()["detail"].lower()

    def test_accept_revoked_token_400(self, admin_client, make_invite):
        """POST /api/invitations/accept — revoked invite → 400."""
        tok = make_invite(admin_client, "acc-rev@acme.test")
        from app.database import SessionLocal
        from app.models import Invitation
        db = SessionLocal()
        try:
            inv = db.query(Invitation).filter(Invitation.email == "acc-rev@acme.test").one()
            inv.revoked_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/invitations/accept", json={
                "token": tok, "name": "Revoked One", "password": PASS,
            })
            assert r.status_code == 400
            assert "revoked" in r.json()["detail"].lower()

    def test_accept_already_used_400(self, admin_client, make_invite):
        """POST /api/invitations/accept — re-accept used token → 400."""
        tok = make_invite(admin_client, "acc-used@acme.test")
        _accept(tok, name="First Use")
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/invitations/accept", json={
                "token": tok, "name": "Second Use", "password": PASS,
            })
            assert r.status_code == 400

    def test_accept_email_collision_409(self, admin_client, make_invite, db_path):
        """POST /api/invitations/accept — email got registered elsewhere
        between send + accept → 409 (the User-exists collision check)."""
        tok = make_invite(admin_client, "collide@acme.test")
        # Simulate a separate signup grabbing that email in a new org.
        from app.main import app
        with TestClient(app) as other:
            other.post("/api/auth/signup", json={
                "organization_name": "Other Inc", "name": "Collider",
                "email": "collide@acme.test", "password": PASS,
            })
        with TestClient(app) as c:
            r = c.post("/api/invitations/accept", json={
                "token": tok, "name": "Too Late", "password": PASS,
            })
            assert r.status_code == 409

    def test_accept_seeds_project_membership(self, admin_client, make_invite):
        """POST /api/invitations/accept — plain project id seeds a member
        ProjectMembership (the non-lead seed branch)."""
        p = admin_client.post("/api/projects", json={"name": "SeedProj", "color": "#000000"}).json()
        tok = make_invite(admin_client, "seed@acme.test", role="member", project_ids=[p["id"]])
        _c, me = _accept(tok, name="Seeded Member")
        members = admin_client.get(f"/api/projects/{p['id']}/members").json()
        roles = {m["user_id"]: m["project_role"] for m in members}
        assert roles.get(me["id"]) == "member"

    def test_accept_skips_deleted_project(self, admin_client, make_invite):
        """POST /api/invitations/accept — project deleted between invite and
        accept is silently skipped (the p is None / cross-org seed branch)."""
        p = admin_client.post("/api/projects", json={"name": "Doomed", "color": "#000000"}).json()
        tok = make_invite(admin_client, "ghost@acme.test", role="member", project_ids=[p["id"]])
        # Delete the project before acceptance.
        assert admin_client.delete(f"/api/projects/{p['id']}").status_code in (200, 204)
        _c, me = _accept(tok, name="Ghost Member")
        # User is created fine, just with no membership.
        assert me["role"] == "member"

    def test_accept_skips_malformed_project_entry(self, admin_client, db_path):
        """POST /api/invitations/accept — a malformed initial_project_ids
        entry (non-numeric) is skipped (the _parse ValueError/empty branch)."""
        from app.auth import generate_random_token
        r = admin_client.post("/api/invitations", json={
            "email": "malformed@acme.test", "role": "member",
        })
        inv_id = r.json()["id"]
        raw, h = generate_random_token()
        conn = sqlite3.connect(str(db_path))
        # Stuff garbage + an empty entry into initial_project_ids.
        conn.execute(
            "UPDATE invitations SET token_hash=?, initial_project_ids=? WHERE id=?",
            (h, "not-a-number,,L:also-bad", inv_id),
        )
        conn.commit()
        conn.close()
        _c, me = _accept(raw, name="Malformed Seed")
        assert me["role"] == "member"


def self_member(admin_client, db_path):
    """Convenience: spin up an authed plain member in the admin's org."""
    from app.auth import generate_random_token
    import uuid
    email = f"m-{uuid.uuid4().hex[:8]}@acme.test"
    r = admin_client.post("/api/invitations", json={
        "email": email, "role": "member",
    })
    inv_id = r.json()["id"]
    raw, h = generate_random_token()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv_id))
    conn.commit()
    conn.close()
    return _accept(raw, name="Plain Member")


# ===========================================================================
# app/routes/dsar.py
# ===========================================================================
class TestDsarExport:
    def test_export_returns_user_payload(self, admin_client):
        """GET /api/auth/data-export — returns the caller's bundle including a
        reported bug (exercises the _bug_dict serialiser)."""
        p = admin_client.post("/api/projects", json={"name": "ExpProj", "color": "#000000"}).json()
        bug = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Exported Bug", "description": "desc here",
        }).json()
        r = admin_client.get("/api/auth/data-export")
        assert r.status_code == 200
        body = r.json()
        assert body["user"]["email"] == admin_client.admin_me["email"]
        assert body["organization"]["id"] == admin_client.admin_me["org_id"]
        assert any(b["id"] == bug["id"] for b in body["bugs_reported"])

    def test_export_empty_collections(self, admin_client):
        """GET /api/auth/data-export — a brand-new admin with no data still
        gets well-formed empty lists."""
        r = admin_client.get("/api/auth/data-export")
        assert r.status_code == 200
        body = r.json()
        assert body["bugs_reported"] == []
        assert body["saved_views"] == []

    def test_export_unauthenticated_401(self, client):
        """GET /api/auth/data-export — no session → 401."""
        assert client.get("/api/auth/data-export").status_code == 401


class TestDsarDelete:
    def test_delete_wrong_password_400(self, admin_client):
        """DELETE /api/auth/account — wrong password → 400."""
        r = admin_client.request("DELETE", "/api/auth/account", json={"password": "wrong-one"})
        assert r.status_code == 400

    def test_delete_last_admin_blocked_409(self, admin_client):
        """DELETE /api/auth/account — sole admin can't self-delete → 409."""
        r = admin_client.request("DELETE", "/api/auth/account", json={"password": PASS})
        assert r.status_code == 409
        assert "last admin" in r.json()["detail"].lower()

    def test_delete_non_admin_succeeds_204(self, admin_client, db_path):
        """DELETE /api/auth/account — a plain member deletes themselves →
        204, and the audit row + cookie-clear path run."""
        member_client, _ = self_member(admin_client, db_path)
        r = member_client.request("DELETE", "/api/auth/account", json={"password": PASS})
        assert r.status_code == 204
        # Session is cleared — a follow-up call is unauthenticated.
        assert member_client.get("/api/auth/me").status_code == 401

    def test_delete_admin_with_other_admin_succeeds(self, admin_client, db_path):
        """DELETE /api/auth/account — admin deletes self when ANOTHER active
        admin exists → 204 (the other_admins > 0 branch)."""
        # Promote a second user to admin first.
        _second_user(admin_client, db_path, "co-admin@acme.test", role="admin",
                     name="Co Admin")
        # The original admin can now delete themselves.
        r = admin_client.request("DELETE", "/api/auth/account", json={"password": PASS})
        assert r.status_code == 204


# ===========================================================================
# app/routes/audit.py
# ===========================================================================
class TestAudit:
    def test_list_admin_ok(self, admin_client):
        """GET /api/audit — admin sees rows."""
        admin_client.post("/api/projects", json={"name": "AuditProj", "color": "#000000"})
        rows = admin_client.get("/api/audit").json()
        assert isinstance(rows, list)
        assert rows

    def test_list_member_forbidden_403(self, admin_client, db_path):
        """GET /api/audit — member lacks can_view_audit → 403."""
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.get("/api/audit").status_code == 403

    def test_filter_by_entity_type(self, admin_client):
        """GET /api/audit?entity_type= — entity_type filter clause."""
        admin_client.post("/api/projects", json={"name": "EntProj", "color": "#000000"})
        rows = admin_client.get("/api/audit?entity_type=project").json()
        assert rows
        assert all(r["entity_type"] == "project" for r in rows)
        # A type with no rows yields empty.
        assert admin_client.get("/api/audit?entity_type=nonesuch").json() == []

    def test_filter_by_actor_user_id(self, admin_client):
        """GET /api/audit?actor_user_id= — actor filter clause."""
        admin_client.post("/api/projects", json={"name": "ActProj", "color": "#000000"})
        uid = admin_client.admin_me["id"]
        rows = admin_client.get(f"/api/audit?actor_user_id={uid}").json()
        assert rows
        assert all(r["actor_user_id"] == uid for r in rows)

    def test_filter_by_text_query(self, admin_client):
        """GET /api/audit?q= — free-text q OR-clause (action/detail/name)."""
        admin_client.post("/api/projects", json={"name": "Findable", "color": "#000000"})
        rows = admin_client.get("/api/audit?q=Findable").json()
        assert any("Findable" in (r.get("detail") or "") for r in rows)

    def test_filter_by_numeric_query(self, admin_client):
        """GET /api/audit?q=#<id> — numeric branch matches entity_id."""
        p = admin_client.post("/api/projects", json={"name": "NumProj", "color": "#000000"}).json()
        bug = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Numbered Bug", "description": "desc here",
        }).json()
        # Search by "#<bug id>" exercises the digits_match path.
        rows = admin_client.get(f"/api/audit?q=%23{bug['id']}").json()
        assert isinstance(rows, list)
        assert rows

    def test_pagination_limit_offset(self, admin_client):
        """GET /api/audit?limit=&offset= — pagination clauses."""
        for n in ("P1", "P2", "P3"):
            admin_client.post("/api/projects", json={"name": n, "color": "#000000"})
        page = admin_client.get("/api/audit?limit=1&offset=1").json()
        assert len(page) == 1

    def test_csv_export_admin(self, admin_client):
        """GET /api/audit/export.csv — admin downloads CSV with filters."""
        admin_client.post("/api/projects", json={"name": "CsvProj", "color": "#000000"})
        r = admin_client.get("/api/audit/export.csv?q=CsvProj")
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert "attachment" in r.headers.get("content-disposition", "")
        assert "action" in r.text.splitlines()[0]

    def test_csv_export_member_forbidden_403(self, admin_client, db_path):
        """GET /api/audit/export.csv — member → 403."""
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.get("/api/audit/export.csv").status_code == 403


# ===========================================================================
# app/routes/users.py
# ===========================================================================
class TestUsersList:
    def test_list_all_and_filtered(self, admin_client, db_path):
        """GET /api/users — list incl. inactive, then q-filter + active-only."""
        _second_user(admin_client, db_path, "alpha@acme.test", name="Alpha Person")
        # Plain list returns at least the two users.
        everyone = admin_client.get("/api/users").json()
        assert len(everyone) >= 2
        # Text search narrows it.
        filtered = admin_client.get("/api/users?q=alpha").json()
        assert any(u["email"] == "alpha@acme.test" for u in filtered)
        # include_inactive=false still returns active users (the where clause).
        active = admin_client.get("/api/users?include_inactive=false").json()
        assert all(u["is_active"] for u in active)


class TestUsersCreate:
    def test_create_member_forbidden_403(self, admin_client, db_path):
        """POST /api/users — non-admin can't create → 403."""
        member_client, _ = self_member(admin_client, db_path)
        r = member_client.post("/api/users", json={
            "name": "New Guy", "email": "ng@acme.test", "role": "member", "password": PASS,
        })
        assert r.status_code == 403

    def test_create_user_ok(self, admin_client):
        """POST /api/users — admin pre-provisions a user → 201."""
        r = admin_client.post("/api/users", json={
            "name": "Provisioned", "email": "prov@acme.test", "role": "member", "password": PASS,
        })
        assert r.status_code == 201
        assert r.json()["email"] == "prov@acme.test"

    def test_create_duplicate_email_409(self, admin_client):
        """POST /api/users — duplicate email → IntegrityError → 409."""
        admin_client.post("/api/users", json={
            "name": "Dup One", "email": "dupe@acme.test", "role": "member", "password": PASS,
        })
        r = admin_client.post("/api/users", json={
            "name": "Dup Two", "email": "dupe@acme.test", "role": "member", "password": PASS,
        })
        assert r.status_code == 409

    def test_create_breached_password_400(self, admin_client, monkeypatch):
        """POST /api/users — initial password flagged by HIBP → 400.

        The suite disables the real breach check, so we force the helper to
        report a hit to exercise the _reject_if_breached raise."""
        import app.routes.users as users_route
        monkeypatch.setattr(users_route, "is_password_breached", lambda _p: True)
        r = admin_client.post("/api/users", json={
            "name": "Pwned User", "email": "pwned@acme.test",
            "role": "member", "password": PASS,
        })
        assert r.status_code == 400
        assert "breach" in r.json()["detail"].lower()


class TestUsersGet:
    def test_get_user_ok(self, admin_client):
        """GET /api/users/{id} — same-org user → 200."""
        uid = admin_client.admin_me["id"]
        r = admin_client.get(f"/api/users/{uid}")
        assert r.status_code == 200
        assert r.json()["id"] == uid

    def test_get_user_not_found_404(self, admin_client):
        """GET /api/users/{id} — unknown id → 404."""
        assert admin_client.get("/api/users/99999").status_code == 404

    def test_get_user_cross_org_404(self, two_orgs):
        """GET /api/users/{id} — other org's user → 404."""
        c_a, _c_b, _, me_b = two_orgs
        assert c_a.get(f"/api/users/{me_b['id']}").status_code == 404


class TestUsersUpdate:
    def test_update_member_forbidden_403(self, admin_client, db_path):
        """PUT /api/users/{id} — non-admin → 403."""
        member_client, me = self_member(admin_client, db_path)
        r = member_client.put(f"/api/users/{me['id']}", json={"name": "Renamed"})
        assert r.status_code == 403

    def test_update_not_found_404(self, admin_client):
        """PUT /api/users/{id} — unknown id → 404."""
        assert admin_client.put("/api/users/99999", json={"name": "Nobody"}).status_code == 404

    def test_update_name_changes_ok(self, admin_client, db_path):
        """PUT /api/users/{id} — change a field → audit + 200."""
        other, _ = _second_user(admin_client, db_path, "rename@acme.test", name="Before Name")
        r = admin_client.put(f"/api/users/{other['id']}", json={"name": "After Name"})
        assert r.status_code == 200
        assert r.json()["name"] == "After Name"

    def test_update_no_op_when_unchanged(self, admin_client, db_path):
        """PUT /api/users/{id} — same values → no changes recorded, still 200."""
        other, _ = _second_user(admin_client, db_path, "same@acme.test", name="Same Name")
        r = admin_client.put(f"/api/users/{other['id']}", json={"name": "Same Name"})
        assert r.status_code == 200

    def test_update_email_collision_409(self, admin_client, db_path):
        """PUT /api/users/{id} — changing a user's email to one already taken
        → IntegrityError → 409 (the update flush except branch)."""
        _second_user(admin_client, db_path, "taken@acme.test", name="Taken Email")
        b, _ = _second_user(admin_client, db_path, "mover@acme.test", name="Mover Email")
        r = admin_client.put(f"/api/users/{b['id']}", json={"email": "taken@acme.test"})
        assert r.status_code == 409
        assert "already exists" in r.json()["detail"].lower()

    def test_update_password_reset_by_admin(self, admin_client, db_path):
        """PUT /api/users/{id} — admin sets a new password (the password
        reset branch: kicks sessions + revokes reset tokens)."""
        other, _ = _second_user(admin_client, db_path, "pwreset@acme.test", name="Pw Reset")
        r = admin_client.put(f"/api/users/{other['id']}", json={"password": "BrandNew1!"})
        assert r.status_code == 200

    def test_update_deactivate_other_kicks_sessions(self, admin_client, db_path):
        """PUT /api/users/{id} — deactivating an active user flips
        session_version (the is_active False branch)."""
        other, _ = _second_user(admin_client, db_path, "deact@acme.test", name="Deact Me")
        r = admin_client.put(f"/api/users/{other['id']}", json={"is_active": False})
        assert r.status_code == 200
        assert r.json()["is_active"] is False
        # Reactivate too (covers the toggle path back on).
        r2 = admin_client.put(f"/api/users/{other['id']}", json={"is_active": True})
        assert r2.status_code == 200
        assert r2.json()["is_active"] is True

    def test_self_edit_own_name_allowed(self, admin_client):
        """PUT /api/users/{id} — admin editing only their OWN name passes the
        self-edit guard (actor==target, no role/is_active change → fall-through
        to exit) → 200."""
        uid = admin_client.admin_me["id"]
        r = admin_client.put(f"/api/users/{uid}", json={"name": "Admin Renamed Self"})
        assert r.status_code == 200
        assert r.json()["name"] == "Admin Renamed Self"

    def test_self_demote_blocked_400(self, admin_client):
        """PUT /api/users/{id} — admin demoting THEMSELVES → 400."""
        uid = admin_client.admin_me["id"]
        r = admin_client.put(f"/api/users/{uid}", json={"role": "member"})
        assert r.status_code == 400
        assert "demote yourself" in r.json()["detail"].lower()

    def test_self_deactivate_blocked_400(self, admin_client):
        """PUT /api/users/{id} — admin deactivating THEMSELVES → 400."""
        uid = admin_client.admin_me["id"]
        r = admin_client.put(f"/api/users/{uid}", json={"is_active": False})
        assert r.status_code == 400
        assert "deactivate yourself" in r.json()["detail"].lower()

    def test_demote_other_admin_with_remaining_admin_ok(self, admin_client, db_path):
        """PUT /api/users/{id} — demoting a 2nd admin while the actor stays an
        active admin → allowed (the last-admin guard's *skip* branch:
        n_other_admins > 0 because the actor still counts)."""
        admin2, _ = _second_user(admin_client, db_path, "admin2@acme.test",
                                  role="admin", name="Admin Two")
        r = admin_client.put(f"/api/users/{admin2['id']}", json={"role": "member"})
        assert r.status_code == 200
        assert r.json()["role"] == "member"

    def test_demote_other_admin_to_admin_is_noop_skip(self, admin_client, db_path):
        """PUT /api/users/{id} — 'demoting' a 2nd admin to admin again keeps
        will_be_role==admin so the last-admin guard early-returns (the
        `will_be_role == ADMIN and will_be_active` skip branch)."""
        admin2, _ = _second_user(admin_client, db_path, "admin3@acme.test",
                                  role="admin", name="Admin Three")
        r = admin_client.put(f"/api/users/{admin2['id']}", json={"role": "admin"})
        assert r.status_code == 200


class TestUsersDelete:
    def test_delete_member_ok(self, admin_client, db_path):
        """DELETE /api/users/{id} — admin deletes a member → 200."""
        other, _ = _second_user(admin_client, db_path, "del-me@acme.test", name="Delete Me")
        r = admin_client.delete(f"/api/users/{other['id']}")
        assert r.status_code == 200
        assert r.json()["message"] == "User deleted"

    def test_delete_not_found_404(self, admin_client):
        """DELETE /api/users/{id} — unknown id → 404."""
        assert admin_client.delete("/api/users/99999").status_code == 404

    def test_delete_cross_org_404(self, two_orgs):
        """DELETE /api/users/{id} — other org user → 404."""
        c_a, _c_b, _, me_b = two_orgs
        assert c_a.delete(f"/api/users/{me_b['id']}").status_code == 404

    def test_delete_self_blocked_400(self, admin_client):
        """DELETE /api/users/{id} — deleting yourself → 400."""
        uid = admin_client.admin_me["id"]
        r = admin_client.delete(f"/api/users/{uid}")
        assert r.status_code == 400
        assert "delete yourself" in r.json()["detail"].lower()

    def test_delete_other_admin_with_remaining_admin_ok(self, admin_client, db_path):
        """DELETE /api/users/{id} — deleting a 2nd admin while the actor stays
        an active admin → 200 (the n_other_admins > 0 skip branch in delete)."""
        admin2, _ = _second_user(admin_client, db_path, "delsecond@acme.test",
                                  role="admin", name="Second Admin")
        r = admin_client.delete(f"/api/users/{admin2['id']}")
        assert r.status_code == 200
        assert r.json()["message"] == "User deleted"


# ===========================================================================
# app/routes/memberships.py
# ===========================================================================
class TestMemberships:
    def _project(self, admin_client):
        return admin_client.post("/api/projects", json={
            "name": "MemProj", "color": "#000000",
        }).json()

    def test_add_unknown_user_400(self, admin_client):
        """POST /{pid}/members — user_id not in org → 400."""
        p = self._project(admin_client)
        r = admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": 99999, "role": "member",
        })
        assert r.status_code == 400
        assert "Unknown user" in r.json()["detail"]

    def test_add_member_forbidden_403(self, admin_client, db_path):
        """POST /{pid}/members — a non-lead member can't manage members → 403.

        The member is added to the project (so they can SEE it) but isn't a
        lead, so can_manage_project is False on the add attempt."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "addforbid@acme.test",
                                 name="Add Forbid", project_ids=[p["id"]])
        member_client, _me = _invite_and_accept(
            admin_client, db_path, "plainmem@acme.test", role="member",
            name="Plain Mem", project_ids=[p["id"]],
        )
        resp = member_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        assert resp.status_code == 403
        assert "leads" in resp.json()["detail"].lower()

    def test_add_disabled_user_400(self, admin_client, db_path):
        """POST /{pid}/members — adding a deactivated user → 400."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "disabled@acme.test", name="Disabled User")
        # Deactivate them.
        admin_client.put(f"/api/users/{other['id']}", json={"is_active": False})
        r = admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        assert r.status_code == 400
        assert "disabled" in r.json()["detail"].lower()

    def test_add_duplicate_member_409(self, admin_client, db_path):
        """POST /{pid}/members — adding the same member twice → 409."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "twice@acme.test", name="Twice Member")
        first = admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        assert first.status_code == 201
        dup = admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        assert dup.status_code == 409

    def test_update_member_role_ok(self, admin_client, db_path):
        """PUT /{pid}/members/{uid} — promote a member to lead → 200."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "promote@acme.test", name="Promote Me")
        admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        r = admin_client.put(f"/api/projects/{p['id']}/members/{other['id']}", json={
            "role": "lead",
        })
        assert r.status_code == 200
        assert r.json()["project_role"] == "lead"

    def test_update_member_same_role_no_audit(self, admin_client, db_path):
        """PUT /{pid}/members/{uid} — setting the same role → no-op, 200."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "norole@acme.test", name="No Role Change")
        admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        r = admin_client.put(f"/api/projects/{p['id']}/members/{other['id']}", json={
            "role": "member",
        })
        assert r.status_code == 200

    def test_update_member_not_found_404(self, admin_client):
        """PUT /{pid}/members/{uid} — no membership row → 404."""
        p = self._project(admin_client)
        r = admin_client.put(f"/api/projects/{p['id']}/members/99999", json={"role": "lead"})
        assert r.status_code == 404

    def test_update_member_forbidden_403(self, admin_client, db_path):
        """PUT /{pid}/members/{uid} — non-manager member → 403."""
        p = self._project(admin_client)
        member_client, me = self_member(admin_client, db_path)
        # member isn't on the project and isn't a lead → can_manage_project False
        r = member_client.put(f"/api/projects/{p['id']}/members/{me['id']}", json={"role": "lead"})
        assert r.status_code in (403, 404)

    def test_demote_last_lead_blocked_400(self, admin_client, db_path):
        """PUT /{pid}/members/{uid} — demoting the only lead → 400."""
        # Invite someone as the project's sole lead.
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "solelead@acme.test",
                                 name="Sole Lead", project_ids=[p["id"]], as_lead=True)
        # Remove the admin's auto-lead row so `other` is the only lead.
        from app.database import SessionLocal
        from app.models import ProjectMembership, User
        db = SessionLocal()
        try:
            admin_uid = admin_client.admin_me["id"]
            db.query(ProjectMembership).filter(
                ProjectMembership.project_id == p["id"],
                ProjectMembership.user_id == admin_uid,
            ).delete()
            db.commit()
        finally:
            db.close()
        r = admin_client.put(f"/api/projects/{p['id']}/members/{other['id']}", json={
            "role": "member",
        })
        assert r.status_code == 400
        assert "last project lead" in r.json()["detail"].lower()

    def test_demote_lead_with_another_lead_allowed(self, admin_client, db_path):
        """PUT /{pid}/members/{uid} — demoting one of TWO leads is allowed
        (the other_leads is-not-None pass branch)."""
        p = self._project(admin_client)
        # Admin is auto-lead. Invite a second lead.
        other, _ = _second_user(admin_client, db_path, "twolead@acme.test",
                                 name="Two Lead", project_ids=[p["id"]], as_lead=True)
        # Now demote the second lead — admin remains a lead so it's allowed.
        r = admin_client.put(f"/api/projects/{p['id']}/members/{other['id']}", json={
            "role": "member",
        })
        assert r.status_code == 200
        assert r.json()["project_role"] == "member"

    def test_remove_lead_with_another_lead_allowed(self, admin_client, db_path):
        """DELETE /{pid}/members/{uid} — removing one of TWO leads is allowed
        (the other_leads is-not-None pass branch in remove)."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "rmlead@acme.test",
                                 name="Rm Lead", project_ids=[p["id"]], as_lead=True)
        r = admin_client.delete(f"/api/projects/{p['id']}/members/{other['id']}")
        assert r.status_code == 200
        assert r.json()["message"] == "Member removed"

    def test_remove_member_ok(self, admin_client, db_path):
        """DELETE /{pid}/members/{uid} — remove a plain member → 200."""
        p = self._project(admin_client)
        other, _ = _second_user(admin_client, db_path, "remove@acme.test", name="Remove Me")
        admin_client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": other["id"], "role": "member",
        })
        r = admin_client.delete(f"/api/projects/{p['id']}/members/{other['id']}")
        assert r.status_code == 200
        assert r.json()["message"] == "Member removed"

    def test_remove_member_not_found_404(self, admin_client):
        """DELETE /{pid}/members/{uid} — no membership row → 404."""
        p = self._project(admin_client)
        assert admin_client.delete(f"/api/projects/{p['id']}/members/99999").status_code == 404

    def test_remove_member_forbidden_403(self, admin_client, db_path):
        """DELETE /{pid}/members/{uid} — non-manager member → 403."""
        p = self._project(admin_client)
        member_client, me = self_member(admin_client, db_path)
        r = member_client.delete(f"/api/projects/{p['id']}/members/{me['id']}")
        assert r.status_code in (403, 404)

    def test_remove_last_lead_blocked_400(self, admin_client):
        """DELETE /{pid}/members/{uid} — removing the only lead → 400."""
        p = self._project(admin_client)
        me = admin_client.admin_me["id"]
        # Admin is the auto-lead and only lead.
        r = admin_client.delete(f"/api/projects/{p['id']}/members/{me}")
        assert r.status_code == 400
        assert "last project lead" in r.json()["detail"].lower()

    def test_list_members_non_member_404(self, admin_client, db_path):
        """GET /{pid}/members — a non-member, non-admin → 404 (can_access
        False branch)."""
        p = self._project(admin_client)
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.get(f"/api/projects/{p['id']}/members").status_code == 404

    def test_list_members_empty_project(self, admin_client, db_path):
        """GET /{pid}/members — a project with NO membership rows returns an
        empty list (the `if user_ids:` false branch). We strip the admin's
        auto-lead row directly so the membership table is empty; the admin
        still has visibility via the org-admin path."""
        p = self._project(admin_client)
        from app.database import SessionLocal
        from app.models import ProjectMembership
        db = SessionLocal()
        try:
            db.query(ProjectMembership).filter(
                ProjectMembership.project_id == p["id"],
            ).delete()
            db.commit()
        finally:
            db.close()
        r = admin_client.get(f"/api/projects/{p['id']}/members")
        assert r.status_code == 200
        assert r.json() == []


# ===========================================================================
# app/routes/organizations.py
# ===========================================================================
class TestOrganizations:
    def test_get_my_org_ok(self, admin_client):
        """GET /api/organization — returns the caller's org."""
        r = admin_client.get("/api/organization")
        assert r.status_code == 200
        assert r.json()["name"] == "Acme Co"

    def test_rename_org_ok(self, admin_client):
        """PUT /api/organization — admin renames → audit + 200."""
        r = admin_client.put("/api/organization", json={"name": "Acme Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Acme Renamed"

    def test_update_org_no_change(self, admin_client):
        """PUT /api/organization — identical value → no-change branch, 200."""
        r = admin_client.put("/api/organization", json={"name": "Acme Co"})
        assert r.status_code == 200
        assert r.json()["name"] == "Acme Co"

    def test_update_org_member_forbidden_403(self, admin_client, db_path):
        """PUT /api/organization — non-admin → require_admin 403."""
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.put("/api/organization", json={"name": "Hax"}).status_code == 403


# ===========================================================================
# app/routes/branding.py
# ===========================================================================
class TestBranding:
    def test_get_branding_ok(self, admin_client):
        """GET /api/branding — admin reads (defaults all None)."""
        r = admin_client.get("/api/branding")
        assert r.status_code == 200
        assert r.json()["accent_color"] is None

    def test_get_branding_member_forbidden_403(self, admin_client, db_path):
        """GET /api/branding — non-admin → require_admin 403."""
        member_client, _ = self_member(admin_client, db_path)
        assert member_client.get("/api/branding").status_code == 403

    def test_set_valid_color_and_logo(self, admin_client):
        """PUT /api/branding — valid 6-hex colour + data-url logo → 200."""
        ok_logo = "data:image/png;base64,iVBORw0KGgo="
        r = admin_client.put("/api/branding", json={
            "accent_color": "#6366f1", "logo_data_url": ok_logo,
        })
        assert r.status_code == 200
        assert r.json()["accent_color"] == "#6366f1"
        assert r.json()["logo_data_url"] == ok_logo

    def test_bad_color_422(self, admin_client):
        """PUT /api/branding — non-hex colour → validator → 422."""
        r = admin_client.put("/api/branding", json={"accent_color": "rebeccapurple"})
        assert r.status_code == 422

    def test_empty_color_treated_as_none(self, admin_client):
        """PUT /api/branding — empty-string colour → None branch, 200."""
        r = admin_client.put("/api/branding", json={"accent_color": ""})
        assert r.status_code == 200

    def test_empty_logo_and_email_treated_as_none(self, admin_client):
        """PUT /api/branding — empty logo + empty email → both validators take
        the None early-return branch, 200."""
        r = admin_client.put("/api/branding", json={
            "logo_data_url": "", "email_from_override": "",
        })
        assert r.status_code == 200
        assert r.json()["logo_data_url"] is None
        assert r.json()["email_from_override"] is None

    def test_valid_email_override_accepted(self, admin_client):
        """PUT /api/branding — a clean email override passes the validator's
        success return and persists."""
        r = admin_client.put("/api/branding", json={
            "email_from_override": "noreply@acme.test",
        })
        assert r.status_code == 200
        assert r.json()["email_from_override"] == "noreply@acme.test"

    def test_bad_logo_not_data_url_422(self, admin_client):
        """PUT /api/branding — logo that isn't a data: URL → 422."""
        r = admin_client.put("/api/branding", json={"logo_data_url": "http://x/y.png"})
        assert r.status_code == 422

    def test_logo_too_large_422(self, admin_client):
        """PUT /api/branding — oversize logo → 422."""
        oversize = "data:image/png;base64," + ("A" * 250_000)
        assert admin_client.put("/api/branding", json={"logo_data_url": oversize}).status_code == 422

    def test_bad_email_override_422(self, admin_client):
        """PUT /api/branding — email override with a space / no @ → 422."""
        assert admin_client.put("/api/branding", json={
            "email_from_override": "bad email@a.test",
        }).status_code == 422
        assert admin_client.put("/api/branding", json={
            "email_from_override": "noatsign",
        }).status_code == 422

    def test_no_change_path(self, admin_client):
        """PUT /api/branding — empty body → 'no changes' branch, 200."""
        assert admin_client.put("/api/branding", json={}).status_code == 200


# ===========================================================================
# app/routes/invitations.py — _client_ip trusted-proxy branch
# ===========================================================================
def test_accept_records_forwarded_ip_when_proxy_trusted(db_path, monkeypatch):
    """POST /api/invitations/accept — with TRUST_PROXY_FORWARDED_FOR on and an
    X-Forwarded-For header, _client_ip honours the forwarded value and stamps
    it into the new user's session row (the XFF branch of _client_ip).

    Built as a standalone app because the flag is read at request time from
    settings, and the rest of the suite runs with it off.
    """
    import sys
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
    monkeypatch.setenv("CSRF_PROTECTION", "false")
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    monkeypatch.setenv("TRUST_PROXY_FORWARDED_FOR", "true")
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    from app.main import app
    from app.auth import generate_random_token

    with TestClient(app) as admin:
        admin.post("/api/auth/signup", json={
            "organization_name": "Proxy Co", "name": "Proxy Admin",
            "email": "proxy@acme.test", "password": PASS,
        })
        inv = admin.post("/api/invitations", json={
            "email": "behindproxy@acme.test", "role": "member",
        }).json()
        raw, h = generate_random_token()
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE invitations SET token_hash=? WHERE id=?", (h, inv["id"]))
        conn.commit()
        conn.close()
        with TestClient(app) as invitee:
            r = invitee.post(
                "/api/invitations/accept",
                json={"token": raw, "name": "Proxy Joiner", "password": PASS},
                headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
            )
            assert r.status_code == 200, r.text
            new_id = r.json()["id"]

    # The session row's ip_address should be the first forwarded hop.
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT ip_address FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (new_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "203.0.113.7"
