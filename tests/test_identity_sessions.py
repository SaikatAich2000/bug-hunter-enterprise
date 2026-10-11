"""B.2: old-address notice, pending-2FA binding, push token re-bind, metrics token."""
from __future__ import annotations

import pytest

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"


# ------------------------------------------------------------- notice to the old address

def test_email_change_notifies_the_old_address_only(client, monkeypatch):
    from app.routes import auth as auth_routes

    sent_codes, notices = [], []
    monkeypatch.setattr(auth_routes, "notify_email_change_code",
                        lambda email, name, code, from_address=None: sent_codes.append((email, code)))
    monkeypatch.setattr(auth_routes, "notify_email_changed",
                        lambda old, name, new, from_address=None: notices.append((old, new)))
    c, _ = sign_up("Acme", "owner@acme.test")
    other, _ = sign_up("Other Org", "boss@other.test")
    assert c.post("/api/auth/email-change/request",
                  json={"current_password": PASSWORD, "new_email": "new@acme.test"}).status_code == 202
    assert notices == []  # nothing is announced before the change happens
    code = sent_codes[-1][1]
    assert c.post("/api/auth/email-change/confirm", json={"code": code}).status_code == 200
    assert notices == [("owner@acme.test", "new@acme.test")]
    assert other.get("/api/auth/me").json()["email"] == "boss@other.test"


def test_email_change_notice_text_names_the_new_address(client, monkeypatch):
    from app import email_service

    captured = {}
    monkeypatch.setattr(email_service, "deliver",
                        lambda subject, to, body, from_address=None: captured.update(to=to, body=body) or True)
    email_service.notify_email_changed("old@acme.test", "Owner", "new@acme.test")
    assert captured["to"] == ["old@acme.test"] and "new@acme.test" in captured["body"]


def test_a_failed_confirmation_sends_no_notice(client, monkeypatch):
    from app.routes import auth as auth_routes

    notices = []
    monkeypatch.setattr(auth_routes, "notify_email_changed", lambda *a, **k: notices.append(a))
    c, _ = sign_up("Acme", "owner@acme.test")
    c.post("/api/auth/email-change/request", json={"current_password": PASSWORD, "new_email": "new@acme.test"})
    assert c.post("/api/auth/email-change/confirm", json={"code": "000000"}).status_code == 400
    assert notices == []


# ------------------------------------------------- pending 2FA token bound to session version

