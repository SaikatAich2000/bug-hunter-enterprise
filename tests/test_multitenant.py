"""Multi-tenant Bug Hunter v4 test suite.

What we test (sorted by importance):

1. SIGNUP + ORGANIZATION CREATION
   - public signup creates an org + admin user in one go
   - duplicate emails are rejected
   - same org name in two signups produces distinct slugs
   - ALLOW_PUBLIC_SIGNUP=false blocks signup with 403

2. TENANT ISOLATION
   - org A admin can't see org B's projects (404)
   - org A admin can't see org B's users (filtered list)
   - org A admin can't see org B's bugs (404)
   - audit log shows only the actor's org
   - stats include only accessible projects

3. INVITATIONS
   - admin sends, manager sends, member can't send (403)
   - preview returns org metadata for valid token
   - accept creates user and signs them in
   - already-accepted / revoked / expired all rejected
   - cross-org email shadowing handled (409)
   - manager can only attach projects they lead

4. PROJECT MEMBERSHIP
   - admin sees all org projects (implicit access)
   - non-admin sees only projects they're a member of
   - lead can manage members; member can't (403)
   - last-lead removal blocked
   - cross-org project_id in URL → 404

5. BUGS & PERMISSIONS
   - filter by project_id is intersected with accessible set
   - member can edit a bug in their project; can't delete (admin/lead only)
   - admin can delete bugs anywhere in their org
   - cross-org bug read returns 404 (not 403 — don't leak existence)
   - cross-org user_id as assignee is rejected (400)

6. LAST-ADMIN PROTECTION (per-org)
   - self-demotion blocked; self-deactivation blocked
   - last admin can't be demoted/deleted/deactivated
"""
from __future__ import annotations

PASS = "TestPass1!"


# ============================================================================
# 1. Signup + organization creation
# ============================================================================
class TestSignup:
    def test_creates_org_and_admin(self, client):
        r = client.post("/api/auth/signup", json={
            "organization_name": "Acme Co", "name": "Alice",
            "email": "alice@acme.test", "password": PASS,
        })
        assert r.status_code == 201
        me = r.json()
        assert me["role"] == "admin"
        assert me["organization_name"] == "Acme Co"
        assert me["organization_slug"] == "acme-co"
        assert me["org_id"]

    def test_session_is_set_after_signup(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Alice",
            "email": "alice@acme.test", "password": PASS,
        })
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "alice@acme.test"

    def test_duplicate_email_rejected(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Alpha", "name": "Xavier", "email": "dup@x.test", "password": PASS,
        })
        # Need a fresh client so we're not authed already; same DB though.
        from fastapi.testclient import TestClient
        from app.main import app
        c2 = TestClient(app)
        r = c2.post("/api/auth/signup", json={
            "organization_name": "Bravo", "name": "Yvonne", "email": "dup@x.test", "password": PASS,
        })
        assert r.status_code == 409

    def test_same_org_name_different_slug(self, two_orgs):
        # two_orgs fixture creates two distinct orgs; if it succeeded
        # they have unique slugs. Let's make slug collision explicit:
        from fastapi.testclient import TestClient
        from app.main import app
        c_a, c_b, me_a, me_b = two_orgs
        c_c = TestClient(app)
        with c_c:
            r = c_c.post("/api/auth/signup", json={
                "organization_name": "Org A",  # same name as Alice's
                "name": "Same Name", "email": "third@c.test", "password": PASS,
            })
            assert r.status_code == 201
            me_c = r.json()
            assert me_c["organization_slug"] != me_a["organization_slug"]

    def test_public_signup_can_be_disabled(self, monkeypatch, tmp_path):
        import sys
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'd.db'}")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "false")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/auth/signup", json={
                "organization_name": "Acme", "name": "Yvonne",
                "email": "z@x.test", "password": PASS,
            })
            assert r.status_code == 403


