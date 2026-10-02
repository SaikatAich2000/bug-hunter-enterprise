"""Tests for app/chatbot/llm.py (optional local llama.cpp layer) with a fake llama_cpp module.
RAM-budget math, availability gating, lazy load/idle-unload, JSON extraction, intent dispatch.
"""
from __future__ import annotations

import io
import sys
import types

import pytest

from app.chatbot import executor, llm


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    # Rebind after the fixture purges app.*; otherwise monkeypatches land on a stale generation.
    import importlib
    global llm, executor
    llm = importlib.import_module("app.chatbot.llm")
    executor = importlib.import_module("app.chatbot.executor")
    monkeypatch.setattr(llm, "_llm", None)
    monkeypatch.setattr(llm, "_loaded_at", 0.0)
    monkeypatch.setattr(llm, "_last_used_at", 0.0)
    monkeypatch.setattr(llm, "_shortfall_warned", False)
    monkeypatch.setattr(llm, "_reaper_started", False)
    monkeypatch.setattr(llm, "_budget_cache", None)  # don't leak a cached budget


# _read_int
def test_read_int_valid(tmp_path):
    p = tmp_path / "v"
    p.write_text("12345\n", encoding="utf-8")
    assert llm._read_int(str(p)) == 12345


def test_read_int_max_and_blank(tmp_path):
    p = tmp_path / "v"
    p.write_text("max", encoding="utf-8")
    assert llm._read_int(str(p)) is None
    p.write_text("   ", encoding="utf-8")
    assert llm._read_int(str(p)) is None


def test_read_int_missing_and_garbage(tmp_path):
    assert llm._read_int(str(tmp_path / "nope")) is None
    p = tmp_path / "g"
    p.write_text("notanint", encoding="utf-8")
    assert llm._read_int(str(p)) is None


# _read_meminfo_kb (bare open shim)
def _meminfo_open(content):
    def _open(path, *a, **k):
        if path == "/proc/meminfo":
            return io.StringIO(content)
        raise FileNotFoundError(path)
    return _open


def test_read_meminfo_found(monkeypatch):
    monkeypatch.setattr(
        llm, "open",
        _meminfo_open("MemTotal: 100 kB\nMemAvailable: 2048 kB\n"),
        raising=False,
    )
    assert llm._read_meminfo_kb("MemAvailable") == 2048


def test_read_meminfo_key_absent(monkeypatch):
    monkeypatch.setattr(llm, "open", _meminfo_open("MemTotal: 100 kB\n"), raising=False)
    assert llm._read_meminfo_kb("MemAvailable") is None


def test_read_meminfo_oserror(monkeypatch):
    def _boom(path, *a, **k):
        raise OSError("no /proc")
    monkeypatch.setattr(llm, "open", _boom, raising=False)
    assert llm._read_meminfo_kb("MemAvailable") is None


def test_read_meminfo_malformed_line(monkeypatch):
    monkeypatch.setattr(llm, "open", _meminfo_open("MemAvailable:\n"), raising=False)
    assert llm._read_meminfo_kb("MemAvailable") is None


# _detect_container_limit_mb
def _read_int_map(mapping):
    return lambda path: mapping.get(path)


def test_container_limit_v2(monkeypatch):
    monkeypatch.setattr(llm, "_read_int", _read_int_map(
        {"/sys/fs/cgroup/memory.max": 512 * 1024 * 1024}))
    assert llm._detect_container_limit_mb() == 512


def test_container_limit_v1(monkeypatch):
    monkeypatch.setattr(llm, "_read_int", _read_int_map({
        "/sys/fs/cgroup/memory.max": None,
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": 256 * 1024 * 1024,
    }))
    assert llm._detect_container_limit_mb() == 256


def test_container_limit_v1_unlimited_sentinel(monkeypatch):
    monkeypatch.setattr(llm, "_read_int", _read_int_map({
        "/sys/fs/cgroup/memory.max": None,
        "/sys/fs/cgroup/memory/memory.limit_in_bytes": 1 << 63,
    }))
    assert llm._detect_container_limit_mb() is None


def test_container_limit_none(monkeypatch):
    monkeypatch.setattr(llm, "_read_int", _read_int_map({}))
    assert llm._detect_container_limit_mb() is None


# _detect_available_mb
def test_available_min_of_cg_and_mem(monkeypatch):
    monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 300)
    monkeypatch.setattr(llm, "_read_meminfo_kb", lambda key: 1024 * 1024)  # 1024 MB
    assert llm._detect_available_mb() == 300