def _enrol_2fa(c):
    import pyotp

    secret = c.post("/api/auth/2fa/begin").json()["secret"]
    assert c.post("/api/auth/2fa/confirm", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    return secret


def test_pending_2fa_token_dies_when_the_session_version_moves(client):
    import pyotp

    c, me = sign_up("Acme", "owner@acme.test")
    secret = _enrol_2fa(c)
    from app.config import get_settings  # noqa: F401  (settings loaded for the app under test)

    # step 1: password only
    other = new_client()
    pending = other.post("/api/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}).json()["pending_token"]
    # the owner changes their password (bumps session_version) before the second step
    assert c.post("/api/auth/change-password",
                  json={"current_password": PASSWORD, "new_password": "Newpass99!x"}).status_code == 204
    res = other.post("/api/auth/login/totp", json={"pending_token": pending, "code": pyotp.TOTP(secret).now()})
    assert res.status_code == 400
    assert other.get("/api/auth/me").status_code == 401


def test_pending_2fa_token_still_works_when_nothing_changed(client):
    import pyotp

    c, _ = sign_up("Acme", "owner@acme.test")
    secret = _enrol_2fa(c)
    other = new_client()
    pending = other.post("/api/auth/login", json={"email": "owner@acme.test", "password": PASSWORD}).json()["pending_token"]
    # a code is single use per time step, so take the next step's code
    from app import totp
    code = pyotp.TOTP(secret).at(totp._now() + 30)
    import time
    original = totp._now
    totp._now = lambda: time.time() + 30
    try:
        res = other.post("/api/auth/login/totp", json={"pending_token": pending, "code": code})
    finally:
        totp._now = original
    assert res.status_code == 200


def test_pending_tokens_of_the_old_format_and_a_forged_version_are_refused(client):
    from app import totp

    assert totp.parse_pending(totp.make_pending_token(7, 3)) == (7, 3)
    assert totp.parse_pending_token(totp.make_pending_token(7, 3)) == 7
    legacy = totp._pending_signer().sign(b"7").decode()
    assert totp.parse_pending(legacy) is None
    assert totp.parse_pending("garbage") is None


# ------------------------------------------------------------------------ push re-bind

def test_a_push_token_moves_to_the_account_that_switches_onto_the_device(client):
    from app.database import SessionLocal
    from app.models import PushSubscription

    token = "fcm-token-for-the-shared-phone-0001"
    a, a_me = sign_up("Acme", "owner@acme.test")
    b, b_me = sign_up("Other Org", "boss@other.test")
    assert a.post("/api/devices/register", json={"token": token}).status_code == 200
    assert b.post("/api/devices/register", json={"token": token}).status_code == 200  # was 409
    with SessionLocal() as db:
        rows = db.query(PushSubscription).filter_by(token=token).all()
        assert [r.user_id for r in rows] == [b_me["id"]]
    # the previous account no longer holds it: unregistering from A is a no-op for B's row
    assert a.delete(f"/api/devices/{token}").status_code == 204
    with SessionLocal() as db:
        assert db.query(PushSubscription).filter_by(token=token).count() == 1


def test_web_push_subscribe_rebinds_too(client):
    from app.database import SessionLocal
    from app.models import PushSubscription

    token = "fcm-token-for-the-shared-browser-02"
    a, _ = sign_up("Acme", "owner@acme.test")
    b, b_me = sign_up("Other Org", "boss@other.test")
    assert a.post("/api/push/subscribe", json={"token": token, "platform": "web"}).status_code == 200
    assert b.post("/api/push/subscribe", json={"token": token, "platform": "web"}).status_code == 200
    with SessionLocal() as db:
        assert [r.user_id for r in db.query(PushSubscription).filter_by(token=token)] == [b_me["id"]]


def test_the_service_still_refuses_a_silent_takeover_without_rebind(client):
    from app import push_service
    from app.database import SessionLocal

    a, a_me = sign_up("Acme", "owner@acme.test")
    _, b_me = sign_up("Other Org", "boss@other.test")
    with SessionLocal() as db:
        push_service.register(db, user_id=a_me["id"], token="tok-0000000000000000001")
        db.commit()
        with pytest.raises(push_service.PushTokenConflict):
            push_service.register(db, user_id=b_me["id"], token="tok-0000000000000000001")


# ------------------------------------------------------------------- metrics token

@pytest.fixture
def metrics_app(client, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "METRICS_ENABLED", True)
    return s


def test_metrics_need_a_token_in_production(metrics_app, client, monkeypatch):
    monkeypatch.setattr(type(metrics_app), "is_production", property(lambda self: True))
    monkeypatch.setattr(metrics_app, "METRICS_TOKEN", "")
    assert client.get("/api/metrics").status_code == 403
    monkeypatch.setattr(metrics_app, "METRICS_TOKEN", "s3cret-metrics-token")
    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/api/metrics", headers={"Authorization": "Bearer s3cret-metrics-token"}).status_code == 200


def test_metrics_stay_open_outside_production_without_a_token(metrics_app, client, monkeypatch):
    monkeypatch.setattr(type(metrics_app), "is_production", property(lambda self: False))
    monkeypatch.setattr(metrics_app, "METRICS_TOKEN", "")
    assert client.get("/api/metrics").status_code == 200


def test_production_config_refuses_metrics_without_a_token():
    import types

    from app.main import _production_config_errors

    base = dict(
        SESSION_SECRET="x" * 40, BOOTSTRAP_ADMIN_EMAIL="", BOOTSTRAP_ADMIN_PASSWORD="",
        AUTO_LOGIN_ENABLED=False, GIT_CREDENTIAL_ENCRYPTION_KEY="", COOKIE_SECURE=True,
        APP_BASE_URL="https://bugs.example.com", EMAIL_BACKEND="smtp", is_production_env=True,
    )
    bad = _production_config_errors(types.SimpleNamespace(**base, METRICS_ENABLED=True, METRICS_TOKEN=""))
    assert any("METRICS_TOKEN" in e for e in bad)
    ok = _production_config_errors(types.SimpleNamespace(**base, METRICS_ENABLED=True, METRICS_TOKEN="t"))
    assert not any("METRICS_TOKEN" in e for e in ok)


def test_concurrent_registrations_of_one_token_leave_exactly_one_owner(client):
    """Rebinding is one upsert statement: racing accounts never duplicate or lose the row."""
    import threading

    from app import push_service
    from app.database import SessionLocal
    from app.models import PushSubscription

    _, a = sign_up("Acme", "owner@acme.test")
    _, b = sign_up("Other Org", "boss@other.test")
    token = "tok-race-000000000000000001"
    errors = []

    def go(user_id):
        try:
            with SessionLocal() as db:
                push_service.register(db, user_id=user_id, token=token, rebind=True)
                db.commit()
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(uid,)) for uid in (a["id"], b["id"]) * 4]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    with SessionLocal() as db:
        rows = db.query(PushSubscription).filter_by(token=token).all()
        assert len(rows) == 1 and rows[0].user_id in (a["id"], b["id"])
