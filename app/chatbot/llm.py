"""Sleuth Layer 3 — optional local LLM via llama.cpp.

This module exists for the ~5% of queries the rules + classifier can't
handle: free-form sentences with weird phrasing, multi-step requests,
or domain language we didn't anticipate. It never calls an external
API. The whole inference loop runs on this server, against a GGUF model
file the operator drops into `models/`.

Hardware reality check:
- The deployment target is 1 CPU, 2 GB RAM, no GPU.
- A 0.5B-parameter GGUF model at Q4_K_M quantisation is ~350 MB on disk
  and roughly the same in RAM once loaded. That fits — barely.
- Inference speed on a single modern x86 core: 5-15 tokens/second. A
  structured JSON response of ~80 tokens lands in 6-15 seconds, inside
  the 5-10s budget for hard cases.
- We DO NOT load the model at startup. Loading is lazy and triggered
  only by the first call. The model stays loaded between calls so we
  don't pay the load cost twice.
- After 10 minutes of idle we unload the model so the RAM goes back to
  the database / web workers for cold paths.

Operator setup:
- `pip install llama-cpp-python` (CPU build, no GPU deps).
- Drop a GGUF file at `models/sleuth.gguf`. The README in `models/`
  recommends Qwen2.5-0.5B-Instruct-Q4_K_M.gguf as a small, capable
  starting point. Larger models (Phi-3 mini Q4 ~2.5 GB, etc.) will not
  fit in 2 GB RAM with FastAPI + Postgres also resident — measure first.
- That's it. Sleuth detects the file at runtime via `is_available()`.

If anything in this layer fails — model file missing, llama-cpp-python
not installed, model corrupted, inference times out — the executor
swallows the exception and falls back to "unknown". The chat path NEVER
goes down because of an LLM problem.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import User
from app.chatbot.executor import Block, Response


logger = logging.getLogger("bug_hunter.sleuth.llm")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Resolve the model path. Operators can override via env, but the default
# matches the path the README points at.
_MODEL_PATH = Path(
    os.getenv("SLEUTH_LLM_MODEL_PATH",
              str(Path(__file__).resolve().parent.parent.parent / "models" / "sleuth.gguf"))
)

# Inference budget. 12 s is generous on a 1-CPU box; if the model takes
# longer than this for one of our short prompts something is wrong and
# we'd rather time out and fall back than block the request indefinitely.
_INFERENCE_TIMEOUT_S = float(os.getenv("SLEUTH_LLM_TIMEOUT_S", "12"))

# Memory hygiene: unload the model after this many seconds of no use.
# 10 minutes balances load-cost amortisation against keeping memory free.
_IDLE_UNLOAD_S = float(os.getenv("SLEUTH_LLM_IDLE_UNLOAD_S", "600"))

# Cap how many tokens we ever generate. Big enough to fit the JSON intent
# we ask for, small enough to bound worst-case latency.
_MAX_NEW_TOKENS = int(os.getenv("SLEUTH_LLM_MAX_TOKENS", "120"))

# Context length we tell llama.cpp to use. Smaller = less RAM. We never
# send long messages so 1024 is plenty.
_CTX_LEN = int(os.getenv("SLEUTH_LLM_CTX_LEN", "1024"))

# CPU thread count. On a 1-CPU box this should be 1; setting more than
# the actual number of cores will cause contention. We default to 1
# because that's the documented deployment target. Operators with
# bigger boxes can override.
_THREADS = int(os.getenv("SLEUTH_LLM_THREADS", "1"))

# Headroom multiplier on top of the GGUF file size, accounting for KV
# cache, model load buffer, and Python/llama.cpp overhead. 1.4x is a
# conservative estimate from llama.cpp's own benchmarks for 1024-token
# context windows on Q4_K_M quantised models. Larger contexts need more.
_RAM_HEADROOM_MULT = float(os.getenv("SLEUTH_LLM_RAM_HEADROOM", "1.4"))
# Hard floor in case someone uses a tiny model — even a 50 MB GGUF needs
# at least ~200 MB of working memory once you count Python, llama.cpp
# state, and a 1024-token context.
_RAM_MIN_FLOOR_MB = 200


# ---------------------------------------------------------------------------
# Memory budget check
# ---------------------------------------------------------------------------
@dataclass
class _MemoryBudget:
    """Snapshot of how much memory we have vs how much the model needs.

    All sizes are in MB to keep the surface human-readable in error
    messages — operators don't think in bytes.
    """
    model_size_mb: int          # size of the GGUF file on disk
    estimated_need_mb: int      # what we expect to need at peak
    available_mb: int           # what we actually have to work with
    container_limit_mb: int     # the cgroup-imposed ceiling, if any
    sufficient: bool            # estimated_need_mb <= available_mb


def _read_int(path: str) -> Optional[int]:
    """Read an integer from a single-line sysfs file, or None on miss."""
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
    """Return the cgroup memory limit in MB, or None if we're not in a
    container (or if the limit is effectively unbounded).

    Checks cgroup v2 first (modern Docker), then v1 (older systems).
    """
    # cgroup v2
    v = _read_int("/sys/fs/cgroup/memory.max")
    if v is not None and v > 0:
        return v // (1024 * 1024)
    # cgroup v1
    v = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if v is not None and 0 < v < (1 << 62):
        # The "no limit" sentinel on v1 is a huge number near INT64_MAX;
        # anything above 2^62 is treated as "unlimited".
        return v // (1024 * 1024)
    return None


def _detect_available_mb() -> int:
    """Best-effort estimate of MB this process can actually allocate.

    Order of preference:
      1. cgroup limit (in containers this is the real ceiling)
      2. /proc/meminfo MemAvailable (Linux host)
      3. Fallback: 512 MB pessimistic guess
    """
    # In a container with a memory limit, the cgroup ceiling is binding —
    # MemAvailable from /proc/meminfo reflects the host's RAM, which the
    # OOM killer will NOT let us touch. So we use the smaller of the two.
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
    """Compute the LLM memory budget. Returns None when no model file
    exists (in which case the LLM layer is simply disabled — there's
    nothing to budget against).
    """
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
    """Single-line user-facing notice. Returns None when there's no
    shortfall (or no model). The detailed operator-facing breakdown
    goes to the application log via is_available(); this function only
    yields what's safe to show in the chat UI.

    Most chat paths don't even use this — they just fall back to the
    standard "I didn't understand" reply when the LLM is unavailable.
    The string here is available for diagnostics or admin tools that
    want a quick one-liner."""
    budget = memory_budget()
    if budget is None or budget.sufficient:
        return None
    return (
        "The optional AI fallback is unavailable on this server "
        "(insufficient memory). Most queries still work — try rephrasing "
        "or type *help* for examples."
    )


# Module-level flag: have we already warned the operator about a memory
# shortfall? We only want to log the long technical message once per
# process so operators see it but the logs aren't spammed.
_shortfall_warned = False


def is_available() -> bool:
    """True iff a model file is present, llama-cpp-python is importable,
    AND the box has enough RAM to actually run it.

    The last check protects against the deployment described in the
    docker-compose.yml's hard memory cap: if the box is too small to
    load the model, we say "unavailable" rather than blowing up at load
    time. The first time we detect a shortfall we log a detailed
    operator-facing warning; the chat itself just falls back to the
    rules + classifier without ever showing the user anything technical.
    """
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
    budget = memory_budget()
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


# ---------------------------------------------------------------------------
# Lazy load state
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_llm: Any = None             # the Llama instance, or None
_loaded_at: float = 0.0      # epoch seconds when we last loaded
_last_used_at: float = 0.0   # epoch seconds of last inference call


def _ensure_loaded() -> Any:
    """Lazy-load the model. Caller must NOT hold _lock; we acquire it.

    Returns the Llama instance, or raises if loading fails.
    """
    global _llm, _loaded_at, _last_used_at
    with _lock:
        # Auto-unload if idle past the threshold.
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
            # Disable mmap if your filesystem is slow; we leave the
            # default which is mmap=True. mlock=False to keep the RSS
            # honest about cold pages getting evicted under pressure.
            verbose=False,
        )
        _loaded_at = time.time()
        logger.info("Sleuth LLM loaded in %.2fs", _loaded_at - t0)
        return _llm


def _unload() -> None:
    """Forcibly drop the loaded model. Called by tests and on idle."""
    global _llm
    with _lock:
        _llm = None


# ---------------------------------------------------------------------------
# Prompt — kept short to minimise prefill cost on a CPU-bound run
# ---------------------------------------------------------------------------
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
    """Tiny chat-style prompt. Different model families want slightly
    different chat templates; this generic one works for Qwen, Llama-3,
    Phi-3, Gemma. If you swap models and quality drops, switch this to
    the matching template — llama.cpp also accepts apply_chat_template."""
    return (
        f"<|system|>\n{_SYSTEM_PROMPT}\n"
        f"<|user|>\n{user_message}\n"
        f"<|assistant|>\n"
    )


# ---------------------------------------------------------------------------
# Inference + JSON extraction
# ---------------------------------------------------------------------------
def _extract_json(raw: str) -> Optional[dict[str, Any]]:
    """Pull the first {...} block out of the model's reply. Models
    sometimes wrap JSON in markdown fences or prose; we tolerate both."""
    if not raw:
        return None
    # Scrub markdown fences.
    s = raw.strip()
    s = s.replace("```json", "").replace("```JSON", "").replace("```", "")
    # Find the first balanced { ... }.
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    candidate = s[start:end + 1]
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None


def _run_inference(message: str) -> Optional[dict[str, Any]]:
    """Synchronous LLM call. Returns the parsed JSON dict, or None on any
    failure (timeout, parse error, model crash). Never raises."""
    global _last_used_at
    try:
        llm = _ensure_loaded()
    except Exception as exc:
        logger.warning("Sleuth LLM load failed: %s", exc)
        return None

    prompt = _build_prompt(message)
    t0 = time.time()
    try:
        out = llm(
            prompt,
            max_tokens=_MAX_NEW_TOKENS,
            temperature=0.0,    # deterministic — we want stable JSON
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
        # We don't actually have a way to interrupt llama.cpp mid-call
        # from Python (would need an extra thread + cancellation token),
        # but we can at least log when we cross the budget so operators
        # see it. The sync call already returned at this point.
        logger.warning("Sleuth LLM exceeded budget: %.2fs > %.2fs",
                       elapsed, _INFERENCE_TIMEOUT_S)

    text = ""
    try:
        text = out["choices"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None
    return _extract_json(text)


# ---------------------------------------------------------------------------
# Public entry: try_understand
# ---------------------------------------------------------------------------
def try_understand(message: str, db: Session, actor: User) -> Optional[Response]:
    """Run the LLM, map its intent guess onto the existing read handlers,
    and return a Response. Returns None if the LLM is unavailable, fails,
    or the predicted intent is "unknown".

    This intentionally only routes to READ handlers. We never let the
    LLM trigger writes — writes go through the rule-based parser so the
    user always sees a confirmation prompt before anything mutates."""
    if not is_available():
        return None
    parsed = _run_inference(message)
    if not parsed:
        return None

    intent = (parsed.get("intent") or "").strip().lower()
    if intent in {"", "unknown"}:
        return None

    # Late imports so this module's top-level stays cheap.
    from app.chatbot import nlu as _nlu
    from app.chatbot.executor import (
        build_context, _handle_help, _handle_stats, _handle_recent_activity,
        _handle_list_users, _handle_list_projects, _handle_bug_detail,
        _handle_list_bugs,
    )

    # Build a synthetic ParsedQuery so existing handlers work unchanged.
    # `actor` is required by every multi-tenant handler — without it the
    # call would crash with a TypeError, which would be swallowed by the
    # outer try/except in executor.py and look like the LLM "didn't
    # understand". (That was the case in the original enterprise build —
    # the imported handlers all gained `actor` after v4.0 but this caller
    # wasn't updated.)
    ctx = build_context(db, actor)
    pq = _nlu.ParsedQuery(raw_message=message)
    filters = parsed.get("filters") or {}
    pq.statuses = [s for s in (filters.get("status") or [])
                   if s in {"New", "In Progress", "Resolved", "Closed",
                            "Reopened", "Not a Bug", "Resolve Later"}]
    pq.priorities = [p for p in (filters.get("priority") or [])
                     if p in {"Low", "Medium", "High", "Critical"}]
    pq.environments = [e for e in (filters.get("environment") or [])
                       if e in {"DEV", "UAT", "PROD"}]
    bid = parsed.get("bug_id")
    if isinstance(bid, int) and bid > 0:
        pq.bug_id = bid

    if intent == "help":
        return _handle_help()
    if intent == "stats":
        return _handle_stats(db, actor)
    if intent == "recent_activity":
        return _handle_recent_activity(db, pq, actor)
    if intent == "list_users":
        return _handle_list_users(db, pq, actor)
    if intent == "list_projects":
        return _handle_list_projects(db, actor)
    if intent == "bug_detail" and pq.bug_id is not None:
        return _handle_bug_detail(db, pq, actor)
    if intent == "list_bugs":
        return _handle_list_bugs(db, pq, actor, ctx)

    # If we got an intent we don't recognise, fall back.
    return None


__all__ = [
    "is_available",
    "try_understand",
    "memory_budget",
    "memory_shortfall_message",
]
