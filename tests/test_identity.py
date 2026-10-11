"""B.2 identity: sign-up default."""
from __future__ import annotations


def test_public_signup_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLIC_SIGNUP", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # ignore a developer's local .env
    import importlib

    import app.config

    importlib.reload(app.config)  # class-level settings are read at import; the harness restores it

    assert app.config.Settings().ALLOW_PUBLIC_SIGNUP is False
