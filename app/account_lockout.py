"""Per-account login lockout (T3).

In-memory sliding-window counter keyed by email. After N failed attempts
in a rolling window, the email is locked for L seconds — subsequent
requests are rejected with 429 before the bcrypt verify even runs.

Design notes:
  - The bucket is keyed by email, not by IP. IP rate limit lives in
    app/main.py and complements this. A determined attacker proxying
    through many IPs is stopped by the per-account counter.
  - Unknown emails also tick the counter. If we only ticked known emails
    after they existed, an attacker could enumerate accounts by which
    addresses do or do not lock — same enumeration risk we closed for
    response timing (G1).
  - Lockout is a known DoS vector: a hostile party can lock a target
    user's account by spamming bad logins. Operators who can't accept
    that trade-off should keep the limit high and the window short, or
    disable via LOGIN_FAIL_LIMIT=0.
  - Multi-worker uvicorn deployments get per-worker buckets, which
    means the effective limit is N * threshold. For stricter global
    enforcement, push limits into nginx (limit_req) or a shared store.
  - Memory is bounded by _LOCKOUT_BUCKETS_MAX so a churn of unique
    emails can't grow unboundedly.
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from fastapi import HTTPException


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except ValueError:
        return default


# Configuration knobs — env-overridable for ops.
_LOGIN_FAIL_LIMIT = _env_int("LOGIN_FAIL_LIMIT", 10)
_LOGIN_FAIL_WINDOW_SECONDS = _env_int("LOGIN_FAIL_WINDOW_SECONDS", 900)  # 15 min
_LOGIN_LOCKOUT_SECONDS = _env_int("LOGIN_LOCKOUT_SECONDS", 900)          # 15 min
_LOCKOUT_BUCKETS_MAX = 10_000


@dataclass
class _Bucket:
    fails: deque = field(default_factory=deque)
    locked_until: float = 0.0


_buckets: dict[str, _Bucket] = {}
_lock = Lock()


def _key(email: str) -> str:
    return (email or "").strip().lower()


def _evict_old(bucket: _Bucket, cutoff: float) -> None:
    while bucket.fails and bucket.fails[0] < cutoff:
        bucket.fails.popleft()


def check_locked(email: str) -> None:
    """Raise 429 if the account is currently locked out. Called BEFORE
    the password verify so the bcrypt cost isn't paid during a lockout
    flood (defense against amplification)."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return
    now = time.monotonic()
    key = _key(email)
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None or bucket.locked_until <= now:
            return
        retry_after = max(1, int(bucket.locked_until - now))
    raise HTTPException(
        status_code=429,
        detail="Too many failed sign-in attempts. Please try again later.",
        headers={"Retry-After": str(retry_after)},
    )


def record_failure(email: str) -> None:
    """Increment the failure counter for this email. If the rolling
    window has accumulated >= LOGIN_FAIL_LIMIT failures, set the lockout
    timestamp. Called only when the login itself failed."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return
    now = time.monotonic()
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    key = _key(email)
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            if len(_buckets) >= _LOCKOUT_BUCKETS_MAX:
                # Drop an arbitrary old entry to keep memory bounded.
                _buckets.pop(next(iter(_buckets)), None)
            bucket = _Bucket()
            _buckets[key] = bucket
        _evict_old(bucket, cutoff)
        bucket.fails.append(now)
        if len(bucket.fails) >= _LOGIN_FAIL_LIMIT:
            bucket.locked_until = now + _LOGIN_LOCKOUT_SECONDS


def clear(email: str) -> None:
    """Reset the bucket for this email — called on successful login so a
    user who finally types their password right doesn't carry the
    failure debt forward."""
    key = _key(email)
    with _lock:
        _buckets.pop(key, None)


def _reset_for_tests() -> None:
    """Wipe all state. Tests must call this between cases so leftover
    buckets don't leak across the suite."""
    with _lock:
        _buckets.clear()
