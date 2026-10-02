"""Direct unit tests for defensive route-helper guards that earlier HTTP-layer
checks pre-empt; calling the helpers directly keeps the safety net tested."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.conftest import default_org_id


def test_cov_is_current_false_on_unparseable_cookie(client):
    """`_is_current` returns False for a missing or malformed session cookie."""
    import app.routes.sessions as sessions

    class _Req:
        def __init__(self, cookies):
            self.cookies = cookies

    sess = type("S", (), {"jti": "whatever"})()
    # No cookie at all.
    assert sessions._is_current(_Req({}), sess) is False
    # Cookie present but not a valid token.
    assert sessions._is_current(_Req({sessions.COOKIE_NAME: "not-a-real-token"}), sess) is False


def test_cov_last_admin_guardrail_blocks_demoting_sole_admin(client):
    """The last-admin guardrail raises 400 when demoting the only admin.
    The route's self-edit guard normally fires first, so call the helper
    directly against the bootstrap admin."""
    import app.routes.users as users
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == "admin"))
        assert admin is not None, "bootstrap admin should exist after startup"
        with pytest.raises(HTTPException) as ei:
            users._check_last_admin_guardrail(db, admin, admin.id, {"role": "user"})
        assert ei.value.status_code == 400
        assert "last admin" in ei.value.detail.lower()
        # Deactivating the sole admin must also be blocked.
        with pytest.raises(HTTPException):
            users._check_last_admin_guardrail(db, admin, admin.id, {"is_active": False})
    finally:
        db.close()


def test_cov_delete_last_admin_blocked(client):
    """Deleting the only admin must be rejected. In normal HTTP flow
    `require_admin` ensures a second admin always exists, so this guard is
    never reached; call the route function directly with a non-admin actor to
    exercise it."""
    import app.routes.users as users
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.role == "admin"))
        assert admin is not None
        # Use a distinct non-admin actor so actor.id != target.id and the
        # "delete yourself" guard doesn't fire before the last-admin check.
        actor = User(org_id=default_org_id(), name="probe", email="probe.cov@test.local", role="user",
                     is_active=True, password_hash="x")
        db.add(actor)
        db.flush()
        with pytest.raises(HTTPException) as ei:
            users.delete_user(admin.id, db=db, actor=actor)
        assert ei.value.status_code == 400
        assert "last admin" in ei.value.detail.lower()
        db.rollback()
    finally:
        db.close()
