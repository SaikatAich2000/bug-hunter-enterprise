"""Configurable password policy (_check_password_strength); no password is exempt.
Imports live inside tests; Settings class attributes are patched so every instance sees the change.
"""
import pytest


def _check():
    """Return _check_password_strength from the currently loaded app.schemas."""
    from app.schemas import _check_password_strength
    return _check_password_strength


@pytest.mark.parametrize("pw", ["legacy-default", "LEGACY-DEFAULT", "changeme", "ChangeMe"])
def test_no_password_bypasses_the_policy(pw):
    with pytest.raises(ValueError):
        _check()(pw)


def test_default_rules_unchanged():
    check = _check()
    assert check("abcd1234") == "abcd1234"     # letter + digit, length 8: passes
    with pytest.raises(ValueError):
        check("short1")                        # too short
    with pytest.raises(ValueError):
        check("abcdefgh")                      # no digit
    with pytest.raises(ValueError):
        check("password123")                   # on the common-password list


def test_configurable_min_length(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 12)
    check = _check()
    with pytest.raises(ValueError):
        check("abcd1234")                      # 8 chars falls below the new minimum
    assert check("abcdefgh1234") == "abcdefgh1234"


def test_complexity_can_be_disabled(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_REQUIRE_COMPLEXITY", False)
    check = _check()
    assert check("abcdefgh") == "abcdefgh"     # letters-only accepted when complexity is off


def test_too_long_rejected_regardless():
    check = _check()
    with pytest.raises(ValueError):
        check("a1" * 200)                      # 400 chars exceeds the 200-char DoS guard


def test_signup_and_invitation_accept_refuse_the_old_exempt_passwords(client):
    """Tenancy: the two enterprise paths that create accounts (sign-up, invitation accept) run
    the same policy, so no tenant can start with "changeme"."""
    for pw in ("changeme", "legacy-default"):
        res = client.post("/api/auth/signup", json={
            "name": "Owner", "email": "owner@policy.test", "password": pw, "organization_name": "Policy Org",
        })
        assert res.status_code == 422, res.text
    res = client.post("/api/invitations/accept", json={
        "token": "x" * 40, "name": "Invitee", "password": "changeme",
    })
    assert res.status_code == 422, res.text
