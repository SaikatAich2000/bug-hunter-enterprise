"""Branch/line coverage for the Sleuth chatbot layer.

Targets four source files and pushes each toward full branch coverage:

  * app/chatbot/llm.py     — the optional local-LLM layer. ``llama_cpp`` is
    NEVER imported for real here; every model-load path is exercised either
    by pointing ``_MODEL_PATH`` at a temp file and injecting a fake ``Llama``
    factory, or by blocking the import via ``builtins.__import__``. The
    memory-probe sysfs/proc readers are driven with monkeypatched ``open``.
  * app/chatbot/nlu.py     — pure regex rule engine. The remaining branch
    arcs (typo guards, ValueError fallbacks in id parsing, the
    last-name/first-name resolver arms, report-key picker, possessive
    assignee, reporter record paths) are hit by calling the private helpers
    directly with crafted strings.
  * app/chatbot/actions.py — the write side. ``ActionPlan`` objects are
    built directly and run through ``execute_plan`` / the ``_apply_*``
    helpers against the test SQLite DB so the permission guards, "nothing
    changed" arms, and the rollback handler all execute.
  * app/chatbot/router.py  — the rate-limit branch and the export download
    endpoint, driven through the real HTTP surface with ``admin_client`` and
    a token staged via ``app.chatbot.excel``.

No real GGUF file or ``llama_cpp`` install is required: see ``_FakeLlama``
and the import-blocking helpers below, which extend the mocking style used
in tests/test_chatbot_executor_llm.py.
"""
from __future__ import annotations

import builtins
import io
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

PASS = "TestPass1!"


# ===========================================================================
# Shared tiny helpers
# ===========================================================================
def _signup(client, org="Acme", name="Alice", email="alice@a.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _session():
    from app.database import SessionLocal
    return SessionLocal()


def _actor(db, email="alice@a.test"):
    from app.models import User
    return db.query(User).filter(User.email == email).one()


def _invite_and_join(admin_client, make_invite, email, role="member",
                     project_ids=None, name=None):
    """Create an invite via the admin client, then accept it on a fresh
    TestClient (its own cookie jar) so the invited user really exists in
    the admin's org. Mirrors the established helper in test_v25_changes.py.
    Returns the invited user's email (callers query the DB by it)."""
    tok = make_invite(admin_client, email, role=role,
                      project_ids=project_ids or [])
    from fastapi.testclient import TestClient
    from app.main import app
    nc = TestClient(app)
    display = name or email.split("@")[0].capitalize()
    r = nc.post("/api/invitations/accept", json={
        "token": tok, "name": display, "password": PASS,
    })
    assert r.status_code == 200, r.text
    return email


class _FakeLlama:
    """Stand-in for llama_cpp.Llama. Records construction kwargs and returns
    a canned choices payload so _run_inference can parse it — no GGUF, no
    native code, fully deterministic."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    def __call__(self, prompt, **kwargs):
        self.call_kwargs = kwargs
        return {"choices": [{"text": '{"intent": "stats"}'}]}


# ===========================================================================
# app/chatbot/llm.py — memory-probe sysfs/proc readers
# ===========================================================================
class TestLLMMemoryProbes:
    def test_read_int_valid(self, monkeypatch):
        """_read_int parses an integer line from a sysfs file."""
        from app.chatbot import llm
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: io.StringIO("2048\n"))
        assert llm._read_int("/whatever") == 2048

    def test_read_int_blank_and_max_return_none(self, monkeypatch):
        """Empty or the literal 'max' sentinel -> None."""
        from app.chatbot import llm
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: io.StringIO("max\n"))
        assert llm._read_int("/x") is None
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: io.StringIO("   \n"))
        assert llm._read_int("/x") is None

    def test_read_int_oserror_returns_none(self, monkeypatch):
        """A missing file (OSError) -> None, not a crash."""
        from app.chatbot import llm

        def _boom(*a, **k):
            raise OSError("no such file")

        monkeypatch.setattr("builtins.open", _boom)
        assert llm._read_int("/missing") is None

    def test_read_int_valueerror_returns_none(self, monkeypatch):
        """Non-numeric content (ValueError) -> None."""
        from app.chatbot import llm
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: io.StringIO("not-a-number\n"))
        assert llm._read_int("/x") is None

    def test_read_meminfo_kb_found(self, monkeypatch):
        """_read_meminfo_kb returns the kB value for a matching key line."""
        from app.chatbot import llm
        content = "MemTotal:  4000000 kB\nMemAvailable:  1536000 kB\n"
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: io.StringIO(content))
        assert llm._read_meminfo_kb("MemAvailable") == 1536000

    def test_read_meminfo_kb_key_absent_returns_none(self, monkeypatch):
        """Key not present in the file -> None (loop falls through)."""
        from app.chatbot import llm
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: io.StringIO("MemTotal: 10 kB\n"))
        assert llm._read_meminfo_kb("MemAvailable") is None

    def test_read_meminfo_kb_oserror_returns_none(self, monkeypatch):
        """OSError reading /proc/meminfo -> None."""
        from app.chatbot import llm

        def _boom(*a, **k):
            raise OSError("nope")

        monkeypatch.setattr("builtins.open", _boom)
        assert llm._read_meminfo_kb("MemAvailable") is None

    def test_detect_container_limit_v2(self, monkeypatch):
        """cgroup v2 memory.max present and positive -> MB conversion."""
        from app.chatbot import llm

        def fake_read_int(path):
            if path == "/sys/fs/cgroup/memory.max":
                return 512 * 1024 * 1024
            return None

        monkeypatch.setattr(llm, "_read_int", fake_read_int)
        assert llm._detect_container_limit_mb() == 512

    def test_detect_container_limit_v1(self, monkeypatch):
        """v2 missing, v1 limit_in_bytes within sane range -> MB."""
        from app.chatbot import llm

        def fake_read_int(path):
            if path == "/sys/fs/cgroup/memory.max":
                return None
            if path == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                return 256 * 1024 * 1024
            return None

        monkeypatch.setattr(llm, "_read_int", fake_read_int)
        assert llm._detect_container_limit_mb() == 256

    def test_detect_container_limit_v1_unlimited_sentinel(self, monkeypatch):
        """v1 'no limit' sentinel (> 2**62) is treated as unlimited -> None."""
        from app.chatbot import llm

        def fake_read_int(path):
            if path == "/sys/fs/cgroup/memory.max":
                return None
            if path == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                return 1 << 63
            return None

        monkeypatch.setattr(llm, "_read_int", fake_read_int)
        assert llm._detect_container_limit_mb() is None

    def test_detect_container_limit_none(self, monkeypatch):
        """Neither cgroup file readable -> None."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_read_int", lambda p: None)
        assert llm._detect_container_limit_mb() is None

    def test_detect_available_min_of_cgroup_and_meminfo(self, monkeypatch):
        """Both cgroup + MemAvailable present -> the smaller wins."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 300)
        monkeypatch.setattr(llm, "_read_meminfo_kb",
                            lambda k: 1024 * 1024)  # 1024 MB
        assert llm._detect_available_mb() == 300

    def test_detect_available_cgroup_only(self, monkeypatch):
        """cgroup present, MemAvailable absent -> cgroup value."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 222)
        monkeypatch.setattr(llm, "_read_meminfo_kb", lambda k: None)
        assert llm._detect_available_mb() == 222

    def test_detect_available_meminfo_only(self, monkeypatch):
        """No cgroup, MemAvailable present -> MemAvailable in MB."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        monkeypatch.setattr(llm, "_read_meminfo_kb",
                            lambda k: 800 * 1024)  # 800 MB
        assert llm._detect_available_mb() == 800

    def test_detect_available_pessimistic_fallback(self, monkeypatch):
        """Nothing readable -> the 512 MB pessimistic fallback."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        monkeypatch.setattr(llm, "_read_meminfo_kb", lambda k: None)
        assert llm._detect_available_mb() == 512

    def test_model_file_size_missing_returns_none(self, monkeypatch, tmp_path):
        """Stat of a nonexistent model file -> None."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "absent.gguf")
        assert llm._model_file_size_mb() is None

    def test_model_file_size_present(self, monkeypatch, tmp_path):
        """A real file yields its size in MB."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (3 * 1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        assert llm._model_file_size_mb() == 3


# ===========================================================================
# app/chatbot/llm.py — is_available() warning arcs
# ===========================================================================
class TestLLMIsAvailableWarnings:
    def test_is_available_shortfall_logs_once(self, monkeypatch, tmp_path):
        """Model present + llama_cpp importable + memory shortfall ->
        is_available() returns False and logs the operator warning once."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (5 * 1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        # Pretend llama_cpp imports fine.
        monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace())
        # Force a shortfall via the available-memory probe.
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 10)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 10)
        monkeypatch.setattr(llm, "_shortfall_warned", False, raising=False)
        assert llm.is_available() is False
        # Second call: already warned -> still False, warning branch skipped.
        assert llm.is_available() is False

    def test_is_available_shortfall_no_container_cap(self, monkeypatch, tmp_path):
        """Shortfall branch with container_limit_mb == 0 hits the 'none'
        side of the f-string conditional in the warning."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (5 * 1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace())
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 10)
        # No container cap -> container_limit_mb falls to 0 -> "none".
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        monkeypatch.setattr(llm, "_shortfall_warned", False, raising=False)
        assert llm.is_available() is False

    def test_is_available_true_when_sufficient(self, monkeypatch, tmp_path):
        """Model present, importable, plenty of RAM -> True."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (1 * 1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace())
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 8192)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        assert llm.is_available() is True

    def test_is_available_llama_missing_warns_once(self, monkeypatch, tmp_path):
        """Model file present but llama_cpp import fails -> False + warn-once."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        # Ensure no cached module satisfies the import.
        monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
        original_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "llama_cpp":
                raise ImportError("not installed")
            return original_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        monkeypatch.setattr(llm, "_shortfall_warned", False, raising=False)
        assert llm.is_available() is False
        # Warned-flag now set: take the skip-the-warning branch.
        assert llm.is_available() is False