def test_available_cg_only(monkeypatch):
    monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 300)
    monkeypatch.setattr(llm, "_read_meminfo_kb", lambda key: None)
    assert llm._detect_available_mb() == 300


def test_available_mem_only(monkeypatch):
    monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
    monkeypatch.setattr(llm, "_read_meminfo_kb", lambda key: 2048 * 1024)
    assert llm._detect_available_mb() == 2048


def test_available_fallback(monkeypatch):
    monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: None)
    monkeypatch.setattr(llm, "_read_meminfo_kb", lambda key: None)
    assert llm._detect_available_mb() == 512


# _model_file_size_mb / memory_budget / memory_shortfall_message
def test_model_file_size_present(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x" * (3 * 1024 * 1024))
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    assert llm._model_file_size_mb() == 3


def test_model_file_size_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "absent.gguf")
    assert llm._model_file_size_mb() is None


def test_memory_budget_none_without_model(monkeypatch):
    monkeypatch.setattr(llm, "_model_file_size_mb", lambda: None)
    assert llm.memory_budget() is None


def test_memory_budget_sufficient(monkeypatch):
    monkeypatch.setattr(llm, "_model_file_size_mb", lambda: 100)
    monkeypatch.setattr(llm, "_detect_available_mb", lambda: 4096)
    monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 4096)
    b = llm.memory_budget()
    assert b.sufficient is True
    assert b.model_size_mb == 100


def test_memory_shortfall_message(monkeypatch):
    monkeypatch.setattr(llm, "_model_file_size_mb", lambda: 1000)
    monkeypatch.setattr(llm, "_detect_available_mb", lambda: 100)
    monkeypatch.setattr(llm, "_detect_container_limit_mb", lambda: 100)
    assert "AI fallback is unavailable" in llm.memory_shortfall_message()


def test_memory_shortfall_message_none_when_ok(monkeypatch):
    monkeypatch.setattr(llm, "memory_budget", lambda: None)
    assert llm.memory_shortfall_message() is None


# is_available
def test_is_available_no_model(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "absent.gguf")
    assert llm.is_available() is False


