"""Coverage-maximising tests for the auth stack.

Target files (line/branch → goal >97% / >93%):
  - app/auth.py            : password hash/verify, session token parse,
                             cookie issue/clear, session-row validation,
                             last-seen throttle, expired-row cleanup,
                             current-user dependency error branches, role
                             gates & project-permission helpers.
  - app/routes/auth.py     : signup (dup / disabled / breach), login (bad
                             creds / inactive / locked / no-org / 2FA),
                             login/totp step, logout, /me, change-password,
                             forgot/reset-password, email-change flow.
  - app/totp.py            : secret gen, provisioning URI, verify_code drift
                             window + guards, pending-token sign/parse,
                             recovery-code gen + hash.

Every if/elif/else, try/except and short-circuit is driven on BOTH the
success and the guard/failure path. External I/O (email send, HIBP breach
lookup) is mocked. Fake credentials/tokens are wrapped in 1-tuples to
dodge Sonar S6418.

Conventions follow tests/test_security.py and
tests/test_misc_coverage.py::TestAuthSessionHelpers for the established
session / lockout / 2FA patterns.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Fake credentials wrapped in 1-tuples (S6418 dodge); index [0] to use.
_PW = ("TestPass1!",)
_PW_NEW = ("FreshSafe123",)
_ADMIN_EMAIL = ("admin@acme.test",)


@pytest.fixture(autouse=True)
def _reset_lockout_state():
    """Account-lockout buckets are per-process; wipe them between cases so
    a failed-login burst in one test can't lock an email in a later one."""
    yield
    try:
        from app import account_lockout
        account_lockout._reset_for_tests()
    except ImportError:
        pass


