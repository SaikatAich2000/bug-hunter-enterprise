"""Role-policy and session-admin gates: admin-only delete (bugs/projects/users), manager
limits (no admin role, no delete), audit hidden from users, and session list/revoke rules.
"""
from __future__ import annotations


# Helpers duplicated from test_api so this file is self-contained.
def _make_user(client, name="Alice", email=None, role="user", password="TestUserPwd9X"):
    email = email or f"{name.lower()}@example.com"
    body = {"name": name, "email": email, "role": role, "password": password}
    # Tag to every project so non-admins see everything (untagged sees nothing).
    pids = [p["id"] for p in client.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = client.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(client, name="Mobile", color="#c9764f"):
    r = client.post("/api/projects", json={"name": name, "color": color})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **extra):
    body = {
        "title": "smoke bug",
        "project_id": project_id,
        "priority": "High",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _login(client, email, pw):
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text


def _logout(client):
    client.post("/api/auth/logout")


class TestBugDeletePermissions:
    def test_admin_can_delete_bug(self, admin_client):
        p = _make_project(admin_client, name="DelProj1")
        bug = _make_bug(admin_client, p["id"], title="kill me")
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 200

    def test_manager_cannot_delete_bug(self, admin_client):
        """Managers cannot delete a bug."""
        p = _make_project(admin_client, name="DelProj2")
        bug = _make_bug(admin_client, p["id"], title="manager must not delete me")
        _make_user(admin_client, name="Mgr", email="mgr1@x.com",
                   role="manager", password="Mgr1234567")
        _logout(admin_client)
        _login(admin_client, "mgr1@x.com", "Mgr1234567")
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 403
        assert "admin" in r.json()["detail"].lower()

    def test_user_cannot_delete_bug(self, admin_client):
        p = _make_project(admin_client, name="DelProj3")
        bug = _make_bug(admin_client, p["id"], title="user must not delete me")
        _make_user(admin_client, name="Bob", email="bob1@x.com",
                   role="user", password="Bob1234567")
        _logout(admin_client)
        _login(admin_client, "bob1@x.com", "Bob1234567")
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 403

    def test_user_cannot_delete_their_own_bug(self, admin_client):
        """Per spec: 'they cannot delete any bug, even the ones they created'."""
        p = _make_project(admin_client, name="DelProj4")
        _make_user(admin_client, name="Carol", email="carol1@x.com",
                   role="user", password="Carol12345")
        _logout(admin_client)
        _login(admin_client, "carol1@x.com", "Carol12345")
        bug = _make_bug(admin_client, p["id"], title="my own bug")
        # Reporter status doesn't grant delete rights.
        r = admin_client.delete(f"/api/bugs/{bug['id']}")
        assert r.status_code == 403


# Every user can edit any bug — edit/reassign is unrestricted.
class TestBugEditPermissions:
    def test_user_can_edit_someone_elses_bug(self, admin_client):
        p = _make_project(admin_client, name="EditProj1")
        bug = _make_bug(admin_client, p["id"], title="admin's bug")
        _make_user(admin_client, name="Dave", email="dave@x.com",
                   role="user", password="Dave12345")
        _logout(admin_client)
        _login(admin_client, "dave@x.com", "Dave12345")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "title": "Dave changed it",
            "priority": "Low",
            "status": "In Progress",
        })
        assert r.status_code == 200
        assert r.json()["title"] == "Dave changed it"
        assert r.json()["status"] == "In Progress"

    def test_user_can_reassign_bug(self, admin_client):
        """Spec: 'reassign any bug to anyone else'."""
        p = _make_project(admin_client, name="EditProj2")
        someone = _make_user(admin_client, name="Eve", email="eve@x.com",
                             role="user", password="Eve1234567")
        bug = _make_bug(admin_client, p["id"], title="reassign me")

        _make_user(admin_client, name="Frank", email="frank@x.com",
                   role="user", password="Frank12345")
        _logout(admin_client)
        _login(admin_client, "frank@x.com", "Frank12345")

        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "assignee_ids": [someone["id"]],
        })
        assert r.status_code == 200, r.text
        assignee_ids = [a["id"] for a in r.json()["assignees"]]
        assert someone["id"] in assignee_ids

    def test_can_edit_flag_is_true_for_user_on_any_bug(self, admin_client):
        p = _make_project(admin_client, name="EditProj3")
        bug = _make_bug(admin_client, p["id"], title="check can_edit")
        _make_user(admin_client, name="Grace", email="grace@x.com",
                   role="user", password="Grace12345")
        _logout(admin_client)
        _login(admin_client, "grace@x.com", "Grace12345")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["can_edit"] is True


