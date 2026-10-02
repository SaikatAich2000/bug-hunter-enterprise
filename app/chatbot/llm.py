"""Sleuth Layer 3: optional local LLM via llama.cpp.

Handles free-form queries the rules and classifier miss, running a small GGUF
model (models/sleuth.gguf) on-CPU with no external API. Target box is 1 CPU /
2 GB RAM; the model loads lazily and unloads after 10 min idle. Any failure
(missing model, import error, timeout) is caught by the executor, which falls
back to "unknown" so the chat path stays up. Detected at runtime via is_available().
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.orm import Session

from app.chatbot.executor import Response
from app.models import User

if TYPE_CHECKING:
    from app.chatbot import nlu as _nlu


logger = logging.getLogger("bug_hunter.sleuth.llm")


# Env-overridable; default matches the README path.
_MODEL_PATH = Path(
    os.getenv("SLEUTH_LLM_MODEL_PATH",
              str(Path(__file__).resolve().parent.parent.parent / "models" / "sleuth.gguf"))
)

# Time out and fall back rather than block indefinitely on a slow model.
_INFERENCE_TIMEOUT_S = float(os.getenv("SLEUTH_LLM_TIMEOUT_S", "12"))

# Unload after idle so RAM returns to Postgres/web workers.
_IDLE_UNLOAD_S = float(os.getenv("SLEUTH_LLM_IDLE_UNLOAD_S", "600"))

# Enough for the JSON we ask for, small enough to bound latency on a slow CPU.
_MAX_NEW_TOKENS = int(os.getenv("SLEUTH_LLM_MAX_TOKENS", "120"))

# Smaller context = less RAM; our prompts are short.
_CTX_LEN = int(os.getenv("SLEUTH_LLM_CTX_LEN", "1024"))

# 1 thread is the documented target; more than physical cores causes contention.
_THREADS = int(os.getenv("SLEUTH_LLM_THREADS", "1"))

# 1.4x headroom over file size covers KV cache, load buffer, and llama.cpp
# overhead for Q4_K_M at 1024-token context.
_RAM_HEADROOM_MULT = float(os.getenv("SLEUTH_LLM_RAM_HEADROOM", "1.4"))
# Even a tiny GGUF needs ~200 MB with Python + llama.cpp state + context.
_RAM_MIN_FLOOR_MB = 200


@dataclass
class _MemoryBudget:
    """Snapshot of memory availability vs model requirements. All sizes in MB."""
    model_size_mb: int          # size of the GGUF file on disk
    estimated_need_mb: int      # what we expect to need at peak
    available_mb: int           # what we actually have to work with
    container_limit_mb: int     # the cgroup-imposed ceiling, if any
    sufficient: bool            # estimated_need_mb <= available_mb


def _read_int(path: str) -> Optional[int]:
    """Read a single integer from a sysfs file, or None on any failure."""
    try:
        with open(path) as fh:
            v = fh.read().strip()
        if v in ("", "max"):
            return None
        return int(v)
    except (OSError, ValueError):
        return None


def _read_meminfo_kb(key: str) -> Optional[int]:
    """Read /proc/meminfo's value for `key` (e.g. 'MemAvailable'), in kB."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    parts = line.split()
                    return int(parts[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _detect_container_limit_mb() -> Optional[int]:
    """Return the cgroup memory limit in MB, or None if unbounded / not in a
    container. Checks cgroup v2 first, then v1."""
    # cgroup v2
    v = _read_int("/sys/fs/cgroup/memory.max")
    if v is not None and v > 0:
        return v // (1024 * 1024)
    # cgroup v1 — the "no limit" sentinel is near INT64_MAX; skip above 2^62.
    v = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v is not None and 0 < v < (1 << 62):
        return v // (1024 * 1024)
    return None


def _detect_available_mb() -> int:
    """Estimate allocatable MB: cgroup limit, then MemAvailable, then 512 MB.
    The cgroup ceiling is binding, so take the smaller when both are known."""
    cg = _detect_container_limit_mb()
    mem_kb = _read_meminfo_kb("MemAvailable")
    if cg is not None and mem_kb is not None:
        return min(cg, mem_kb // 1024)
    if cg is not None:
        return cg
    if mem_kb is not None:
        return mem_kb // 1024
    return 512   # pessimistic fallback


def _model_file_size_mb() -> Optional[int]:
    """Return the GGUF file size in MB, or None if the file is missing."""
    try:
        return _MODEL_PATH.stat().st_size // (1024 * 1024)
    except OSError:
        return None


def memory_budget() -> Optional[_MemoryBudget]:
    """Compute the LLM memory budget, or None if no model file is present."""
    model_mb = _model_file_size_mb()
    if model_mb is None:
        return None
    estimated = max(int(model_mb * _RAM_HEADROOM_MULT), _RAM_MIN_FLOOR_MB)
    available = _detect_available_mb()
    container_limit = _detect_container_limit_mb() or 0
    return _MemoryBudget(
        model_size_mb=model_mb,
        estimated_need_mb=estimated,
        available_mb=available,
        container_limit_mb=container_limit,
        sufficient=(estimated <= available),
    )


def memory_shortfall_message() -> Optional[str]:
    """Short user-facing notice when the LLM is disabled for low memory, or None.
    The detailed operator breakdown is logged by is_available()."""
    budget = memory_budget()
    if budget is None or budget.sufficient:
        return None
    return (
        "The optional AI fallback is unavailable on this server "
        "(insufficient memory). Most queries still work — try rephrasing "
        "or type *help* for examples."
    )


# Log the detailed memory-shortfall message at most once per process.
_shortfall_warned = False


def is_available() -> bool:
    """True only if a model file is present, llama-cpp-python is importable, and
    the box has enough RAM. The memory check keeps a too-small box from blowing
    up at load time; the first shortfall logs one detailed warning."""
    global _shortfall_warned
    if not _MODEL_PATH.exists():
        return False
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        if not _shortfall_warned:
            logger.warning(
                "Sleuth: a model file is at %s but llama-cpp-python is "
                "not installed. Install it (pip install llama-cpp-python) "
                "or remove the file. Layer 3 is disabled.",
                _MODEL_PATH,
            )
            _shortfall_warned = True
        return False
    budget = _cached_budget()
    if budget is not None and not budget.sufficient:
        if not _shortfall_warned:
            logger.warning(
                "Sleuth LLM disabled: model file is %d MB at %s, peak "
                "need ~%d MB, only %d MB available (container cap: %s). "
                "Raise the docker-compose memory limit to at least %d MB "
                "for Layer 3 to activate. Layers 1 (rules) and 2 "
                "(classifier) continue to operate normally.",
                budget.model_size_mb, _MODEL_PATH,
                budget.estimated_need_mb, budget.available_mb,
                f"{budget.container_limit_mb} MB"
                    if budget.container_limit_mb else "none",
                max(budget.estimated_need_mb + 256, 1024),
            )
            _shortfall_warned = True
        return False
    return True


_lock = threading.Lock()
# Held across a full decode: the shared Llama KV-cache isn't thread-safe and
# concurrent threadpool requests would interleave and corrupt it. Kept distinct
# from _lock so a running inference doesn't block idle-unload.
_inference_lock = threading.Lock()
_llm: Any = None             # the Llama instance, or None
_loaded_at: float = 0.0      # epoch seconds when we last loaded
_last_used_at: float = 0.0   # epoch seconds of last inference call


def _ensure_loaded() -> Any:
    """Lazy-load the model (caller must not hold _lock); return the Llama
    instance or raise."""
    global _llm, _loaded_at
    with _lock:
        # Unload if idle past the threshold before deciding whether to load.
        if (_llm is not None and _last_used_at > 0
                and (time.time() - _last_used_at) > _IDLE_UNLOAD_S):
            logger.info("Sleuth LLM idle past %.0fs — unloading", _IDLE_UNLOAD_S)
            _llm = None

        if _llm is not None:
            return _llm

        if not _MODEL_PATH.exists():
            raise FileNotFoundError(
                f"GGUF model not found at {_MODEL_PATH}. "
                "Drop a model file there or set SLEUTH_LLM_MODEL_PATH."
            )
        from llama_cpp import Llama
        logger.info("Loading Sleuth LLM from %s (this takes a few seconds)",
                    _MODEL_PATH)
        t0 = time.time()
        _llm = Llama(
            model_path=str(_MODEL_PATH),
            n_ctx=_CTX_LEN,
            n_threads=_THREADS,
            # Defaults: mmap on (faster load), mlock off (OS can evict cold pages).
            verbose=False,
        )
        _loaded_at = time.time()
        logger.info("Sleuth LLM loaded in %.2fs", _loaded_at - t0)
        _ensure_reaper()   # free RAM again once this load goes idle
        return _llm


def _unload() -> None:
    """Drop the loaded model. Used by the idle reaper and tests."""
    global _llm
    with _lock:
        _llm = None


# Idle reaper: a daemon thread frees RAM proactively, since _ensure_loaded's
# lazy-unload only fires on the next request and would leave the model resident
# indefinitely after a usage burst.
_reaper_started = False


def _maybe_unload_idle() -> bool:
    """Unload the model if idle past _IDLE_UNLOAD_S; return True if unloaded.
    Safe from any thread."""
    global _llm
    with _lock:
        if (_llm is not None and _last_used_at > 0
                and (time.time() - _last_used_at) > _IDLE_UNLOAD_S):
            logger.info("Sleuth LLM idle past %.0fs — unloading (reaper)", _IDLE_UNLOAD_S)
            _llm = None
            return True
    return False


def _reaper_loop() -> None:  # pragma: no cover - infinite background daemon loop
    # Poll at 1/4 the idle window (clamped 5-60 s) to reclaim RAM without spinning.
    interval = min(60.0, max(5.0, _IDLE_UNLOAD_S / 4.0))
    while True:
        time.sleep(interval)
        try:
            _maybe_unload_idle()
        except Exception:  # noqa: BLE001 - a reaper hiccup must never crash the app
            logger.debug("Sleuth LLM reaper iteration failed", exc_info=True)


def _ensure_reaper() -> None:
    """Start the idle-reaper daemon thread once per process."""
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    threading.Thread(target=_reaper_loop, name="sleuth-llm-reaper",
                     daemon=True).start()


# Memory-budget cache: is_available() runs per message, but the budget inputs
# are static after startup, so cache briefly to avoid re-parsing /proc + sysfs.
_BUDGET_TTL_S = 300.0
_budget_cache: Optional[tuple[float, Optional["_MemoryBudget"]]] = None


def _cached_budget() -> Optional["_MemoryBudget"]:
    global _budget_cache
    now = time.time()
    if _budget_cache is not None and (now - _budget_cache[0]) < _BUDGET_TTL_S:
        return _budget_cache[1]
    budget = memory_budget()
    _budget_cache = (now, budget)
    return budget


def _reset_caches_for_test() -> None:
    """Clear the memoized budget. Test hook only."""
    global _budget_cache
    _budget_cache = None


# --- Prompt (kept short to minimise prefill cost on CPU) ---
_SYSTEM_PROMPT = (
    "You are Sleuth, an assistant for a bug tracker. Read the user's "
    "request and respond ONLY with a single JSON object describing the "
    "intent. No prose, no markdown, just JSON.\n"
    "\n"
    "Schema:\n"
    "  {\n"
    '    "intent": "list_bugs"|"stats"|"recent_activity"|"list_users"|'
    '"list_projects"|"bug_detail"|"help"|"unknown",\n'
    '    "filters": {\n'
    '        "status":     ["New"|"In Progress"|"Resolved"|"Closed"|"Reopened"],\n'
    '        "priority":   ["Low"|"Medium"|"High"|"Critical"],\n'
    '        "environment":["DEV"|"UAT"|"PROD"]\n'
    "    },\n"
    '    "bug_id": <int or null>\n'
    "  }\n"
    "\n"
    "Use empty arrays / null where unsure. Pick 'unknown' if the message "
    "isn't a bug-tracker query at all."
)


def _build_prompt(user_message: str) -> str:
    """Build the inference prompt. This generic chat template works for Qwen,
    Llama-3, Phi-3, and Gemma; swap to a model-specific one if quality drops."""
    return (
        f"<|system|>\n{_SYSTEM_PROMPT}\n"
        f"<|user|>\n{user_message}\n"
        f"<|assistant|>\n"
    )


def _extract_json(raw: str) -> Optional[dict[str, Any]]:
    """Extract the first JSON object from the reply, tolerating markdown fences
    and prose. raw_decode is string-aware, so a brace inside a string value
    doesn't truncate parsing."""
    if not raw:
        return None
    # Strip markdown fences, then find and decode the first JSON object.
    s = raw.strip()
    s = s.replace("```json", "").replace("```JSON", "").replace("```", "")
    decoder = json.JSONDecoder()
    idx = s.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(s[idx:])
        except ValueError:
            # This brace didn't start valid JSON; try the next one.
            idx = s.find("{", idx + 1)
            continue
        return obj
    return None


def _run_inference(message: str) -> Optional[dict[str, Any]]:
    """Run a synchronous LLM call. Returns the parsed JSON dict, or None on
    any failure (load error, timeout, parse error). Never raises."""
    global _last_used_at
    try:
        llm = _ensure_loaded()
    except Exception as exc:
        logger.warning("Sleuth LLM load failed: %s", exc)
        return None

    prompt = _build_prompt(message)
    t0 = time.time()
    try:
        # One inference at a time: the KV-cache is not concurrency-safe.
        with _inference_lock:
            out = llm(
                prompt,
                max_tokens=_MAX_NEW_TOKENS,
                temperature=0.0,    # deterministic for stable JSON
                top_p=1.0,
                stop=["<|user|>", "<|system|>", "</s>"],
                echo=False,
            )
    except Exception as exc:
        logger.warning("Sleuth LLM inference failed: %s", exc)
        return None
    finally:
        _last_used_at = time.time()

    elapsed = time.time() - t0
    if elapsed > _INFERENCE_TIMEOUT_S:
        # Post-hoc only: llama.cpp has no Python-level cancellation. Logged so
        # operators can raise the timeout or use a smaller model.
        logger.warning("Sleuth LLM exceeded budget: %.2fs > %.2fs",
                       elapsed, _INFERENCE_TIMEOUT_S)

    text = ""
    try:
        text = out["choices"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    return _extract_json(text)


_LLM_STATUSES = frozenset({
    "New", "In Progress", "Resolved", "Closed",
    "Reopened", "Not a Bug", "Resolve Later",
})
_LLM_PRIORITIES = frozenset({"Low", "Medium", "High", "Critical"})
_LLM_ENVIRONMENTS = frozenset({"DEV", "UAT", "PROD"})


def _build_pq_from_llm(message: str, parsed: dict) -> "_nlu.ParsedQuery":
    """Translate the LLM's JSON output into a ParsedQuery for the rule-based
    handlers."""
    from app.chatbot import nlu as _nlu
    pq = _nlu.ParsedQuery(raw_message=message)
    filters = parsed.get("filters") or {}
    pq.statuses = [s for s in (filters.get("status") or []) if s in _LLM_STATUSES]
    pq.priorities = [p for p in (filters.get("priority") or []) if p in _LLM_PRIORITIES]
    pq.environments = [e for e in (filters.get("environment") or []) if e in _LLM_ENVIRONMENTS]
    bid = parsed.get("bug_id")
    if isinstance(bid, int) and bid > 0:
        pq.bug_id = bid
    # The compact schema has no role/time fields; recover them from the raw
    # message via the NLU helpers so "managers only" / "activity yesterday" work.
    _nlu._populate_role_filter(message, pq)
    pq.time_window = _nlu._parse_time_window(message)
    return pq


def _dispatch_llm_intent(intent: str, db: Session, pq, ctx, actor: User) -> Optional[Response]:
    """Route a predicted intent to its rule-based handler. Read-only."""
    from app.access import accessible_project_ids
    from app.chatbot.executor import (
        _handle_bug_detail,
        _handle_help,
        _handle_list_bugs,
        _handle_list_projects,
        _handle_list_users,
        _handle_recent_activity,
        _handle_stats,
    )
    # Computed lazily per branch (not once up front) so intents that need no DB/actor
    # (help, an id-less bug_detail, unknown) stay callable with db=actor=None, as the
    # dispatch-routing unit tests do.
    if intent == "help":
        return _handle_help()
    if intent == "stats":
        return _handle_stats(db, accessible_project_ids(db, actor))
    if intent == "recent_activity":
        return _handle_recent_activity(db, pq, actor, accessible_project_ids(db, actor))
    if intent == "list_users":
        return _handle_list_users(db, pq, actor)
    if intent == "list_projects":
        return _handle_list_projects(db, accessible_project_ids(db, actor))
    if intent == "bug_detail" and pq.bug_id is not None:
        return _handle_bug_detail(db, pq, accessible_project_ids(db, actor))
    if intent == "list_bugs":
        return _handle_list_bugs(db, pq, ctx, owner_id=actor.id,
                                 accessible=accessible_project_ids(db, actor))
    return None


def try_understand(message: str, db: Session, actor: User) -> Optional[Response]:
    """Run the LLM and map its predicted intent onto a read handler, or None if
    it's unavailable/failed/"unknown". Read handlers only — writes always go
    through the rule-based parser so the user sees a confirmation first."""
    if not is_available():
        return None
    parsed = _run_inference(message)
    if not parsed:
        return None

    intent = (parsed.get("intent") or "").strip().lower()
    if intent in {"", "unknown"}:
        return None

    from app.chatbot.executor import build_context
    ctx = build_context(db, actor)
    pq = _build_pq_from_llm(message, parsed)
    return _dispatch_llm_intent(intent, db, pq, ctx, actor)


__all__ = [
    "is_available",
    "try_understand",
    "memory_budget",
    "memory_shortfall_message",
]
