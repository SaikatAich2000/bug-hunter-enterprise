"""Supplemental coverage for defensive guards the HTTP layer pre-empts.

Each guard below protects a real invariant that an earlier check — auth /
``require_admin``, the self-edit guard, an FK ``ondelete=CASCADE``, or a
Pydantic min-length validator — makes unreachable end-to-end. Calling the
helper directly keeps the safety net under test (the same technique the
sibling Bug-Hunter repo uses in its ``test_cov_fill.py``). Every test takes a
fixture so the hermetic-SQLite ``app_env`` is active before any ``app.*``
module is imported (never touch a real database).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select


# --------------------------------------------------------------------------
# app/routes/projects.py — _derive_key / _unique_key
# --------------------------------------------------------------------------
def test_derive_key_falls_back_to_default_key(client):
    """projects.py:55,60,65 — names that strip to no usable key → 'P'.
    ProjectIn enforces min length 2, so the HTTP path never sends these."""
    from app.routes import projects as p
    assert p._derive_key("") == "P"          # 54-55: no words at all
    assert p._derive_key("   ") == "P"       # whitespace-only → no words
    assert p._derive_key("!!!") == "P"       # 59-60: single word, all bad chars
    assert p._derive_key(".. ??") == "P"     # 64-65: multiword, all bad chars


def test_unique_key_gives_up_after_999_collisions(client):
    """projects.py:77-78 — >999 key collisions raises 500. Unreachable in a
    real org (would need ~1000 projects sharing one base key)."""
    from app.routes import projects as p

    class _AlwaysCollide:
        def scalar(self, *a, **k):
            return 1  # every candidate already taken → never settles

    with pytest.raises(HTTPException) as ei:
        p._unique_key(_AlwaysCollide(), 1, "AB")
    assert ei.value.status_code == 500


# --------------------------------------------------------------------------
# app/routes/sessions.py — _is_current
# --------------------------------------------------------------------------
def test_is_current_false_on_missing_or_garbage_cookie(client):
    """sessions.py:29-30 — no / unparseable session cookie → False. Over HTTP
    the admin's own cookie always parses (auth validated it first)."""
    from app.routes import sessions as s
    sess = SimpleNamespace(jti="abc")
    assert s._is_current(SimpleNamespace(cookies={}), sess) is False
    assert s._is_current(
        SimpleNamespace(cookies={s.COOKIE_NAME: "not-a-real-token"}), sess) is False


# --------------------------------------------------------------------------
# app/routes/invitations.py — _client_ip
# --------------------------------------------------------------------------
def test_client_ip_empty_when_no_proxy_and_no_client(client):
    """invitations.py:71 — no trusted proxy and request.client is None → ''.
    Starlette/TestClient always populate request.client over HTTP."""
    import app.routes.invitations as inv
    assert inv._client_ip(SimpleNamespace(client=None, headers={})) == ""


# --------------------------------------------------------------------------
# app/routes/organizations.py + branding.py — org-missing 404 guards
# --------------------------------------------------------------------------
def test_org_and_branding_404_when_org_row_missing(client):
    """organizations.py:35-36,47-48 + branding.py:77-78,93-94 — the org-None
    404 guards. An authenticated user's org always exists (org→user CASCADE),
    so these never fire end-to-end; call the handlers with a user pointing at
    a non-existent org."""
    from app.routes import organizations as orgs
    from app.routes import branding as br
    from app.schemas import OrganizationUpdate
    from app.database import SessionLocal

    ghost = SimpleNamespace(org_id=10**9, id=1, name="Ghost")
    db = SessionLocal()
    try:
        for fn in (
            lambda: orgs.get_my_org(user=ghost, db=db),
            lambda: orgs.update_my_org(
                payload=OrganizationUpdate.model_construct(), user=ghost, db=db),
            lambda: br.get_branding(user=ghost, db=db),
            lambda: br.update_branding(
                payload=br.BrandingIn.model_construct(), user=ghost, db=db),
        ):
            with pytest.raises(HTTPException) as ei:
                fn()
            assert ei.value.status_code == 404
    finally:
        db.close()


# --------------------------------------------------------------------------
# app/routes/users.py — manager-role + last-admin guards
# --------------------------------------------------------------------------
def test_user_admin_guards_via_direct_calls(admin_client):
    """users.py:156-165 (non-admin can't touch admin / grant admin) and
    192-196 / 296-300 (can't remove the last admin). ``require_admin`` + the
    self-edit guard pre-empt all of these over HTTP, so drive them directly."""
    from app.routes import users as u
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == u.ROLE_ADMIN))
        assert admin is not None, "bootstrap admin should exist after signup"
        member = User(
            org_id=admin.org_id, name="Mem", email="mem.fill@acme.test",
            role="member", is_active=True, password_hash=admin.password_hash,
        )
        db.add(member)
        db.flush()

        # _check_manager_role_limits — non-admin editing an admin → 403.
        with pytest.raises(HTTPException) as ei:
            u._check_manager_role_limits(member, admin, {})
        assert ei.value.status_code == 403
        # non-admin trying to grant the admin role → 403.
        with pytest.raises(HTTPException) as ei:
            u._check_manager_role_limits(member, member, {"role": u.ROLE_ADMIN})
        assert ei.value.status_code == 403

        # _check_last_admin_guardrail — demoting the sole admin → 400.
        with pytest.raises(HTTPException) as ei:
            u._check_last_admin_guardrail(db, admin, admin.id, {"role": "member"})
        assert ei.value.status_code == 400

        # delete_user — deleting the sole admin; a non-admin actor (id != target)
        # slips past the self-guard so we reach the last-admin count → 400.
        with pytest.raises(HTTPException) as ei:
            u.delete_user(admin.id, actor=member, db=db)
        assert ei.value.status_code == 400

        db.rollback()
    finally:
        db.close()