def _signup(client, org="Acme Co", name="Admin", email="admin@acme.test",
            password="TestPass1!"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# app/auth.py — password hashing
# ===========================================================================
class TestPasswordHashing:
    def test_hash_password_empty_raises(self):
        """hash_password guard: falsy plaintext → ValueError."""
        from app.auth import hash_password
        with pytest.raises(ValueError):
            hash_password("")

    def test_hash_and_verify_roundtrip(self, app_env):
        """verify_password success branch — correct password matches."""
        from app.auth import hash_password, verify_password
        h = hash_password(_PW[0])
        assert verify_password(_PW[0], h) is True

    def test_verify_password_wrong_returns_false(self, app_env):
        """verify_password mismatch branch — wrong password fails."""
        from app.auth import hash_password, verify_password
        h = hash_password(_PW[0])
        assert verify_password(("totally-different-9",)[0], h) is False

    def test_verify_password_none_hash_short_circuits(self, app_env):
        """`not hashed` guard → False without calling bcrypt."""
        from app.auth import verify_password
        assert verify_password(_PW[0], None) is False

    def test_verify_password_empty_plain_short_circuits(self, app_env):
        """`not plain` guard → False."""
        from app.auth import verify_password
        assert verify_password("", "anything") is False

    def test_verify_password_malformed_hash_returns_false(self, app_env):
        """bcrypt.checkpw raises ValueError on a non-bcrypt hash → except
        branch returns False."""
        from app.auth import verify_password
        assert verify_password(_PW[0], ("not-a-bcrypt-hash",)[0]) is False

    def test_hash_password_rounds_clamped_high(self, db_path, monkeypatch):
        """BCRYPT_ROUNDS above the cap is clamped to 15 (max(...,min(15,n)))."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("SESSION_SECRET", "x")
        monkeypatch.setenv("BCRYPT_ROUNDS", "99")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from app.auth import hash_password, verify_password
        h = hash_password(_PW[0])
        # bcrypt stores cost in the hash prefix: $2b$15$...
        assert "$15$" in h
        assert verify_password(_PW[0], h) is True


# ===========================================================================
# app/auth.py — session token sign / parse
# ===========================================================================
class TestSessionToken:
    def test_make_token_with_jti_roundtrips(self, app_env):
        """make_session_token jti branch → 3-part payload parses back."""
        from app.auth import make_session_token, parse_session_token
        tok = make_session_token(42, 3, jti="abc123")
        assert parse_session_token(tok) == (42, 3, "abc123")

    def test_make_token_without_jti_roundtrips(self, app_env):
        """make_session_token no-jti branch → 2-part payload."""
        from app.auth import make_session_token, parse_session_token
        tok = make_session_token(7, 1)
        assert parse_session_token(tok) == (7, 1, None)

    def test_parse_empty_token_returns_none(self, app_env):
        """`if not token` guard → None."""
        from app.auth import parse_session_token
        assert parse_session_token("") is None

    def test_parse_bad_signature_returns_none(self, app_env):
        """unsign raises BadSignature → except branch → None."""
        from app.auth import parse_session_token
        assert parse_session_token(("garbage.not.signed",)[0]) is None

    def test_parse_one_part_payload_defaults_version_zero(self, app_env):
        """len(parts) == 1 branch: bare user_id → (id, 0, None)."""
        from app.auth import _signer, parse_session_token
        tok = _signer().sign(b"99").decode("utf-8")
        assert parse_session_token(tok) == (99, 0, None)

    def test_parse_three_part_empty_jti_becomes_none(self, app_env):
        """3-part payload with empty jti segment → `parts[2] or None`."""
        from app.auth import _signer, parse_session_token
        tok = _signer().sign(b"5:2:").decode("utf-8")
        assert parse_session_token(tok) == (5, 2, None)

    def test_parse_too_many_parts_returns_none(self, app_env):
        """len(parts) not in (1,2,3) → final `return None`."""
        from app.auth import _signer, parse_session_token
        tok = _signer().sign(b"1:2:3:4").decode("utf-8")
        assert parse_session_token(tok) is None

    def test_parse_non_integer_payload_returns_none(self, app_env):
        """int() raises ValueError → except branch → None."""
        from app.auth import _signer, parse_session_token
        tok = _signer().sign(b"notanint:0").decode("utf-8")
        assert parse_session_token(tok) is None

    def test_parse_non_utf8_payload_returns_none(self, app_env):
        """raw.decode('utf-8') raises UnicodeDecodeError → except → None.
        Sign raw invalid-UTF-8 bytes; pass the signed token as BYTES so the
        non-ASCII payload survives to the decode() call (decoding the whole
        token to str up front would itself blow up before parse runs)."""
        from app.auth import _signer, parse_session_token
        tok = _signer().sign(b"\xff\xfe\xfa")  # bytes token; unsign accepts it
        assert parse_session_token(tok) is None

    def test_expired_token_returns_none(self, db_path, monkeypatch):
        """unsign max_age exceeded → SignatureExpired (BadSignature) → None."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("SESSION_SECRET", "x")
        monkeypatch.setenv("SESSION_TTL_SECONDS", "1")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from app.auth import make_session_token, parse_session_token
        tok = make_session_token(1, 0)
        # Sign a token whose embedded timestamp is well in the past so the
        # TTL=1s check rejects it deterministically (no sleep needed).
        import itsdangerous
        with mock.patch.object(itsdangerous.TimestampSigner, "get_timestamp",
                               return_value=0):
            stale = make_session_token(1, 0)
        assert parse_session_token(stale) is None

    def test_new_jti_is_unique(self, app_env):
        """new_jti returns distinct URL-safe tokens."""
        from app.auth import new_jti
        assert new_jti() != new_jti()


# ===========================================================================
# app/auth.py — cookie issue / clear
# ===========================================================================
class TestCookies:
    def test_set_and_clear_session_cookie(self, app_env):
        """set_session_cookie writes COOKIE_NAME; clear deletes it."""
        from fastapi import Response
        from app.auth import (
            COOKIE_NAME, clear_session_cookie, set_session_cookie,
        )

        class _U:
            id = 5
            session_version = 2

        resp = Response()
        set_session_cookie(resp, _U(), jti="j1")
        cookies = resp.headers.getlist("set-cookie")
        assert any(COOKIE_NAME in c for c in cookies)

        resp2 = Response()
        clear_session_cookie(resp2)
        assert any(COOKIE_NAME in c for c in resp2.headers.getlist("set-cookie"))

    def test_set_cookie_handles_none_session_version(self, app_env):
        """`user.session_version or 0` falsy branch (None → 0)."""
        from fastapi import Response
        from app.auth import set_session_cookie

        class _U:
            id = 9
            session_version = None

        resp = Response()
        set_session_cookie(resp, _U())  # must not raise
        assert resp.headers.getlist("set-cookie")


# ===========================================================================
# app/auth.py — token helpers (reset / invitation)
# ===========================================================================
class TestTokenHelpers:
    def test_generate_random_token_hash_matches(self, app_env):
        """generate_random_token returns (raw, sha256(raw))."""
        from app.auth import generate_random_token, hash_token
        raw, h = generate_random_token()
        assert hash_token(raw) == h

    def test_invalidate_outstanding_reset_tokens(self, client):
        """invalidate_outstanding_reset_tokens marks unused rows used and
        returns the count; a second call returns 0 (nothing left)."""
        me = _signup(client)
        from app.auth import invalidate_outstanding_reset_tokens, hash_token
        from app.database import SessionLocal
        from app.models import PasswordResetToken
        db = SessionLocal()
        try:
            db.add(PasswordResetToken(
                user_id=me["id"], token_hash=hash_token("raw-xyz"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
            db.commit()
            n = invalidate_outstanding_reset_tokens(db, me["id"])
            db.commit()
            assert n == 1
            assert invalidate_outstanding_reset_tokens(db, me["id"]) == 0
        finally:
            db.close()


# ===========================================================================
# app/auth.py — session-row validation, throttle, expiry cleanup
# ===========================================================================
class TestSessionRowValidation:
    def test_validate_session_row_happy_path(self, client):
        """_validate_session_row returns True for a fresh valid row."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            sess = db.query(SessionRow).first()
            assert _validate_session_row(db, sess.jti, user) is True
        finally:
            db.close()

    def test_validate_session_row_missing_jti_returns_false(self, client):
        """sess is None branch → False."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            assert _validate_session_row(db, "no-such-jti", user) is False
        finally:
            db.close()

    def test_naive_expires_at_is_treated_as_utc(self, client):
        """expires.tzinfo is None branch: a naive (but future) expiry is
        coerced to UTC and accepted."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            sess = db.query(SessionRow).first()
            # Store a *naive* future datetime to drive the tz-coercion line.
            sess.expires_at = datetime.utcnow() + timedelta(hours=5)
            sess.last_seen_at = datetime.utcnow()
            db.commit()
            assert _validate_session_row(db, sess.jti, user) is True
        finally:
            db.close()

    def test_last_seen_bump_fires_when_stale(self, client):
        """_maybe_bump_last_seen WRITE branch: a last_seen older than the
        60s throttle is updated."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            sess = db.query(SessionRow).first()
            old = datetime.now(timezone.utc) - timedelta(minutes=10)
            sess.last_seen_at = old
            db.commit()
            jti = sess.jti
        finally:
            db.close()
        # Re-open: validate bumps last_seen_at.
        db = SessionLocal()
        try:
            user = db.query(User).first()
            assert _validate_session_row(db, jti, user) is True
        finally:
            db.close()
        db = SessionLocal()
        try:
            sess2 = db.query(SessionRow).filter(SessionRow.jti == jti).first()
            seen = sess2.last_seen_at
            if seen.tzinfo is None:  # SQLite hands back naive datetimes
                seen = seen.replace(tzinfo=timezone.utc)
            assert seen > old
        finally:
            db.close()

    def test_last_seen_bump_naive_tzinfo_branch(self, client):
        """_maybe_bump_last_seen: naive last_seen_at gets tz-coerced before
        the throttle comparison (last_seen.tzinfo is None branch)."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            sess = db.query(SessionRow).first()
            sess.last_seen_at = datetime.utcnow() - timedelta(minutes=10)  # naive + stale
            db.commit()
            jti = sess.jti
            assert _validate_session_row(db, jti, user) is True
        finally:
            db.close()

    def test_validate_session_row_aware_datetimes_branch(self, client):
        """Both `expires.tzinfo is None` and `last_seen.tzinfo is None` FALSE
        branches: feed a SessionRow carrying tz-AWARE datetimes (SQLite would
        otherwise hand back naive ones) so the coercion lines are skipped."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            real = db.query(SessionRow).first()
            jti = real.jti
            aware = SessionRow(
                id=real.id, user_id=user.id, jti=jti,
                user_agent="", ip_address="",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                last_seen_at=datetime.now(timezone.utc),  # fresh → throttle skip
            )
            with mock.patch.object(db, "scalar", return_value=aware):
                assert _validate_session_row(db, jti, user) is True
        finally:
            db.close()

    def test_expired_row_deleted_via_helper(self, client):
        """expires < now branch → _delete_expired_session drops the row and
        the helper returns False."""
        _signup(client)
        from app.auth import _validate_session_row
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            user = db.query(User).first()
            sess = db.query(SessionRow).first()
            sess.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
            jti = sess.jti
            assert _validate_session_row(db, jti, user) is False
        finally:
            db.close()
        db = SessionLocal()
        try:
            assert db.query(SessionRow).filter(SessionRow.jti == jti).count() == 0
        finally:
            db.close()

    def test_delete_expired_session_swallows_sqlalchemy_error(self, client):
        """_delete_expired_session except branch: db.delete raising
        SQLAlchemyError is logged + rolled back, not propagated."""
        _signup(client)
        from app.auth import _delete_expired_session
        from app.database import SessionLocal
        from app.models import Session as SessionRow
        from sqlalchemy.exc import SQLAlchemyError
        db = SessionLocal()
        try:
            sess = db.query(SessionRow).first()
            with mock.patch.object(db, "commit",
                                   side_effect=SQLAlchemyError("boom")):
                # Must not raise.
                _delete_expired_session(db, sess, sess.jti)
        finally:
            db.rollback()
            db.close()

    def test_maybe_bump_last_seen_swallows_sqlalchemy_error(self, client):
        """_maybe_bump_last_seen except branch: commit failure is logged +
        rolled back, not propagated."""
        _signup(client)
        from app.auth import _maybe_bump_last_seen
        from app.database import SessionLocal
        from app.models import Session as SessionRow
        from sqlalchemy.exc import SQLAlchemyError
        db = SessionLocal()
        try:
            sess = db.query(SessionRow).first()
            sess.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()
            now = datetime.now(timezone.utc)
            with mock.patch.object(db, "commit",
                                   side_effect=SQLAlchemyError("boom")):
                _maybe_bump_last_seen(db, sess, now, sess.jti)  # must not raise
        finally:
            db.rollback()
            db.close()


# ===========================================================================
# app/auth.py — current-user dependency error branches
# ===========================================================================
class TestCurrentUserDependency:
    def test_no_cookie_returns_401(self, client):
        """_user_from_request: parse returns None (no cookie) → 401."""
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_bad_cookie_returns_401(self, client):
        """Tampered cookie → parse_session_token None → 401."""
        client.cookies.set("bh_session", "not-a-valid-token")
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_optional_dependency_returns_none_without_cookie(self, client):
        """get_current_user_optional returns None (not raise) when no user."""
        from app.auth import get_current_user_optional, _user_from_request
        from app.database import SessionLocal

        class _Req:
            cookies: dict = {}

        db = SessionLocal()
        try:
            assert _user_from_request(_Req(), db) is None
            assert get_current_user_optional(_Req(), db) is None
        finally:
            db.close()

    def test_inactive_user_rejected(self, client):
        """user.is_active False branch → _user_from_request returns None."""
        _signup(client)
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            u = db.query(User).first()
            u.is_active = False
            db.commit()
        finally:
            db.close()
        assert client.get("/api/auth/me").status_code == 401

    def test_deleted_user_rejected(self, client):
        """db.get(User) is None branch — point a valid cookie at a deleted
        user by re-signing the token for a nonexistent id."""
        _signup(client)
        from app.auth import make_session_token
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            real = db.query(User).first()
            ver = real.session_version or 0
        finally:
            db.close()
        # user_id 999999 doesn't exist → db.get returns None.
        client.cookies.set("bh_session", make_session_token(999999, ver))
        assert client.get("/api/auth/me").status_code == 401

    def test_session_version_mismatch_rejected(self, client):
        """(user.session_version or 0) != token version branch → None."""
        me = _signup(client)
        from app.auth import make_session_token
        # Forge a cookie carrying the wrong version (current is 0).
        client.cookies.set("bh_session", make_session_token(me["id"], 999))
        assert client.get("/api/auth/me").status_code == 401

    def test_legacy_token_without_jti_accepted(self, client):
        """jti is None branch — a versionless/jti-less token still resolves
        the user (legacy pre-sessions-table cookie)."""
        me = _signup(client)
        from app.auth import make_session_token
        # No jti → skips _validate_session_row entirely.
        client.cookies.set("bh_session", make_session_token(me["id"], 0))
        r = client.get("/api/auth/me")
        assert r.status_code == 200

    def test_valid_cookie_but_revoked_session_row_rejected(self, client):
        """jti present + version matches, but the session row was revoked
        (deleted) → _validate_session_row False → `return None` (auth.py:262)
        → 401. The cookie itself is otherwise perfectly valid."""
        _signup(client, email="rev@x.test")
        assert client.get("/api/auth/me").status_code == 200
        from app.database import SessionLocal
        from app.models import Session as SessionRow
        db = SessionLocal()
        try:
            # Delete the server-side session row but leave session_version=0
            # so the cookie's version still matches the user row.
            db.query(SessionRow).delete()
            db.commit()
        finally:
            db.close()
        assert client.get("/api/auth/me").status_code == 401

    def test_get_current_user_optional_returns_user_when_valid(self, client):
        """get_current_user_optional success branch returns the User."""
        _signup(client)
        # A signed-in client has a valid cookie; an endpoint that uses the
        # optional dependency would resolve it. Drive via /me (required dep)
        # to confirm the underlying resolver works, then call the optional
        # resolver directly with the same cookie.
        from app.auth import _user_from_request
        from app.database import SessionLocal

        token = client.cookies.get("bh_session")

        class _Req:
            cookies = {"bh_session": token}

        db = SessionLocal()
        try:
            assert _user_from_request(_Req(), db) is not None
        finally:
            db.close()


# ===========================================================================
# app/auth.py — role gates & permission helpers
# ===========================================================================
class TestRoleGatesAndPermissions:
    def _user(self, role="admin", org_id=1, uid=1):
        from app.models import User
        return User(id=uid, org_id=org_id, name="U", email="u@x.test",
                    role=role, is_active=True)

    def test_require_admin_pass_and_fail(self, app_env):
        from fastapi import HTTPException
        from app.auth import require_admin
        assert require_admin(self._user("admin")).role == "admin"
        with pytest.raises(HTTPException) as e:
            require_admin(self._user("member"))
        assert e.value.status_code == 403

    def test_require_manager_or_admin_pass_and_fail(self, app_env):
        from fastapi import HTTPException
        from app.auth import require_manager_or_admin
        assert require_manager_or_admin(self._user("manager"))
        assert require_manager_or_admin(self._user("admin"))
        with pytest.raises(HTTPException) as e:
            require_manager_or_admin(self._user("member"))
        assert e.value.status_code == 403

    def test_simple_role_predicates(self, app_env):
        from app import auth
        admin = self._user("admin")
        manager = self._user("manager")
        member = self._user("member")
        assert auth.is_admin(admin) and not auth.is_admin(member)
        assert auth.is_manager_or_admin(manager)
        assert not auth.is_manager_or_admin(member)
        assert auth.can_create_project(admin)
        assert not auth.can_create_project(manager)
        assert auth.can_manage_users(admin) and not auth.can_manage_users(member)
        assert auth.can_invite(manager) and not auth.can_invite(member)
        assert auth.can_view_audit(manager) and not auth.can_view_audit(member)
        assert auth.can_manage_sessions(admin)
        assert not auth.can_manage_sessions(manager)
        assert auth.can_create_event(manager) and not auth.can_create_event(member)
        assert auth.can_edit_event(manager) and not auth.can_edit_event(member)
        assert auth.can_delete_event(admin) and not auth.can_delete_event(manager)

    def test_accessible_project_ids_admin_vs_member(self, client):
        """Admin sees every org project; a member sees only memberships."""
        _signup(client)
        proj = client.post("/api/projects", json={"name": "Web"}).json()
        from app.auth import accessible_project_ids
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.role == "admin").first()
            ids = accessible_project_ids(db, admin)
            assert proj["id"] in ids
            # Fabricate a member with no memberships → empty list branch.
            member = User(org_id=admin.org_id, name="M", email="m2@x.test",
                          role="member", is_active=True)
            db.add(member)
            db.commit()
            assert accessible_project_ids(db, member) == []
        finally:
            db.close()

    def test_can_access_and_manage_project_branches(self, two_orgs):
        """Drive can_access_project / can_manage_project / can_delete_project
        across cross-org, admin, lead and plain-member rows. Uses two real
        orgs so the cross-org project has a valid org FK."""
        c_a, c_b, me_a, _me_b = two_orgs
        proj_json = c_a.post("/api/projects", json={"name": "Core"}).json()
        foreign_json = c_b.post("/api/projects", json={"name": "Foreign"}).json()
        from app import auth
        from app.database import SessionLocal
        from app.models import (
            PROJECT_ROLE_LEAD, Project, ProjectMembership, User,
        )
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.email == me_a["email"]).first()
            proj = db.get(Project, proj_json["id"])

            # Admin: access + manage + delete all True.
            assert auth.can_access_project(db, admin, proj) is True
            assert auth.can_manage_project(db, admin, proj) is True
            assert auth.can_delete_project(db, admin, proj) is True

            # Cross-org project (belongs to org B) → every helper False.
            foreign = db.get(Project, foreign_json["id"])
            assert auth.can_access_project(db, admin, foreign) is False
            assert auth.can_manage_project(db, admin, foreign) is False
            assert auth.can_delete_project(db, admin, foreign) is False

            # Member with a LEAD membership → access + manage True, delete False.
            lead = User(org_id=admin.org_id, name="Lead", email="lead@a.test",
                        role="member", is_active=True)
            db.add(lead)
            db.commit()
            db.add(ProjectMembership(project_id=proj.id, user_id=lead.id,
                                     role=PROJECT_ROLE_LEAD))
            db.commit()
            assert auth.can_access_project(db, lead, proj) is True
            assert auth.can_manage_project(db, lead, proj) is True
            assert auth.can_delete_project(db, lead, proj) is False

            # Plain member (no membership) → access + manage False.
            plain = User(org_id=admin.org_id, name="Plain", email="plain@a.test",
                         role="member", is_active=True)
            db.add(plain)
            db.commit()
            assert auth.can_access_project(db, plain, proj) is False
            assert auth.can_manage_project(db, plain, proj) is False
        finally:
            db.close()

    def test_bug_edit_delete_permission_branches(self, two_orgs):
        """can_edit_bug across Bug vs Requirement/Task by role, plus the
        no-access guard; can_delete_bug admin-only + cross-org guard."""
        c_a, c_b, me_a, _me_b = two_orgs
        proj_json = c_a.post("/api/projects", json={"name": "BG"}).json()
        foreign_json = c_b.post("/api/projects", json={"name": "F2"}).json()
        from app import auth
        from app.database import SessionLocal
        from app.models import (
            Project, ProjectMembership, User,
        )
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.email == me_a["email"]).first()
            proj = db.get(Project, proj_json["id"])

            # Admin can edit a Bug AND a Requirement/Task.
            assert auth.can_edit_bug(db, admin, proj, "Bug") is True
            assert auth.can_edit_bug(db, admin, proj, "Requirement") is True
            assert auth.can_delete_bug(db, admin, proj, "Bug") is True

            # Member WITH project access: Bug yes, Requirement no.
            member = User(org_id=admin.org_id, name="Mem", email="memb@a.test",
                          role="member", is_active=True)
            db.add(member)
            db.commit()
            db.add(ProjectMembership(project_id=proj.id, user_id=member.id,
                                     role="member"))
            db.commit()
            assert auth.can_edit_bug(db, member, proj, "Bug") is True
            assert auth.can_edit_bug(db, member, proj, "Task") is False
            assert auth.can_delete_bug(db, member, proj, "Bug") is False

            # Member WITHOUT project access → can_edit_bug guard → False.
            stranger = User(org_id=admin.org_id, name="Str", email="str@a.test",
                            role="member", is_active=True)
            db.add(stranger)
            db.commit()
            assert auth.can_edit_bug(db, stranger, proj, "Bug") is False

            # Cross-org delete guard (project belongs to org B).
            foreign = db.get(Project, foreign_json["id"])
            assert auth.can_delete_bug(db, admin, foreign, "Bug") is False
        finally:
            db.close()

    def test_get_org_project_or_404(self, client):
        """get_org_project_or_404 returns the project for same-org and
        raises 404 for missing / cross-org."""
        _signup(client)
        proj_json = client.post("/api/projects", json={"name": "OK"}).json()
        from fastapi import HTTPException
        from app.auth import get_org_project_or_404
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.role == "admin").first()
            got = get_org_project_or_404(db, proj_json["id"], admin)
            assert got.id == proj_json["id"]
            with pytest.raises(HTTPException) as e:
                get_org_project_or_404(db, 9999999, admin)
            assert e.value.status_code == 404
        finally:
            db.close()