def test_is_available_no_llama_cpp(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    # Setting the entry to None makes the import machinery treat it as a failed import.
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    assert llm.is_available() is False


def test_is_available_insufficient_ram(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    budget = llm._MemoryBudget(1000, 1400, 200, 200, False)
    monkeypatch.setattr(llm, "memory_budget", lambda: budget)
    assert llm.is_available() is False


def test_is_available_no_llama_cpp_already_warned(monkeypatch, tmp_path):
    # Warning already fired: log skipped but still returns False.
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    monkeypatch.setattr(llm, "_shortfall_warned", True)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    assert llm.is_available() is False


def test_is_available_insufficient_ram_already_warned(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    monkeypatch.setattr(llm, "_shortfall_warned", True)
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    monkeypatch.setattr(llm, "memory_budget",
                        lambda: llm._MemoryBudget(1000, 1400, 200, 200, False))
    assert llm.is_available() is False


def test_is_available_true(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    monkeypatch.setattr(llm, "memory_budget", lambda: None)
    assert llm.is_available() is True


# _ensure_loaded / _unload
def _install_fake_llama(monkeypatch, instances):
    mod = types.ModuleType("llama_cpp")

    class Llama:
        def __init__(self, **kw):
            instances.append(self)
            self.kw = kw

    mod.Llama = Llama
    monkeypatch.setitem(sys.modules, "llama_cpp", mod)
    return mod


def test_ensure_loaded_loads_then_caches(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    monkeypatch.setattr(llm, "_ensure_reaper", lambda: None)  # no real daemon thread
    instances = []
    _install_fake_llama(monkeypatch, instances)
    first = llm._ensure_loaded()
    second = llm._ensure_loaded()
    assert first is second
    assert len(instances) == 1


def test_ensure_loaded_idle_unloads_and_reloads(monkeypatch, tmp_path):
    p = tmp_path / "m.gguf"
    p.write_bytes(b"x")
    monkeypatch.setattr(llm, "_MODEL_PATH", p)
    instances = []
    _install_fake_llama(monkeypatch, instances)
    monkeypatch.setattr(llm, "_llm", object())
    monkeypatch.setattr(llm, "_last_used_at", 1.0)  # ancient
    monkeypatch.setattr(llm, "_IDLE_UNLOAD_S", 1.0)
    llm._ensure_loaded()
    assert len(instances) == 1  # reloaded after idle unload


def test_ensure_loaded_missing_model(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "_MODEL_PATH", tmp_path / "absent.gguf")
    with pytest.raises(FileNotFoundError):
        llm._ensure_loaded()


def test_unload(monkeypatch):
    monkeypatch.setattr(llm, "_llm", object())
    llm._unload()
    assert llm._llm is None


# Idle reaper + budget cache
def test_maybe_unload_idle_unloads_when_idle(monkeypatch):
    monkeypatch.setattr(llm, "_llm", object())
    monkeypatch.setattr(llm, "_last_used_at", 1.0)        # ancient
    monkeypatch.setattr(llm, "_IDLE_UNLOAD_S", 1.0)
    assert llm._maybe_unload_idle() is True
    assert llm._llm is None


def test_maybe_unload_idle_keeps_recent(monkeypatch):
    import time as _t
    monkeypatch.setattr(llm, "_llm", object())
    monkeypatch.setattr(llm, "_last_used_at", _t.time())  # just used
    monkeypatch.setattr(llm, "_IDLE_UNLOAD_S", 600.0)
    assert llm._maybe_unload_idle() is False
    assert llm._llm is not None


def test_maybe_unload_idle_noop_when_unloaded(monkeypatch):
    monkeypatch.setattr(llm, "_llm", None)
    assert llm._maybe_unload_idle() is False


def test_ensure_reaper_starts_once(monkeypatch):
    started = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            started.append(name)

        def start(self):
            pass

    monkeypatch.setattr(llm.threading, "Thread", _FakeThread)
    monkeypatch.setattr(llm, "_reaper_started", False)
    llm._ensure_reaper()
    llm._ensure_reaper()   # second call is a no-op
    assert started == ["sleuth-llm-reaper"]
    assert llm._reaper_started is True


def test_cached_budget_memoizes(monkeypatch):
    calls = {"n": 0}

    def fake_budget():
        calls["n"] += 1
        return None

    monkeypatch.setattr(llm, "memory_budget", fake_budget)
    monkeypatch.setattr(llm, "_budget_cache", None)
    llm._cached_budget()
    llm._cached_budget()
    assert calls["n"] == 1   # second call served from cache
    llm._reset_caches_for_test()
    llm._cached_budget()
    assert calls["n"] == 2   # cache cleared → recomputed


# _extract_json
def test_extract_json_empty():
    assert llm._extract_json("") is None


def test_extract_json_fenced():
    assert llm._extract_json('```json\n{"intent": "stats"}\n```') == {"intent": "stats"}


def test_extract_json_with_prose():
    assert llm._extract_json('Sure! {"intent": "help"} done') == {"intent": "help"}


def test_extract_json_skips_non_dict_then_finds_dict():
    assert llm._extract_json('[1,2] then {"a": 1}') == {"a": 1}


def test_extract_json_none_when_no_json():
    assert llm._extract_json("just prose, no braces") is None


def test_extract_json_invalid():
    assert llm._extract_json("{not valid json") is None


# _run_inference
def test_run_inference_load_fails(monkeypatch):
    def _boom():
        raise RuntimeError("load failed")
    monkeypatch.setattr(llm, "_ensure_loaded", _boom)
    assert llm._run_inference("hi") is None


def test_run_inference_success(monkeypatch):
    def fake_llm(prompt, **kw):
        return {"choices": [{"text": '{"intent": "stats"}'}]}
    monkeypatch.setattr(llm, "_ensure_loaded", lambda: fake_llm)
    assert llm._run_inference("stats please") == {"intent": "stats"}


def test_run_inference_call_raises(monkeypatch):
    def fake_llm(prompt, **kw):
        raise RuntimeError("decode crash")
    monkeypatch.setattr(llm, "_ensure_loaded", lambda: fake_llm)
    assert llm._run_inference("x") is None


def test_run_inference_over_budget_logs_but_returns(monkeypatch):
    def fake_llm(prompt, **kw):
        return {"choices": [{"text": '{"intent": "help"}'}]}
    monkeypatch.setattr(llm, "_ensure_loaded", lambda: fake_llm)
    monkeypatch.setattr(llm, "_INFERENCE_TIMEOUT_S", -1.0)  # always "over budget"
    assert llm._run_inference("x") == {"intent": "help"}


def test_run_inference_bad_output_shape(monkeypatch):
    monkeypatch.setattr(llm, "_ensure_loaded", lambda: (lambda prompt, **kw: {}))
    assert llm._run_inference("x") is None


# _build_pq_from_llm
def test_build_pq_filters_and_bug_id():
    parsed = {
        "filters": {
            "status": ["New", "Bogus"],
            "priority": ["High", "Nope"],
            "environment": ["PROD", "XX"],
        },
        "bug_id": 42,
    }
    pq = llm._build_pq_from_llm("msg", parsed)
    assert pq.statuses == ["New"]
    assert pq.priorities == ["High"]
    assert pq.environments == ["PROD"]
    assert pq.bug_id == 42


def test_build_pq_rejects_bad_bug_id():
    pq = llm._build_pq_from_llm("msg", {"bug_id": -1})
    assert pq.bug_id is None
    pq2 = llm._build_pq_from_llm("msg", {"bug_id": "notint"})
    assert pq2.bug_id is None


def test_build_pq_recovers_role_and_time_from_message():
    # Compact LLM JSON omits role/time; they must be recovered from the raw message.
    pq = llm._build_pq_from_llm("show admins active today", {})
    assert pq.role_filter == "admin"
    assert pq.time_window is not None
    assert pq.time_window.label == "today"


# _dispatch_llm_intent
@pytest.fixture
def _stub_handlers(monkeypatch):
    sentinels = {}
    for name in ("_handle_help", "_handle_stats", "_handle_recent_activity",
                 "_handle_list_users", "_handle_list_projects",
                 "_handle_bug_detail", "_handle_list_bugs"):
        s = object()
        sentinels[name] = s
        monkeypatch.setattr(executor, name, lambda *a, _s=s, **k: _s)
    return sentinels


def test_dispatch_each_intent(_stub_handlers, monkeypatch):
    pq = types.SimpleNamespace(bug_id=5)
    monkeypatch.setattr("app.access.accessible_project_ids", lambda db, actor: set())
    actor = types.SimpleNamespace(role="admin", id=1, org_id=1)
    assert llm._dispatch_llm_intent("help", None, pq, None, actor) is _stub_handlers["_handle_help"]
    assert llm._dispatch_llm_intent("stats", None, pq, None, actor) is _stub_handlers["_handle_stats"]
    assert llm._dispatch_llm_intent("recent_activity", None, pq, None, actor) is _stub_handlers["_handle_recent_activity"]
    assert llm._dispatch_llm_intent("list_users", None, pq, None, actor) is _stub_handlers["_handle_list_users"]
    assert llm._dispatch_llm_intent("list_projects", None, pq, None, actor) is _stub_handlers["_handle_list_projects"]
    assert llm._dispatch_llm_intent("bug_detail", None, pq, None, actor) is _stub_handlers["_handle_bug_detail"]
    assert llm._dispatch_llm_intent("list_bugs", None, pq, None, actor) is _stub_handlers["_handle_list_bugs"]


def test_dispatch_bug_detail_without_id_returns_none(_stub_handlers):
    pq = types.SimpleNamespace(bug_id=None)
    assert llm._dispatch_llm_intent("bug_detail", None, pq, None, None) is None


def test_dispatch_unknown_returns_none(_stub_handlers):
    pq = types.SimpleNamespace(bug_id=None)
    assert llm._dispatch_llm_intent("nonsense", None, pq, None, None) is None


# try_understand
def test_try_understand_unavailable(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: False)
    assert llm.try_understand("x", None, None) is None


def test_try_understand_inference_none(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "_run_inference", lambda m: None)
    assert llm.try_understand("x", None, None) is None


def test_try_understand_unknown_intent(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "_run_inference", lambda m: {"intent": "unknown"})
    assert llm.try_understand("x", None, None) is None


def test_try_understand_dispatches(monkeypatch):
    monkeypatch.setattr(llm, "is_available", lambda: True)
    monkeypatch.setattr(llm, "_run_inference", lambda m: {"intent": "stats"})
    monkeypatch.setattr(executor, "build_context", lambda db, actor: "ctx")
    sentinel = object()
    monkeypatch.setattr(llm, "_dispatch_llm_intent", lambda *a, **k: sentinel)
    assert llm.try_understand("show stats", None, None) is sentinel