class TestProjectDeletePermissions:
    def test_manager_cannot_delete_project(self, admin_client):
        """Project delete is admin-only; a manager is blocked."""
        p = _make_project(admin_client, name="MgrCannotDel")
        _make_user(admin_client, name="Mgr2", email="mgr2@x.com",
                   role="manager", password="Mgr2_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr2@x.com", "Mgr2_abc99")
        r = admin_client.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 403

    def test_manager_can_create_and_edit_project(self, admin_client):
        _make_user(admin_client, name="Mgr3", email="mgr3@x.com",
                   role="manager", password="Mgr3_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr3@x.com", "Mgr3_abc99")
        r = admin_client.post("/api/projects", json={"name": "MgrMade"})
        assert r.status_code == 201
        pid = r.json()["id"]
        r = admin_client.put(f"/api/projects/{pid}", json={
            "name": "MgrRenamed", "color": "#abcdef",
        })
        assert r.status_code == 200
        assert r.json()["name"] == "MgrRenamed"

    def test_user_cannot_create_or_edit_project(self, admin_client):
        p = _make_project(admin_client, name="UserCantTouch")
        _make_user(admin_client, name="Henry", email="henry@x.com",
                   role="user", password="Henry12345")
        _logout(admin_client)
        _login(admin_client, "henry@x.com", "Henry12345")
        r = admin_client.post("/api/projects", json={"name": "Forbidden"})
        assert r.status_code == 403
        r = admin_client.put(f"/api/projects/{p['id']}", json={"name": "X"})
        assert r.status_code == 403


# Manager can manage users but not delete, and cannot grant the admin role.
class TestUserManagementPermissions:
    def test_manager_can_create_and_edit_user(self, admin_client):
        _make_user(admin_client, name="Mgr4", email="mgr4@x.com",
                   role="manager", password="Mgr4_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr4@x.com", "Mgr4_abc99")

        r = admin_client.post("/api/users", json={
            "name": "MgrMade", "email": "mgrmade@x.com",
            "role": "user", "password": "Made12345",
        })
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]
        r = admin_client.put(f"/api/users/{new_id}", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_manager_cannot_create_admin(self, admin_client):
        _make_user(admin_client, name="Mgr5", email="mgr5@x.com",
                   role="manager", password="Mgr5_abc99")
        _logout(admin_client)
        _login(admin_client, "mgr5@x.com", "Mgr5_abc99")
        r = admin_client.post("/api/users", json={
            "name": "Sneaky", "email": "sneaky@x.com",
            "role": "admin", "password": "Sneaky1234",
        })
        assert r.status_code == 403

    def test_manager_cannot_promote_user_to_admin(self, admin_client):
        _make_user(admin_client, name="Mgr6", email="mgr6@x.com",
                   role="manager", password="Mgr6_abc99")
        target = _make_user(admin_client, name="Target", email="target@x.com",
                            role="user", password="Target1234")
        _logout(admin_client)
        _login(admin_client, "mgr6@x.com", "Mgr6_abc99")
        r = admin_client.put(f"/api/users/{target['id']}", json={"role": "admin"})
        assert r.status_code == 403

    def test_manager_cannot_edit_admin(self, admin_client):
        _make_user(admin_client, name="Mgr7", email="mgr7@x.com",
                   role="manager", password="Mgr7_abc99")
        admin_me = admin_client.get("/api/auth/me").json()
        _logout(admin_client)
        _login(admin_client, "mgr7@x.com", "Mgr7_abc99")
        r = admin_client.put(f"/api/users/{admin_me['id']}", json={"name": "Hijacked"})
        assert r.status_code == 403

    def test_manager_cannot_delete_user(self, admin_client):
        _make_user(admin_client, name="Mgr8", email="mgr8@x.com",
                   role="manager", password="Mgr8_abc99")
        target = _make_user(admin_client, name="Target2", email="t2@x.com",
                            role="user", password="Target1234")
        _logout(admin_client)
        _login(admin_client, "mgr8@x.com", "Mgr8_abc99")
        r = admin_client.delete(f"/api/users/{target['id']}")
        assert r.status_code == 403

    def test_user_cannot_create_user(self, user_client):
        r = user_client.post("/api/users", json={
            "name": "X", "email": "x@x.com",
            "role": "user", "password": "TestUserPwd9X",
        })
        assert r.status_code == 403


class TestAuditVisibility:
    def test_audit_403_for_user(self, user_client):
        r = user_client.get("/api/audit")
        assert r.status_code == 403

    def test_audit_200_for_manager(self, admin_client):
        _make_user(admin_client, name="MgrA", email="mgra@x.com",
                   role="manager", password="MgrA_abc99")
        _logout(admin_client)
        _login(admin_client, "mgra@x.com", "MgrA_abc99")
        r = admin_client.get("/api/audit")
        assert r.status_code == 200

    def test_audit_200_for_admin(self, admin_client):
        r = admin_client.get("/api/audit")
        assert r.status_code == 200


def _new_client():
    """Fresh TestClient with its own cookie jar — session tests needing independent cookies spin up their own."""
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)


