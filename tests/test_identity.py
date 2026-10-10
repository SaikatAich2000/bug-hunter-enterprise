"""B.2 identity: sign-up default, verification, case-insensitive emails, throttles, notices."""
from __future__ import annotations

from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD, new_client, sign_up

PASSWORD = "Passw0rd!x"


def _signup_body(email, org="Acme"):
    return {"name": "Owner", "email": email, "password": PASSWORD, "organization_name": org}


def test_public_signup_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLIC_SIGNUP", raising=False)
    from app.config import Settings

    assert Settings().ALLOW_PUBLIC_SIGNUP is False
