"""Login lockout: in-memory sliding windows, per email+IP and per email.

A failure counts against the (email, IP) pair and against the email overall. The pair locks
at LOGIN_FAIL_LIMIT, so a stranger hammering a known address only locks out their own IP and
the account's owner can still sign in. The email as a whole locks at the higher
LOGIN_FAIL_EMAIL_LIMIT, which stops a distributed guesser. The check runs before bcrypt, and
unknown emails tick the counters too, to prevent account enumeration; buckets are per-worker
and bounded by _LOCKOUT_BUCKETS_MAX.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from fastapi import HTTPException

from app.config import env_int

# All limits overridable via env vars.
_LOGIN_FAIL_LIMIT = env_int("LOGIN_FAIL_LIMIT", 10)
_LOGIN_FAIL_EMAIL_LIMIT = env_int("LOGIN_FAIL_EMAIL_LIMIT", 50)
_LOGIN_FAIL_WINDOW_SECONDS = env_int("LOGIN_FAIL_WINDOW_SECONDS", 900)  # 15 min
_LOGIN_LOCKOUT_SECONDS = env_int("LOGIN_LOCKOUT_SECONDS", 900)          # 15 min
_LOCKOUT_BUCKETS_MAX = 10_000


@dataclass
class _Bucket:
    fails: deque = field(default_factory=deque)
    locked_until: float = 0.0


_buckets: dict[str, _Bucket] = {}
_lock = Lock()


def _email_limit() -> int:
    """The per-email limit can never undercut the per-address one."""
    return max(_LOGIN_FAIL_EMAIL_LIMIT, _LOGIN_FAIL_LIMIT)


def _key(email: str) -> str:
    return (email or "").strip().lower()


def _pair_key(email: str, ip: str) -> str:
    # NUL cannot appear in an email, so a pair key never collides with an email key.
    return f"{_key(email)}\x00{ip or ''}"


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


def check_locked(email: str, ip: str = "") -> None:
    """Raise 429 if locked; runs before the verify so bcrypt cost isn't paid during a flood."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return
    now = time.monotonic()
    with _lock:
        remaining = max(
            (b.locked_until - now for k in (_pair_key(email, ip), _key(email))
             if (b := _buckets.get(k)) is not None),
            default=0.0,
        )
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail="Too many failed sign-in attempts. Please try again later.",
            headers={"Retry-After": str(max(1, int(remaining)))},
        )


def _tick(key: str, limit: int, now: float) -> bool:
    """Count one failure in ``key``'s window; True when it trips the lock. Caller holds _lock."""
    bucket = _buckets.get(key)
    if bucket is None:
        if len(_buckets) >= _LOCKOUT_BUCKETS_MAX:
            # Bound memory without dropping an active lock.
            _reclaim_buckets(now)
        bucket = _Bucket()
        _buckets[key] = bucket
    _evict_old(bucket, now - _LOGIN_FAIL_WINDOW_SECONDS)
    bucket.fails.append(now)
    if len(bucket.fails) < limit:
        return False
    bucket.locked_until = now + _LOGIN_LOCKOUT_SECONDS
    # Clear stale timestamps so the lock doesn't immediately re-trip on expiry.
    bucket.fails.clear()
    return True


def record_failure(email: str, ip: str = "") -> bool:
    """Record a failed login against the email+IP pair and the email overall.
    Returns True when this failure is the one that locked either."""
    if _LOGIN_FAIL_LIMIT <= 0:
        return False
    now = time.monotonic()
    with _lock:
        pair_locked = _tick(_pair_key(email, ip), _LOGIN_FAIL_LIMIT, now)
        email_locked = _tick(_key(email), _email_limit(), now)
    return pair_locked or email_locked


def clear(email: str, ip: str = "") -> None:
    """Reset the caller's pair bucket and the email's total on successful login."""
    with _lock:
        _buckets.pop(_pair_key(email, ip), None)
        _buckets.pop(_key(email), None)