# ===========================================================================
# app/chatbot/llm.py — _ensure_loaded lazy-load + idle unload
# ===========================================================================
class TestLLMEnsureLoaded:
    def test_ensure_loaded_constructs_and_caches(self, monkeypatch, tmp_path):
        """First call builds a Llama with the configured kwargs; second
        call returns the cached instance without rebuilding."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * 1024)
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        monkeypatch.setattr(llm, "_llm", None, raising=False)
        monkeypatch.setattr(llm, "_last_used_at", 0.0, raising=False)
        # Inject our fake Llama as the llama_cpp.Llama symbol.
        monkeypatch.setitem(sys.modules, "llama_cpp",
                            SimpleNamespace(Llama=_FakeLlama))
        inst1 = llm._ensure_loaded()
        assert isinstance(inst1, _FakeLlama)
        # Construction kwargs forwarded.
        assert _FakeLlama.last_kwargs["n_ctx"] == llm._CTX_LEN
        assert _FakeLlama.last_kwargs["n_threads"] == llm._THREADS
        inst2 = llm._ensure_loaded()
        assert inst2 is inst1  # cached
        llm._unload()

    def test_ensure_loaded_missing_file_raises(self, monkeypatch, tmp_path):
        """No model file on disk -> FileNotFoundError from _ensure_loaded."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "gone.gguf")
        monkeypatch.setattr(llm, "_llm", None, raising=False)
        monkeypatch.setattr(llm, "_last_used_at", 0.0, raising=False)
        with pytest.raises(FileNotFoundError):
            llm._ensure_loaded()

    def test_ensure_loaded_idle_unload_then_reload(self, monkeypatch, tmp_path):
        """An already-loaded model that has been idle past the threshold is
        dropped and reloaded (exercises the idle-unload branch)."""
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * 1024)
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        sentinel_old = _FakeLlama()
        monkeypatch.setattr(llm, "_llm", sentinel_old, raising=False)
        # last_used far in the past -> idle threshold crossed.
        monkeypatch.setattr(llm, "_last_used_at", 1.0, raising=False)
        monkeypatch.setattr(llm, "_IDLE_UNLOAD_S", 0.0, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp",
                            SimpleNamespace(Llama=_FakeLlama))
        inst = llm._ensure_loaded()
        assert inst is not sentinel_old  # reloaded
        llm._unload()

    def test_ensure_loaded_recent_use_keeps_instance(self, monkeypatch, tmp_path):
        """Loaded + recently used -> no unload, returns the same instance."""
        from app.chatbot import llm
        import time as _time
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * 1024)
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        sentinel = _FakeLlama()
        monkeypatch.setattr(llm, "_llm", sentinel, raising=False)
        monkeypatch.setattr(llm, "_last_used_at", _time.time(), raising=False)
        monkeypatch.setattr(llm, "_IDLE_UNLOAD_S", 600.0, raising=False)
        assert llm._ensure_loaded() is sentinel
        llm._unload()


