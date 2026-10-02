"""Unit tests for pure helpers in app/auth.py: hashing, session tokens, reset tokens, role predicates."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import auth


def _u(role: str):
    return SimpleNamespace(role=role)


# Password hashing
def test_hash_password_roundtrip():
    h = auth.hash_password("CorrectHorse9")
    assert h
    assert h != "CorrectHorse9"
    assert auth.verify_password("CorrectHorse9", h) is True
    assert auth.verify_password("wrong", h) is False


def test_hash_password_rejects_empty():
    with pytest.raises(ValueError):
        auth.hash_password("")


def test_verify_password_false_paths():
    assert auth.verify_password("", "whatever") is False
    assert auth.verify_password("pw", None) is False
    # Malformed hash causes bcrypt to raise; verify_password swallows it and returns False.
    assert auth.verify_password("pw", "not-a-bcrypt-hash") is False


# Session tokens
def test_session_token_roundtrip_with_jti():
    jti = auth.new_jti()
    tok = auth.make_session_token(42, 3, jti=jti)
    assert auth.parse_session_token(tok) == (42, 3, jti)


def test_session_token_roundtrip_without_jti():
    tok = auth.make_session_token(7, 1)
    assert auth.parse_session_token(tok) == (7, 1, None)


def test_parse_session_token_rejects_garbage():
    assert auth.parse_session_token("") is None
    assert auth.parse_session_token("not.a.valid.token") is None


def test_parse_session_token_legacy_single_int():
    # Pre-versioned cookies carried a bare int; the parser treats them as v0.
    signed = auth._signer().sign(b"99").decode("utf-8")
    assert auth.parse_session_token(signed) == (99, 0, None)


def test_parse_session_token_non_int_payload_is_none():
    signed = auth._signer().sign(b"abc:def").decode("utf-8")
    assert auth.parse_session_token(signed) is None


def test_parse_session_token_too_many_parts_is_none():
    signed = auth._signer().sign(b"1:2:3:4").decode("utf-8")
    assert auth.parse_session_token(signed) is None


# Reset tokens
def test_reset_token_hash_is_stable_and_matches():
    raw, h = auth.generate_token()
    assert h == auth.hash_token(raw)
    assert h != raw
    assert len(h) == 64  # SHA-256 hex digest


# Role-based permission predicates
@pytest.mark.parametrize(
    "fn,admin,manager,user",
    [
        (auth.can_delete_bug, True, False, False),
        (auth.can_edit_event, True, True, False),
        (auth.can_delete_event, True, False, False),
        (auth.can_manage_projects, True, True, False),
    ],
)
def test_permission_predicates(fn, admin, manager, user):
    assert fn(_u("admin")) is admin
    assert fn(_u("manager")) is manager
    assert fn(_u("user")) is user


def test_can_edit_bug_per_type():
    # Regular bugs are open to all roles; Requirements and Tasks are manager-or-above.
    assert auth.can_edit_bug(_u("user"), None, [], "Bug") is True
    assert auth.can_edit_bug(_u("user"), None, [], "Requirement") is False
    assert auth.can_edit_bug(_u("manager"), None, [], "Task") is True
    assert auth.can_edit_bug(_u("admin"), None, [], "Requirement") is True
