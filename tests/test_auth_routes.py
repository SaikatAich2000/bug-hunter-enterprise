"""Auth route and helper tests (app/routes/auth.py, app/auth.py).
Imports live inside tests: conftest re-imports app.* per test, so top-level refs go stale.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD


# Small helpers — kept local so re-imports never leave a stale module reference.
def _admin_user_id(client) -> int:
    """Log in as admin and return the bootstrap admin's id via /api/auth/me."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
    })
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _insert_reset_token(user_id: int, *, expires_in_hours: float = 2.0,
                        used: bool = False) -> str:
    """Insert a PasswordResetToken row directly and return the raw token (expires_in_hours<0 = expired, used=True = already-used)."""
    # Import inside the call to bind to the current app generation.
    from app.auth import generate_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    raw, token_hash = generate_token()
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        prt = PasswordResetToken(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=now + timedelta(hours=expires_in_hours),
            used_at=now if used else None,
        )
        db.add(prt)
        db.commit()
    finally:
        db.close()
    return raw


def _token_used_at(raw_token: str):
    """Return the used_at value for the token row matching ``raw_token``."""
    from sqlalchemy import select

    from app.auth import hash_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    h = hash_token(raw_token)
    db = SessionLocal()
    try:
        prt = db.scalar(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == h)
        )
        return prt.used_at if prt is not None else None
    finally:
        db.close()