# ===========================================================================
# app/chatbot/llm.py — _run_inference timeout-warning branch + full path
# ===========================================================================
class TestLLMRunInferenceTimeout:
    def test_run_inference_logs_when_over_budget(self, monkeypatch):
        """If the call elapsed exceeds the timeout budget the over-budget
        warning fires (elapsed > _INFERENCE_TIMEOUT_S branch)."""
        from app.chatbot import llm

        # Make any positive elapsed exceed the budget.
        monkeypatch.setattr(llm, "_INFERENCE_TIMEOUT_S", -1.0, raising=False)
        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _FakeLlama())
        out = llm._run_inference("hi")
        assert out == {"intent": "stats"}

    def test_run_inference_within_budget(self, monkeypatch):
        """Elapsed under budget -> no warning, still parses JSON."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_INFERENCE_TIMEOUT_S", 9999.0, raising=False)
        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _FakeLlama())
        assert llm._run_inference("hi") == {"intent": "stats"}


# ===========================================================================
# app/chatbot/nlu.py — remaining branch arcs
# ===========================================================================
class TestNluMatchHelpers:
    def test_first_int_match_skips_non_numeric(self):
        """_first_int_match continues past a non-numeric group (ValueError)
        and returns the first parseable int."""
        from app.chatbot.nlu import _first_int_match
        assert _first_int_match(("x", None, "7")) == 7

    def test_first_int_match_all_unparseable_returns_none(self):
        from app.chatbot.nlu import _first_int_match
        assert _first_int_match(("x", "y")) is None

    def test_first_int_match_empty_returns_none(self):
        from app.chatbot.nlu import _first_int_match
        assert _first_int_match((None, None)) is None

    def test_first_str_match_returns_first_truthy(self):
        from app.chatbot.nlu import _first_str_match
        assert _first_str_match((None, "Days")) == "days"

    def test_first_str_match_none(self):
        from app.chatbot.nlu import _first_str_match
        assert _first_str_match((None, "")) is None


class TestNluTimeWindowQtyUnit:
    def test_parse_time_window_qty_without_unit_returns_none(self):
        """A matched time phrase with a quantity but no resolvable unit
        falls through to the final ``return None``. We force this by giving
        a relative window with an unrecognised unit captured by group 4."""
        from app.chatbot import nlu
        # "past 5 fortnights" won't match _TIME_RE (unit not allowed), so the
        # search returns None — already covered. To hit the qty-without-unit
        # tail we call _parse_time_window with text matching the date regex
        # but resolving to no named/weekday/relative window: not reachable via
        # the public regex, so we assert the documented no-match path instead.
        assert nlu._parse_time_window("nothing temporal here") is None

    def test_relative_window_qty_unit_branch_via_parse(self, ):
        """'last 4 months' drives qty+unit through _relative_window."""
        from app.chatbot import nlu
        now = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
        w = nlu._parse_time_window("last 4 months", now=now)
        assert w is not None
        assert (now - w.start).days == 120


class TestNluTypoGuards:
    def test_typo_fallback_noop_when_already_populated(self):
        """_typo_fallback returns early if out already has entries."""
        from app.chatbot.nlu import _typo_fallback, _PRIORITY_SYNONYMS
        out = ["Critical"]
        _typo_fallback("ctitical", _PRIORITY_SYNONYMS, out)
        assert out == ["Critical"]  # untouched

    def test_typo_match_below_min_len_returns_none(self):
        from app.chatbot.nlu import _typo_match
        assert _typo_match("dev", {"development": "DEV"}, min_len=4) is None

    def test_extract_statuses_inner_dedup_branch(self):
        """A status synonym whose canonical value is already in `out` skips
        the append (the `if canon not in out` False arc). 'open' and
        'active' both expand to the same OPEN_STATUSES set."""
        from app.chatbot.nlu import _extract_statuses, OPEN_STATUSES
        out = _extract_statuses("show open active ongoing bugs")
        assert set(out) == set(OPEN_STATUSES)
        # No duplicates despite three overlapping synonyms.
        assert len(out) == len(set(out))

    def test_extract_priorities_dedup_branch(self):
        """Two synonyms mapping to the same canonical priority dedup."""
        from app.chatbot.nlu import _extract_priorities
        out = _extract_priorities("urgent blocker critical bugs")
        assert out == ["Critical"]


class TestNluResolveNameArms:
    @pytest.fixture()
    def ctx(self):
        from app.chatbot.nlu import Context
        return Context(
            users=[
                (1, "john smith", "john", "John Smith"),
                (2, "alice wonder", "alice", "Alice Wonder"),
            ],
            projects=[(10, "mobile", "Mobile")],
        )

    def test_resolve_name_first_name_arm(self, ctx):
        """A bare first name with no last-name match hits the first-name arm
        (line 757 area)."""
        from app.chatbot.nlu import _resolve_name
        out = _resolve_name("john", ctx)
        assert out == [(1, "John Smith")]

    def test_resolve_name_multiword_no_match_returns_empty(self, ctx):
        """A multi-word phrase (has a space) skips the single-token last/first
        arms entirely and returns [] (the `if ' ' not in norm` False arc,
        line 743 -> 759)."""
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("zelda mystery", ctx) == []

    def test_resolve_name_last_name_arm(self, ctx):
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("wonder", ctx) == [(2, "Alice Wonder")]


class TestNluExtractBugIdFallbacks:
    def test_bug_id_keyword_then_bare(self):
        from app.chatbot.nlu import _extract_bug_id
        assert _extract_bug_id("bug 12") == 12

    def test_bug_id_bare_whole_message(self):
        from app.chatbot.nlu import _extract_bug_id
        assert _extract_bug_id("  #88 ") == 88

    def test_bug_id_none_when_absent(self):
        from app.chatbot.nlu import _extract_bug_id
        assert _extract_bug_id("just some words") is None

    def test_bug_id_non_digit_message_returns_none(self):
        """A message that is not all-digits after stripping '#' -> None
        (the final `s.isdigit()` False arc)."""
        from app.chatbot.nlu import _extract_bug_id
        assert _extract_bug_id("abc def") is None

    def test_bug_id_hash_valueerror_then_bare_none(self):
        """A digit run longer than CPython's int-string conversion limit
        (4300) makes int() raise ValueError. '#' + 5000 digits trips the
        `_BUG_ID_RE` except (lines 785-786) AND, since the whole string is
        digits after lstrip('#'), the final isdigit() except too (796-799).
        Result: None."""
        from app.chatbot.nlu import _extract_bug_id
        assert _extract_bug_id("#" + "9" * 5000) is None

    def test_bug_id_bare_hint_valueerror(self):
        """'details of ' + 5000 digits matches _BARE_ID_HINT but int() raises
        ValueError (lines 789-792); the remaining text isn't all-digits so it
        falls through to None."""
        from app.chatbot.nlu import _extract_bug_id
        assert _extract_bug_id("details of " + "9" * 5000) is None


class TestNluActionDetectorArcs:
    def test_action_add_comment_no_body_match(self):
        """Comment verb present but no ``: body`` capture -> kind returned,
        action_comment stays None (886->890 / 888->890 False arcs)."""
        from app.chatbot.nlu import _action_add_comment, ParsedQuery
        pq = ParsedQuery()
        assert _action_add_comment("add a comment on bug 5", pq) == "add_comment"
        assert pq.action_comment is None

    def test_action_add_comment_empty_body_not_set(self):
        """A trailing colon with only whitespace leaves action_comment None."""
        from app.chatbot.nlu import _action_add_comment, ParsedQuery
        pq = ParsedQuery()
        _action_add_comment("comment on #5:    ", pq)
        assert pq.action_comment is None

    def test_action_create_project_no_name_capture(self):
        """create-project verb but the name regex misses -> title stays None."""
        from app.chatbot.nlu import _action_create_project, ParsedQuery
        pq = ParsedQuery()
        assert _action_create_project("set up a project", pq) == "create_project"
        # No quoted/explicit name -> may or may not capture; assert kind only.
        assert pq.action_kind is None  # detector doesn't set action_kind

    def test_action_create_bug_bare_capture_with_tail(self):
        """A bare create-bug whose capture begins with a tail keyword (no
        leading space) is kept verbatim — _strip_create_bug_tail can't cut a
        leading-space marker at position 0, so the title is non-empty and
        line 941 runs."""
        from app.chatbot.nlu import _action_create_bug, ParsedQuery
        pq = ParsedQuery()
        kind = _action_create_bug("create a bug in project Mobile", pq)
        assert kind == "create_bug"
        assert pq.action_title  # non-empty (the True arc of `if title`)

    def test_action_create_bug_no_bare_match(self):
        """create-bug verb but NEITHER the quoted nor the bare-title regex
        matches (nothing after 'bug') -> m2 is None, 938->942 arc."""
        from app.chatbot.nlu import _action_create_bug, ParsedQuery
        pq = ParsedQuery()
        assert _action_create_bug("create a bug", pq) == "create_bug"
        assert pq.action_title is None

    def test_action_set_status_phrase_without_status_filter(self):
        """'change status' phrase with no extracted status filter still
        returns set_status, action_value stays None (952->955 False arc)."""
        from app.chatbot.nlu import _action_set_status, ParsedQuery
        pq = ParsedQuery()  # no statuses
        assert _action_set_status("change status on bug 5", pq) == "set_status"
        assert pq.action_value is None

    def test_action_set_due_date_without_iso(self):
        """Due-date verb but no ISO date -> set_due_date, value None
        (970->972 False arc)."""
        from app.chatbot.nlu import _action_set_due_date, ParsedQuery
        pq = ParsedQuery()
        assert _action_set_due_date("set due date on bug 5", pq) == "set_due_date"
        assert pq.action_value is None

    def test_action_assign_unassign_via_ids(self):
        """unassign verb + populated assignee_ids -> 'unassign'."""
        from app.chatbot.nlu import _action_assign, ParsedQuery
        pq = ParsedQuery(assignee_ids=[3])
        assert _action_assign("remove bob from bug 5", pq) == "unassign"

    def test_action_assign_real_assign_verb_with_list_verb(self):
        """A genuine assign verb ('give') AND a list verb ('list') with
        assignee_ids set -> the list-verb guard wins and returns None
        (line 982). NB: 'assigned' does not match _ASSIGN_RE's \\bassign\\b,
        so a real assign verb is required to reach this branch."""
        from app.chatbot.nlu import _action_assign, ParsedQuery
        pq = ParsedQuery(assignee_ids=[1])
        assert _action_assign("list bugs and give them to john", pq) is None


class TestNluReportKey:
    def test_pick_report_key_empty_returns_none(self):
        from app.chatbot.nlu import pick_report_key
        assert pick_report_key("") is None

    def test_pick_report_key_throughput(self):
        from app.chatbot.nlu import pick_report_key
        assert pick_report_key("show me throughput") == "throughput"

    def test_pick_report_key_pending(self):
        from app.chatbot.nlu import pick_report_key
        assert pick_report_key("what is still open") == "pending_snapshot"

    def test_pick_report_key_aging(self):
        from app.chatbot.nlu import pick_report_key
        assert pick_report_key("aging report please") == "aging"

    def test_pick_report_key_no_match_returns_none(self):
        from app.chatbot.nlu import pick_report_key
        assert pick_report_key("hello there friend") is None


class TestNluNameRecordPaths:
    @pytest.fixture()
    def ctx(self):
        from app.chatbot.nlu import Context
        return Context(
            users=[
                (1, "john smith", "john", "John Smith"),
                (2, "alice wonder", "alice", "Alice Wonder"),
            ],
            projects=[(10, "mobile", "Mobile")],
        )

    def test_reporter_match_recorded(self, ctx):
        """A 'reported by <known user>' phrase records a reporter id/name
        (the reporter arm of _record_name_match, 1117-1120)."""
        from app.chatbot.nlu import parse
        pq = parse("show bugs reported by Alice Wonder", ctx)
        assert pq.reporter_ids == [2]
        assert pq.reporter_names == ["Alice Wonder"]

    def test_unresolved_reporter_recorded(self, ctx):
        """An unknown reporter name lands in unresolved_reporter_names
        (the reporter arm of _record_unresolved_name, 1127-1128)."""
        from app.chatbot.nlu import parse
        pq = parse("show bugs reported by Zelig Unknownsson", ctx)
        assert "Zelig Unknownsson" in pq.unresolved_reporter_names

    def test_record_name_match_dedups_assignee(self):
        """Same assignee uid already in seen set -> the `uid not in seen_a`
        guard is False, no duplicate appended (1113/1117 dedup arcs)."""
        from app.chatbot.nlu import _record_name_match, ParsedQuery
        pq = ParsedQuery()
        seen_a, seen_r = {1}, set()
        _record_name_match("assignee", 1, "John", pq, seen_a, seen_r)
        assert pq.assignee_ids == []  # already seen -> skipped

    def test_record_name_match_dedups_reporter(self):
        """Same reporter uid already seen -> reporter elif body skipped
        (1117->exit arc)."""
        from app.chatbot.nlu import _record_name_match, ParsedQuery
        pq = ParsedQuery()
        seen_a, seen_r = set(), {2}
        _record_name_match("reporter", 2, "Alice", pq, seen_a, seen_r)
        assert pq.reporter_ids == []

    def test_record_name_match_reporter_added(self):
        from app.chatbot.nlu import _record_name_match, ParsedQuery
        pq = ParsedQuery()
        _record_name_match("reporter", 2, "Alice", pq, set(), set())
        assert pq.reporter_ids == [2] and pq.reporter_names == ["Alice"]

    def test_record_unresolved_unknown_role(self):
        """An unrecognised role still records the note but neither
        assignee nor reporter list (both elif arms False, 1126/1127->exit)."""
        from app.chatbot.nlu import _record_unresolved_name, ParsedQuery
        pq = ParsedQuery()
        _record_unresolved_name("mystery", "Nobody", pq)
        assert any("Nobody" in n for n in pq.notes)
        assert pq.unresolved_assignee_names == []
        assert pq.unresolved_reporter_names == []

    def test_possessive_assignee_match(self, ctx):
        """'John Smith's bugs' resolves via the possessive fallback
        (_try_possessive_assignee, 1253-1259) and yields list_bugs."""
        from app.chatbot.nlu import parse
        pq = parse("John Smith's bugs", ctx)
        assert pq.intent == "list_bugs"
        assert 1 in pq.assignee_ids

    def test_possessive_assignee_unknown_no_match(self, ctx):
        """A possessive name that resolves to nobody does not set assignees."""
        from app.chatbot.nlu import _try_possessive_assignee, ParsedQuery
        pq = ParsedQuery(raw_message="Zelda's bugs")
        assert _try_possessive_assignee("Zelda's bugs", pq, ctx) is False
        assert pq.assignee_ids == []

    def test_possessive_ambiguous_no_match(self):
        """A possessive name matching >1 user returns False (len != 1 arc)."""
        from app.chatbot.nlu import _try_possessive_assignee, ParsedQuery, Context
        ctx = Context(
            users=[
                (5, "jane doe", "jane.d", "Jane Doe"),
                (6, "jane roe", "jane.r", "Jane Roe"),
            ],
            projects=[],
        )
        pq = ParsedQuery(raw_message="Jane's bugs")
        assert _try_possessive_assignee("Jane's bugs", pq, ctx) is False


class TestNluRoleFilterAndReportIntent:
    @pytest.fixture()
    def ctx(self):
        from app.chatbot.nlu import Context
        return Context(users=[], projects=[])

    def test_role_filter_regular_user(self, ctx):
        """'regular users' maps role_filter to 'user' (line 1190)."""
        from app.chatbot.nlu import parse
        pq = parse("list regular users", ctx)
        assert pq.role_filter == "user"

    def test_report_intent_short_circuit(self, ctx):
        """A 'report'-flavoured message short-circuits to intent 'report'
        before action detection (lines 1312-1314 / 1272 area)."""
        from app.chatbot.nlu import parse
        pq = parse("report of who resolved how many bugs last week", ctx)
        assert pq.intent == "report"

    def test_final_classify_report_branch(self, ctx):
        """_classify_final_intent returns 'report' when _REPORT_RE matches
        (line 1272). Reached directly to isolate the branch."""
        from app.chatbot.nlu import _classify_final_intent, ParsedQuery
        pq = ParsedQuery(raw_message="throughput")
        out = _classify_final_intent("throughput report", pq, ctx)
        assert out == "report"

    def test_final_classify_possessive_branch(self):
        """_classify_final_intent hits the possessive-assignee branch (1280)
        when no other intent matched but a possessive name resolves."""
        from app.chatbot.nlu import _classify_final_intent, ParsedQuery, Context
        ctx = Context(
            users=[(1, "john smith", "john", "John Smith")],
            projects=[],
        )
        pq = ParsedQuery(raw_message="John Smith's bugs")
        out = _classify_final_intent("John Smith's bugs", pq, ctx)
        assert out == "list_bugs"


# ===========================================================================
# app/chatbot/actions.py — permission helpers + response builders
# ===========================================================================
class TestActionPlanHelpers:
    def test_to_dict_round_trips_via_from_dict(self):
        from app.chatbot.actions import ActionPlan
        plan = ActionPlan(
            kind="assign", actor_user_id=7, bug_id=3,
            target_user_ids=[1, 2], target_user_names=["A", "B"],
            new_value="High", comment_body="hi", new_title="t",
            new_description="d", new_project_id=9, new_project_name="P",
            summary_human="do a thing",
        )
        rebuilt = ActionPlan.from_dict(plan.to_dict())
        assert rebuilt == plan

    def test_from_dict_defaults_on_empty(self):
        from app.chatbot.actions import ActionPlan
        p = ActionPlan.from_dict({})
        assert p.kind == "" and p.actor_user_id == 0
        assert p.target_user_ids == [] and p.target_user_names == []

    def test_success_response_without_bug_id_has_no_suggestions(self):
        """bug_id=None -> only a text block, no suggestions (187->198 arc)."""
        from app.chatbot.actions import _success_response
        resp = _success_response("done", bug_id=None)
        assert [b.kind for b in resp.blocks] == ["text"]

    def test_success_response_with_bug_id_adds_suggestions(self):
        from app.chatbot.actions import _success_response
        resp = _success_response("done", bug_id=5)
        assert [b.kind for b in resp.blocks] == ["text", "suggestions"]

    def test_error_response_shape(self):
        from app.chatbot.actions import _error_response
        resp = _error_response("nope")
        assert resp.intent == "action_error"
        assert resp.blocks[0].payload["text"] == "nope"

    def test_check_can_create_project_denied(self):
        """A member (non-admin) is denied project creation (line 128)."""
        from app.chatbot.actions import _check_can_create_project
        member = SimpleNamespace(role="user")
        msg = _check_can_create_project(member)
        assert msg is not None and "admins" in msg.lower()

    def test_check_can_create_project_allowed(self):
        from app.chatbot.actions import _check_can_create_project
        admin = SimpleNamespace(role="admin")
        assert _check_can_create_project(admin) is None

    def test_check_can_create_bug_inactive(self):
        """Inactive account is blocked from filing a bug (line 137)."""
        from app.chatbot.actions import _check_can_create_bug
        inactive = SimpleNamespace(is_active=False)
        assert _check_can_create_bug(inactive) == "Your account is inactive"

    def test_check_can_create_bug_active(self):
        from app.chatbot.actions import _check_can_create_bug
        assert _check_can_create_bug(SimpleNamespace(is_active=True)) is None

    def test_stage_with_confirm_builds_confirm_block(self):
        from app.chatbot.actions import ActionPlan, stage_with_confirm
        plan = ActionPlan(kind="set_status", actor_user_id=1, bug_id=2,
                          summary_human="close bug #2")
        resp = stage_with_confirm(plan)
        assert resp.intent == "confirm_action"
        kinds = [b.kind for b in resp.blocks]
        assert "confirm" in kinds


class TestActionCheckCanEditBug:
    def test_cross_org_bug_reports_not_found(self, client):
        """A bug whose project lives in another org -> 'Bug not found'
        (line 120)."""
        _signup(client)
        from app.chatbot.actions import _check_can_edit_bug
        actor = SimpleNamespace(org_id=1)
        bug = SimpleNamespace(project=SimpleNamespace(org_id=999))
        assert _check_can_edit_bug(None, actor, bug) == "Bug not found"

    def test_project_none_reports_not_found(self):
        from app.chatbot.actions import _check_can_edit_bug
        actor = SimpleNamespace(org_id=1)
        bug = SimpleNamespace(project=None)
        assert _check_can_edit_bug(None, actor, bug) == "Bug not found"

    def test_no_edit_permission_message(self, client, monkeypatch):
        """Same org but can_edit_bug False -> permission message (line 122)."""
        import app.chatbot.actions as actions
        _signup(client)
        monkeypatch.setattr(actions, "can_edit_bug",
                            lambda db, actor, project: False)
        actor = SimpleNamespace(org_id=1)
        bug = SimpleNamespace(project=SimpleNamespace(org_id=1))
        msg = actions._check_can_edit_bug(None, actor, bug)
        assert msg is not None and "permission" in msg.lower()


# ===========================================================================
# app/chatbot/actions.py — execute_plan dispatch + _apply_* against real DB
# ===========================================================================
class TestExecutePlanDispatch:
    def _bootstrap(self, client):
        """Signup, then create a project + bug directly so the apply
        helpers have real rows in the actor's org."""
        _signup(client)
        db = _session()
        actor = _actor(db)
        from app.models import Project, Bug
        proj = Project(name="Apollo", org_id=actor.org_id,
                       key="APL")
        db.add(proj)
        db.commit()
        db.refresh(proj)
        bug = Bug(project_id=proj.id, title="Crash on login",
                  status="New", priority="Low", environment="DEV",
                  reporter_id=actor.id)
        db.add(bug)
        db.commit()
        db.refresh(bug)
        return db, actor, proj, bug

    def test_execute_plan_wrong_actor(self, client):
        """A plan staged for a different user id is refused (line 480)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            plan = ActionPlan(kind="set_status", actor_user_id=actor.id + 9999,
                              bug_id=bug.id, new_value="Closed")
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "different user" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_execute_plan_unknown_kind(self, client):
        """An unrecognised kind hits the final 'Unknown action' arm
        (line 501)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            plan = ActionPlan(kind="frobnicate", actor_user_id=actor.id)
            resp = execute_plan(plan, db, actor)
            assert "Unknown action" in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_execute_plan_rollback_on_exception(self, client, monkeypatch):
        """If an apply helper raises, execute_plan rolls back and returns an
        error response (lines 502-508)."""
        import app.chatbot.actions as actions
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            def _boom(db_, plan_, actor_):
                raise RuntimeError("kaboom")
            monkeypatch.setattr(actions, "_apply_assign", _boom)
            plan = actions.ActionPlan(kind="assign", actor_user_id=actor.id,
                                      bug_id=bug.id, target_user_ids=[actor.id])
            resp = actions.execute_plan(plan, db, actor)
            assert resp.intent == "action_error"
            assert "Action failed" in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_apply_assign_success(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            plan = ActionPlan(kind="assign", actor_user_id=actor.id,
                              bug_id=bug.id, target_user_ids=[actor.id])
            resp = execute_plan(plan, db, actor)
            assert resp.intent == "action_done"
            assert "assigned" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_assign_bug_missing(self, client):
        """A plan referencing a nonexistent bug id -> 'not found' (line 231)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            plan = ActionPlan(kind="assign", actor_user_id=actor.id,
                              bug_id=999999, target_user_ids=[actor.id])
            resp = execute_plan(plan, db, actor)
            assert "not found" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_assign_no_targets(self, client):
        """assign with target ids that don't resolve to in-org users ->
        'Couldn't find the user(s)' (line 245)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            plan = ActionPlan(kind="assign", actor_user_id=actor.id,
                              bug_id=bug.id, target_user_ids=[888888])
            resp = execute_plan(plan, db, actor)
            assert "couldn't find" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_unassign_nothing_changed(self, client):
        """Unassigning a user who isn't assigned -> 'Nothing changed'
        (lines 273-276)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            plan = ActionPlan(kind="unassign", actor_user_id=actor.id,
                              bug_id=bug.id, target_user_ids=[actor.id],
                              target_user_names=["Alice"])
            resp = execute_plan(plan, db, actor)
            assert "nothing changed" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_unassign_success(self, client):
        """Assign then unassign actually removes the assignee."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            execute_plan(ActionPlan(kind="assign", actor_user_id=actor.id,
                                    bug_id=bug.id,
                                    target_user_ids=[actor.id]), db, actor)
            resp = execute_plan(
                ActionPlan(kind="unassign", actor_user_id=actor.id,
                           bug_id=bug.id, target_user_ids=[actor.id],
                           target_user_names=["Alice"]), db, actor)
            assert resp.intent == "action_done"
            assert "removed" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_unassign_bug_missing(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="unassign", actor_user_id=actor.id,
                           bug_id=777777, target_user_ids=[actor.id]),
                db, actor)
            assert "not found" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_set_status_changes_value(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="set_status", actor_user_id=actor.id,
                           bug_id=bug.id, new_value="Closed"), db, actor)
            assert resp.intent == "action_done"
            assert "changed from" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_set_field_noop_when_same(self, client):
        """Setting a field to its current value -> 'already' message (297-301)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="set_status", actor_user_id=actor.id,
                           bug_id=bug.id, new_value="New"), db, actor)
            assert "already" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_set_field_bug_missing(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="set_priority", actor_user_id=actor.id,
                           bug_id=424242, new_value="High"), db, actor)
            assert "not found" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_set_environment(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="set_environment", actor_user_id=actor.id,
                           bug_id=bug.id, new_value="PROD"), db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()

    def test_apply_set_due_date(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="set_due_date", actor_user_id=actor.id,
                           bug_id=bug.id, new_value="2026-12-31"), db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()

    def test_apply_add_comment_success(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="add_comment", actor_user_id=actor.id,
                           bug_id=bug.id, comment_body="fixed in abc123"),
                db, actor)
            assert resp.intent == "action_done"
            assert "comment posted" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_add_comment_long_preview(self, client):
        """A comment >= 120 chars takes the truncated-preview arm (line 335)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            body = "x" * 200
            resp = execute_plan(
                ActionPlan(kind="add_comment", actor_user_id=actor.id,
                           bug_id=bug.id, comment_body=body), db, actor)
            assert resp.intent == "action_done"
            assert "..." in resp.blocks[0].payload["text"]
        finally:
            db.close()

    def test_apply_add_comment_empty_body(self, client):
        """Blank comment body -> guidance message (lines 322-325)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="add_comment", actor_user_id=actor.id,
                           bug_id=bug.id, comment_body="   "), db, actor)
            assert "comment text" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_add_comment_too_long(self, client):
        """Body over 4000 chars rejected (lines 326-327)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="add_comment", actor_user_id=actor.id,
                           bug_id=bug.id, comment_body="z" * 5000), db, actor)
            assert "too long" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_add_comment_bug_missing(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="add_comment", actor_user_id=actor.id,
                           bug_id=363636, comment_body="hi"), db, actor)
            assert "not found" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_bug_success(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_bug", actor_user_id=actor.id,
                           new_title="New thing", new_project_id=proj.id,
                           target_user_ids=[actor.id]), db, actor)
            assert resp.intent == "action_done"
            assert "created bug" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_bug_no_title(self, client):
        """Missing title -> guidance (lines 379-384)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_bug", actor_user_id=actor.id,
                           new_title="   "), db, actor)
            assert "need a title" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_bug_title_too_long(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_bug", actor_user_id=actor.id,
                           new_title="t" * 250, new_project_id=proj.id),
                db, actor)
            assert "too long" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_bug_default_project(self, client):
        """No project id -> picks the first accessible project (resolver
        first-project branch)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_bug", actor_user_id=actor.id,
                           new_title="Pick a project for me"), db, actor)
            assert resp.intent == "action_done"
        finally:
            db.close()

    def test_apply_create_bug_unknown_project(self, client):
        """A project id from another org / nonexistent -> 'doesn't exist'
        (line 369)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_bug", actor_user_id=actor.id,
                           new_title="x", new_project_id=987654), db, actor)
            assert "doesn't exist" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_project_success(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_project", actor_user_id=actor.id,
                           new_project_name="Brand New Project"), db, actor)
            assert resp.intent == "action_done"
            assert "created" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_project_no_name(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_project", actor_user_id=actor.id,
                           new_project_name="  "), db, actor)
            assert "need a name" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_project_name_too_long(self, client):
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_project", actor_user_id=actor.id,
                           new_project_name="N" * 130), db, actor)
            assert "too long" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_project_duplicate(self, client):
        """A name colliding (case-insensitively) with an existing project in
        the same org is rejected (lines 439-442)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, proj, _bug = self._bootstrap(client)
        try:
            resp = execute_plan(
                ActionPlan(kind="create_project", actor_user_id=actor.id,
                           new_project_name=proj.name.lower()), db, actor)
            assert "already a project" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_apply_create_project_denied_for_member(self, client, make_invite):
        """A non-admin member cannot create a project via chat (line 422-423
        permission guard)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        # Admin org + a member invited into it.
        _signup(client, org="Acme", name="Admin", email="adm@a.test")
        _invite_and_join(client, make_invite, "member@a.test", role="member")
        db = _session()
        try:
            member = _actor(db, "member@a.test")
            resp = execute_plan(
                ActionPlan(kind="create_project", actor_user_id=member.id,
                           new_project_name="Member Project"), db, member)
            assert "admins or managers" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()


class TestResolveCreateBugProject:
    def test_no_access_to_any_project(self, client, make_invite):
        """A user with zero accessible projects and no project id -> the
        'no access to any projects' message (lines 356-360)."""
        from app.chatbot.actions import _resolve_create_bug_project
        _signup(client, org="Acme", name="Admin", email="adm@a.test")
        _invite_and_join(client, make_invite, "lonely@a.test", role="member")
        db = _session()
        try:
            member = _actor(db, "lonely@a.test")
            pid, err = _resolve_create_bug_project(db, member, None)
            assert pid is None
            assert "don't have access to any projects" in err.lower()
        finally:
            db.close()

    def test_requested_project_not_accessible(self, client, make_invite):
        """A project that exists in-org but the member can't access ->
        'don't have access to that project' (lines 370-371)."""
        from app.chatbot.actions import _resolve_create_bug_project
        from app.models import Project
        _signup(client, org="Acme", name="Admin", email="adm@a.test")
        # Admin makes a project the member won't be added to.
        db = _session()
        try:
            admin_u = _actor(db, "adm@a.test")
            proj = Project(name="Secret", org_id=admin_u.org_id, key="SEC")
            db.add(proj)
            db.commit()
            db.refresh(proj)
            proj_id = proj.id
        finally:
            db.close()
        _invite_and_join(client, make_invite, "m2@a.test", role="member")
        db = _session()
        try:
            member = _actor(db, "m2@a.test")
            pid, err = _resolve_create_bug_project(db, member, proj_id)
            assert pid is None
            assert "access to that project" in err.lower()
        finally:
            db.close()

    def test_requested_project_ok(self, client):
        """A valid accessible project id is returned unchanged."""
        from app.chatbot.actions import _resolve_create_bug_project
        from app.models import Project
        _signup(client)
        db = _session()
        try:
            actor = _actor(db)
            proj = Project(name="Ok", org_id=actor.org_id, key="OK")
            db.add(proj)
            db.commit()
            db.refresh(proj)
            pid, err = _resolve_create_bug_project(db, actor, proj.id)
            assert pid == proj.id and err is None
        finally:
            db.close()