class TestSessionsAdmin:
    def test_admin_login_creates_session_row(self, admin_client):
        r = admin_client.get("/api/sessions")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) >= 1
        assert any(row["is_current"] for row in rows)
        assert any(row.get("user_email") == "admin@test.local" for row in rows)
        for row in rows:
            assert "user_agent" in row
            assert "ip_address" in row

    def test_user_cannot_list_sessions(self, user_client):
        r = user_client.get("/api/sessions")
        assert r.status_code == 403

    def test_manager_cannot_list_sessions(self, admin_client):
        _make_user(admin_client, name="MgrS", email="mgrs@x.com",
                   role="manager", password="MgrS_abc99")
        _logout(admin_client)
        _login(admin_client, "mgrs@x.com", "MgrS_abc99")
        r = admin_client.get("/api/sessions")
        assert r.status_code == 403

    def test_user_cannot_revoke_session(self, admin_client):
        # Use a separate client so the user has their own cookie jar.
        _make_user(admin_client, name="UU", email="uu@x.com",
                   role="user", password="UU_abc1234")
        user_dev = _new_client()
        r = user_dev.post("/api/auth/login", json={
            "email": "uu@x.com", "password": "UU_abc1234",
        })
        assert r.status_code == 200

        r = admin_client.get("/api/sessions")
        user_rows = [s for s in r.json() if s.get("user_email") == "uu@x.com"]
        assert user_rows, "user uu@x.com should have an active session"
        sid = user_rows[0]["id"]

        r = user_dev.delete(f"/api/sessions/{sid}")
        assert r.status_code == 403

    def test_admin_can_revoke_user_session(self, admin_client):
        """Revoking a user's session row invalidates their cookie on the next request."""
        _make_user(admin_client, name="UV", email="uv@x.com",
                   role="user", password="UV_abc1234")
        user_dev = _new_client()
        r = user_dev.post("/api/auth/login", json={
            "email": "uv@x.com", "password": "UV_abc1234",
        })
        assert r.status_code == 200
        assert user_dev.get("/api/auth/me").status_code == 200

        r = admin_client.get("/api/sessions")
        user_rows = [s for s in r.json() if s.get("user_email") == "uv@x.com"]
        assert user_rows
        sid = user_rows[0]["id"]

        r = admin_client.delete(f"/api/sessions/{sid}")
        assert r.status_code == 200

        # Cookie should now be invalid.
        r = user_dev.get("/api/auth/me")
        assert r.status_code == 401

    def test_admin_cannot_revoke_their_own_current_session(self, admin_client):
        """Safety: the API rejects revoking the cookie you're using right now."""
        r = admin_client.get("/api/sessions")
        own = next(s for s in r.json() if s["is_current"])
        r = admin_client.delete(f"/api/sessions/{own['id']}")
        assert r.status_code == 400
        assert "log out" in r.json()["detail"].lower()

    def test_admin_can_revoke_their_own_OTHER_session(self, admin_client):
        """Admin logged in on two devices can revoke the other while keeping the current."""
        other_dev = _new_client()
        r = other_dev.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        assert r.status_code == 200

        r = admin_client.get("/api/sessions")
        admin_sessions = [s for s in r.json() if s.get("user_email") == "admin@test.local"]
        assert len(admin_sessions) == 2
        # The non-current session belongs to other_dev.
        other = next(s for s in admin_sessions if not s["is_current"])

        r = admin_client.delete(f"/api/sessions/{other['id']}")
        assert r.status_code == 200

        r = other_dev.get("/api/auth/me")
        assert r.status_code == 401

        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200

    def test_logout_removes_session_row(self, admin_client):
        """After logout the session row is deleted; logging back in creates a fresh one."""
        r = admin_client.get("/api/sessions")
        admin_sessions = [s for s in r.json() if s.get("user_email") == "admin@test.local"]
        assert len(admin_sessions) == 1

        admin_client.post("/api/auth/logout")
        # No active session, so the list endpoint should reject the request.
        r = admin_client.get("/api/sessions")
        assert r.status_code == 401

        admin_client.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        r = admin_client.get("/api/sessions")
        assert r.status_code == 200
        admin_sessions = [s for s in r.json() if s.get("user_email") == "admin@test.local"]
        assert len(admin_sessions) == 1, \
            f"expected exactly 1 admin session after logout/login, got {len(admin_sessions)}"


class TestPasswordChangeKeepsCurrentDevice:
    def test_change_password_keeps_current_session_works(self, admin_client):
        """After change-password the current cookie still authenticates (re-issued jti); other devices die on the session_version bump."""
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234",
            "new_password": "NewAdmin1234",
        })
        assert r.status_code == 204

        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200, r.text

        # No need to reset the password; conftest gives each fixture a fresh DB.