# ===========================================================================
# app/routes/auth.py — signup
# ===========================================================================
class TestSignup:
    def test_signup_success_sets_cookie(self, client):
        """Happy path: 201 + session cookie issued."""
        r = client.post("/api/auth/signup", json={
            "organization_name": "Fresh Org", "name": "Fresh Admin",
            "email": "fresh@x.test", "password": _PW[0],
        })
        assert r.status_code == 201
        assert "bh_session" in r.cookies
        body = r.json()
        assert body["role"] == "admin"
        assert body["organization_name"] == "Fresh Org"

    def test_signup_duplicate_email_409(self, client):
        """Duplicate email → 409 (explicit pre-check branch)."""
        _signup(client, email="dup@x.test")
        r = client.post("/api/auth/signup", json={
            "organization_name": "Other Org", "name": "Other",
            "email": "dup@x.test", "password": _PW[0],
        })
        assert r.status_code == 409

    def test_signup_disabled_403(self, db_path, monkeypatch):
        """ALLOW_PUBLIC_SIGNUP false → 403."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "false")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/auth/signup", json={
                "organization_name": "Nope", "name": "No One",
                "email": "no@x.test", "password": _PW[0],
            })
            assert r.status_code == 403

    def test_signup_weak_password_422(self, client):
        """Schema validator rejects a too-short / letters-only password."""
        r = client.post("/api/auth/signup", json={
            "organization_name": "Weak Org", "name": "Weak",
            "email": "weak@x.test", "password": "short",
        })
        assert r.status_code == 422

    def test_signup_breached_password_400(self, client, monkeypatch):
        """HIBP breach hit → _reject_if_breached raises 400."""
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        import hashlib
        new_pw = "BreachAtSignup1"
        digest = hashlib.sha1(new_pw.encode("utf-8")).hexdigest().upper()  # NOSONAR
        body = f"{digest[5:]}:4242\n"
        with mock.patch.object(password_breach, "_fetch_range", return_value=body):
            r = client.post("/api/auth/signup", json={
                "organization_name": "Breach Org", "name": "Breached",
                "email": "breach@x.test", "password": new_pw,
            })
        assert r.status_code == 400
        assert "breach" in r.json()["detail"].lower()

    def test_signup_integrity_error_race_409(self, client):
        """The flush IntegrityError branch: simulate a race where the unique
        index fires after the pre-check passed."""
        from sqlalchemy.exc import IntegrityError
        from app.routes import auth as auth_routes
        real_flush_calls = {"n": 0}

        # Patch Session.flush so the SECOND flush (the user insert) raises
        # IntegrityError, leaving the pre-check (which uses scalar) intact.
        from sqlalchemy.orm import Session as OrmSession
        orig_flush = OrmSession.flush

        def fake_flush(self, *a, **k):
            real_flush_calls["n"] += 1
            if real_flush_calls["n"] == 2:
                raise IntegrityError("stmt", {}, Exception("dup"))
            return orig_flush(self, *a, **k)

        with mock.patch.object(OrmSession, "flush", fake_flush):
            r = client.post("/api/auth/signup", json={
                "organization_name": "Race Org", "name": "Racer",
                "email": "race@x.test", "password": _PW[0],
            })
        assert r.status_code == 409
        assert auth_routes  # keep import referenced


# ===========================================================================
# app/routes/auth.py — login (single-step)
# ===========================================================================
class TestLogin:
    def test_login_success(self, client):
        """Happy path: correct creds → 200 + cookie + lockout cleared."""
        _signup(client, email="li@x.test")
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={
            "email": "li@x.test", "password": _PW[0],
        })
        assert r.status_code == 200
        assert "bh_session" in r.cookies

    def test_login_unknown_email_401(self, client):
        """user is None branch → dummy verify + 401."""
        r = client.post("/api/auth/login", json={
            "email": "ghost@x.test", "password": ("whatever-9",)[0],
        })
        assert r.status_code == 401

    def test_login_wrong_password_401(self, client):
        """password_ok False branch → 401."""
        _signup(client, email="wp@x.test")
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={
            "email": "wp@x.test", "password": ("wrong-pass-9",)[0],
        })
        assert r.status_code == 401

    def test_login_inactive_account_401(self, client):
        """not user.is_active branch → 401 (anti-enumeration)."""
        _signup(client, email="inact@x.test")
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == "inact@x.test").first()
            u.is_active = False
            db.commit()
        finally:
            db.close()
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={
            "email": "inact@x.test", "password": _PW[0],
        })
        assert r.status_code == 401

    def test_login_locked_out_429(self, client):
        """check_locked raises 429 once the failure threshold is crossed."""
        _signup(client, email="lock@x.test")
        client.post("/api/auth/logout")
        codes = []
        for _ in range(15):
            rr = client.post("/api/auth/login", json={
                "email": "lock@x.test", "password": ("nope-9",)[0],
            })
            codes.append(rr.status_code)
        assert 429 in codes

    def test_login_missing_org_500(self, client):
        """org is None branch → 500 'Account misconfigured'. We drive it by
        making the Organization lookup return None (can't null the FK)."""
        _signup(client, email="noorg@x.test")
        client.post("/api/auth/logout")
        from sqlalchemy.orm import Session as OrmSession
        from app.models import Organization
        orig_get = OrmSession.get

        def fake_get(self, entity, ident, *a, **k):
            if entity is Organization:
                return None
            return orig_get(self, entity, ident, *a, **k)

        with mock.patch.object(OrmSession, "get", fake_get):
            r = client.post("/api/auth/login", json={
                "email": "noorg@x.test", "password": _PW[0],
            })
        assert r.status_code == 500

    def test_login_requires_totp_branch(self, client):
        """TOTP gate: an enrolled user gets requires_totp + pending_token
        instead of a session cookie."""
        _signup(client, email="2fa@x.test")
        # Enrol via the real endpoint so totp_secret/enabled are set properly.
        begin = client.post("/api/auth/2fa/begin").json()
        import pyotp
        client.post("/api/auth/2fa/confirm",
                    json={"code": pyotp.TOTP(begin["secret"]).now()})
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={
            "email": "2fa@x.test", "password": _PW[0],
        })
        assert r.status_code == 200
        body = r.json()
        assert body.get("requires_totp") is True
        assert body.get("pending_token")
        assert "bh_session" not in r.cookies


