"""B.2: public sign-up must verify its address before the first login."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD, new_client

PASSWORD = "Passw0rd!x"


@pytest.fixture
def verifying(client, monkeypatch):
    """The app with verification on, plus an outbox capturing the links it would mail."""
    from app.config import get_settings
    from app.routes import auth as auth_routes

    monkeypatch.setattr(get_settings(), "EMAIL_VERIFICATION_REQUIRED", True)
    outbox: list[tuple[str, str]] = []
    monkeypatch.setattr(auth_routes, "notify_email_verification",
                        lambda email, name, url, from_address=None: outbox.append((email, url)))
    return outbox


def _signup(email="owner@acme.test", org="Acme Corp"):
    c = new_client()
    res = c.post("/api/auth/signup", json={"name": "Owner", "email": email, "password": PASSWORD,
                                           "organization_name": org})
    return c, res


def _token(outbox, index=-1):
    return re.search(r"token=([^&\s]+)", outbox[index][1]).group(1)


def test_signup_waits_for_verification_and_starts_no_session(verifying):
    c, res = _signup()
    assert res.status_code == 202
    assert res.json() == {"verification_required": True, "email": "owner@acme.test"}
    assert c.get("/api/auth/me").status_code == 401
    assert len(verifying) == 1 and verifying[0][0] == "owner@acme.test"


def test_unverified_user_cannot_log_in_until_verified(verifying):
    c, _ = _signup()
    creds = {"email": "owner@acme.test", "password": PASSWORD}
    res = c.post("/api/auth/login", json=creds)
    assert res.status_code == 403
    assert "email" in res.json()["detail"].lower()
    # a wrong password still looks like any other failed login (no account probing)
    bad = c.post("/api/auth/login", json={**creds, "password": "Wrong1234!"})
    assert bad.status_code == 401
    assert c.post("/api/auth/verify-email", json={"token": _token(verifying)}).status_code == 204
    assert c.post("/api/auth/login", json=creds).status_code == 200


def test_verification_link_is_single_use_and_unknown_tokens_fail(verifying):
    c, _ = _signup()
    token = _token(verifying)
    assert c.post("/api/auth/verify-email", json={"token": token}).status_code == 204
    assert c.post("/api/auth/verify-email", json={"token": token}).status_code == 400
    assert c.post("/api/auth/verify-email", json={"token": "nope"}).status_code == 400


def test_an_expired_link_is_refused_and_a_resend_issues_a_working_one(verifying):
    from app.database import SessionLocal
    from app.models import EmailVerificationToken

    c, _ = _signup()
    old = _token(verifying)
    with SessionLocal() as db:
        row = db.query(EmailVerificationToken).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=1)  # past the cooldown
        db.commit()
    assert c.post("/api/auth/verify-email", json={"token": old}).status_code == 400
    assert c.post("/api/auth/resend-verification", json={"email": "owner@acme.test"}).status_code == 204
    assert len(verifying) == 2
    assert c.post("/api/auth/verify-email", json={"token": _token(verifying)}).status_code == 204
    assert c.post("/api/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}).status_code == 200


def test_resend_retires_older_links_and_is_throttled_per_account(verifying):
    from app.database import SessionLocal
    from app.models import EmailVerificationToken

    c, _ = _signup()
    first = _token(verifying)
    # inside the cooldown nothing new is mailed
    assert c.post("/api/auth/resend-verification", json={"email": "owner@acme.test"}).status_code == 204
    assert len(verifying) == 1
    with SessionLocal() as db:
        db.query(EmailVerificationToken).update(
            {EmailVerificationToken.created_at: datetime.now(timezone.utc) - timedelta(hours=1)})
        db.commit()
    assert c.post("/api/auth/resend-verification", json={"email": "Owner@Acme.test"}).status_code == 204
    assert len(verifying) == 2
    assert c.post("/api/auth/verify-email", json={"token": first}).status_code == 400  # retired
    assert c.post("/api/auth/verify-email", json={"token": _token(verifying)}).status_code == 204


def test_resend_never_reveals_whether_an_account_exists(verifying):
    c = new_client()
    assert c.post("/api/auth/resend-verification", json={"email": "ghost@nowhere.test"}).status_code == 204
    assert verifying == []
    # an already verified account gets nothing either
    ok = new_client().post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    assert ok.status_code == 200
    assert c.post("/api/auth/resend-verification", json={"email": BOOTSTRAP_EMAIL}).status_code == 204
    assert verifying == []


def test_verification_stays_inside_its_organization(verifying):
    """Verifying one org's admin never unlocks another org's pending admin."""
    a, _ = _signup("a@one.test", "Org One")
    b, _ = _signup("b@two.test", "Org Two")
    assert a.post("/api/auth/verify-email", json={"token": _token(verifying, 0)}).status_code == 204
    assert a.post("/api/auth/login", json={"email": "a@one.test", "password": PASSWORD}).status_code == 200
    assert b.post("/api/auth/login", json={"email": "b@two.test", "password": PASSWORD}).status_code == 403


def test_signup_signs_in_at_once_when_verification_is_off(client):
    c, res = _signup()
    assert res.status_code == 201
    assert c.get("/api/auth/me").json()["email"] == "owner@acme.test"


def test_invited_and_admin_created_users_are_verified_without_a_link(verifying, admin_client):
    from app.auth import generate_token
    from app.database import SessionLocal
    from app.models import Invitation, User

    assert admin_client.post("/api/users", json={"name": "Made", "email": "made@x.test", "role": "user",
                                                 "password": "User12345"}).status_code == 201
    assert admin_client.post("/api/invitations", json={"email": "inv@x.test", "role": "user"}).status_code == 201
    raw, h = generate_token()
    with SessionLocal() as db:
        db.query(Invitation).filter_by(email="inv@x.test").one().token_hash = h
        db.commit()
    assert new_client().post("/api/invitations/accept",
                             json={"token": raw, "name": "Inv", "password": PASSWORD}).status_code == 200
    with SessionLocal() as db:
        rows = db.query(User).filter(User.email.in_(["made@x.test", "inv@x.test", BOOTSTRAP_EMAIL])).all()
        assert len(rows) == 3 and all(u.email_verified_at is not None for u in rows)
    assert new_client().post("/api/auth/login", json={"email": "made@x.test", "password": "User12345"}).status_code == 200


def test_upgrade_backfills_existing_users_once_and_never_verifies_new_signups(verifying):
    """Dropping the column simulates an older database: every existing user is stamped
    verified on the next boot, and a sign-up made afterwards stays unverified across boots."""
    from sqlalchemy import text

    from app.database import SessionLocal, engine, init_db
    from app.models import User

    with engine.begin() as c:
        c.execute(text("DROP INDEX IF EXISTS uq_users_email_lower"))
        c.execute(text("ALTER TABLE users DROP COLUMN email_verified_at"))
    assert init_db() is True
    with SessionLocal() as db:
        assert all(u.email_verified_at is not None for u in db.query(User).all())
    _signup("late@acme.test")
    assert init_db() is True
    with SessionLocal() as db:
        assert db.query(User).filter_by(email="late@acme.test").one().email_verified_at is None


def test_the_verify_page_route_serves_its_html(client, tmp_path, monkeypatch):
    from app import main

    page = tmp_path / "verify-email.html"
    page.write_text("<html>verify __APP_NAME__</html>", encoding="utf-8")
    monkeypatch.setattr(main.settings, "STATIC_DIR", tmp_path, raising=False)
    main._html_cache.clear()
    res = client.get("/verify-email.html?token=abc")
    assert res.status_code == 200 and "verify" in res.text


def test_verify_endpoints_are_rate_limited(client):
    from app import main

    assert "/api/auth/verify-email" in main._RATE_RULES
    assert "/api/auth/resend-verification" in main._RATE_RULES