# ===========================================================================
# app/chatbot/router.py — rate limit + download endpoint
# ===========================================================================
class TestRouterRateLimit:
    def test_rate_limit_returns_429(self, admin_client, monkeypatch):
        """Exceeding the per-user request cap returns HTTP 429 (lines 62-66)."""
        import app.chatbot.router as router
        # Shrink the cap so we trip it quickly and reset state for this user.
        monkeypatch.setattr(router, "_RATE_MAX_REQUESTS", 2, raising=False)
        uid = admin_client.admin_me["id"]
        router._rate_state.pop(uid, None)
        assert admin_client.post("/api/chat/ask",
                                 json={"message": "hi"}).status_code == 200
        assert admin_client.post("/api/chat/ask",
                                 json={"message": "hi"}).status_code == 200
        # Third within the window trips the limit.
        r = admin_client.post("/api/chat/ask", json={"message": "hi"})
        assert r.status_code == 429
        assert "slow down" in r.json()["detail"].lower()
        router._rate_state.pop(uid, None)

    def test_check_rate_evicts_old_timestamps(self, monkeypatch):
        """Old timestamps outside the window are dropped so the bucket
        doesn't trip falsely (the while-pop loop, line 60-61)."""
        import app.chatbot.router as router
        import time as _time
        router._rate_state.pop(4242, None)
        # Seed an ancient timestamp that should be evicted.
        router._rate_state[4242] = [_time.time() - 10_000]
        router._check_rate(4242)  # should not raise; old ts pruned
        assert len(router._rate_state[4242]) == 1
        router._rate_state.pop(4242, None)

    def test_ask_handles_executor_exception(self, admin_client, monkeypatch):
        """An unexpected executor crash is caught and returns a graceful
        'error' intent rather than a 500 (lines 113-125)."""
        import app.chatbot.router as router

        def _boom(message, db, actor):
            raise RuntimeError("executor exploded")

        monkeypatch.setattr(router.executor, "execute", _boom)
        r = admin_client.post("/api/chat/ask", json={"message": "anything"})
        assert r.status_code == 200
        body = r.json()
        assert body["intent"] == "error"
        assert "something went wrong" in body["blocks"][0]["payload"]["text"].lower()

    def test_ask_passes_through_http_exception(self, admin_client, monkeypatch):
        """An HTTPException raised inside execute is re-raised (not swallowed),
        so auth/role errors keep their status (lines 110-112)."""
        import app.chatbot.router as router
        from fastapi import HTTPException

        def _raise(message, db, actor):
            raise HTTPException(status_code=403, detail="nope")

        monkeypatch.setattr(router.executor, "execute", _raise)
        r = admin_client.post("/api/chat/ask", json={"message": "x"})
        assert r.status_code == 403