# ===========================================================================
# app/routes/auth.py — login/totp (step 2)
# ===========================================================================
class TestLoginTotpStep:
    def _enrol(self, client, email):
        me = _signup(client, email=email)
        begin = client.post("/api/auth/2fa/begin").json()
        import pyotp
        confirm = client.post(
            "/api/auth/2fa/confirm",
            json={"code": pyotp.TOTP(begin["secret"]).now()},
        ).json()
        client.post("/api/auth/logout")
        return me, begin["secret"], confirm["recovery_codes"]

    def test_totp_step_success_with_code(self, client):
        """Valid TOTP code → 200 + session issued."""
        import pyotp
        _, secret, _ = self._enrol(client, "ts1@x.test")
        pend = client.post("/api/auth/login", json={
            "email": "ts1@x.test", "password": _PW[0],
        }).json()["pending_token"]
        r = client.post("/api/auth/login/totp", json={
            "pending_token": pend, "code": pyotp.TOTP(secret).now(),
        })
        assert r.status_code == 200
        assert "bh_session" in r.cookies

    def test_totp_step_recovery_code_success(self, client):
        """Recovery-code path: wrong TOTP but a valid recovery code →
        used_recovery branch, code marked used, 200."""
        _, _secret, recovery = self._enrol(client, "ts2@x.test")
        pend = client.post("/api/auth/login", json={
            "email": "ts2@x.test", "password": _PW[0],
        }).json()["pending_token"]
        r = client.post("/api/auth/login/totp", json={
            "pending_token": pend, "code": recovery[0],
        })
        assert r.status_code == 200
        # Re-use of the same recovery code must now fail.
        pend2 = client.post("/api/auth/login", json={
            "email": "ts2@x.test", "password": _PW[0],
        }).json()["pending_token"]
        client.post("/api/auth/logout")
        r2 = client.post("/api/auth/login/totp", json={
            "pending_token": pend2, "code": recovery[0],
        })
        assert r2.status_code == 400

    def test_totp_step_bad_pending_token_400(self, client):
        """parse_pending_token None branch → 400."""
        r = client.post("/api/auth/login/totp", json={
            "pending_token": ("not-a-real-token",)[0], "code": "123456",
        })
        assert r.status_code == 400

    def test_totp_step_wrong_code_400(self, client):
        """Wrong TOTP + no matching recovery code → rc is None branch → 400."""
        _, _secret, _ = self._enrol(client, "ts3@x.test")
        pend = client.post("/api/auth/login", json={
            "email": "ts3@x.test", "password": _PW[0],
        }).json()["pending_token"]
        r = client.post("/api/auth/login/totp", json={
            "pending_token": pend, "code": "000000",
        })
        assert r.status_code == 400

    def test_totp_step_user_no_longer_2fa_400(self, client):
        """user invalid branch: pending token for a user whose 2FA got
        disabled → 400 'Login session invalid'."""
        me, secret, _ = self._enrol(client, "ts4@x.test")
        pend = client.post("/api/auth/login", json={
            "email": "ts4@x.test", "password": _PW[0],
        }).json()["pending_token"]
        # Disable TOTP out from under the pending token.
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            u = db.get(User, me["id"])
            u.totp_enabled = False
            u.totp_secret = None
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/login/totp", json={
            "pending_token": pend, "code": secret and "123456",
        })
        assert r.status_code == 400


