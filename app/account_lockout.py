"""Per-account login lockout: in-memory sliding window keyed by email (not IP).

After N failures in the window the email gets a 429 lock before bcrypt runs.
Unknown emails tick the counter too, to prevent account enumeration; buckets
are per-worker and bounded by _LOCKOUT_BUCKETS_MAX.
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


# All three limits overridable via env vars.
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


def _reclaim_buckets(now: float) -> None:
    """Bound _buckets by reclaiming idle/unlocked ones first — evicting active
    locks would let bucket-churn flush a victim's lock. Caller holds _lock."""
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    dead = [
        k for k, b in _buckets.items()
        if b.locked_until <= now and (not b.fails or b.fails[-1] < cutoff)
    ]
    for k in dead:
        del _buckets[k]
    if len(_buckets) >= _LOCKOUT_BUCKETS_MAX and _buckets:
        unlocked = [k for k, b in _buckets.items() if b.locked_until <= now]
        if unlocked:
            oldest = min(
                unlocked,
                key=lambda k: _buckets[k].fails[-1] if _buckets[k].fails else 0.0,
            )
        else:
            oldest = min(_buckets, key=lambda k: _buckets[k].locked_until)
        del _buckets[oldest]


def check_locked(email: str) -> None:
    """Raise 429 if locked; runs before the verify so bcrypt cost isn't paid during a flood."""
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
    """Record a failed login; lock once the rolling window hits LOGIN_FAIL_LIMIT."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return
    now = time.monotonic()
    cutoff = now - _LOGIN_FAIL_WINDOW_SECONDS
    key = _key(email)
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            if len(_buckets) >= _LOCKOUT_BUCKETS_MAX:
                # Bound memory without dropping an active lock.
                _reclaim_buckets(now)
            bucket = _Bucket()
            _buckets[key] = bucket
        _evict_old(bucket, cutoff)
        bucket.fails.append(now)
        if len(bucket.fails) >= _LOGIN_FAIL_LIMIT:
            bucket.locked_until = now + _LOGIN_LOCKOUT_SECONDS
            # Clear stale timestamps so the lock doesn't immediately re-trip on expiry.
            bucket.fails.clear()


def clear(email: str) -> None:
    """Reset the bucket on successful login."""
    key = _key(email)
    with _lock:
        _buckets.pop(key, None)


def _reset_for_tests() -> None:
    """Wipe all state between test cases."""
    with _lock:
        _buckets.clear()
