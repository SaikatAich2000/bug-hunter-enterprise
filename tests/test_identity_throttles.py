"""B.2: throttles on account deletion, 2FA management and invitations (per IP and per org)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import sign_up

PASSWORD = "Passw0rd!x"


def _reset_buckets():
    from app import main

    main._rate_buckets.clear()


def _hits_limit(call, allowed):
    """``call()`` returns a response; the (allowed+1)th must be the first 429."""
    _reset_buckets()
    codes = [call().status_code for _ in range(allowed + 1)]
    assert 429 not in codes[:allowed], codes
    assert codes[allowed] == 429, codes


def test_account_deletion_attempts_are_throttled(client):
    c, _ = sign_up("Acme", "owner@acme.test")
    _hits_limit(lambda: c.request("DELETE", "/api/auth/account", json={"password": "wrong"}), 3)


def test_2fa_disable_and_recovery_regeneration_are_throttled(client):
    c, _ = sign_up("Acme", "owner@acme.test")
    _hits_limit(lambda: c.post("/api/auth/2fa/disable", json={"password": "wrong"}), 5)
    _hits_limit(lambda: c.post("/api/auth/2fa/recovery-codes/regenerate", json={"password": "wrong"}), 5)


def test_2fa_enrolment_and_code_checks_are_throttled(client):
    c, _ = sign_up("Acme", "owner@acme.test")
    _hits_limit(lambda: c.post("/api/auth/2fa/begin"), 5)
    _hits_limit(lambda: c.post("/api/auth/2fa/confirm", json={"code": "000000"}), 8)


def test_2fa_login_code_check_stays_throttled(client):
    _hits_limit(lambda: client.post("/api/auth/login/totp", json={"pending_token": "x", "code": "000000"}), 8)


def test_invitations_have_a_per_ip_burst_guard(client):
    c, _ = sign_up("Acme", "owner@acme.test")
    _hits_limit(lambda: c.post("/api/invitations", json={"email": "not-an-email", "role": "user"}), 20)


def test_invitations_are_limited_per_minute_per_organization(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "INVITE_RATE_PER_MINUTE", 2)
    a, _ = sign_up("Org A", "a@one.test")
    b, _ = sign_up("Org B", "b@two.test")
    _reset_buckets()
    assert a.post("/api/invitations", json={"email": "x1@one.test", "role": "user"}).status_code == 201
    assert a.post("/api/invitations", json={"email": "x2@one.test", "role": "user"}).status_code == 201
    res = a.post("/api/invitations", json={"email": "x3@one.test", "role": "user"})
    assert res.status_code == 429 and res.headers["retry-after"] == "60"
    # another organization is not affected by A's burst
    assert b.post("/api/invitations", json={"email": "y1@two.test", "role": "user"}).status_code == 201
    # a refused attempt left nothing behind
    assert len([i for i in a.get("/api/invitations").json()]) == 2


def test_invitations_have_a_daily_cap_per_organization(client, monkeypatch):
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Invitation

    monkeypatch.setattr(get_settings(), "INVITE_DAILY_CAP", 3)
    a, _ = sign_up("Org A", "a@one.test")
    b, _ = sign_up("Org B", "b@two.test")
    _reset_buckets()
    for n in range(3):
        assert a.post("/api/invitations", json={"email": f"x{n}@one.test", "role": "user"}).status_code == 201
    # age them past the burst window so only the daily ceiling is in play
    with SessionLocal() as db:
        for inv in db.query(Invitation).all():
            inv.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    res = a.post("/api/invitations", json={"email": "x9@one.test", "role": "user"})
    assert res.status_code == 429 and "daily" in res.json()["detail"]
    assert b.post("/api/invitations", json={"email": "y1@two.test", "role": "user"}).status_code == 201
    # a day later the allowance is back
    with SessionLocal() as db:
        for inv in db.query(Invitation).all():
            inv.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        db.commit()
    assert a.post("/api/invitations", json={"email": "x9@one.test", "role": "user"}).status_code == 201