# Password-reset request: POST /api/auth/forgot-password
def test_cov_forgot_password_known_email_creates_token(client):
    """A known, active email returns 204 and persists a reset-token row."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")  # call as anonymous

    res = client.post("/api/auth/forgot-password",
                      json={"email": BOOTSTRAP_EMAIL})
    assert res.status_code == 204

    # Confirm the token row was actually persisted.
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import PasswordResetToken
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(PasswordResetToken).where(PasswordResetToken.user_id == uid)
        ).all()
    finally:
        db.close()
    assert len(rows) >= 1


def test_cov_forgot_password_unknown_email_204_default(client):
    """Unknown address gets the same 204 as a known one (no enumeration)."""
    res = client.post("/api/auth/forgot-password",
                      json={"email": "no-such-person@example.com"})
    assert res.status_code == 204


# Password-reset completion: POST /api/auth/reset-password
def test_cov_reset_password_valid_token_succeeds(client):
    """A fresh token resets the password: 204, marked used, new password works, old fails."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid)

    new_pw = "FreshReset123"
    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": new_pw})
    assert res.status_code == 204, res.text

    assert _token_used_at(raw) is not None  # token is single-use

    ok = client.post("/api/auth/login",
                     json={"email": BOOTSTRAP_EMAIL, "password": new_pw})
    assert ok.status_code == 200
    bad = client.post("/api/auth/login",
                      json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    assert bad.status_code == 401  # old password no longer works


def test_cov_reset_password_expired_token_rejected(client):
    """An expired token is rejected with 400 (the expires < now branch)."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid, expires_in_hours=-1.0)  # already expired

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "AfterExpiry123"})
    assert res.status_code == 400
    assert "expired" in res.json()["detail"].lower()


def test_cov_reset_password_used_token_rejected(client):
    """An already-used token is rejected with 400 (the used_at is not None branch)."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")
    raw = _insert_reset_token(uid, used=True)

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "AfterUsed1234"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_unknown_token_rejected(client):
    """A token that hashes to no stored row is rejected with 400 (prt is None guard)."""
    res = client.post("/api/auth/reset-password",
                      json={"token": "this-token-was-never-issued-xyz",
                            "new_password": "NeverMinted123"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_inactive_user_rejected(client, admin_client):
    """A valid token for a since-deactivated user is rejected with 400."""
    created = admin_client.post("/api/users", json={
        "name": "Resettable", "email": "resettable@test.local",
        "role": "user", "password": "Resettable9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    raw = _insert_reset_token(uid)

    deact = admin_client.put(f"/api/users/{uid}", json={"is_active": False})
    assert deact.status_code == 200

    admin_client.post("/api/auth/logout")
    res = admin_client.post("/api/auth/reset-password",
                            json={"token": raw, "new_password": "Inactive1234"})
    assert res.status_code == 400
    assert "invalid or expired" in res.json()["detail"].lower()


def test_cov_reset_password_naive_expiry_is_handled(client):
    """A tz-naive expires_at (SQLite artifact) is coerced to UTC instead of raising."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import generate_token
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    raw, token_hash = generate_token()
    naive_future = datetime.utcnow() + timedelta(hours=2)  # deliberately tz-naive
    db = SessionLocal()
    try:
        db.add(PasswordResetToken(
            user_id=uid, token_hash=token_hash,
            expires_at=naive_future, used_at=None,
        ))
        db.commit()
    finally:
        db.close()

    res = client.post("/api/auth/reset-password",
                      json={"token": raw, "new_password": "NaiveExpiry12"})
    assert res.status_code == 204, res.text


# Login edge cases — routes/auth.py
def test_cov_login_wrong_password_401(client):
    """Wrong password for a real account returns a unified 401."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": "definitely-the-wrong-one-9",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_cov_login_inactive_user_blocked(client, admin_client):
    """A deactivated account gets the same unified 401 even with the correct password."""
    created = admin_client.post("/api/users", json={
        "name": "Switched Off", "email": "switchedoff@test.local",
        "role": "user", "password": "SwitchedOff9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]
    off = admin_client.put(f"/api/users/{uid}", json={"is_active": False})
    assert off.status_code == 200
    admin_client.post("/api/auth/logout")

    res = admin_client.post("/api/auth/login", json={
        "email": "switchedoff@test.local", "password": "SwitchedOff9",
    })
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid email or password"


def test_cov_login_lockout_returns_429(client):
    """A full lockout bucket returns 429 before bcrypt verify (bucket pre-filled for determinism)."""
    from app import account_lockout
    account_lockout._reset_for_tests()
    try:
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(BOOTSTRAP_EMAIL)
        # Even the correct password must be rejected once the bucket is locked.
        res = client.post("/api/auth/login", json={
            "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
        })
        assert res.status_code == 429
        assert res.headers.get("Retry-After")
    finally:
        account_lockout._reset_for_tests()


# logout branches
def test_cov_logout_token_user_deleted_skips_audit(client, admin_client):
    """Logout with a cookie whose user was deleted skips the audit write but still returns 204."""
    created = admin_client.post("/api/users", json={
        "name": "Ephemeral", "email": "ephemeral@test.local",
        "role": "user", "password": "Ephemeral9",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["id"]

    # Second TestClient so the admin session cookie is undisturbed.
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as user_c:
        login = user_c.post("/api/auth/login", json={
            "email": "ephemeral@test.local", "password": "Ephemeral9",
        })
        assert login.status_code == 200

        # Admin delete cascades session rows, so logout carries an orphaned token.
        dele = admin_client.delete(f"/api/users/{uid}")
        assert dele.status_code in (200, 204), dele.text

        out = user_c.post("/api/auth/logout")
        assert out.status_code == 204  # idempotent regardless


def test_cov_logout_legacy_cookie_without_jti(client):
    """A legacy 2-part cookie without jti writes the audit row but skips the per-session delete."""
    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from app.auth import COOKIE_NAME, make_session_token
    legacy = make_session_token(uid, 0)  # no jti — produces a 2-part token

    res = client.post("/api/auth/logout", cookies={COOKIE_NAME: legacy})
    assert res.status_code == 204


def test_cov_logout_without_cookie_is_idempotent(client):
    """Logout without a cookie is idempotent: 204."""
    res = client.post("/api/auth/logout")
    assert res.status_code == 204


# _client_ip
def test_cov_client_ip_trust_enabled_empty_xff_falls_through(monkeypatch):
    """Empty X-Forwarded-For with trust enabled falls back to the transport client host."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)

    from starlette.requests import Request

    from app.routes.auth import _client_ip

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"")],  # present but empty
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.20.30.40", 12345),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "10.20.30.40"


def test_cov_client_ip_trust_enabled_uses_rightmost_xff(monkeypatch):
    """With trust enabled, _client_ip uses the rightmost XFF entry; the leftmost is attacker-controlled."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_HOP_COUNT", 1)

    from starlette.requests import Request

    from app.routes.auth import _client_ip

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        # attacker-forged, real proxy-appended peer
        "headers": [(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")],
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.0.0.1", 555),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "10.0.0.1"


def test_cov_client_ip_trust_enabled_garbage_xff_falls_through(monkeypatch):
    """Garbage XFF can't poison the session/audit IP: ValueError is swallowed, falls back to transport host."""
    import app.config as config
    monkeypatch.setattr(config.Settings, "TRUST_PROXY_FORWARDED_FOR", True)

    from starlette.requests import Request

    from app.routes.auth import _client_ip

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [(b"x-forwarded-for", b"not-an-ip-address")],  # not a valid IP
        "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": ("10.20.30.40", 12345),
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == "10.20.30.40"


def test_cov_client_ip_no_client_returns_empty(monkeypatch):
    """No transport client: _client_ip returns the empty string."""
    from starlette.requests import Request

    from app.routes.auth import _client_ip

    scope = {
        "type": "http", "method": "POST", "path": "/api/auth/login",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("testserver", 80), "client": None,  # absent transport client
        "raw_path": b"/api/auth/login",
    }
    assert _client_ip(Request(scope)) == ""


# get_current_user via the HTTP layer
def test_cov_me_missing_cookie_401(client):
    """No session cookie: get_current_user raises 401."""
    res = client.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["detail"] == "Not authenticated"


def test_cov_me_invalid_signature_401(client):
    """A cookie failing the signature check returns 401."""
    from app.auth import COOKIE_NAME
    res = client.get("/api/auth/me",
                     cookies={COOKIE_NAME: "not.a.validly.signed.token"})
    assert res.status_code == 401


def test_cov_me_expired_token_401(client):
    """A token older than SESSION_TTL_SECONDS is rejected (401); the signer backdates its own timestamp so no real time elapses."""
    import time as _time

    uid = _admin_user_id(client)
    client.post("/api/auth/logout")

    from itsdangerous import TimestampSigner

    from app.auth import COOKIE_NAME, _signer

    live = _signer()  # same secret + salt the app validates with

    class _OldSigner(TimestampSigner):
        def get_timestamp(self) -> int:  # returns a timestamp far in the past
            return int(_time.time()) - 200_000

    old = _OldSigner(live.secret_keys[-1], salt=live.salt)
    expired = old.sign(f"{uid}:0".encode("utf-8")).decode("utf-8")

    res = client.get("/api/auth/me", cookies={COOKIE_NAME: expired})
    assert res.status_code == 401


# parse_session_token — non-UTF-8 payload
def test_cov_parse_session_token_non_utf8_payload_returns_none(client):
    """A signed non-UTF-8 payload: parse_session_token swallows UnicodeDecodeError and returns None."""
    from app import auth
    bad_bytes = b"\xff\xfe\x00bad"  # invalid UTF-8
    signed = auth._signer().sign(bad_bytes)  # keep as bytes
    assert auth.parse_session_token(signed) is None


# Session-row validation (driven through a real authenticated request)
def test_cov_expired_session_row_deletes_and_rejects(client):
    """An expired backing session row is rejected and deleted inline."""
    uid = _admin_user_id(client)

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Session as SessionRow

    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        jti = sess.jti
        sess.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)  # back-dated
        db.commit()
    finally:
        db.close()

    # Validation reaches the expiry check, deletes the stale row, and rejects.
    res = client.get("/api/auth/me")
    assert res.status_code == 401

    db = SessionLocal()
    try:
        gone = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
    finally:
        db.close()
    assert gone is None, "expired session row should have been deleted in-line"


def test_cov_naive_session_expiry_is_coerced(client):
    """A tz-naive future session expires_at is coerced to UTC and accepted."""
    uid = _admin_user_id(client)

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Session as SessionRow

    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        sess.expires_at = datetime.utcnow() + timedelta(hours=2)  # tz-naive future
        # Back-date last_seen_at so the throttled bump path runs on this request.
        sess.last_seen_at = datetime.utcnow() - timedelta(minutes=10)
        db.commit()
    finally:
        db.close()

    res = client.get("/api/auth/me")
    assert res.status_code == 200


def test_cov_last_seen_bump_runs_after_throttle_window(client):
    """After the throttle window, _maybe_bump_last_seen writes a fresh last_seen_at."""
    uid = _admin_user_id(client)

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Session as SessionRow

    old = datetime.now(timezone.utc) - timedelta(hours=1)
    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        sess.last_seen_at = old
        db.commit()
    finally:
        db.close()

    res = client.get("/api/auth/me")
    assert res.status_code == 200

    db = SessionLocal()
    try:
        sess2 = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        bumped = sess2.last_seen_at
    finally:
        db.close()
    bumped_aware = bumped if bumped.tzinfo else bumped.replace(tzinfo=timezone.utc)
    assert bumped_aware > old, "last_seen_at should have been bumped forward"


# Direct-call tests: SQLite reads tz columns naive and commit() never fails on a healthy DB,
# so the aware arms and SQLAlchemyError handlers are unreachable via HTTP.
def _persisted_session_row(client):
    """Log in and return (uid, jti, db, SessionRow) backed by a real persisted row."""
    uid = _admin_user_id(client)
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Session as SessionRow
    db = SessionLocal()
    sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
    assert sess is not None
    return uid, sess.jti, db, sess


def test_cov_validate_session_row_aware_expiry_branch(client):
    """Aware future expires_at skips naive coercion and validates True."""
    from app.auth import _validate_session_row
    from app.models import User
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        sess.expires_at = datetime.now(timezone.utc) + timedelta(hours=2)  # aware
        sess.last_seen_at = datetime.now(timezone.utc)  # recent, bump skipped
        user = db.get(User, uid)
        assert _validate_session_row(db, jti, user) is True
    finally:
        db.close()


def test_cov_maybe_bump_last_seen_aware_recent_is_noop(client):
    """Aware recent last_seen_at early-returns at the throttle guard without writing."""
    from app.auth import _maybe_bump_last_seen
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        now = datetime.now(timezone.utc)
        sess.last_seen_at = now  # aware, within throttle window
        before = sess.last_seen_at
        _maybe_bump_last_seen(db, sess, now, jti)
        assert sess.last_seen_at == before  # unchanged (throttled)
    finally:
        db.close()


def test_cov_delete_expired_session_commit_error_is_swallowed(client, monkeypatch):
    """SQLAlchemyError from commit() during delete is logged and rolled back, not propagated."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.auth import _delete_expired_session
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        rolled_back = {"n": 0}
        real_rollback = db.rollback

        def _boom_commit():
            raise SQLAlchemyError("simulated commit failure")

        def _count_rollback():
            rolled_back["n"] += 1
            return real_rollback()

        monkeypatch.setattr(db, "commit", _boom_commit)
        monkeypatch.setattr(db, "rollback", _count_rollback)
        _delete_expired_session(db, sess, jti)  # must not raise
        assert rolled_back["n"] == 1
    finally:
        db.close()


def test_cov_maybe_bump_last_seen_commit_error_is_swallowed(client, monkeypatch):
    """Commit failure while bumping last_seen_at rolls back and does not raise."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.auth import _maybe_bump_last_seen
    uid, jti, db, sess = _persisted_session_row(client)
    try:
        rolled_back = {"n": 0}
        real_rollback = db.rollback

        def _boom_commit():
            raise SQLAlchemyError("simulated commit failure")

        def _count_rollback():
            rolled_back["n"] += 1
            return real_rollback()

        now = datetime.now(timezone.utc)
        sess.last_seen_at = now - timedelta(hours=1)  # aware, past the throttle window
        monkeypatch.setattr(db, "commit", _boom_commit)
        monkeypatch.setattr(db, "rollback", _count_rollback)
        _maybe_bump_last_seen(db, sess, now, jti)  # must not raise
        assert rolled_back["n"] == 1
    finally:
        db.close()


# _user_from_request (the session-cookie resolver behind get_current_user)
def test_cov_user_from_request_paths(client):
    """Returns None anonymously and the User when authenticated."""
    uid = _admin_user_id(client)

    from sqlalchemy import select
    from starlette.requests import Request

    from app.auth import (
        COOKIE_NAME,
        _user_from_request,
        make_session_token,
    )
    from app.database import SessionLocal
    from app.models import Session as SessionRow

    def _req(cookie_header: bytes | None) -> Request:
        headers = [(b"cookie", cookie_header)] if cookie_header else []
        return Request({
            "type": "http", "method": "GET", "path": "/api/auth/me",
            "headers": headers, "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 0),
            "raw_path": b"/api/auth/me",
        })

    db = SessionLocal()
    try:
        assert _user_from_request(_req(None), db) is None  # anonymous

        # Rebuild a valid cookie from the real session row so the jti resolves.
        sess = db.scalar(select(SessionRow).where(SessionRow.user_id == uid))
        assert sess is not None
        tok = make_session_token(uid, 0, jti=sess.jti)
        user = _user_from_request(_req(f"{COOKIE_NAME}={tok}".encode()), db)
        assert user is not None
        assert user.id == uid
    finally:
        db.close()


# require_admin / require_manager_or_admin — 403 for low roles
def test_cov_require_admin_forbids_regular_user(user_client):
    """A regular user on an admin-only endpoint gets 403."""
    res = user_client.get("/api/sessions")
    assert res.status_code == 403


def test_cov_require_admin_allows_admin(admin_client):
    """An admin passes require_admin."""
    res = admin_client.get("/api/sessions")
    assert res.status_code == 200


def test_cov_require_admin_unit_raises_for_manager():
    """require_admin raises 403 for a manager when called directly."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.auth import require_admin

    manager = SimpleNamespace(role="manager")
    with pytest.raises(HTTPException) as ei:
        require_admin(manager)
    assert ei.value.status_code == 403


def test_cov_require_manager_or_admin_forbids_user():
    """Raises 403 for a plain user; passes for manager or admin."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.auth import require_manager_or_admin

    plain_user = SimpleNamespace(role="user")
    with pytest.raises(HTTPException) as ei:
        require_manager_or_admin(plain_user)
    assert ei.value.status_code == 403

    mgr = SimpleNamespace(role="manager")
    assert require_manager_or_admin(mgr) is mgr
    adm = SimpleNamespace(role="admin")
    assert require_manager_or_admin(adm) is adm