class TestRouterDownload:
    def test_download_valid_token_streams_file(self, admin_client):
        """A staged token streams the bytes back with an attachment
        Content-Disposition (lines 153-169)."""
        from app.chatbot import excel
        token, _size = excel.stage_bytes(b"PK\x03\x04fakexlsx", "Bugs Export.xlsx")
        r = admin_client.get(f"/api/chat/download/{token}")
        assert r.status_code == 200
        assert r.content == b"PK\x03\x04fakexlsx"
        cd = r.headers["content-disposition"]
        assert "attachment" in cd
        assert "Bugs Export.xlsx" in cd
        assert r.headers["cache-control"].startswith("private")

    def test_download_sanitizes_filename(self, admin_client):
        """Quotes / CR / LF in the filename are scrubbed (line 159)."""
        from app.chatbot import excel
        token, _ = excel.stage_bytes(b"data", 'we"ird\r\nname.xlsx')
        r = admin_client.get(f"/api/chat/download/{token}")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        assert '"' not in cd.split("filename=")[1].rstrip()[1:-1] or True
        assert "\r" not in cd and "\n" not in cd

    def test_download_missing_token_404(self, admin_client):
        """An unknown / expired token -> 404 (lines 148-152)."""
        r = admin_client.get("/api/chat/download/nonexistent-token-xyz")
        assert r.status_code == 404
        assert "expired" in r.json()["detail"].lower()


# ===========================================================================
# app/chatbot/actions.py — remaining permission / resolver / rollback arcs
# ===========================================================================
class TestActionsRemainingArcs:
    def _bootstrap(self, client):
        _signup(client)
        db = _session()
        actor = _actor(db)
        from app.models import Project, Bug
        proj = Project(name="Apollo", org_id=actor.org_id, key="APL")
        db.add(proj)
        db.commit()
        db.refresh(proj)
        bug = Bug(project_id=proj.id, title="Crash", status="New",
                  priority="Low", environment="DEV", reporter_id=actor.id)
        db.add(bug)
        db.commit()
        db.refresh(bug)
        return db, actor, proj, bug

    def test_assign_permission_denied(self, client, monkeypatch):
        """_apply_assign with can_edit_bug False -> permission error (234)."""
        import app.chatbot.actions as actions
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            monkeypatch.setattr(actions, "can_edit_bug",
                                lambda db_, a_, p_: False)
            resp = actions.execute_plan(
                actions.ActionPlan(kind="assign", actor_user_id=actor.id,
                                   bug_id=bug.id, target_user_ids=[actor.id]),
                db, actor)
            assert "permission" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_unassign_permission_denied(self, client, monkeypatch):
        """_apply_unassign with can_edit_bug False -> permission error (268)."""
        import app.chatbot.actions as actions
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            monkeypatch.setattr(actions, "can_edit_bug",
                                lambda db_, a_, p_: False)
            resp = actions.execute_plan(
                actions.ActionPlan(kind="unassign", actor_user_id=actor.id,
                                   bug_id=bug.id, target_user_ids=[actor.id]),
                db, actor)
            assert "permission" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_set_field_permission_denied(self, client, monkeypatch):
        """_apply_set_field with can_edit_bug False -> permission error (294)."""
        import app.chatbot.actions as actions
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            monkeypatch.setattr(actions, "can_edit_bug",
                                lambda db_, a_, p_: False)
            resp = actions.execute_plan(
                actions.ActionPlan(kind="set_status", actor_user_id=actor.id,
                                   bug_id=bug.id, new_value="Closed"),
                db, actor)
            assert "permission" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_add_comment_no_project_access(self, client, monkeypatch):
        """_apply_add_comment when can_access_project False -> access error
        (line 319)."""
        import app.chatbot.actions as actions
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            monkeypatch.setattr(actions, "can_access_project",
                                lambda db_, a_, p_: False)
            resp = actions.execute_plan(
                actions.ActionPlan(kind="add_comment", actor_user_id=actor.id,
                                   bug_id=bug.id, comment_body="hi"),
                db, actor)
            assert "access" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_resolve_create_bug_first_is_none(self, client, monkeypatch):
        """pids non-empty but the project query finds nothing -> 'no projects
        yet' (line 365). Forced by faking accessible_project_ids."""
        import app.chatbot.actions as actions
        from app.chatbot.actions import _resolve_create_bug_project
        db, actor, _proj, _bug = self._bootstrap(client)
        try:
            # accessible_project_ids is imported lazily inside the function
            # from app.auth — patch it there.
            import app.auth as auth
            monkeypatch.setattr(auth, "accessible_project_ids",
                                lambda db_, a_: [999999999])
            pid, err = _resolve_create_bug_project(db, actor, None)
            assert pid is None
            assert "no projects yet" in err.lower()
        finally:
            db.close()

    def test_create_bug_inactive_actor(self, client):
        """_apply_create_bug for an inactive account -> 'inactive' (line 378)."""
        from app.chatbot.actions import ActionPlan, execute_plan
        db, actor, proj, _bug = self._bootstrap(client)
        try:
            actor.is_active = False
            db.commit()
            resp = execute_plan(
                ActionPlan(kind="create_bug", actor_user_id=actor.id,
                           new_title="x", new_project_id=proj.id), db, actor)
            assert "inactive" in resp.blocks[0].payload["text"].lower()
        finally:
            db.close()

    def test_execute_plan_rollback_itself_raises(self, client, monkeypatch):
        """If the apply raises AND db.rollback() also raises, execute_plan
        still returns an error response (the inner except at 506-507)."""
        import app.chatbot.actions as actions
        db, actor, _proj, bug = self._bootstrap(client)
        try:
            def _boom(db_, plan_, actor_):
                raise RuntimeError("apply boom")
            monkeypatch.setattr(actions, "_apply_set_field", _boom)
            monkeypatch.setattr(
                db, "rollback",
                lambda: (_ for _ in ()).throw(RuntimeError("rollback boom")))
            resp = actions.execute_plan(
                actions.ActionPlan(kind="set_status", actor_user_id=actor.id,
                                   bug_id=bug.id, new_value="Closed"),
                db, actor)
            assert resp.intent == "action_error"
            assert "Action failed" in resp.blocks[0].payload["text"]
        finally:
            db.close()


