"""Pytest fixtures — v4 multi-tenant.

Each test gets a fresh SQLite file in tmp_path so tests are fully
isolated. Convenience fixtures:
  - client     : raw, unauthenticated TestClient.
  - two_orgs   : two TestClients sharing one DB, each logged in as the
                 admin of a different org. Use this for cross-tenant
                 isolation tests.
  - make_invite: helper that creates an invitation and returns a usable
                 raw token (we can't intercept email, so we patch the
                 token_hash via the DB).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def app_env(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
    # Wipe module cache so the engine picks up the new env vars.
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def _signup(client, org, name, email, password="TestPass1!"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture()
def two_orgs(app_env):
    """Org A and Org B share one app/db so we can verify isolation."""
    from fastapi.testclient import TestClient
    from app.main import app
    c_a = TestClient(app)
    c_b = TestClient(app)
    with c_a, c_b:
        me_a = _signup(c_a, "Org A", "Alice Admin", "alice@a.test")
        me_b = _signup(c_b, "Org B", "Bob B", "bob@b.test")
        yield c_a, c_b, me_a, me_b


def _patch_invitation_token(db_path, invitation_id):
    """Re-stamp an invitation's token_hash and return the matching raw
    token. The accept endpoint never reveals the original."""
    from sqlalchemy import create_engine, text
    from app.auth import generate_random_token
    raw, h = generate_random_token()
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE invitations SET token_hash = :h WHERE id = :i"),
            {"h": h, "i": invitation_id},
        )
    engine.dispose()
    return raw


@pytest.fixture()
def make_invite(db_path):
    """Returns helper(client, email, role='member', project_ids=None,
    as_lead=False) → raw_token."""
    def _do(client, email, role="member", project_ids=None, as_lead=False):
        r = client.post("/api/invitations", json={
            "email": email, "role": role,
            "project_ids": project_ids or [], "as_lead": as_lead,
        })
        assert r.status_code == 201, r.text
        inv_id = r.json()["id"]
        return _patch_invitation_token(db_path, inv_id)
    return _do
