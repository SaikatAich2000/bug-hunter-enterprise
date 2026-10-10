"""B.2 identity: sign-up default."""
from __future__ import annotations


def test_public_signup_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLIC_SIGNUP", raising=False)
    from app.config import Settings

    assert Settings().ALLOW_PUBLIC_SIGNUP is False
