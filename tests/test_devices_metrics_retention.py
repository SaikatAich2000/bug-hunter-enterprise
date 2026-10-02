"""Device registration, push channel preferences, Prometheus metrics, audit retention and the
bootstrap rules of the enterprise edition."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD, new_client, sign_up

TOKEN_A = "fcm-token-aaaaaaaaaaaaaaaa"
TOKEN_B = "fcm-token-bbbbbbbbbbbbbbbb"


# --- devices and preferences ---------------------------------------------------------------------


def test_device_registration_is_idempotent_and_owned_by_one_account(client):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")

    first = a.post("/api/devices/register", json={"token": TOKEN_A, "platform": "Android"})
    assert first.status_code == 200, first.text
    assert first.json()["platform"] == "android"
    assert a.post("/api/devices/register", json={"token": TOKEN_A}).status_code == 200
    # a token bound to another account cannot be taken over
    assert b.post("/api/devices/register", json={"token": TOKEN_A}).status_code == 409
    assert a.post("/api/devices/register", json={"token": "short"}).status_code == 422


def test_unregistering_only_removes_my_own_token(client):
    from app.database import SessionLocal
    from app.models import PushSubscription

    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")
    a.post("/api/devices/register", json={"token": TOKEN_A})
    assert b.delete(f"/api/devices/{TOKEN_A}").status_code == 204
    with SessionLocal() as db:
        assert db.query(PushSubscription).count() == 1
    assert a.delete(f"/api/devices/{TOKEN_A}").status_code == 204
    assert a.delete(f"/api/devices/{TOKEN_A}").status_code == 204
    with SessionLocal() as db:
        assert db.query(PushSubscription).count() == 0


def test_preferences_default_on_and_change_independently(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    assert c.get("/api/notifications/preferences").json() == {
        "mentions": True, "assignments": True, "activity": True}
    res = c.put("/api/notifications/preferences", json={"activity": False})
    assert res.json() == {"mentions": True, "assignments": True, "activity": False}
    assert c.put("/api/notifications/preferences", json={"mentions": False}).json() == {
        "mentions": False, "assignments": True, "activity": False}
    assert c.get("/api/notifications/preferences").json()["activity"] is False


def test_channels_follow_the_notification_kind():
    from app.push_service import channel_for

    assert channel_for("assigned") == "assignments"
    assert channel_for("comment", "ping @alice") == "mentions"
    assert channel_for("comment", "plain text") == "activity"
    assert channel_for("status") == "activity"


def test_push_skips_opted_out_users_and_sends_android_data_only(client, monkeypatch):
    from app import push_service
    from app.config import get_settings
    a, me = sign_up("Acme Corp", "owner@acme.test")
    a.post("/api/devices/register", json={"token": TOKEN_A, "platform": "android"})
    a.post("/api/devices/register", json={"token": TOKEN_B, "platform": "web"})
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)
    calls = []
    monkeypatch.setattr(
        push_service.fcm_transport, "send",
        lambda tokens, **kw: calls.append((list(tokens), kw)) or [],
    )

    assert push_service.push_to_users([me["id"]], title="t", body="b", channel="mentions") == 2
    by_tokens = {tuple(t): kw for t, kw in calls}
    assert by_tokens[(TOKEN_A,)]["data_only"] is True
    assert not by_tokens[(TOKEN_B,)].get("data_only")
    assert by_tokens[(TOKEN_A,)]["channel"] == "mentions"

    a.put("/api/notifications/preferences", json={"mentions": False})
    calls.clear()
    assert push_service.push_to_users([me["id"]], title="t", body="b", channel="mentions") == 0
    assert calls == []
    # other channels are unaffected
    assert push_service.push_to_users([me["id"]], title="t", body="b", channel="assignments") == 2


# --- metrics -------------------------------------------------------------------------------------


def test_metrics_are_off_by_default(client):
    assert client.get("/api/metrics").status_code == 404


def test_metrics_render_when_enabled_and_honour_the_token(client, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "METRICS_ENABLED", True)
    monkeypatch.setattr(settings, "METRICS_TOKEN", "")
    client.get("/api/health")
    res = client.get("/api/metrics")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "bh_http_requests_total" in res.text
    assert 'route="/api/health"' in res.text

    monkeypatch.setattr(settings, "METRICS_TOKEN", "s3cret-token")
    assert client.get("/api/metrics").status_code == 401
    assert client.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/api/metrics", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200


def test_login_outcomes_are_counted(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "METRICS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "METRICS_TOKEN", "")
    client.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    assert 'bh_events_total{event="login_success"}' in client.get("/api/metrics").text


# --- audit retention -----------------------------------------------------------------------------


def _backdate(db, activity_id, days):
    from app.models import Activity

    row = db.get(Activity, activity_id)
    row.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.commit()


def test_retention_deletes_only_rows_older_than_the_window(client):
    from app.database import SessionLocal
    from app.jobs.audit_retention import purge_expired
    from app.models import Activity

    sign_up("Acme Corp", "owner@acme.test")
    with SessionLocal() as db:
        ids = [a.id for a in db.query(Activity).order_by(Activity.id).all()]
        assert len(ids) >= 2
        _backdate(db, ids[0], 400)
        _backdate(db, ids[1], 30)
        total = db.query(Activity).count()
        assert purge_expired(db, 0) == 0  # 0 keeps everything
        assert purge_expired(db, 365) == 1
        assert db.query(Activity).count() == total - 1
        assert db.get(Activity, ids[1]) is not None
        assert purge_expired(db, 365) == 0


# --- bootstrap and meta --------------------------------------------------------------------------


def test_meta_exposes_the_public_enterprise_settings(client):
    meta = client.get("/api/meta").json()
    assert meta["signup_enabled"] is True
    assert "privacy_contact_email" in meta
    assert meta["audit_retention_days"] >= 0


def test_bootstrap_admin_belongs_to_the_configured_organization(client):
    me = client.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD}).json()
    assert me["role"] == "admin"
    assert me["organization_name"] == "Default Organization"
    assert client.get("/api/projects").json()[0]["name"] == "General"


def test_bootstrap_is_idempotent_and_leaves_changed_passwords_alone(client):
    import app.main as main

    c = new_client()
    assert c.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD}).status_code == 200
    assert c.post("/api/auth/change-password", json={
        "current_password": BOOTSTRAP_PASSWORD, "new_password": "Changed-Pw-123"}).status_code == 204
    main._bootstrap()
    main._bootstrap()
    again = new_client()
    assert again.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": "Changed-Pw-123"}).status_code == 200
    assert again.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD}).status_code == 401


def test_bootstrap_can_reset_the_admin_password_on_request(client, monkeypatch):
    import app.main as main
    from app.config import get_settings

    c = new_client()
    c.post("/api/auth/login", json={"email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD})
    c.post("/api/auth/change-password", json={"current_password": BOOTSTRAP_PASSWORD, "new_password": "Changed-Pw-123"})
    monkeypatch.setattr(get_settings(), "BOOTSTRAP_ADMIN_RESET_PASSWORD", True)
    main._bootstrap()
    assert new_client().post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD}).status_code == 200


@pytest.mark.parametrize("path", ["/signup", "/accept-invite", "/privacy", "/delete-account"])
def test_public_pages_are_served_without_a_session(client, path):
    res = client.get(path)
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
