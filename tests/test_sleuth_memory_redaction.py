"""Unit tests for two pure Sleuth helpers (no DB): secret redaction and the per-user memory store."""
from __future__ import annotations

from app.chatbot import memory as mem
from app.chatbot.redaction import redact


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def test_redact_empty_and_none():
    assert redact("") == ""
    assert redact(None) == ""  # type: ignore[arg-type]  # negative-path test


def test_redact_scrubs_common_secret_shapes():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456"
    samples = {
        "password: hunter2": "[REDACTED]",  # synthetic test fixture, not a real credential
        "api_key = sk-abcdefghijklmnop1234": "[REDACTED]",
        "Authorization: Bearer abcdef.ghijkl": "[REDACTED]",
        f"token {jwt}": "[REDACTED]",
        "AIzaSyD-1234567890abcdefghijklmnopqrstuv": "[REDACTED]",
        "ghp_0123456789abcdefghij0123456789abcd": "[REDACTED]",
        "AKIAIOSFODNN7EXAMPLE": "[REDACTED]",
        "deadbeefdeadbeefdeadbeefdeadbeef0000": "[REDACTED]",
    }
    for text, marker in samples.items():
        assert marker in redact(text), text

    # Confirm the raw secret is gone, not just that [REDACTED] appears (a naive regex could scrub the label only).
    leak_checks = {
        "Authorization: Bearer abcdef.ghijkl": "abcdef.ghijkl",
        "api_key = sk-abcdefghijklmnop1234": "sk-abcdefghijklmnop1234",
        "github_pat_11ABCDEFG0123456789abcdef": "github_pat_11ABCDEFG0123456789abcdef",
        "bearer sk-zzzzzzzzzzzzzzzzzzzz": "sk-zzzzzzzzzzzzzzzzzzzz",
    }
    for text, secret in leak_checks.items():
        assert secret not in redact(text), text


def test_redact_pem_private_key_block():
    # Fake body; exercises the PEM-block regex without touching a real key.
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "NOT-A-REAL-KEY-THIS-IS-ONLY-TEST-FIXTURE-PADDING-XXXXXXXXXXXX\n"
        "-----END RSA PRIVATE KEY-----"
    )  # synthetic, not a real private key
    assert "[REDACTED]" in redact(pem)
    assert "BEGIN RSA PRIVATE KEY" not in redact(pem)


def test_redact_keeps_innocuous_text():
    clean = "The login button is broken on the dashboard."
    assert redact(clean) == clean


def test_redact_keeps_label_when_scrubbing_value():
    out = redact("password: superSecretValue")
    assert "password" in out
    assert "superSecretValue" not in out


# ---------------------------------------------------------------------------
# Conversation memory store
# ---------------------------------------------------------------------------
def _fresh_store():
    s = mem._Store()
    return s


def test_touch_creates_and_get_reads_without_creating():
    s = _fresh_store()
    assert s.get(1) is None          # read doesn't create
    sess = s.touch(1)                # create
    assert sess is not None
    assert s.get(1) is sess


def test_remember_bug():
    s = _fresh_store()
    s.remember_bug(5, 99)
    assert s.get(5).last_bug_id == 99


def test_pending_action_stage_take_is_single_use():
    s = _fresh_store()
    s.stage_pending(3, {"op": "close", "bug": 1})
    assert s.take_pending(3) == {"op": "close", "bug": 1}
    assert s.take_pending(3) is None          # already consumed
    assert s.take_pending(999) is None        # unknown user


def test_reset():
    s = _fresh_store()
    s.remember_bug(8, 1)
    s.reset(8)
    assert s.get(8) is None


def test_ttl_eviction(monkeypatch):
    s = _fresh_store()
    clock = {"t": 1000.0}
    monkeypatch.setattr(mem.time, "time", lambda: clock["t"])
    s.touch(1)
    clock["t"] += mem._TTL_SECONDS + 1        # age past the TTL
    assert s.get(1) is None                    # evicted on read


def test_capacity_eviction_drops_lru(monkeypatch):
    s = _fresh_store()
    clock = {"t": 0.0}
    monkeypatch.setattr(mem.time, "time", lambda: clock["t"])
    for i in range(mem._MAX_SESSIONS):
        clock["t"] += 1
        s.touch(i)
    assert len(s._all_sessions_for_test()) == mem._MAX_SESSIONS
    clock["t"] += 1
    s.touch(10_000)                            # over cap; LRU (uid 0) should be evicted
    sessions = s._all_sessions_for_test()
    assert len(sessions) == mem._MAX_SESSIONS
    assert 0 not in sessions
    assert 10_000 in sessions
    s._clear_all_for_test()
    assert s._all_sessions_for_test() == {}