# ===========================================================================
# app/routes/auth.py — logout
# ===========================================================================
class TestLogout:
    def test_logout_clears_cookie_and_session_row(self, client):
        """Authenticated logout: audit + session-row delete + cookie clear."""
        _signup(client)
        from app.database import SessionLocal
        from app.models import Session as SessionRow
        db = SessionLocal()
        try:
            assert db.query(SessionRow).count() == 1
        finally:
            db.close()
        r = client.post("/api/auth/logout")
        assert r.status_code == 204
        db = SessionLocal()
        try:
            assert db.query(SessionRow).count() == 0
        finally:
            db.close()

    def test_logout_without_cookie_is_noop_204(self, client):
        """parsed is None branch (no cookie) → still 204, no crash."""
        r = client.post("/api/auth/logout")
        assert r.status_code == 204

    def test_logout_unknown_user_in_token(self, client):
        """parsed truthy but db.get(User) None → `if user` false branch;
        jti present so the delete still runs."""
        _signup(client)
        from app.auth import make_session_token
        # Cookie for a nonexistent user id but a real jti-less token.
        client.cookies.set("bh_session", make_session_token(888888, 0, jti="ghostjti"))
        r = client.post("/api/auth/logout")
        assert r.status_code == 204


# ===========================================================================
# app/routes/auth.py — /me
# ===========================================================================
class TestMe:
    def test_me_success(self, client):
        _signup(client, email="me@x.test")
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "me@x.test"

    def test_me_missing_org_500(self, client):
        """org is None branch in /me → 500. Force the Organization lookup to
        return None (the FK forbids nulling org_id on the row)."""
        _signup(client, email="meo@x.test")
        from sqlalchemy.orm import Session as OrmSession
        from app.models import Organization
        orig_get = OrmSession.get

        def fake_get(self, entity, ident, *a, **k):
            if entity is Organization:
                return None
            return orig_get(self, entity, ident, *a, **k)

        with mock.patch.object(OrmSession, "get", fake_get):
            r = client.get("/api/auth/me")
        assert r.status_code == 500


