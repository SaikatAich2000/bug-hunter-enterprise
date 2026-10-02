"""Two-factor authentication: enrolment, the two-step sign-in, single-use codes, recovery codes,
switching off."""
from __future__ import annotations

import pyotp
import pytest

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"


class Clock:
    """A controllable clock for app.totp: every authenticator code belongs to one 30 s step."""

    def __init__(self, monkeypatch):
        import app.totp

        self.now = 1_900_000_000.0
        monkeypatch.setattr(app.totp, "_now", lambda: self.now)

    def code(self, secret):
        return pyotp.TOTP(secret).at(self.now)

    def next_step(self):
        self.now += 30


@pytest.fixture
def clock(client, monkeypatch):
    return Clock(monkeypatch)


def _enrol(c, clock):
    begin = c.post("/api/auth/2fa/begin")
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]
    confirm = c.post("/api/auth/2fa/confirm", json={"code": clock.code(secret)})
    assert confirm.status_code == 200, confirm.text
    clock.next_step()  # the confirming code is spent; sign-in needs a newer one
    return secret, confirm.json()["recovery_codes"]


def _first_step(email):
    c = new_client()
    res = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c, res


def test_enrolment_returns_a_provisioning_uri_and_one_time_recovery_codes(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    status = c.get("/api/auth/2fa/status").json()
    assert status["enabled"] is False
    assert status["available"] is True

    begin = c.post("/api/auth/2fa/begin").json()
    assert begin["otpauth_uri"].startswith("otpauth://totp/")
    assert "acme.test" in begin["otpauth_uri"]
    # still off until a valid code is confirmed
    assert c.get("/api/auth/2fa/status").json()["enabled"] is False
    assert c.post("/api/auth/2fa/confirm", json={"code": "000000"}).status_code == 400

    done = c.post("/api/auth/2fa/confirm", json={"code": clock.code(begin["secret"])}).json()
    assert done["enabled"] is True
    codes = done["recovery_codes"]
    assert len(codes) == len(set(codes)) >= 8
    status = c.get("/api/auth/2fa/status").json()
    assert status["enabled"] is True
    assert status["unused_recovery_codes"] == len(codes)
    assert c.get("/api/auth/me").json()["totp_enabled"] is True
    # an active secret is never overwritten
    assert c.post("/api/auth/2fa/begin").status_code == 409


def test_confirm_without_begin_is_refused(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    assert c.post("/api/auth/2fa/confirm", json={"code": "123456"}).status_code == 400


def test_password_step_alone_never_signs_in_when_2fa_is_on(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    _enrol(c, clock)

    other, res = _first_step("owner@acme.test")
    assert res.status_code == 200
    body = res.json()
    assert body["requires_totp"] is True
    assert "pending_token" in body
    assert other.get("/api/auth/me").status_code == 401


def test_second_step_accepts_a_current_code_and_rejects_a_wrong_one(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret, _codes = _enrol(c, clock)

    other, res = _first_step("owner@acme.test")
    pending = res.json()["pending_token"]
    wrong = "000000" if clock.code(secret) != "000000" else "111111"
    assert other.post("/api/auth/login/totp", json={"pending_token": pending, "code": wrong}).status_code == 400
    assert other.get("/api/auth/me").status_code == 401

    ok = other.post("/api/auth/login/totp", json={"pending_token": pending, "code": clock.code(secret)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["email"] == "owner@acme.test"
    assert other.get("/api/auth/me").status_code == 200


def test_an_authenticator_code_works_only_once(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret, _ = _enrol(c, clock)

    first, res = _first_step("owner@acme.test")
    code = clock.code(secret)
    assert first.post("/api/auth/login/totp", json={"pending_token": res.json()["pending_token"], "code": code}).status_code == 200

    # the same code, replayed within its validity window, is refused
    replay, res2 = _first_step("owner@acme.test")
    assert replay.post("/api/auth/login/totp", json={"pending_token": res2.json()["pending_token"], "code": code}).status_code == 400
    assert replay.get("/api/auth/me").status_code == 401

    # the code that confirmed enrolment is spent too
    older, res3 = _first_step("owner@acme.test")
    spent = pyotp.TOTP(secret).at(clock.now - 30)
    assert older.post("/api/auth/login/totp", json={"pending_token": res3.json()["pending_token"], "code": spent}).status_code == 400

    # the next step's code is fine
    clock.next_step()
    later, res4 = _first_step("owner@acme.test")
    assert later.post("/api/auth/login/totp", json={"pending_token": res4.json()["pending_token"], "code": clock.code(secret)}).status_code == 200


def test_codes_from_far_outside_the_window_are_refused(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret, _ = _enrol(c, clock)
    other, res = _first_step("owner@acme.test")
    stale = pyotp.TOTP(secret).at(clock.now - 300)
    assert other.post("/api/auth/login/totp", json={"pending_token": res.json()["pending_token"], "code": stale}).status_code == 400


def test_a_tampered_pending_token_is_refused(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret, _ = _enrol(c, clock)
    other = new_client()
    res = other.post("/api/auth/login/totp", json={"pending_token": "garbage.token", "code": clock.code(secret)})
    assert res.status_code == 400


def test_a_recovery_code_works_once(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    _secret, codes = _enrol(c, clock)

    other, res = _first_step("owner@acme.test")
    pending = res.json()["pending_token"]
    ok = other.post("/api/auth/login/totp", json={"pending_token": pending, "code": codes[0]})
    assert ok.status_code == 200, ok.text
    assert other.get("/api/auth/2fa/status").json()["unused_recovery_codes"] == len(codes) - 1

    again, res2 = _first_step("owner@acme.test")
    reuse = again.post("/api/auth/login/totp", json={"pending_token": res2.json()["pending_token"], "code": codes[0]})
    assert reuse.status_code == 400


def test_regenerating_recovery_codes_needs_the_password_and_voids_the_old_ones(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    _secret, old = _enrol(c, clock)
    assert c.post("/api/auth/2fa/recovery-codes/regenerate", json={"password": "wrong-wrong"}).status_code == 400
    new = c.post("/api/auth/2fa/recovery-codes/regenerate", json={"password": PASSWORD}).json()["recovery_codes"]
    assert set(new).isdisjoint(old)

    other, res = _first_step("owner@acme.test")
    stale = other.post("/api/auth/login/totp", json={"pending_token": res.json()["pending_token"], "code": old[0]})
    assert stale.status_code == 400


def test_disabling_needs_the_password_and_restores_plain_login(client, clock):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    _enrol(c, clock)
    assert c.post("/api/auth/2fa/disable", json={"password": "wrong-wrong"}).status_code == 400
    assert c.post("/api/auth/2fa/disable", json={"password": PASSWORD}).status_code == 204
    assert c.get("/api/auth/2fa/status").json()["enabled"] is False
    # regenerating makes no sense once it is off
    assert c.post("/api/auth/2fa/recovery-codes/regenerate", json={"password": PASSWORD}).status_code == 400

    other, res = _first_step("owner@acme.test")
    assert res.status_code == 200
    assert "requires_totp" not in res.json()
    assert other.get("/api/auth/me").status_code == 200


def test_two_factor_can_be_turned_off_for_the_whole_server(client, monkeypatch):
    from app.config import get_settings

    c, _ = sign_up("Acme Corp", "owner@acme.test")
    monkeypatch.setattr(get_settings(), "TOTP_ENABLED", False)
    assert c.get("/api/auth/2fa/status").json() == {
        "enabled": False, "available": False, "enrolled_at": None, "unused_recovery_codes": 0}
    assert c.post("/api/auth/2fa/begin").status_code in (400, 403, 404)


def test_an_admin_can_turn_off_a_colleagues_two_factor_and_only_admins_see_who_has_it(client, clock):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    owner.post("/api/users", json={"name": "Dana", "email": "dana@acme.test", "role": "user", "password": PASSWORD})
    owner.post("/api/users", json={"name": "Max", "email": "max@acme.test", "role": "manager", "password": PASSWORD})
    dana, res = _first_step("dana@acme.test")
    assert dana.get("/api/auth/me").status_code == 200
    secret, _codes = _enrol(dana, clock)
    dana_id = next(u["id"] for u in owner.get("/api/users").json() if u["email"] == "dana@acme.test")

    listing = {u["email"]: u["totp_enabled"] for u in owner.get("/api/users").json()}
    assert listing["dana@acme.test"] is True
    assert listing["max@acme.test"] is False
    manager, _ = _first_step("max@acme.test")
    assert all(u["totp_enabled"] is None for u in manager.get("/api/users").json())

    # only admins may reset, and only inside their own organization
    assert manager.post(f"/api/users/{dana_id}/reset-2fa").status_code == 403
    other, _ = sign_up("Other Org", "owner@other.test")
    assert other.post(f"/api/users/{dana_id}/reset-2fa").status_code == 404

    assert owner.post(f"/api/users/{dana_id}/reset-2fa").status_code == 204
    assert owner.post(f"/api/users/{dana_id}/reset-2fa").status_code == 400  # nothing left to reset
    plain, res = _first_step("dana@acme.test")
    assert "requires_totp" not in res.json()
    assert plain.get("/api/auth/2fa/status").json()["enabled"] is False