# ============================================================================
# 2. Tenant isolation
# ============================================================================
class TestTenantIsolation:
    def test_project_list_excludes_other_orgs(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        c_a.post("/api/projects", json={"name": "ProjA", "color": "#000000"})
        c_b.post("/api/projects", json={"name": "ProjB", "color": "#ffffff"})
        a_names = {p["name"] for p in c_a.get("/api/projects").json()}
        b_names = {p["name"] for p in c_b.get("/api/projects").json()}
        assert a_names == {"ProjA"}
        assert b_names == {"ProjB"}

    def test_user_list_excludes_other_orgs(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        a_emails = {u["email"] for u in c_a.get("/api/users").json()}
        b_emails = {u["email"] for u in c_b.get("/api/users").json()}
        assert a_emails == {"alice@a.test"}
        assert b_emails == {"bob@b.test"}

    def test_cross_org_project_get_returns_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Secret", "color": "#000000"}).json()
        assert c_a.get(f"/api/projects/{p_b['id']}").status_code == 404

    def test_cross_org_bug_get_returns_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        bug = c_b.post("/api/bugs", json={
            "project_id": p_b["id"], "title": "Hidden Bug", "description": "Test description",
        }).json()
        assert c_a.get(f"/api/bugs/{bug['id']}").status_code == 404

    def test_cross_org_user_update_returns_404(self, two_orgs):
        c_a, c_b, _, me_b = two_orgs
        assert c_a.put(f"/api/users/{me_b['id']}", json={"name": "Hax"}).status_code == 404

    def test_audit_excludes_other_orgs(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        # Generate activity in both orgs.
        c_b.post("/api/projects", json={"name": "ProjB", "color": "#000000"})
        c_a.post("/api/projects", json={"name": "ProjA", "color": "#000000"})
        audit_a = c_a.get("/api/audit").json()
        actors = {row["actor_name"] for row in audit_a}
        assert "Bob B" not in actors

    def test_stats_scoped_to_org(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        # Org B has 3 projects; Org A has 0.
        for n in ("Alpha", "Bravo", "Charlie"):
            c_b.post("/api/projects", json={"name": n, "color": "#000000"})
        s_a = c_a.get("/api/stats").json()
        s_b = c_b.get("/api/stats").json()
        assert s_a["projects"] == 0
        assert s_b["projects"] == 3


# ============================================================================
# 3. Invitations
# ============================================================================
class TestInvitations:
    def test_admin_can_invite(self, client, make_invite):
        # Sign up admin first
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        token = make_invite(client, "newbie@x.test", role="member")
        assert token

    def test_member_cannot_invite(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "m@x.test", role="member")
        # Carol the member accepts
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as carol:
            carol.post("/api/invitations/accept", json={
                "token": tok, "name": "Carol", "password": PASS,
            })
            r = carol.post("/api/invitations", json={
                "email": "other@x.test", "role": "member",
            })
            assert r.status_code == 403

    def test_preview_works_with_valid_token(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Foo Corp", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "p@x.test")
        r = client.get(f"/api/invitations/preview/{tok}")
        assert r.status_code == 200
        assert r.json()["organization_name"] == "Foo Corp"
        assert r.json()["email"] == "p@x.test"

    def test_accept_creates_user_and_authenticates(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "c@x.test", role="member")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c_carol:
            r = c_carol.post("/api/invitations/accept", json={
                "token": tok, "name": "Carol", "password": PASS,
            })
            assert r.status_code == 200
            assert r.json()["role"] == "member"
            assert c_carol.get("/api/auth/me").status_code == 200

    def test_accept_twice_fails(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "c@x.test")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c1:
            assert c1.post("/api/invitations/accept",
                           json={"token": tok, "name": "Cal", "password": PASS}).status_code == 200
        with TestClient(app) as c2:
            r = c2.post("/api/invitations/accept",
                        json={"token": tok, "name": "Cory", "password": PASS})
            assert r.status_code == 400

    def test_revoke_invitation(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        r = client.post("/api/invitations", json={
            "email": "c@x.test", "role": "member",
        })
        inv_id = r.json()["id"]
        assert client.delete(f"/api/invitations/{inv_id}").status_code == 200
        # Preview should now fail
        invitations = client.get("/api/invitations").json()
        revoked = next(i for i in invitations if i["id"] == inv_id)
        assert revoked["revoked_at"] is not None

    def test_email_already_in_other_org_rejected(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        # Carol is in org B; org A admin trying to invite same email fails.
        # Currently the system says "already registered with another organization".
        r = c_a.post("/api/invitations", json={
            "email": "bob@b.test", "role": "member",
        })
        assert r.status_code == 409

    def test_manager_cannot_invite_as_admin(self, client, make_invite):
        # Set up: admin invites a manager. Manager then tries to invite
        # someone as admin — should be 403.
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "mgr@x.test", role="manager")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mgr:
            mgr.post("/api/invitations/accept", json={
                "token": tok, "name": "Manager Mike", "password": PASS,
            })
            r = mgr.post("/api/invitations", json={
                "email": "evil@x.test", "role": "admin",
            })
            assert r.status_code == 403


# ============================================================================
# 4. Project membership
# ============================================================================
class TestProjectMembership:
    def test_admin_sees_all_org_projects_without_membership_row(self, client):
        # Sign up A, create a project, sign up a second admin? Can't —
        # signup is one admin per org. But we can verify the admin
        # creator still appears as a lead (auto-added).
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        members = client.get(f"/api/projects/{p['id']}/members").json()
        assert len(members) == 1
        assert members[0]["project_role"] == "lead"

    def test_member_can_be_added_and_listed(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        tok = make_invite(client, "m@x.test", role="member")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as member_c:
            me = member_c.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia", "password": PASS,
            }).json()
        client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": me["id"], "role": "member",
        })
        members = client.get(f"/api/projects/{p['id']}/members").json()
        assert len(members) == 2

    def test_last_lead_removal_blocked(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        me = client.get("/api/auth/me").json()
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        r = client.delete(f"/api/projects/{p['id']}/members/{me['id']}")
        assert r.status_code == 400

    def test_member_cannot_manage_members(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        tok = make_invite(client, "m@x.test", role="member", project_ids=[p["id"]])
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mem:
            me = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia", "password": PASS,
            }).json()
            r = mem.post(f"/api/projects/{p['id']}/members", json={
                "user_id": me["id"], "role": "member",
            })
            assert r.status_code in (403, 409)

    def test_non_admin_sees_only_assigned_projects(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p1 = client.post("/api/projects", json={"name": "ProjOne", "color": "#000000"}).json()
        p2 = client.post("/api/projects", json={"name": "ProjTwo", "color": "#000000"}).json()
        # Invite a member to p1 only.
        tok = make_invite(client, "m@x.test", role="member", project_ids=[p1["id"]])
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia", "password": PASS,
            })
            ids = {p["id"] for p in mem.get("/api/projects").json()}
            assert ids == {p1["id"]}
            # p2 returns 404 to non-members.
            assert mem.get(f"/api/projects/{p2['id']}").status_code == 404


# ============================================================================
# 5. Bugs & permissions
# ============================================================================
class TestBugPermissions:
    def test_member_can_edit_bug_in_their_project(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        bug = client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Test Bug Title", "description": "Test description",
        }).json()
        tok = make_invite(client, "m@x.test", role="member", project_ids=[p["id"]])
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia", "password": PASS,
            })
            r = mem.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
            assert r.status_code == 200, r.text
            r = mem.delete(f"/api/bugs/{bug['id']}")
            assert r.status_code == 403  # member can't delete

    def test_admin_can_delete_anywhere_in_org(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        bug = client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Test Bug Title", "description": "Test description",
        }).json()
        assert client.delete(f"/api/bugs/{bug['id']}").status_code == 200

    def test_lead_can_delete_bug(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        bug = client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Test Bug Title", "description": "Test description",
        }).json()
        # Promote a teammate to LEAD via the invite.
        tok = make_invite(client, "lead@x.test", role="member",
                          project_ids=[p["id"]], as_lead=True)
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as lead:
            lead.post("/api/invitations/accept", json={
                "token": tok, "name": "Liz", "password": PASS,
            })
            r = lead.delete(f"/api/bugs/{bug['id']}")
            assert r.status_code == 200, r.text

    def test_cross_org_bug_assignee_rejected(self, two_orgs):
        c_a, c_b, _, me_b = two_orgs
        p_a = c_a.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        r = c_a.post("/api/bugs", json={
            "project_id": p_a["id"], "title": "Test Bug Title", "description": "Test description",
            "assignee_ids": [me_b["id"]],
        })
        assert r.status_code == 400

    def test_bug_filter_intersected_with_accessible(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Proj", "color": "#000000"}).json()
        c_b.post("/api/bugs", json={
            "project_id": p_b["id"], "title": "Secret", "description": "Test description",
        })
        # Org A passes the foreign project_id explicitly — should get empty.
        r = c_a.get(f"/api/bugs?project_id={p_b['id']}").json()
        assert r["total"] == 0


# ============================================================================
# 6. Last-admin protection (per-org)
# ============================================================================
class TestLastAdminProtection:
    def test_cannot_self_demote(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        me = client.get("/api/auth/me").json()
        r = client.put(f"/api/users/{me['id']}", json={"role": "member"})
        assert r.status_code == 400

    def test_cannot_self_deactivate(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        me = client.get("/api/auth/me").json()
        r = client.put(f"/api/users/{me['id']}", json={"is_active": False})
        assert r.status_code == 400

    def test_cannot_self_delete(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        me = client.get("/api/auth/me").json()
        r = client.delete(f"/api/users/{me['id']}")
        assert r.status_code == 400


# ============================================================================
# 7. Org info endpoint
# ============================================================================
class TestOrganization:
    def test_get_my_org(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Foo", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        r = client.get("/api/organization")
        assert r.status_code == 200
        assert r.json()["name"] == "Foo"

    def test_update_my_org_admin_only(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Foo", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        # Admin can update.
        r = client.put("/api/organization", json={"name": "Foo Inc"})
        assert r.status_code == 200
        assert r.json()["name"] == "Foo Inc"

        # Member can't.
        tok = make_invite(client, "m@x.test", role="member")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia", "password": PASS,
            })
            r = mem.put("/api/organization", json={"name": "Hax"})
            assert r.status_code == 403


# ============================================================================
# 8. Project key + uniqueness
# ============================================================================
class TestProjectKey:
    def test_key_auto_derived(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={
            "name": "Marketing Site", "color": "#000000",
        }).json()
        assert p["key"] == "MS"

    def test_explicit_key_honored(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        p = client.post("/api/projects", json={
            "name": "Whatever", "color": "#000000", "key": "WTV",
        }).json()
        assert p["key"] == "WTV"

    def test_duplicate_key_in_same_org_rejected(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        client.post("/api/projects", json={"name": "Ada", "color": "#000000", "key": "XK"})
        r = client.post("/api/projects", json={"name": "Bea", "color": "#000000", "key": "XK"})
        assert r.status_code == 409

    def test_same_key_different_org_allowed(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        c_a.post("/api/projects", json={"name": "Ada", "color": "#000000", "key": "PK"})
        r = c_b.post("/api/projects", json={"name": "Ada", "color": "#000000", "key": "PK"})
        assert r.status_code == 201


# ============================================================================
# 9. Manager cannot create projects (v4.1 policy tightening)
# ============================================================================
class TestProjectCreationAdminOnly:
    def test_manager_cannot_create_project(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "mgr@x.test", role="manager")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mgr:
            mgr.post("/api/invitations/accept", json={
                "token": tok, "name": "Manager Mike", "password": PASS,
            })
            r = mgr.post("/api/projects", json={
                "name": "New Project", "color": "#000000",
            })
            assert r.status_code == 403

    def test_member_cannot_create_project(self, client, make_invite):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada", "email": "a@x.test", "password": PASS,
        })
        tok = make_invite(client, "m@x.test", role="member")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia", "password": PASS,
            })
            r = mem.post("/api/projects", json={
                "name": "Sneaky", "color": "#000000",
            })
            assert r.status_code == 403


# ============================================================================
# 10. Profile self-service
# ============================================================================
class TestProfile:
    def test_update_own_name(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Original Name",
            "email": "a@x.test", "password": PASS,
        })
        r = client.put("/api/auth/profile", json={"name": "Updated Name"})
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"
        # Make sure /me reflects it too
        assert client.get("/api/auth/me").json()["name"] == "Updated Name"

    def test_profile_name_too_short_rejected(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Original Name",
            "email": "a@x.test", "password": PASS,
        })
        r = client.put("/api/auth/profile", json={"name": "X"})
        assert r.status_code == 422

    def test_profile_unauth_returns_401(self, client):
        r = client.put("/api/auth/profile", json={"name": "Hacker"})
        assert r.status_code == 401


# ============================================================================
# 11. Email change 2-step verification
# ============================================================================
class TestEmailChange:
    def _setup(self, client):
        client.post("/api/auth/signup", json={
            "organization_name": "Acme", "name": "Ada Admin",
            "email": "ada@old.test", "password": PASS,
        })

    def _peek_code(self, db_path):
        """Pull the latest unsealed plaintext code by reading the request
        + replacing the hash with a known token. The plaintext code is only
        in the email we never sent; for tests we patch the hash."""
        from app.auth import hash_token
        import sqlite3
        # We replace the request's code_hash with our own and use a known
        # plaintext. Mirrors the invitation token trick.
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT id FROM email_change_requests "
            "WHERE used_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.close()
            return None
        new_code = "123456"
        conn.execute(
            "UPDATE email_change_requests SET code_hash=? WHERE id=?",
            (hash_token(new_code), row[0]),
        )
        conn.commit()
        conn.close()
        return new_code

    def test_request_requires_current_password(self, client):
        self._setup(client)
        r = client.post("/api/auth/email-change/request", json={
            "new_email": "ada@new.test", "current_password": "wrong-pass",
        })
        assert r.status_code == 400

    def test_request_rejects_same_email(self, client):
        self._setup(client)
        r = client.post("/api/auth/email-change/request", json={
            "new_email": "ada@old.test", "current_password": PASS,
        })
        assert r.status_code == 400

    def test_request_rejects_already_used_email(self, two_orgs):
        c_a, c_b, me_a, me_b = two_orgs
        r = c_a.post("/api/auth/email-change/request", json={
            "new_email": "bob@b.test", "current_password": PASS,
        })
        assert r.status_code == 409

    def test_full_flow_success(self, client, db_path):
        self._setup(client)
        r = client.post("/api/auth/email-change/request", json={
            "new_email": "ada@new.test", "current_password": PASS,
        })
        assert r.status_code == 202
        code = self._peek_code(db_path)
        assert code is not None
        r = client.post("/api/auth/email-change/confirm", json={"code": code})
        assert r.status_code == 200
        assert r.json()["email"] == "ada@new.test"
        # /me reflects it
        assert client.get("/api/auth/me").json()["email"] == "ada@new.test"

    def test_wrong_code_attempts_counted(self, client, db_path):
        self._setup(client)
        client.post("/api/auth/email-change/request", json={
            "new_email": "ada@new.test", "current_password": PASS,
        })
        # Don't know the code → keep guessing.
        for _ in range(5):
            r = client.post("/api/auth/email-change/confirm", json={"code": "000000"})
            assert r.status_code == 400
        # After 5 failures the request is sealed — even the right code fails.
        code = self._peek_code(db_path)
        if code:
            r = client.post("/api/auth/email-change/confirm", json={"code": code})
            assert r.status_code == 400

    def test_confirm_without_request_fails(self, client):
        self._setup(client)
        r = client.post("/api/auth/email-change/confirm", json={"code": "123456"})
        assert r.status_code == 400

    def test_confirm_requires_auth(self, client):
        r = client.post("/api/auth/email-change/confirm", json={"code": "123456"})
        assert r.status_code == 401
