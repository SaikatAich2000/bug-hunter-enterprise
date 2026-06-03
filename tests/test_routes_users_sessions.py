"""Coverage tests for users / sessions / projects / memberships routes.

These exercise the under-covered branches of:
  - app/routes/users.py
  - app/routes/sessions.py
  - app/routes/projects.py
  - app/routes/memberships.py

All tests reuse the conftest's `client`, `two_orgs`, and `make_invite`
fixtures so they share the same TestClient / SQLite-tmp pattern as the
rest of the suite.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

PASS = "TestPass1!"


def _signup_admin(client, org="Acme", name="Ada Admin",
                  email="ada@x.test", password=PASS):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# users.py
# ---------------------------------------------------------------------------
class TestUsersList:
    def test_list_returns_self(self, client):
        _signup_admin(client)
        r = client.get("/api/users")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["email"] == "ada@x.test"

    def test_list_q_filter_matches_name(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
        r = client.get("/api/users", params={"q": "mia"})
        assert r.status_code == 200
        emails = {u["email"] for u in r.json()}
        assert "mia@x.test" in emails
        assert "ada@x.test" not in emails

    def test_list_q_filter_matches_role(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mgr@x.test", role="manager")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mgr:
            mgr.post("/api/invitations/accept", json={
                "token": tok, "name": "Manager Mike", "password": PASS,
            })
        r = client.get("/api/users", params={"q": "manager"})
        assert r.status_code == 200
        roles = {u["role"] for u in r.json()}
        assert "manager" in roles

    def test_list_q_filter_escapes_like_wildcards(self, client):
        """% and _ in the query string must be treated literally, not as
        SQL wildcards. Searching for a literal '%' should match nothing."""
        _signup_admin(client)
        r = client.get("/api/users", params={"q": "%"})
        assert r.status_code == 200
        assert r.json() == []

    def test_list_exclude_inactive(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "x@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia M", "password": PASS,
            }).json()
        # Deactivate
        client.put(f"/api/users/{me['id']}", json={"is_active": False})
        all_users = client.get("/api/users", params={"include_inactive": "true"}).json()
        active_only = client.get("/api/users", params={"include_inactive": "false"}).json()
        assert len(all_users) == 2
        assert all(u["is_active"] for u in active_only)

    def test_list_requires_auth(self, client):
        r = client.get("/api/users")
        assert r.status_code == 401


class TestUsersGet:
    def test_get_one_same_org(self, client):
        me = _signup_admin(client)
        r = client.get(f"/api/users/{me['id']}")
        assert r.status_code == 200
        assert r.json()["email"] == "ada@x.test"

    def test_get_missing_returns_404(self, client):
        _signup_admin(client)
        r = client.get("/api/users/999999")
        assert r.status_code == 404


class TestUsersCreate:
    def test_admin_can_create_user(self, client):
        _signup_admin(client)
        r = client.post("/api/users", json={
            "name": "New Hire", "email": "nh@x.test",
            "role": "member", "password": PASS,
        })
        assert r.status_code == 201, r.text
        assert r.json()["email"] == "nh@x.test"
        assert r.json()["role"] == "member"

    def test_duplicate_email_409(self, client):
        _signup_admin(client)
        client.post("/api/users", json={
            "name": "First", "email": "dup@x.test",
            "role": "member", "password": PASS,
        })
        r = client.post("/api/users", json={
            "name": "Second", "email": "dup@x.test",
            "role": "member", "password": PASS,
        })
        assert r.status_code == 409

    def test_member_cannot_create_user(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            r = mem.post("/api/users", json={
                "name": "Nope", "email": "nope@x.test",
                "role": "member", "password": PASS,
            })
            assert r.status_code == 403


class TestUsersUpdate:
    def test_member_cannot_update_user(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
            r = mem.put(f"/api/users/{me_mem['id']}", json={"name": "Newer"})
            assert r.status_code == 403

    def test_update_missing_user_404(self, client):
        _signup_admin(client)
        r = client.put("/api/users/999999", json={"name": "Whatever"})
        assert r.status_code == 404

    def test_admin_can_change_user_name_and_role(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        r = client.put(f"/api/users/{me_mem['id']}", json={
            "name": "Mia Manager", "role": "manager",
        })
        assert r.status_code == 200
        assert r.json()["role"] == "manager"
        assert r.json()["name"] == "Mia Manager"

    def test_admin_password_reset_bumps_session_version(self, client, make_invite):
        """When an admin resets a user's password, the user's
        session_version must be bumped so their existing sessions fail."""
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
            # Mia has a working session at this point.
            assert mem.get("/api/auth/me").status_code == 200
            # Admin (client) resets her password.
            r = client.put(f"/api/users/{me_mem['id']}",
                           json={"password": "NewerPass1!"})
            assert r.status_code == 200
            # Mia's existing session_version no longer matches → auth fails.
            assert mem.get("/api/auth/me").status_code == 401

    def test_deactivating_user_bumps_session_version(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
            assert mem.get("/api/auth/me").status_code == 200
            r = client.put(f"/api/users/{me_mem['id']}",
                           json={"is_active": False})
            assert r.status_code == 200
            # Disabled user → 401 on her stale cookie.
            assert mem.get("/api/auth/me").status_code == 401

    def test_update_duplicate_email_409(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        r = client.put(f"/api/users/{me_mem['id']}",
                       json={"email": "ada@x.test"})
        assert r.status_code == 409

    def test_cannot_demote_last_admin_via_role_change(self, client):
        me = _signup_admin(client)
        r = client.put(f"/api/users/{me['id']}", json={"role": "member"})
        # _check_self_edit_guardrails catches this first → 400
        assert r.status_code == 400


class TestUsersDelete:
    def test_member_cannot_delete(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
            r = mem.delete(f"/api/users/{me_mem['id']}")
            assert r.status_code == 403

    def test_admin_can_delete_member(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        r = client.delete(f"/api/users/{me_mem['id']}")
        assert r.status_code == 200
        assert client.get(f"/api/users/{me_mem['id']}").status_code == 404

    def test_delete_missing_404(self, client):
        _signup_admin(client)
        r = client.delete("/api/users/999999")
        assert r.status_code == 404

    def test_cross_org_delete_returns_404(self, two_orgs):
        c_a, _c_b, _me_a, me_b = two_orgs
        # Alice tries to delete Bob (org B admin) — should look like 404.
        r = c_a.delete(f"/api/users/{me_b['id']}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# sessions.py
# ---------------------------------------------------------------------------
class TestSessionsList:
    def test_member_cannot_list_sessions(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            r = mem.get("/api/sessions")
            assert r.status_code == 403

    def test_admin_sees_own_session_marked_current(self, client):
        _signup_admin(client)
        r = client.get("/api/sessions")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        # Admin's own session should be flagged as current.
        currents = [r for r in rows if r["is_current"]]
        assert len(currents) == 1
        assert currents[0]["user_email"] == "ada@x.test"

    def test_admin_sees_other_users_sessions_in_org(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            # Mia has a session row now.
            assert mem.get("/api/auth/me").status_code == 200
        rows = client.get("/api/sessions").json()
        emails = {r["user_email"] for r in rows}
        assert "ada@x.test" in emails
        assert "mia@x.test" in emails
        # Mia's session is NOT marked current (current is the admin's).
        mia_rows = [r for r in rows if r["user_email"] == "mia@x.test"]
        assert all(not r["is_current"] for r in mia_rows)

    def test_cross_org_sessions_invisible(self, two_orgs):
        c_a, _c_b, _me_a, _me_b = two_orgs
        rows = c_a.get("/api/sessions").json()
        emails = {r["user_email"] for r in rows}
        # Alice sees only her own session — not Bob's from org B.
        assert emails == {"alice@a.test"}

    def test_expired_sessions_swept(self, client, db_path):
        """list_sessions sweeps any session row with expires_at < now."""
        _signup_admin(client)
        # Stuff an expired session row into the DB.
        from sqlalchemy import create_engine, text
        from datetime import datetime, timezone, timedelta
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            uid = conn.execute(
                text("SELECT id FROM users WHERE email = :e"),
                {"e": "ada@x.test"},
            ).scalar()
            past = datetime.now(timezone.utc) - timedelta(days=1)
            conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(user_id, jti, ip_address, user_agent, created_at, "
                    " last_seen_at, expires_at) "
                    "VALUES (:u, :j, :ip, :ua, :c, :l, :e)"
                ),
                {
                    "u": uid, "j": "expired-jti-xyz",
                    "ip": "1.2.3.4", "ua": "ExpiredBot/1.0",
                    "c": past, "l": past, "e": past,
                },
            )
        engine.dispose()
        # Listing should sweep the expired row.
        rows = client.get("/api/sessions").json()
        assert all(r["user_agent"] != "ExpiredBot/1.0" for r in rows)


class TestSessionsRevoke:
    def test_member_cannot_revoke(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            # Admin lists, grabs Mia's session id.
            sessions = client.get("/api/sessions").json()
            mia_sess = next(s for s in sessions if s["user_email"] == "mia@x.test")
            r = mem.delete(f"/api/sessions/{mia_sess['id']}")
            assert r.status_code == 403

    def test_revoke_missing_session_404(self, client):
        _signup_admin(client)
        r = client.delete("/api/sessions/999999")
        assert r.status_code == 404

    def test_admin_cannot_revoke_own_current_session(self, client):
        _signup_admin(client)
        sessions = client.get("/api/sessions").json()
        my_sess = next(s for s in sessions if s["is_current"])
        r = client.delete(f"/api/sessions/{my_sess['id']}")
        assert r.status_code == 400

    def test_admin_revokes_other_user_session(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            # Mia is currently authed.
            assert mem.get("/api/auth/me").status_code == 200
            # Admin revokes Mia's session.
            sessions = client.get("/api/sessions").json()
            mia_sess = next(s for s in sessions if s["user_email"] == "mia@x.test")
            r = client.delete(f"/api/sessions/{mia_sess['id']}")
            assert r.status_code == 200
            # Mia's next request now fails (session row gone).
            assert mem.get("/api/auth/me").status_code == 401

    def test_cross_org_revoke_returns_404(self, two_orgs):
        c_a, c_b, _me_a, _me_b = two_orgs
        # Find Bob's session id from B's own admin panel.
        sessions_b = c_b.get("/api/sessions").json()
        assert sessions_b, "Bob's TestClient should have produced a session"
        bob_sess = sessions_b[0]
        # Org A's admin tries to revoke that id — should look like 404.
        r = c_a.delete(f"/api/sessions/{bob_sess['id']}")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# projects.py
# ---------------------------------------------------------------------------
class TestProjectsList:
    def test_empty_list_for_member_with_no_projects(self, client, make_invite):
        _signup_admin(client)
        tok = make_invite(client, "lone@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Lone Member", "password": PASS,
            })
            r = mem.get("/api/projects")
            assert r.status_code == 200
            assert r.json() == []

    def test_list_includes_member_count(self, client):
        _signup_admin(client)
        client.post("/api/projects", json={"name": "Web"})
        rows = client.get("/api/projects").json()
        assert len(rows) == 1
        # Admin creator is auto-added as a lead → member_count == 1.
        assert rows[0]["member_count"] == 1
        assert rows[0]["can_manage"] is True

    def test_requires_auth(self, client):
        assert client.get("/api/projects").status_code == 401


class TestProjectsCreate:
    def test_create_with_explicit_duplicate_key_409(self, client):
        _signup_admin(client)
        client.post("/api/projects", json={"name": "Alpha", "key": "DUP"})
        r = client.post("/api/projects", json={"name": "Bravo", "key": "DUP"})
        assert r.status_code == 409

    def test_auto_key_suffix_when_derived_key_collides(self, client):
        """Two different projects whose names derive the same base key
        (e.g. 'Marketing Site' and 'Mobile Service' both → 'MS') get
        unique keys via the numeric-suffix path in _unique_key."""
        _signup_admin(client)
        p1 = client.post("/api/projects", json={"name": "Marketing Site"}).json()
        p2 = client.post("/api/projects", json={"name": "Mobile Service"}).json()
        assert p1["key"] == "MS"
        assert p2["key"] != p1["key"]
        assert p2["key"].startswith("MS")


class TestProjectsGet:
    def test_get_cross_org_returns_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Hidden"}).json()
        r = c_a.get(f"/api/projects/{p_b['id']}")
        assert r.status_code == 404

    def test_get_non_member_returns_404(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Secret"}).json()
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            r = mem.get(f"/api/projects/{p['id']}")
            assert r.status_code == 404

    def test_get_one_includes_member_count(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.get(f"/api/projects/{p['id']}")
        assert r.status_code == 200
        assert r.json()["member_count"] == 1


class TestProjectsUpdate:
    def test_admin_can_update_name_and_color(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.put(f"/api/projects/{p['id']}", json={
            "name": "Marketing", "color": "#123456",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "Marketing"
        assert r.json()["color"] == "#123456"

    def test_member_cannot_update_project(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member",
                          project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            r = mem.put(f"/api/projects/{p['id']}", json={"name": "Hacked"})
            assert r.status_code == 403

    def test_update_with_new_unique_key(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.put(f"/api/projects/{p['id']}", json={
            "name": p["name"], "key": "NEWKEY",
        })
        assert r.status_code == 200
        assert r.json()["key"] == "NEWKEY"

    def test_update_with_conflicting_key_409(self, client):
        _signup_admin(client)
        p1 = client.post("/api/projects", json={"name": "Alpha", "key": "AK"}).json()
        p2 = client.post("/api/projects", json={"name": "Bravo", "key": "BK"}).json()
        r = client.put(f"/api/projects/{p2['id']}", json={
            "name": p2["name"], "key": "AK",
        })
        assert r.status_code == 409
        # p1 untouched.
        assert client.get(f"/api/projects/{p1['id']}").json()["key"] == "AK"

    def test_update_missing_returns_404(self, client):
        _signup_admin(client)
        r = client.put("/api/projects/999999", json={"name": "Whatever"})
        assert r.status_code == 404


class TestProjectsDelete:
    def test_lead_cannot_delete_project(self, client, make_invite):
        """can_delete_project is admin-only — leads can manage but
        not nuke the project."""
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "lead@x.test", role="member",
                          project_ids=[p["id"]], as_lead=True)
        with TestClient(__import__("app.main", fromlist=["app"]).app) as lead:
            lead.post("/api/invitations/accept", json={
                "token": tok, "name": "Liz Lead", "password": PASS,
            })
            r = lead.delete(f"/api/projects/{p['id']}")
            assert r.status_code == 403

    def test_delete_blocked_when_bugs_exist(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Some bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        })
        r = client.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 409
        # Project still exists.
        assert client.get(f"/api/projects/{p['id']}").status_code == 200

    def test_delete_empty_project_succeeds(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 200
        assert client.get(f"/api/projects/{p['id']}").status_code == 404

    def test_delete_missing_404(self, client):
        _signup_admin(client)
        r = client.delete("/api/projects/999999")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# memberships.py
# ---------------------------------------------------------------------------
class TestMembershipsList:
    def test_non_member_gets_404(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "lone@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Lone Member", "password": PASS,
            })
            r = mem.get(f"/api/projects/{p['id']}/members")
            assert r.status_code == 404

    def test_cross_org_returns_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Hidden"}).json()
        r = c_a.get(f"/api/projects/{p_b['id']}/members")
        assert r.status_code == 404

    def test_member_visible_to_project_member(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member",
                          project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            })
            rows = mem.get(f"/api/projects/{p['id']}/members").json()
            emails = {r["user_email"] for r in rows}
            assert {"ada@x.test", "mia@x.test"}.issubset(emails)
            # leads sort first
            assert rows[0]["project_role"] == "lead"


class TestMembershipsAdd:
    def test_add_unknown_user_400(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": 999999, "role": "member",
        })
        assert r.status_code == 400

    def test_add_cross_org_user_400(self, two_orgs):
        c_a, c_b, _me_a, me_b = two_orgs
        p_a = c_a.post("/api/projects", json={"name": "A1"}).json()
        # Alice trying to add Bob (different org) → unknown user.
        r = c_a.post(f"/api/projects/{p_a['id']}/members", json={
            "user_id": me_b["id"], "role": "member",
        })
        assert r.status_code == 400

    def test_add_inactive_user_400(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        client.put(f"/api/users/{me_mem['id']}", json={"is_active": False})
        r = client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": me_mem["id"], "role": "member",
        })
        assert r.status_code == 400

    def test_add_duplicate_409(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member")
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        # First add succeeds.
        r1 = client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": me_mem["id"], "role": "member",
        })
        assert r1.status_code == 201
        # Second add — already a member.
        r2 = client.post(f"/api/projects/{p['id']}/members", json={
            "user_id": me_mem["id"], "role": "member",
        })
        assert r2.status_code == 409

    def test_member_cannot_add_others(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok_a = make_invite(client, "mia@x.test", role="member",
                            project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok_a, "name": "Mia Member", "password": PASS,
            })
            # Another user we'll try to add.
            tok_b = make_invite(client, "bob@x.test", role="member")
            with TestClient(__import__("app.main", fromlist=["app"]).app) as bob:
                me_bob = bob.post("/api/invitations/accept", json={
                    "token": tok_b, "name": "Bob Bee", "password": PASS,
                }).json()
            r = mem.post(f"/api/projects/{p['id']}/members", json={
                "user_id": me_bob["id"], "role": "member",
            })
            assert r.status_code == 403


class TestMembershipsUpdate:
    def test_update_missing_404(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.put(f"/api/projects/{p['id']}/members/999999",
                       json={"role": "member"})
        assert r.status_code == 404

    def test_update_promotes_member_to_lead(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member",
                          project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        r = client.put(f"/api/projects/{p['id']}/members/{me_mem['id']}",
                       json={"role": "lead"})
        assert r.status_code == 200
        assert r.json()["project_role"] == "lead"

    def test_cannot_demote_last_lead(self, client):
        _signup_admin(client)
        me = client.get("/api/auth/me").json()
        p = client.post("/api/projects", json={"name": "Web"}).json()
        # Admin is the only lead → can't demote to member.
        r = client.put(f"/api/projects/{p['id']}/members/{me['id']}",
                       json={"role": "member"})
        assert r.status_code == 400

    def test_demote_after_other_lead_exists(self, client, make_invite):
        _signup_admin(client)
        me = client.get("/api/auth/me").json()
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "lead2@x.test", role="member",
                          project_ids=[p["id"]], as_lead=True)
        with TestClient(__import__("app.main", fromlist=["app"]).app) as lead2:
            lead2.post("/api/invitations/accept", json={
                "token": tok, "name": "Second Lead", "password": PASS,
            })
        # Now there's a second lead — demote the original admin.
        r = client.put(f"/api/projects/{p['id']}/members/{me['id']}",
                       json={"role": "member"})
        assert r.status_code == 200
        assert r.json()["project_role"] == "member"

    def test_cross_org_update_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Hidden"}).json()
        me_b = c_b.get("/api/auth/me").json()
        r = c_a.put(f"/api/projects/{p_b['id']}/members/{me_b['id']}",
                    json={"role": "member"})
        assert r.status_code == 404

    def test_member_cannot_update(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member",
                          project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
            r = mem.put(f"/api/projects/{p['id']}/members/{me_mem['id']}",
                        json={"role": "lead"})
            assert r.status_code == 403


class TestMembershipsRemove:
    def test_remove_missing_404(self, client):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        r = client.delete(f"/api/projects/{p['id']}/members/999999")
        assert r.status_code == 404

    def test_remove_member_works(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member",
                          project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
        r = client.delete(f"/api/projects/{p['id']}/members/{me_mem['id']}")
        assert r.status_code == 200
        rows = client.get(f"/api/projects/{p['id']}/members").json()
        emails = {r["user_email"] for r in rows}
        assert "mia@x.test" not in emails

    def test_cross_org_remove_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        p_b = c_b.post("/api/projects", json={"name": "Hidden"}).json()
        me_b = c_b.get("/api/auth/me").json()
        r = c_a.delete(f"/api/projects/{p_b['id']}/members/{me_b['id']}")
        assert r.status_code == 404

    def test_member_cannot_remove_others(self, client, make_invite):
        _signup_admin(client)
        p = client.post("/api/projects", json={"name": "Web"}).json()
        tok = make_invite(client, "mia@x.test", role="member",
                          project_ids=[p["id"]])
        with TestClient(__import__("app.main", fromlist=["app"]).app) as mem:
            me_mem = mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mia Member", "password": PASS,
            }).json()
            # Mia tries to remove herself → not a lead/admin → 403.
            r = mem.delete(f"/api/projects/{p['id']}/members/{me_mem['id']}")
            assert r.status_code == 403