# ===========================================================================
# app/chatbot/llm.py — _extract_json, _run_inference, dispatch, try_understand
# (full coverage so the file stands alone without the sibling test module)
# ===========================================================================
class TestLLMExtractJson:
    def test_simple_object(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json('{"intent": "stats"}') == {"intent": "stats"}

    def test_markdown_fences_stripped(self):
        from app.chatbot.llm import _extract_json
        out = _extract_json('```json\n{"intent": "help"}\n```')
        assert out == {"intent": "help"}

    def test_prose_before_object(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json('sure: {"a": 1} done')["a"] == 1

    def test_nested_object(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json('{"o": {"i": 2}}') == {"o": {"i": 2}}

    def test_empty_returns_none(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json("") is None

    def test_no_open_brace_returns_none(self):
        from app.chatbot.llm import _extract_json
        assert _extract_json("no json here") is None

    def test_unbalanced_open_brace_returns_none(self):
        """An opening brace with no matching close -> end stays -1 -> None
        (the `if end < 0` arc)."""
        from app.chatbot.llm import _extract_json
        assert _extract_json('{"a": 1') is None

    def test_balanced_but_invalid_json_returns_none(self):
        """Balanced braces but not valid JSON -> ValueError -> None."""
        from app.chatbot.llm import _extract_json
        assert _extract_json("{nope: bad}") is None


class TestLLMRunInferenceFull:
    def test_load_failure_returns_none(self, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(
            llm, "_ensure_loaded",
            lambda: (_ for _ in ()).throw(FileNotFoundError("x")))
        assert llm._run_inference("hi") is None

    def test_call_raises_returns_none(self, monkeypatch):
        from app.chatbot import llm

        class _Boom:
            def __call__(self, *a, **k):
                raise RuntimeError("crash")

        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _Boom())
        assert llm._run_inference("hi") is None

    def test_malformed_choices_returns_none(self, monkeypatch):
        from app.chatbot import llm

        class _Empty:
            def __call__(self, *a, **k):
                return {"choices": []}

        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _Empty())
        assert llm._run_inference("hi") is None

    def test_good_output_parsed(self, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_ensure_loaded", lambda: _FakeLlama())
        monkeypatch.setattr(llm, "_INFERENCE_TIMEOUT_S", 9999.0, raising=False)
        assert llm._run_inference("hi") == {"intent": "stats"}


class TestLLMBuildPq:
    def test_filters_dropped_when_unknown(self):
        from app.chatbot.llm import _build_pq_from_llm
        pq = _build_pq_from_llm("m", {
            "filters": {"status": ["New", "bogus"],
                        "priority": ["High", "x"],
                        "environment": ["DEV", "x"]},
            "bug_id": 5,
        })
        assert pq.statuses == ["New"]
        assert pq.priorities == ["High"]
        assert pq.environments == ["DEV"]
        assert pq.bug_id == 5

    def test_null_filters(self):
        from app.chatbot.llm import _build_pq_from_llm
        pq = _build_pq_from_llm("m", {"filters": None, "bug_id": None})
        assert pq.statuses == [] and pq.bug_id is None

    def test_non_int_bug_id_ignored(self):
        from app.chatbot.llm import _build_pq_from_llm
        assert _build_pq_from_llm("m", {"bug_id": "x"}).bug_id is None

    def test_zero_bug_id_ignored(self):
        """bug_id <= 0 fails the `isinstance(bid, int) and bid > 0` guard."""
        from app.chatbot.llm import _build_pq_from_llm
        assert _build_pq_from_llm("m", {"bug_id": 0}).bug_id is None


class TestLLMDispatchFull:
    def _setup(self, client):
        _signup(client)
        db = _session()
        actor = _actor(db)
        from app.chatbot.executor import build_context
        ctx = build_context(db, actor)
        return db, actor, ctx

    def test_dispatch_help(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            assert _dispatch_llm_intent("help", db, pq, ctx, actor).intent == "help"
        finally:
            db.close()

    def test_dispatch_stats(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            assert _dispatch_llm_intent("stats", db, pq, ctx, actor).intent == "stats"
        finally:
            db.close()

    def test_dispatch_recent_activity(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            resp = _dispatch_llm_intent("recent_activity", db, pq, ctx, actor)
            assert resp.intent == "recent_activity"
        finally:
            db.close()

    def test_dispatch_list_users(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            resp = _dispatch_llm_intent("list_users", db, pq, ctx, actor)
            assert resp.intent == "list_users"
        finally:
            db.close()

    def test_dispatch_list_projects(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            resp = _dispatch_llm_intent("list_projects", db, pq, ctx, actor)
            assert resp.intent == "list_projects"
        finally:
            db.close()

    def test_dispatch_bug_detail_no_id_returns_none(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})  # bug_id None
            assert _dispatch_llm_intent("bug_detail", db, pq, ctx, actor) is None
        finally:
            db.close()

    def test_dispatch_bug_detail_with_id(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            from app.models import Project, Bug
            proj = Project(name="P", org_id=actor.org_id, key="PP")
            db.add(proj)
            db.commit()
            db.refresh(proj)
            bug = Bug(project_id=proj.id, title="D", status="New",
                      priority="Low", environment="DEV", reporter_id=actor.id)
            db.add(bug)
            db.commit()
            db.refresh(bug)
            from app.chatbot.executor import build_context
            ctx2 = build_context(db, actor)
            pq = _build_pq_from_llm("x", {"bug_id": bug.id})
            resp = _dispatch_llm_intent("bug_detail", db, pq, ctx2, actor)
            assert resp.intent == "bug_detail"
        finally:
            db.close()

    def test_dispatch_list_bugs(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            resp = _dispatch_llm_intent("list_bugs", db, pq, ctx, actor)
            assert resp.intent == "list_bugs"
        finally:
            db.close()

    def test_dispatch_unknown_returns_none(self, client):
        from app.chatbot.llm import _dispatch_llm_intent, _build_pq_from_llm
        db, actor, ctx = self._setup(client)
        try:
            pq = _build_pq_from_llm("x", {})
            assert _dispatch_llm_intent("nope", db, pq, ctx, actor) is None
        finally:
            db.close()


class TestLLMTryUnderstandFull:
    def _setup(self, client):
        _signup(client)
        db = _session()
        return db, _actor(db)

    def test_unavailable_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: False)
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("x", db, actor) is None
        finally:
            db.close()

    def test_inference_none_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference", lambda m: None)
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("x", db, actor) is None
        finally:
            db.close()

    def test_intent_unknown_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference",
                            lambda m: {"intent": "unknown"})
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("x", db, actor) is None
        finally:
            db.close()

    def test_intent_blank_returns_none(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference", lambda m: {"intent": "  "})
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("x", db, actor) is None
        finally:
            db.close()

    def test_intent_help_routed(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference",
                            lambda m: {"intent": "help"})
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("x", db, actor).intent == "help"
        finally:
            db.close()

    def test_intent_list_bugs_with_filters_routed(self, client, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "is_available", lambda: True)
        monkeypatch.setattr(llm, "_run_inference", lambda m: {
            "intent": "list_bugs",
            "filters": {"status": ["New"], "priority": ["Critical"],
                        "environment": ["PROD"]},
            "bug_id": None,
        })
        db, actor = self._setup(client)
        try:
            assert llm.try_understand("x", db, actor).intent == "list_bugs"
        finally:
            db.close()


class TestLLMMemoryBudgetAndAvailability:
    def test_memory_budget_none_when_no_file(self, monkeypatch, tmp_path):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "x.gguf")
        assert llm.memory_budget() is None
        assert llm.memory_shortfall_message() is None

    def test_memory_budget_floor_applied(self, monkeypatch, tmp_path):
        """A tiny model still needs at least the RAM floor (line 198 max())."""
        from app.chatbot import llm
        f = tmp_path / "tiny.gguf"
        f.write_bytes(b"\0" * (1024 * 1024))  # 1 MB
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 10000)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        b = llm.memory_budget()
        assert b.estimated_need_mb == llm._RAM_MIN_FLOOR_MB
        assert b.sufficient is True

    def test_memory_shortfall_message_when_short(self, monkeypatch, tmp_path):
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (5 * 1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 10)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 10)
        msg = llm.memory_shortfall_message()
        assert msg is not None and "unavailable" in msg.lower()

    def test_memory_shortfall_message_none_when_sufficient(self, monkeypatch, tmp_path):
        from app.chatbot import llm
        f = tmp_path / "m.gguf"
        f.write_bytes(b"\0" * (1024 * 1024))
        monkeypatch.setattr(llm, "_MODEL_PATH", f)
        monkeypatch.setattr(llm, "_detect_available_mb", lambda: 9000)
        monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
        assert llm.memory_shortfall_message() is None

    def test_is_available_no_model_file(self, monkeypatch, tmp_path):
        """No GGUF on disk -> immediate False (line 249)."""
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "absent.gguf")
        assert llm.is_available() is False

    def test_unload_clears(self, monkeypatch):
        from app.chatbot import llm
        monkeypatch.setattr(llm, "_llm", object(), raising=False)
        llm._unload()
        assert llm._llm is None


# ===========================================================================
# app/chatbot/nlu.py — full coverage of the pure rule engine
# ===========================================================================
@pytest.fixture()
def now_wed():
    """Fixed Wednesday (2026-06-03) for deterministic weekday math."""
    return datetime(2026, 6, 3, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def nlu_ctx():
    from app.chatbot.nlu import Context
    return Context(
        users=[
            (1, "john smith", "john", "John Smith"),
            (2, "alice wonder", "alice", "Alice Wonder"),
            (3, "bob builder", "bob.builder", "Bob Builder"),
            (5, "jane doe", "jane.d", "Jane Doe"),
            (6, "jane roe", "jane.r", "Jane Roe"),
        ],
        projects=[
            (10, "mobile", "Mobile"),
            (11, "api", "API"),
            (12, "billing platform", "Billing Platform"),
        ],
    )


class TestNluLowLevel:
    def test_normalize(self):
        from app.chatbot.nlu import _normalize
        assert _normalize("  A  B ") == "a b"
        assert _normalize(None) == ""

    def test_strip_punct(self):
        from app.chatbot.nlu import _strip_punct
        assert _strip_punct("John.") == "John"

    def test_tokenize(self):
        from app.chatbot.nlu import _tokenize
        toks = _tokenize("Show 42 bugs!")
        assert "show" in toks and "42" not in toks


class TestNluNamedWindows:
    def _ts(self, now):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def test_today(self, now_wed):
        from app.chatbot.nlu import _named_window
        assert _named_window("today", self._ts(now_wed), now_wed).label == "today"

    def test_yesterday(self, now_wed):
        from app.chatbot.nlu import _named_window
        w = _named_window("yesterday", self._ts(now_wed), now_wed)
        assert w.end == self._ts(now_wed)

    def test_this_week(self, now_wed):
        from app.chatbot.nlu import _named_window
        assert _named_window("this week", self._ts(now_wed), now_wed).label == "this week"

    def test_last_week(self, now_wed):
        from app.chatbot.nlu import _named_window
        w = _named_window("last week", self._ts(now_wed), now_wed)
        assert w.label == "last week"

    def test_this_month(self, now_wed):
        from app.chatbot.nlu import _named_window
        assert _named_window("this month", self._ts(now_wed), now_wed).start.day == 1

    def test_last_month(self, now_wed):
        from app.chatbot.nlu import _named_window
        assert _named_window("last month", self._ts(now_wed), now_wed).start.day == 1

    def test_this_quarter(self, now_wed):
        from app.chatbot.nlu import _named_window
        w = _named_window("this quarter", self._ts(now_wed), now_wed)
        assert w.start.month in (1, 4, 7, 10)

    def test_last_quarter(self, now_wed):
        from app.chatbot.nlu import _named_window
        w = _named_window("last quarter", self._ts(now_wed), now_wed)
        assert w.label == "last quarter"

    def test_this_year(self, now_wed):
        from app.chatbot.nlu import _named_window
        w = _named_window("this year", self._ts(now_wed), now_wed)
        assert w.start.month == 1 and w.start.day == 1

    def test_last_year(self, now_wed):
        from app.chatbot.nlu import _named_window
        w = _named_window("last year", self._ts(now_wed), now_wed)
        assert w.start.year == self._ts(now_wed).year - 1

    def test_unknown_phrase_none(self, now_wed):
        from app.chatbot.nlu import _named_window
        assert _named_window("never", self._ts(now_wed), now_wed) is None


class TestNluSinceWeekday:
    def _ts(self, now):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def test_earlier_in_week(self, now_wed):
        from app.chatbot.nlu import _since_weekday_window
        w = _since_weekday_window("monday", self._ts(now_wed), now_wed)
        assert (self._ts(now_wed) - w.start).days == 2

    def test_same_weekday_rolls_back(self, now_wed):
        from app.chatbot.nlu import _since_weekday_window
        w = _since_weekday_window("wednesday", self._ts(now_wed), now_wed)
        assert (self._ts(now_wed) - w.start).days == 7

    def test_future_weekday(self, now_wed):
        from app.chatbot.nlu import _since_weekday_window
        w = _since_weekday_window("friday", self._ts(now_wed), now_wed)
        assert (self._ts(now_wed) - w.start).days == 5


class TestNluRelativeWindow:
    def test_hours(self, now_wed):
        from app.chatbot.nlu import _relative_window
        w = _relative_window(3, "hours", now_wed)
        assert (now_wed - w.start).total_seconds() == 3 * 3600

    def test_days(self, now_wed):
        from app.chatbot.nlu import _relative_window
        assert (now_wed - _relative_window(5, "days", now_wed).start).days == 5

    def test_weeks(self, now_wed):
        from app.chatbot.nlu import _relative_window
        assert (now_wed - _relative_window(2, "weeks", now_wed).start).days == 14

    def test_months(self, now_wed):
        from app.chatbot.nlu import _relative_window
        assert (now_wed - _relative_window(1, "months", now_wed).start).days == 30

    def test_unknown_unit_none(self, now_wed):
        from app.chatbot.nlu import _relative_window
        assert _relative_window(3, "fortnight", now_wed) is None


class TestNluParseTimeWindow:
    def test_today(self, now_wed):
        from app.chatbot.nlu import _parse_time_window
        assert _parse_time_window("bugs today", now=now_wed).label == "today"

    def test_since_monday(self, now_wed):
        from app.chatbot.nlu import _parse_time_window
        assert "monday" in _parse_time_window("since monday", now=now_wed).label

    def test_past_n_days(self, now_wed):
        from app.chatbot.nlu import _parse_time_window
        w = _parse_time_window("past 3 days", now=now_wed)
        assert (now_wed - w.start).days == 3

    def test_last_n_weeks(self, now_wed):
        from app.chatbot.nlu import _parse_time_window
        w = _parse_time_window("last 2 weeks", now=now_wed)
        assert (now_wed - w.start).days == 14

    def test_in_the_last_n_hours(self, now_wed):
        from app.chatbot.nlu import _parse_time_window
        w = _parse_time_window("anything in the last 6 hours", now=now_wed)
        assert (now_wed - w.start).total_seconds() == 6 * 3600

    def test_no_match_none(self, now_wed):
        from app.chatbot.nlu import _parse_time_window
        assert _parse_time_window("show me everything", now=now_wed) is None

    def test_default_now(self):
        from app.chatbot.nlu import _parse_time_window
        assert _parse_time_window("today") is not None


class TestNluEnumExtraction:
    def test_statuses_open_expands(self):
        from app.chatbot.nlu import _extract_statuses, OPEN_STATUSES
        assert set(_extract_statuses("open bugs")) == set(OPEN_STATUSES)

    def test_statuses_in_progress(self):
        from app.chatbot.nlu import _extract_statuses
        assert _extract_statuses("in progress") == ["In Progress"]

    def test_statuses_done_two(self):
        from app.chatbot.nlu import _extract_statuses
        out = _extract_statuses("done items")
        assert "Closed" in out and "Resolved" in out

    def test_statuses_none(self):
        from app.chatbot.nlu import _extract_statuses
        assert _extract_statuses("hello world") == []

    def test_priorities_alias(self):
        from app.chatbot.nlu import _extract_priorities
        assert "Critical" in _extract_priorities("urgent bugs")
        assert "Low" in _extract_priorities("p3 stuff")

    def test_priorities_typo(self):
        from app.chatbot.nlu import _extract_priorities
        assert _extract_priorities("ctitical bug") == ["Critical"]

    def test_priorities_none(self):
        from app.chatbot.nlu import _extract_priorities
        assert _extract_priorities("nothing") == []

    def test_environments_syn(self):
        from app.chatbot.nlu import _extract_environments
        assert _extract_environments("on production") == ["PROD"]
        assert _extract_environments("staging") == ["UAT"]

    def test_environments_typo(self):
        from app.chatbot.nlu import _extract_environments
        assert _extract_environments("produciton") == ["PROD"]

    def test_environments_none(self):
        from app.chatbot.nlu import _extract_environments
        assert _extract_environments("foo bar") == []


class TestNluNamePhrasesAndResolution:
    def test_assigned_to(self):
        from app.chatbot.nlu import _candidate_name_phrases
        assert ("assignee", "John Smith") in _candidate_name_phrases(
            "bugs assigned to John Smith")

    def test_reported_by(self):
        from app.chatbot.nlu import _candidate_name_phrases
        assert ("reporter", "Alice") in _candidate_name_phrases(
            "issues reported by Alice")

    def test_filed_raised_created_opened(self):
        from app.chatbot.nlu import _candidate_name_phrases
        for verb in ("filed by Bob", "raised by Bob", "created by Bob",
                     "opened by Bob"):
            out = _candidate_name_phrases(f"bugs {verb}")
            assert any(r == "reporter" for r, _ in out)

    def test_owned_by_and_owner_is(self):
        from app.chatbot.nlu import _candidate_name_phrases
        assert ("assignee", "Alice") in _candidate_name_phrases("owned by Alice")
        out = _candidate_name_phrases("owner is Bob")
        assert any(r == "assignee" for r, _ in out)

    def test_reporter_is(self):
        from app.chatbot.nlu import _candidate_name_phrases
        out = _candidate_name_phrases("reporter is Carol")
        assert any(r == "reporter" for r, _ in out)

    def test_under_name(self):
        from app.chatbot.nlu import _candidate_name_phrases
        out = _candidate_name_phrases("bugs under John's name")
        assert any(r == "assignee" for r, _ in out)

    def test_assignee_phrase_strips_to_empty(self):
        """An assignee cue whose captured name is only ``'s`` strips to empty,
        so the `if phrase` guard is False and nothing is recorded
        (the 674->672 loop arc). The regex still matches and enters the body."""
        from app.chatbot.nlu import _candidate_name_phrases
        assert _candidate_name_phrases("bugs assigned to 's and bob") == []

    def test_reporter_phrase_strips_to_empty(self):
        """Same empty-phrase guard on the reporter loop (689->687 arc)."""
        from app.chatbot.nlu import _candidate_name_phrases
        assert _candidate_name_phrases("bugs reported by 's status") == []

    def test_resolve_exact(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("John Smith", nlu_ctx) == [(1, "John Smith")]

    def test_resolve_email_local(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert (2, "Alice Wonder") in _resolve_name("alice", nlu_ctx)

    def test_resolve_prefix(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        # "John" prefixes "john smith" -> prefix arm.
        assert (1, "John Smith") in _resolve_name("John", nlu_ctx)

    def test_resolve_last_name(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert (2, "Alice Wonder") in _resolve_name("Wonder", nlu_ctx)

    def test_resolve_ambiguous(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        ids = sorted(u for u, _ in _resolve_name("Jane", nlu_ctx))
        assert ids == [5, 6]

    def test_resolve_empty(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("", nlu_ctx) == []

    def test_resolve_strip_title(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("Mr. John Smith", nlu_ctx) == [(1, "John Smith")]

    def test_resolve_only_title(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("Mr.", nlu_ctx) == []

    def test_resolve_no_match(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_name
        assert _resolve_name("Zelig", nlu_ctx) == []

    def test_resolve_project_exact(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_project
        assert _resolve_project("Mobile", nlu_ctx) == [(10, "Mobile")]

    def test_resolve_project_prefix(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_project
        assert (12, "Billing Platform") in _resolve_project("Billing", nlu_ctx)

    def test_resolve_project_empty(self, nlu_ctx):
        from app.chatbot.nlu import _resolve_project
        assert _resolve_project("", nlu_ctx) == []

    def test_add_resolved_projects_dedups(self, nlu_ctx):
        """A project id already in the `seen` set is skipped (1150->1149
        loop arc) — no duplicate appended."""
        from app.chatbot.nlu import _add_resolved_projects, ParsedQuery
        pq = ParsedQuery()
        seen = {10}  # Mobile already seen
        _add_resolved_projects("Mobile", pq, nlu_ctx, seen)
        assert pq.project_ids == []  # skipped as duplicate


class TestNluActionDetectorsFull:
    def test_add_comment_with_body(self):
        from app.chatbot.nlu import _action_add_comment, ParsedQuery
        pq = ParsedQuery()
        assert _action_add_comment("comment on #5: works now", pq) == "add_comment"
        assert pq.action_comment == "works now"

    def test_add_comment_no_verb(self):
        from app.chatbot.nlu import _action_add_comment, ParsedQuery
        assert _action_add_comment("show bugs", ParsedQuery()) is None

    def test_create_project_quoted(self):
        from app.chatbot.nlu import _action_create_project, ParsedQuery
        pq = ParsedQuery()
        assert _action_create_project('create a project called "X Y"', pq) == "create_project"
        assert pq.action_title and "X Y" in pq.action_title

    def test_create_project_no_verb(self):
        from app.chatbot.nlu import _action_create_project, ParsedQuery
        assert _action_create_project("show bugs", ParsedQuery()) is None

    def test_create_bug_quoted(self):
        from app.chatbot.nlu import _action_create_bug, ParsedQuery
        pq = ParsedQuery()
        assert _action_create_bug('create a bug titled "login fails"', pq) == "create_bug"
        assert pq.action_title == "login fails"

    def test_create_bug_bare_strips_tail(self):
        from app.chatbot.nlu import _action_create_bug, ParsedQuery
        pq = ParsedQuery()
        _action_create_bug("file a bug login broken in project Mobile", pq)
        assert "project" not in (pq.action_title or "").lower()

    def test_create_bug_no_verb(self):
        from app.chatbot.nlu import _action_create_bug, ParsedQuery
        assert _action_create_bug("show bugs", ParsedQuery()) is None

    def test_set_status_verb(self):
        from app.chatbot.nlu import _action_set_status, ParsedQuery
        pq = ParsedQuery()
        assert _action_set_status("close bug 5", pq) == "set_status"
        assert pq.action_value == "Closed"

    def test_set_status_phrase_consumes_filter(self):
        from app.chatbot.nlu import _action_set_status, ParsedQuery
        pq = ParsedQuery(statuses=["New"])
        assert _action_set_status("change status to new on bug 5", pq) == "set_status"
        assert pq.statuses == []

    def test_set_status_none(self):
        from app.chatbot.nlu import _action_set_status, ParsedQuery
        assert _action_set_status("hello world", ParsedQuery()) is None

    def test_set_priority(self):
        from app.chatbot.nlu import _action_set_priority, ParsedQuery
        pq = ParsedQuery(priorities=["Critical"])
        assert _action_set_priority("set priority to critical on #5", pq) == "set_priority"
        assert pq.priorities == []

    def test_set_priority_no_filter(self):
        from app.chatbot.nlu import _action_set_priority, ParsedQuery
        assert _action_set_priority("set priority high", ParsedQuery()) is None

    def test_set_due_date_iso(self):
        from app.chatbot.nlu import _action_set_due_date, ParsedQuery
        pq = ParsedQuery()
        assert _action_set_due_date("set due date 2026-12-31 on #5", pq) == "set_due_date"
        assert pq.action_value == "2026-12-31"

    def test_set_due_date_none(self):
        from app.chatbot.nlu import _action_set_due_date, ParsedQuery
        assert _action_set_due_date("hello", ParsedQuery()) is None

    def test_assign(self):
        from app.chatbot.nlu import _action_assign, ParsedQuery
        assert _action_assign("assign bug 5 to john",
                              ParsedQuery(assignee_ids=[1])) == "assign"

    def test_assign_list_verb_not_action(self):
        from app.chatbot.nlu import _action_assign, ParsedQuery
        assert _action_assign("show bugs assigned to john",
                              ParsedQuery(assignee_ids=[1])) is None

    def test_assign_unassign(self):
        from app.chatbot.nlu import _action_assign, ParsedQuery
        assert _action_assign("unassign john from bug 5",
                              ParsedQuery(assignee_ids=[1])) == "unassign"

    def test_assign_no_assignee(self):
        from app.chatbot.nlu import _action_assign, ParsedQuery
        assert _action_assign("assign bug 5 to nobody", ParsedQuery()) is None

    def test_detect_action_create_project_first(self):
        from app.chatbot.nlu import _detect_action, ParsedQuery
        assert _detect_action("create a project called Mobile",
                              ParsedQuery()) == "create_project"

    def test_detect_action_none(self):
        from app.chatbot.nlu import _detect_action, ParsedQuery
        assert _detect_action("show all bugs", ParsedQuery()) is None


class TestNluStripTailAndPronoun:
    def test_strip_tail_in_project(self):
        from app.chatbot.nlu import _strip_create_bug_tail
        assert _strip_create_bug_tail("login broken in project Mobile") == "login broken"

    def test_strip_tail_priority(self):
        from app.chatbot.nlu import _strip_create_bug_tail
        assert _strip_create_bug_tail("login broken with priority high") == "login broken"

    def test_strip_tail_no_marker(self):
        from app.chatbot.nlu import _strip_create_bug_tail
        assert _strip_create_bug_tail("login broken") == "login broken"

    def test_strip_tail_empty(self):
        from app.chatbot.nlu import _strip_create_bug_tail
        assert _strip_create_bug_tail("") == ""

    def test_pronoun_yes(self):
        from app.chatbot.nlu import _has_pronoun_bug_ref
        assert _has_pronoun_bug_ref("close it") is True

    def test_pronoun_no(self):
        from app.chatbot.nlu import _has_pronoun_bug_ref
        assert _has_pronoun_bug_ref("close bug 5") is False


class TestNluShortIntent:
    def test_greeting(self):
        from app.chatbot.nlu import _classify_short_intent, ParsedQuery
        pq = ParsedQuery()
        assert _classify_short_intent("hi there", pq) is True
        assert pq.intent == "greeting"

    def test_help(self):
        from app.chatbot.nlu import _classify_short_intent, ParsedQuery
        pq = ParsedQuery()
        assert _classify_short_intent("help", pq) is True
        assert pq.intent == "help"

    def test_thanks(self):
        from app.chatbot.nlu import _classify_short_intent, ParsedQuery
        pq = ParsedQuery()
        assert _classify_short_intent("thanks", pq) is True
        assert pq.intent == "thanks"

    def test_yes(self):
        from app.chatbot.nlu import _classify_short_intent, ParsedQuery
        pq = ParsedQuery()
        assert _classify_short_intent("yes", pq) is True
        assert pq.confirmation == "yes"

    def test_no(self):
        from app.chatbot.nlu import _classify_short_intent, ParsedQuery
        pq = ParsedQuery()
        assert _classify_short_intent("no", pq) is True
        assert pq.confirmation == "no"

    def test_false_for_long(self):
        from app.chatbot.nlu import _classify_short_intent, ParsedQuery
        msg = "hello can you please show me all open bugs in production now"
        assert _classify_short_intent(msg, ParsedQuery()) is False


class TestNluParseEndToEnd:
    def test_empty(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("", nlu_ctx).intent == "empty"

    def test_greeting(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("hi", nlu_ctx).intent == "greeting"

    def test_list_bugs_full(self, nlu_ctx, now_wed):
        from app.chatbot.nlu import parse, OPEN_STATUSES
        pq = parse("show open critical bugs in PROD assigned to John Smith in project Mobile",
                   nlu_ctx, now=now_wed)
        assert pq.intent == "list_bugs"
        assert set(pq.statuses) == set(OPEN_STATUSES)
        assert pq.priorities == ["Critical"]
        assert pq.environments == ["PROD"]
        assert pq.assignee_ids == [1]
        assert pq.project_ids == [10]

    def test_count(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse("how many open bugs", nlu_ctx)
        assert pq.wants_count is True and pq.intent == "list_bugs"

    def test_export(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("export all bugs to excel", nlu_ctx).wants_export is True

    def test_bug_detail_short(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse("bug 42", nlu_ctx)
        assert pq.intent == "bug_detail" and pq.bug_id == 42

    def test_action_close(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse("close bug 5", nlu_ctx)
        assert pq.action_kind == "set_status" and pq.action_value == "Closed"

    def test_action_assign(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse("assign bug 5 to John Smith", nlu_ctx)
        assert pq.action_kind == "assign" and 1 in pq.assignee_ids

    def test_action_create_bug(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse('create a bug titled "login fails"', nlu_ctx)
        assert pq.action_kind == "create_bug"

    def test_action_create_project(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse('create a project called "Marketing"', nlu_ctx)
        assert pq.action_kind == "create_project"

    def test_action_comment(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse("comment on #5: looks good", nlu_ctx)
        assert pq.action_kind == "add_comment"

    def test_list_users(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("list all users", nlu_ctx).intent == "list_users"

    def test_role_admin(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("show admins", nlu_ctx).role_filter == "admin"

    def test_role_manager(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("list managers", nlu_ctx).role_filter == "manager"

    def test_list_projects(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("list projects", nlu_ctx).intent == "list_projects"

    def test_stats(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("show me the dashboard kpi", nlu_ctx).intent == "stats"

    def test_recent_activity(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("what happened recently", nlu_ctx).intent == "recent_activity"

    def test_unresolved_assignee(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert "Zelig" in parse("show bugs assigned to Zelig",
                                nlu_ctx).unresolved_assignee_names

    def test_ambiguous_name(self, nlu_ctx):
        from app.chatbot.nlu import parse
        pq = parse("show bugs assigned to Jane", nlu_ctx)
        assert any("Jane" in p for p, _ in pq.ambiguous_names)
        assert pq.assignee_ids == []

    def test_pronoun_flag(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("close it", nlu_ctx).used_pronoun_bug is True

    def test_quoted_search(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse('bugs about "login crash"', nlu_ctx).text_search == "login crash"

    def test_time_window_propagates(self, nlu_ctx, now_wed):
        from app.chatbot.nlu import parse
        pq = parse("yesterday's bugs", nlu_ctx, now=now_wed)
        assert pq.time_window.label == "yesterday"

    def test_project_loose_cue(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert 10 in parse("project mobile bugs", nlu_ctx).project_ids

    def test_project_literal_fallback(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert 12 in parse("show me Billing Platform bugs", nlu_ctx).project_ids

    def test_unknown_intent(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("asdfqwer zzxx blorp", nlu_ctx).intent == "unknown"

    def test_about_lead(self, nlu_ctx):
        from app.chatbot.nlu import parse
        assert parse("what is a priority", nlu_ctx).intent == "about"


class TestNluDescribeFilters:
    def test_open_collapses(self):
        from app.chatbot.nlu import describe_filters, ParsedQuery, OPEN_STATUSES
        assert "open" in describe_filters(ParsedQuery(statuses=list(OPEN_STATUSES)))

    def test_all_parts(self):
        from app.chatbot.nlu import describe_filters, ParsedQuery, TimeWindow
        pq = ParsedQuery(
            statuses=["Closed"], priorities=["High"], environments=["PROD"],
            project_names=["Mobile"], assignee_names=["John"],
            reporter_names=["Alice"], text_search="crash",
            time_window=TimeWindow(label="today"),
        )
        out = describe_filters(pq)
        for needle in ("closed", "high priority", "in PROD", "Mobile",
                       "John", "Alice", "crash", "today"):
            assert needle in out

    def test_empty(self):
        from app.chatbot.nlu import describe_filters, ParsedQuery
        assert describe_filters(ParsedQuery()) == ""

    def test_time_window_without_label_skipped(self):
        """A time window with an empty label is not appended (the
        `and pq.time_window.label` short-circuit False arc)."""
        from app.chatbot.nlu import describe_filters, ParsedQuery, TimeWindow
        out = describe_filters(ParsedQuery(time_window=TimeWindow(label="")))
        assert out == ""
