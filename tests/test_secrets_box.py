"""Optional encryption at rest of 2FA and webhook secrets (app/secrets_box.py), and the paths of
the secret-bearing features that depend on it."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"


@pytest.fixture
def box(client, monkeypatch):
    """The module with a settings object whose key can be switched per test."""
    from app import secrets_box
    from app.config import get_settings

    def set_key(key):
        monkeypatch.setattr(get_settings(), "FIELD_ENCRYPTION_KEY", key)

    set_key("")
    return secrets_box, set_key


def test_without_a_key_values_are_stored_as_given(box):
    secrets_box, _ = box
    assert secrets_box.seal("plain-secret") == "plain-secret"
    assert secrets_box.unseal("plain-secret") == "plain-secret"


def test_with_a_key_values_are_encrypted_and_round_trip(box):
    secrets_box, set_key = box
    set_key(Fernet.generate_key().decode())
    sealed = secrets_box.seal("s3cret")
    assert sealed.startswith("enc:v1:")
    assert "s3cret" not in sealed
    assert secrets_box.seal("s3cret") != sealed  # a fresh nonce every time
    assert secrets_box.unseal(sealed) == "s3cret"
    # a value written before the key existed is still readable
    assert secrets_box.unseal("legacy-plain") == "legacy-plain"


def test_an_encrypted_value_cannot_be_read_without_or_with_the_wrong_key(box):
    secrets_box, set_key = box
    set_key(Fernet.generate_key().decode())
    sealed = secrets_box.seal("s3cret")
    set_key("")
    with pytest.raises(secrets_box.SecretUnreadable):
        secrets_box.unseal(sealed)
    set_key(Fernet.generate_key().decode())
    with pytest.raises(secrets_box.SecretUnreadable):
        secrets_box.unseal(sealed)


def test_an_invalid_key_falls_back_to_plain_storage(box):
    secrets_box, set_key = box
    set_key("not-a-fernet-key")
    assert secrets_box.seal("s3cret") == "s3cret"


def test_two_factor_and_webhook_secrets_are_encrypted_in_the_database(client, box):
    import pyotp

    from app.database import SessionLocal
    from app.models import User, Webhook

    _, set_key = box
    set_key(Fernet.generate_key().decode())
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret = c.post("/api/auth/2fa/begin").json()["secret"]
    hook_secret = c.post("/api/webhooks", json={"name": "H", "url": "https://hooks.example.com/x"}).json()["secret"]
    with SessionLocal() as db:
        stored_totp = db.query(User).filter_by(email="owner@acme.test").one().totp_secret
        stored_hook = db.query(Webhook).one().secret
    assert stored_totp.startswith("enc:v1:")
    assert secret not in stored_totp
    assert stored_hook.startswith("enc:v1:")
    assert hook_secret not in stored_hook

    # the encrypted secret still verifies codes
    code = pyotp.TOTP(secret).now()
    assert c.post("/api/auth/2fa/confirm", json={"code": code}).status_code == 200


def test_a_hook_whose_secret_cannot_be_read_records_the_failure_instead_of_sending(client, box, monkeypatch):
    import app.webhooks_delivery as wd
    from app.database import SessionLocal
    from app.models import Webhook

    _, set_key = box
    set_key(Fernet.generate_key().decode())
    c, me = sign_up("Acme Corp", "owner@acme.test")
    hook_id = c.post("/api/webhooks", json={"name": "H", "url": "https://hooks.example.com/x"}).json()["id"]
    set_key("")  # the key is gone: the stored secret is now unreadable
    monkeypatch.setattr(wd, "check_destination", lambda url: None)
    wd.deliver_event(me["org_id"], "webhook.ping", {}, hook_id)
    with SessionLocal() as db:
        hook = db.get(Webhook, hook_id)
        assert hook.consecutive_failures == 1
        assert "FIELD_ENCRYPTION_KEY" in hook.last_error


def test_a_deleted_hook_or_unknown_event_delivers_nothing(client):
    import app.webhooks_delivery as wd

    _, me = sign_up("Acme Corp", "owner@acme.test")
    wd.deliver_event(me["org_id"], "bug.created", {})  # no hooks at all
    wd.deliver_event(me["org_id"], "bug.created", {}, hook_id=999)  # a hook that does not exist


def test_webhook_url_edge_cases_are_rejected_at_save_time(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    too_long = "https://example.com/" + "a" * 600
    for url in (too_long, "http://[::1", "https://", "https://exa mple.com/x"):
        assert c.post("/api/webhooks", json={"name": "H", "url": url}).status_code == 422, url


def test_custom_field_validation_edges(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    pid = c.get("/api/projects").json()[0]["id"]
    base = f"/api/projects/{pid}/custom-fields"
    assert c.post(base, json={"name": "Pick", "field_type": "select", "options": ["a|b"]}).status_code == 422
    assert c.post(base, json={"name": "Pick", "field_type": "select", "options": ["x" * 600]}).status_code == 422
    fid = c.post(base, json={"name": "Note"}).json()["id"]
    assert c.put(f"{base}/{fid}", json={"field_type": "blob"}).status_code == 422
    assert c.put(f"{base}/{fid}", json={"options": ["a|b"]}).status_code == 422
    other = c.post(base, json={"name": "Other"}).json()["id"]
    assert c.put(f"{base}/{other}", json={"name": "Note"}).status_code == 409
    renamed = c.put(f"{base}/{fid}", json={"field_type": "select", "options": ["One", "Two"], "position": 3}).json()
    assert renamed["options"] == ["One", "Two"]
    assert renamed["position"] == 3
    assert c.put(f"{base}/99999", json={"name": "x"}).status_code == 404


def test_invitation_acceptance_conflicts(client, monkeypatch):
    sent = []
    monkeypatch.setattr("app.routes.invitations.notify_invitation",
                        lambda to, inviter, org, url, role, sender=None: sent.append(url))
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    owner.post("/api/invitations", json={"email": "late@acme.test"})
    token = sent[0].rsplit("token=", 1)[1]
    # the address registers elsewhere before the invitation is used
    sign_up("Other Org", "late@acme.test")
    res = new_client().post("/api/invitations/accept", json={"token": token, "name": "Late", "password": PASSWORD})
    assert res.status_code == 409
    # accepting needs a policy-compliant password
    owner.post("/api/invitations", json={"email": "weak@acme.test"})
    weak_token = sent[-1].rsplit("token=", 1)[1]
    assert new_client().post("/api/invitations/accept",
                             json={"token": weak_token, "name": "Weak", "password": "short"}).status_code == 422