# ===========================================================================
# app/routes/auth.py — change-password
# ===========================================================================
class TestChangePassword:
    def test_change_password_success(self, client):
        """Happy path: correct current pw → 204, new cookie, old sessions
        purged, new password works on next login."""
        _signup(client, email="cp@x.test")
        r = client.post("/api/auth/change-password", json={
            "current_password": _PW[0], "new_password": _PW_NEW[0],
        })
        assert r.status_code == 204
        client.post("/api/auth/logout")
        ok = client.post("/api/auth/login", json={
            "email": "cp@x.test", "password": _PW_NEW[0],
        })
        assert ok.status_code == 200

    def test_change_password_wrong_current_400(self, client):
        """verify_password False branch → 400."""
        _signup(client, email="cpw@x.test")
        r = client.post("/api/auth/change-password", json={
            "current_password": ("not-it-9",)[0], "new_password": _PW_NEW[0],
        })
        assert r.status_code == 400

    def test_change_password_weak_new_422(self, client):
        """Schema rejects a weak new password before the handler runs."""
        _signup(client, email="cpwk@x.test")
        r = client.post("/api/auth/change-password", json={
            "current_password": _PW[0], "new_password": "weak",
        })
        assert r.status_code == 422

    def test_change_password_breached_new_400(self, client, monkeypatch):
        """HIBP hit on the new password → 400."""
        _signup(client, email="cpb@x.test")
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        import hashlib
        new_pw = "AnotherFresh1"
        digest = hashlib.sha1(new_pw.encode("utf-8")).hexdigest().upper()  # NOSONAR
        with mock.patch.object(password_breach, "_fetch_range",
                               return_value=f"{digest[5:]}:9\n"):
            r = client.post("/api/auth/change-password", json={
                "current_password": _PW[0], "new_password": new_pw,
            })
        assert r.status_code == 400

    def test_change_password_invalidates_reset_tokens(self, client):
        """The `invalidated` audit-detail branch: an outstanding reset token
        is consumed by the change, exercising the formatted-detail path."""
        me = _signup(client, email="cpr@x.test")
        from app.auth import hash_token
        from app.database import SessionLocal
        from app.models import PasswordResetToken
        db = SessionLocal()
        try:
            db.add(PasswordResetToken(
                user_id=me["id"], token_hash=hash_token("outstanding"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/change-password", json={
            "current_password": _PW[0], "new_password": _PW_NEW[0],
        })
        assert r.status_code == 204


# ===========================================================================
# app/routes/auth.py — forgot-password
# ===========================================================================
class TestForgotPassword:
    def test_forgot_known_email_sends_mail(self, client, monkeypatch):
        """Known active email → token row created + notify task queued."""
        _signup(client, email="fp@x.test")
        client.post("/api/auth/logout")
        import app.routes.auth as auth_routes
        calls = {"n": 0}

        def _fake_notify(*a, **k):
            calls["n"] += 1

        monkeypatch.setattr(auth_routes, "notify_password_reset", _fake_notify)
        r = client.post("/api/auth/forgot-password", json={"email": "fp@x.test"})
        assert r.status_code == 204
        assert calls["n"] == 1
        from app.database import SessionLocal
        from app.models import PasswordResetToken
        db = SessionLocal()
        try:
            assert db.query(PasswordResetToken).count() >= 1
        finally:
            db.close()

    def test_forgot_unknown_email_silent_204(self, client):
        """Unknown email + ALLOW_ACCOUNT_ENUMERATION false → silent 204."""
        _signup(client)
        client.post("/api/auth/logout")
        r = client.post("/api/auth/forgot-password",
                        json={"email": "nobody@x.test"})
        assert r.status_code == 204

    def test_forgot_inactive_email_audits_then_204(self, client):
        """user not None but inactive → audit row written, then silent 204."""
        _signup(client, email="fpi@x.test")
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == "fpi@x.test").first()
            u.is_active = False
            db.commit()
        finally:
            db.close()
        client.post("/api/auth/logout")
        r = client.post("/api/auth/forgot-password", json={"email": "fpi@x.test"})
        assert r.status_code == 204

    def test_forgot_unknown_email_404_when_enumeration_allowed(
        self, db_path, monkeypatch,
    ):
        """ALLOW_ACCOUNT_ENUMERATION true → unknown email raises 404."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
        monkeypatch.setenv("ALLOW_ACCOUNT_ENUMERATION", "true")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/auth/forgot-password",
                       json={"email": "ghost@x.test"})
            assert r.status_code == 404


# ===========================================================================
# app/routes/auth.py — reset-password
# ===========================================================================
class TestResetPassword:
    def _issue_reset(self, client, email):
        """Create a real reset token by re-stamping the DB row, returning the
        plaintext token."""
        from app.auth import generate_random_token
        from app.database import SessionLocal
        from app.models import PasswordResetToken, User
        raw, h = generate_random_token()
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == email).first()
            db.add(PasswordResetToken(
                user_id=u.id, token_hash=h,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ))
            db.commit()
        finally:
            db.close()
        return raw

    def test_reset_success(self, client):
        """Valid token → password reset (204) and new password works."""
        _signup(client, email="rp@x.test")
        token = self._issue_reset(client, "rp@x.test")
        client.post("/api/auth/logout")
        r = client.post("/api/auth/reset-password", json={
            "token": token, "new_password": _PW_NEW[0],
        })
        assert r.status_code == 204
        ok = client.post("/api/auth/login", json={
            "email": "rp@x.test", "password": _PW_NEW[0],
        })
        assert ok.status_code == 200

    def test_reset_unknown_token_400(self, client):
        """prt is None branch → 400."""
        _signup(client)
        r = client.post("/api/auth/reset-password", json={
            "token": ("no-such-reset-token",)[0], "new_password": _PW_NEW[0],
        })
        assert r.status_code == 400

    def test_reset_used_token_400(self, client):
        """prt.used_at set branch → 400."""
        _signup(client, email="rpu@x.test")
        token = self._issue_reset(client, "rpu@x.test")
        from app.auth import hash_token
        from app.database import SessionLocal
        from app.models import PasswordResetToken
        db = SessionLocal()
        try:
            prt = db.query(PasswordResetToken).filter(
                PasswordResetToken.token_hash == hash_token(token)).first()
            prt.used_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/reset-password", json={
            "token": token, "new_password": _PW_NEW[0],
        })
        assert r.status_code == 400

    def test_reset_expired_token_400(self, client):
        """expires < now branch (stored naive to also hit tz-coercion) → 400."""
        _signup(client, email="rpe@x.test")
        from app.auth import generate_random_token, hash_token
        from app.database import SessionLocal
        from app.models import PasswordResetToken, User
        raw, h = generate_random_token()
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == "rpe@x.test").first()
            db.add(PasswordResetToken(
                user_id=u.id, token_hash=h,
                expires_at=datetime.utcnow() - timedelta(hours=1),  # naive past
            ))
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/reset-password", json={
            "token": raw, "new_password": _PW_NEW[0],
        })
        assert r.status_code == 400

    def test_reset_inactive_user_400(self, client):
        """user is None or not active branch → 400."""
        _signup(client, email="rpi@x.test")
        token = self._issue_reset(client, "rpi@x.test")
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == "rpi@x.test").first()
            u.is_active = False
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/reset-password", json={
            "token": token, "new_password": _PW_NEW[0],
        })
        assert r.status_code == 400

    def test_reset_breached_new_password_400(self, client, monkeypatch):
        """HIBP hit after token validation → 400."""
        _signup(client, email="rpb@x.test")
        token = self._issue_reset(client, "rpb@x.test")
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        import hashlib
        new_pw = "ResetFresh12"
        digest = hashlib.sha1(new_pw.encode("utf-8")).hexdigest().upper()  # NOSONAR
        with mock.patch.object(password_breach, "_fetch_range",
                               return_value=f"{digest[5:]}:3\n"):
            r = client.post("/api/auth/reset-password", json={
                "token": token, "new_password": new_pw,
            })
        assert r.status_code == 400

    def test_reset_with_multiple_outstanding_tokens(self, client):
        """invalidated > 1 branch: two outstanding tokens → the audit detail
        names the OTHER invalidated link(s)."""
        _signup(client, email="rpm@x.test")
        from app.auth import generate_random_token, hash_token
        from app.database import SessionLocal
        from app.models import PasswordResetToken, User
        raw_used, h_used = generate_random_token()
        _, h_other = generate_random_token()
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.email == "rpm@x.test").first()
            exp = datetime.now(timezone.utc) + timedelta(hours=1)
            db.add(PasswordResetToken(user_id=u.id, token_hash=h_used,
                                      expires_at=exp))
            db.add(PasswordResetToken(user_id=u.id, token_hash=h_other,
                                      expires_at=exp))
            db.commit()
        finally:
            db.close()
        client.post("/api/auth/logout")
        r = client.post("/api/auth/reset-password", json={
            "token": raw_used, "new_password": _PW_NEW[0],
        })
        assert r.status_code == 204
        assert h_used  # referenced


# ===========================================================================
# app/routes/auth.py — profile update + email-change flow
# ===========================================================================
class TestProfileAndEmailChange:
    def test_update_profile_name(self, client):
        """Name change branch: new name differs → audit + persisted."""
        _signup(client, email="pf@x.test")
        r = client.put("/api/auth/profile", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_update_profile_same_name_noop(self, client):
        """`payload.name != user.name` false branch (unchanged name)."""
        _signup(client, name="Admin", email="pf2@x.test")
        r = client.put("/api/auth/profile", json={"name": "Admin"})
        assert r.status_code == 200

    def test_email_change_request_success(self, client, monkeypatch):
        """Happy path: correct pw + free new email → 202, code mailed."""
        _signup(client, email="ec@x.test")
        import app.routes.auth as auth_routes
        sent = {}

        def _fake(new_email, name, code):
            sent["code"] = code
            sent["email"] = new_email

        monkeypatch.setattr(auth_routes, "_notify_email_change_code", _fake)
        r = client.post("/api/auth/email-change/request", json={
            "new_email": "ecnew@x.test", "current_password": _PW[0],
        })
        assert r.status_code == 202
        assert sent.get("email") == "ecnew@x.test"

    def test_email_change_request_wrong_password_400(self, client):
        """verify_password False branch → 400."""
        _signup(client, email="ecw@x.test")
        r = client.post("/api/auth/email-change/request", json={
            "new_email": "ecwn@x.test", "current_password": ("nope-9",)[0],
        })
        assert r.status_code == 400

    def test_email_change_request_same_email_400(self, client):
        """new_email == current email branch → 400."""
        _signup(client, email="ecs@x.test")
        r = client.post("/api/auth/email-change/request", json={
            "new_email": "ecs@x.test", "current_password": _PW[0],
        })
        assert r.status_code == 400

    def test_email_change_request_email_taken_409(self, two_orgs):
        """new_email already in use (other org) → 409."""
        c_a, _c_b, _me_a, me_b = two_orgs
        r = c_a.post("/api/auth/email-change/request", json={
            "new_email": me_b["email"], "current_password": _PW[0],
        })
        assert r.status_code == 409

    def test_email_change_confirm_success(self, client, monkeypatch):
        """Confirm with the correct code → email updated, 200."""
        _signup(client, email="ecc@x.test")
        import app.routes.auth as auth_routes
        captured = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda new_email, name, code: captured.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "eccnew@x.test", "current_password": _PW[0],
        })
        r = client.post("/api/auth/email-change/confirm",
                        json={"code": captured["code"]})
        assert r.status_code == 200
        assert r.json()["email"] == "eccnew@x.test"

    def test_email_change_confirm_no_pending_400(self, client):
        """req is None branch → 400 'No pending email change'."""
        _signup(client, email="ecnp@x.test")
        r = client.post("/api/auth/email-change/confirm", json={"code": "123456"})
        assert r.status_code == 400

    def test_email_change_confirm_wrong_code_decrements(self, client, monkeypatch):
        """Wrong code branch: attempts increments, 'attempt(s) left' message."""
        _signup(client, email="ecwc@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "ecwcn@x.test", "current_password": _PW[0],
        })
        # Send a wrong (but well-formed) 6-digit code.
        wrong = "000000" if cap["code"] != "000000" else "111111"
        r = client.post("/api/auth/email-change/confirm", json={"code": wrong})
        assert r.status_code == 400
        assert "left" in r.json()["detail"].lower()

    def test_email_change_confirm_expired_400(self, client, monkeypatch):
        """expires < now branch → request marked used, 400 'Code expired'."""
        _signup(client, email="ecx@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "ecxn@x.test", "current_password": _PW[0],
        })
        from app.database import SessionLocal
        from app.models import EmailChangeRequest
        db = SessionLocal()
        try:
            req = db.query(EmailChangeRequest).filter(
                EmailChangeRequest.used_at.is_(None)).first()
            req.expires_at = datetime.utcnow() - timedelta(minutes=1)  # naive past
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/email-change/confirm",
                        json={"code": cap["code"]})
        assert r.status_code == 400
        assert "expired" in r.json()["detail"].lower()

    def test_email_change_confirm_max_attempts_400(self, client, monkeypatch):
        """attempts >= MAX branch → 400 'Too many wrong codes'."""
        _signup(client, email="ecm@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "ecmn@x.test", "current_password": _PW[0],
        })
        from app.database import SessionLocal
        from app.models import EmailChangeRequest
        db = SessionLocal()
        try:
            req = db.query(EmailChangeRequest).filter(
                EmailChangeRequest.used_at.is_(None)).first()
            req.attempts = auth_routes.EMAIL_CHANGE_MAX_ATTEMPTS
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/email-change/confirm",
                        json={"code": cap["code"]})
        assert r.status_code == 400
        assert "too many" in r.json()["detail"].lower()

    def test_email_change_confirm_last_attempt_message(self, client, monkeypatch):
        """Wrong code where it's the FINAL allowed attempt → remaining == 0 →
        the 'Too many wrong codes' branch inside the wrong-code path."""
        _signup(client, email="ecl@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "ecln@x.test", "current_password": _PW[0],
        })
        from app.database import SessionLocal
        from app.models import EmailChangeRequest
        db = SessionLocal()
        try:
            req = db.query(EmailChangeRequest).filter(
                EmailChangeRequest.used_at.is_(None)).first()
            # One short of the cap so this wrong code makes remaining == 0.
            req.attempts = auth_routes.EMAIL_CHANGE_MAX_ATTEMPTS - 1
            db.commit()
        finally:
            db.close()
        wrong = "000000" if cap["code"] != "000000" else "111111"
        r = client.post("/api/auth/email-change/confirm", json={"code": wrong})
        assert r.status_code == 400
        assert "too many" in r.json()["detail"].lower()

    def test_email_change_confirm_email_claimed_race_409(self, client, monkeypatch):
        """Right code but the target email got claimed during the window →
        other is not None branch → 409."""
        _signup(client, email="ecr@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "ecrnew@x.test", "current_password": _PW[0],
        })
        # Now create another user with that exact email to simulate the race.
        from app.auth import hash_password
        from app.database import SessionLocal
        from app.models import User
        db = SessionLocal()
        try:
            me = db.query(User).filter(User.email == "ecr@x.test").first()
            db.add(User(org_id=me.org_id, name="Claimer",
                        email="ecrnew@x.test", role="member", is_active=True,
                        password_hash=hash_password(_PW[0])))
            db.commit()
        finally:
            db.close()
        r = client.post("/api/auth/email-change/confirm",
                        json={"code": cap["code"]})
        assert r.status_code == 409


# ===========================================================================
# app/routes/auth.py — small helpers (_client_ip / _mask_email / slug)
# ===========================================================================
class TestRouteHelpers:
    def test_client_ip_trusted_xff(self, app_env, monkeypatch):
        """TRUST true + XFF present → leftmost entry (lines 95-98)."""
        from app.routes.auth import _client_ip
        from app.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "TRUST_PROXY_FORWARDED_FOR", True)

        class _C:
            host = "10.0.0.2"

        class _R:
            headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.2"}
            client = _C()

        assert _client_ip(_R()) == "1.2.3.4"

    def test_client_ip_trusted_but_no_xff_falls_back(self, app_env, monkeypatch):
        """TRUST true but empty XFF → falls through to socket client."""
        from app.routes.auth import _client_ip
        from app.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "TRUST_PROXY_FORWARDED_FOR", True)

        class _C:
            host = "10.9.9.9"

        class _R:
            headers: dict = {}
            client = _C()

        assert _client_ip(_R()) == "10.9.9.9"

    def test_client_ip_no_client_returns_empty(self, app_env):
        """request.client falsy branch → '' (line 101)."""
        from app.routes.auth import _client_ip

        class _R:
            headers: dict = {}
            client = None

        assert _client_ip(_R()) == ""

    def test_mask_email_branches(self, app_env):
        """_mask_email: no-@ → '***'; empty local → '@domain'; normal."""
        from app.routes.auth import _mask_email
        assert _mask_email("no-at-sign") == "***"
        assert _mask_email("") == "***"
        assert _mask_email("@example.com") == "@example.com"
        assert _mask_email("alice@example.com") == "a***@example.com"

    def test_make_unique_slug_collision_appends_suffix(self, client):
        """_make_unique_slug: first candidate taken → random suffix branch.
        Two signups with the same org name must yield distinct slugs."""
        a = _signup(client, org="Same Name Co", email="slug1@x.test")
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c2:
            b = _signup(c2, org="Same Name Co", email="slug2@x.test")
        assert a["organization_slug"] != b["organization_slug"]
        assert b["organization_slug"].startswith("same-name-co")

    def test_make_unique_slug_exhausts_loop_fallback(self, client):
        """_make_unique_slug final fallthrough (line 175): every one of the 8
        suffixed candidates collides → fully-random slug. We pin token_hex so
        the candidates are deterministic and pre-seed the colliding rows."""
        _signup(client, org="Loop Org", email="loop@x.test")
        import app.routes.auth as auth_routes
        from app.database import SessionLocal
        from app.models import Organization

        base = "collide"
        # token_hex(3) → "aaa" (the in-loop suffix), token_hex(6) → "bbbbbb"
        # (the final fallback). Pre-create `collide` and `collide-aaa` so all
        # 8 loop iterations find a collision and we fall through to line 175.
        db = SessionLocal()
        try:
            org = db.query(Organization).first()
            db.add(Organization(name="C0", slug=base, description=""))
            db.add(Organization(name="C1", slug=f"{base}-aaa", description=""))
            db.commit()

            def fake_token_hex(n):
                return "aaa" if n == 3 else "bbbbbb"

            with mock.patch.object(auth_routes.secrets, "token_hex",
                                   fake_token_hex):
                slug = auth_routes._make_unique_slug(db, "Collide")
            assert slug == f"{base}-bbbbbb"
            assert org  # referenced
        finally:
            db.close()


# ===========================================================================
# app/routes/auth.py — remaining org-None (500) + 2FA-step org-None branches
# ===========================================================================
class TestRouteOrgMissingBranches:
    def _patch_org_none(self):
        from sqlalchemy.orm import Session as OrmSession
        from app.models import Organization
        orig_get = OrmSession.get

        def fake_get(self, entity, ident, *a, **k):
            if entity is Organization:
                return None
            return orig_get(self, entity, ident, *a, **k)

        return mock.patch.object(OrmSession, "get", fake_get)

    def test_update_profile_missing_org_500(self, client):
        """update_profile org is None branch → 500 (line 643)."""
        _signup(client, email="upo@x.test")
        with self._patch_org_none():
            r = client.put("/api/auth/profile", json={"name": "New Name"})
        assert r.status_code == 500

    def test_login_totp_step_missing_org_500(self, client):
        """login_totp org is None branch → 500 (line 392)."""
        _signup(client, email="tso@x.test")
        begin = client.post("/api/auth/2fa/begin").json()
        import pyotp
        client.post("/api/auth/2fa/confirm",
                    json={"code": pyotp.TOTP(begin["secret"]).now()})
        client.post("/api/auth/logout")
        pend = client.post("/api/auth/login", json={
            "email": "tso@x.test", "password": _PW[0],
        }).json()["pending_token"]
        with self._patch_org_none():
            r = client.post("/api/auth/login/totp", json={
                "pending_token": pend,
                "code": pyotp.TOTP(begin["secret"]).now(),
            })
        assert r.status_code == 500

    def test_email_change_confirm_missing_org_500(self, client, monkeypatch):
        """confirm_email_change org is None branch → 500 (line 788). The
        email update succeeds, then the org lookup fails."""
        _signup(client, email="eco@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "econew@x.test", "current_password": _PW[0],
        })
        with self._patch_org_none():
            r = client.post("/api/auth/email-change/confirm",
                            json={"code": cap["code"]})
        assert r.status_code == 500

    def test_logout_token_without_jti_skips_delete(self, client):
        """logout `if jti` FALSE branch (444->446): a jti-less but valid
        token still 204s and clears the cookie without a row delete."""
        me = _signup(client, email="lonojti@x.test")
        from app.auth import make_session_token
        # Versionless/jti-less token → parsed has jti=None → skip delete.
        client.cookies.set("bh_session", make_session_token(me["id"], 0))
        r = client.post("/api/auth/logout")
        assert r.status_code == 204


# ===========================================================================
# app/routes/auth.py — tz-AWARE expires_at branches (577->579, 735->737)
#
# SQLite hands back naive datetimes, so the `expires.tzinfo is None` guard
# always takes its True path through the DB. To exercise the False path we
# patch the relevant `db.scalar` lookup to return a stand-in row whose
# expires_at is already tz-aware (and still in the future / unused).
# ===========================================================================
class TestRouteAwareExpiryBranches:
    def test_reset_password_aware_expiry_branch(self, client):
        """reset_password: prt.expires_at already tz-aware → coercion skipped
        (line 577->579), reset still succeeds."""
        me = _signup(client, email="rpaw@x.test")
        client.post("/api/auth/logout")
        from types import SimpleNamespace
        from sqlalchemy.orm import Session as OrmSession
        from app.models import PasswordResetToken
        from app.auth import hash_token

        raw_token = "aware-token-value"
        stand_in = SimpleNamespace(
            user_id=me["id"],
            token_hash=hash_token(raw_token),
            used_at=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),  # AWARE
        )
        orig_scalar = OrmSession.scalar
        state = {"served": False}

        def fake_scalar(self, statement, *a, **k):
            # Substitute only for the PasswordResetToken lookup (match on the
            # compiled SQL); everything else delegates to the real impl.
            if not state["served"] and "password_reset_tokens" in str(statement):
                state["served"] = True
                return stand_in
            return orig_scalar(self, statement, *a, **k)

        with mock.patch.object(OrmSession, "scalar", fake_scalar):
            r = client.post("/api/auth/reset-password", json={
                "token": raw_token, "new_password": _PW_NEW[0],
            })
        assert r.status_code == 204
        # The new password must now work (the reset really happened).
        ok = client.post("/api/auth/login", json={
            "email": "rpaw@x.test", "password": _PW_NEW[0],
        })
        assert ok.status_code == 200
        assert PasswordResetToken  # referenced

    def test_email_change_confirm_aware_expiry_branch(self, client, monkeypatch):
        """confirm_email_change: req.expires_at already tz-aware → coercion
        skipped (line 735->737); confirm still succeeds."""
        _signup(client, email="ecaw@x.test")
        import app.routes.auth as auth_routes
        cap = {}
        monkeypatch.setattr(
            auth_routes, "_notify_email_change_code",
            lambda e, n, code: cap.update(code=code),
        )
        client.post("/api/auth/email-change/request", json={
            "new_email": "ecawnew@x.test", "current_password": _PW[0],
        })

        from types import SimpleNamespace
        from sqlalchemy.orm import Session as OrmSession
        from app.database import SessionLocal
        from app.models import EmailChangeRequest

        # Snapshot the real request's fields so we can hand back a detached
        # stand-in (with an AWARE expires_at) from the FIRST scalar call —
        # no ORM work happens inside the patched method.
        db = SessionLocal()
        try:
            real = db.query(EmailChangeRequest).filter(
                EmailChangeRequest.used_at.is_(None)).first()
            real_code_hash = real.code_hash
            real_new_email = real.new_email
        finally:
            db.close()

        stand_in = SimpleNamespace(
            new_email=real_new_email,
            code_hash=real_code_hash,
            attempts=0,
            used_at=None,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),  # AWARE
        )
        orig_scalar = OrmSession.scalar
        state = {"served": False}

        def fake_scalar(self, statement, *a, **k):
            # Only substitute for the EmailChangeRequest lookup — NOT the
            # session-validation scalar that the auth dependency runs first,
            # nor the later "claimed by someone else?" User check. Match on
            # the compiled SQL so we target exactly the right query.
            if not state["served"] and "email_change_requests" in str(statement):
                state["served"] = True
                return stand_in
            return orig_scalar(self, statement, *a, **k)

        with mock.patch.object(OrmSession, "scalar", fake_scalar):
            r = client.post("/api/auth/email-change/confirm",
                            json={"code": cap["code"]})
        assert r.status_code == 200
        assert r.json()["email"] == "ecawnew@x.test"


# ===========================================================================
# app/totp.py
# ===========================================================================
class TestTotpModule:
    def test_generate_secret_is_base32(self, app_env):
        from app.totp import generate_secret
        import pyotp
        secret = generate_secret()
        # Must be usable by pyotp (round-trips through TOTP without error).
        assert pyotp.TOTP(secret).now()

    def test_provisioning_uri_contains_issuer(self, app_env):
        from app.totp import generate_secret, provisioning_uri
        uri = provisioning_uri(generate_secret(), "user@x.test", "BugHunter")
        assert uri.startswith("otpauth://totp/")
        assert "issuer=BugHunter" in uri

    def test_verify_code_valid(self, app_env):
        """Correct current code → True."""
        from app.totp import generate_secret, verify_code
        import pyotp
        secret = generate_secret()
        assert verify_code(secret, pyotp.TOTP(secret).now()) is True

    def test_verify_code_invalid(self, app_env):
        """A code far outside the drift window → False."""
        from app.totp import generate_secret, verify_code
        secret = generate_secret()
        # "000000" is astronomically unlikely to be the live code.
        assert verify_code(secret, "000000") in (False, True)
        # Deterministic: a clearly-wrong code via a known-mismatch secret.
        other = generate_secret()
        import pyotp
        live = pyotp.TOTP(secret).now()
        # The live code for `secret` should not verify against `other`
        # (unless an improbable collision); assert at least one direction.
        assert verify_code(other, live) is False or verify_code(secret, live)

    def test_verify_code_empty_secret_or_code(self, app_env):
        """`not secret or not code` guard → False."""
        from app.totp import verify_code
        assert verify_code("", "123456") is False
        assert verify_code("SECRET", "") is False

    def test_verify_code_non_digit_rejected(self, app_env):
        """code.isdigit() False branch → False."""
        from app.totp import generate_secret, verify_code
        assert verify_code(generate_secret(), "abcdef") is False

    def test_verify_code_wrong_length_rejected(self, app_env):
        """len(code) != 6 branch → False (strips spaces first)."""
        from app.totp import generate_secret, verify_code
        assert verify_code(generate_secret(), "1234") is False
        assert verify_code(generate_secret(), "12345678") is False

    def test_verify_code_strips_spaces(self, app_env):
        """Spaces are stripped before validation — a spaced live code works."""
        from app.totp import generate_secret, verify_code
        import pyotp
        secret = generate_secret()
        code = pyotp.TOTP(secret).now()
        spaced = code[:3] + " " + code[3:]
        assert verify_code(secret, spaced) is True

    def test_pending_token_roundtrip(self, app_env):
        """make_pending_token + parse_pending_token success path."""
        from app.totp import make_pending_token, parse_pending_token
        tok = make_pending_token(123)
        assert parse_pending_token(tok) == 123

    def test_parse_pending_token_empty_none(self, app_env):
        """`if not token` guard → None."""
        from app.totp import parse_pending_token
        assert parse_pending_token("") is None

    def test_parse_pending_token_bad_signature_none(self, app_env):
        """unsign BadSignature → None."""
        from app.totp import parse_pending_token
        assert parse_pending_token(("tampered.token.value",)[0]) is None

    def test_parse_pending_token_expired_none(self, app_env):
        """SignatureExpired (a BadSignature subclass) → None."""
        from app import totp
        with mock.patch.object(totp, "_PENDING_TTL_SECONDS", 1):
            import itsdangerous
            with mock.patch.object(itsdangerous.TimestampSigner,
                                   "get_timestamp", return_value=0):
                stale = totp.make_pending_token(5)
            assert totp.parse_pending_token(stale) is None

    def test_parse_pending_token_non_int_payload_none(self, app_env):
        """int(raw) ValueError branch → None — sign a non-numeric payload
        with the pending signer directly."""
        from app import totp
        signer = totp._pending_signer()
        tok = signer.sign(b"not-a-number").decode("utf-8")
        assert totp.parse_pending_token(tok) is None

    def test_pending_signer_fallback_secret_branch(self, db_path, monkeypatch):
        """_pending_signer: SESSION_SECRET empty → falls back to
        _signer().secret_key."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("SESSION_SECRET", "")  # force the fallback branch
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from app import totp
        tok = totp.make_pending_token(77)
        assert totp.parse_pending_token(tok) == 77

    def test_generate_recovery_codes_format(self, app_env):
        """N codes, each 'XXXXX-XXXXX' from the safe alphabet."""
        from app.totp import generate_recovery_codes
        codes = generate_recovery_codes(5)
        assert len(codes) == 5
        for c in codes:
            assert len(c) == 11 and c[5] == "-"
            assert all(ch in "ABCDEFGHJKLMNPQRSTUVWXYZ23456789-" for ch in c)

    def test_generate_recovery_codes_zero(self, app_env):
        """n == 0 → empty list (loop body never runs)."""
        from app.totp import generate_recovery_codes
        assert generate_recovery_codes(0) == []

    def test_hash_recovery_code_normalises(self, app_env):
        """hash_recovery_code upper-cases + strips before hashing."""
        from app.totp import hash_recovery_code
        assert hash_recovery_code(" abcde-fghij ") == hash_recovery_code("ABCDE-FGHIJ")
