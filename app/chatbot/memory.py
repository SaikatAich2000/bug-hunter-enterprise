"""Per-user conversation memory for Sleuth, so "close it" or "assign her to
bug 5" resolve against recent referents. A small in-process, lock-guarded dict
keyed by user_id; it doesn't survive restarts, and a 200-session cap plus
30-minute TTL keep RAM bounded.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

_MAX_SESSIONS = 200
_TTL_SECONDS = 30 * 60   # 30 minutes idle
# Shorter than the session TTL so a stray late "ok" can't fire a write.
_CONFIRM_TTL_SECONDS = 5 * 60   # 5 minutes


@dataclass
class _Session:
    """The mutable state we keep for one user."""
    last_bug_id: Optional[int] = None
    pending_action: Optional[dict[str, Any]] = None
    # Separate from last_seen so later activity doesn't extend the confirm window.
    pending_staged_at: float = 0.0
    # Candidate specs from an upload; created only on explicit confirmation.
    pending_ingest: Optional[dict[str, Any]] = None
    pending_ingest_staged_at: float = 0.0
    last_seen: float = 0.0   # epoch seconds, for TTL eviction


class _Store:
    """Thread-safe in-process session store; every mutation holds the lock and
    refreshes last_seen so ongoing activity keeps a user's context alive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}

    def _evict_expired_locked(self, now: float) -> None:
        # Caller must hold the lock.
        dead = [uid for uid, s in self._sessions.items()
                if (now - s.last_seen) > _TTL_SECONDS]
        for uid in dead:
            self._sessions.pop(uid, None)

    def _evict_oldest_locked(self) -> None:
        # Drop the LRU session when at capacity.
        if len(self._sessions) < _MAX_SESSIONS:
            return
        oldest_uid = min(self._sessions.items(),
                         key=lambda kv: kv[1].last_seen)[0]
        self._sessions.pop(oldest_uid, None)

    def _get_or_create_locked(self, user_id: int, now: float) -> _Session:
        s = self._sessions.get(user_id)
        if s is None:
            self._evict_expired_locked(now)
            self._evict_oldest_locked()
            s = _Session(last_seen=now)
            self._sessions[user_id] = s
        return s

    def touch(self, user_id: int) -> _Session:
        """Return (or create) a session and update last_seen."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_seen = now
            return s

    def get(self, user_id: int) -> Optional[_Session]:
        """Read a session without extending its TTL or creating one."""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            return self._sessions.get(user_id)

    def remember_bug(self, user_id: int, bug_id: int) -> None:
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_bug_id = bug_id
            s.last_seen = now

    def stage_pending(self, user_id: int, action: dict[str, Any]) -> None:
        """Park an action awaiting user confirmation."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.pending_action = dict(action)
            s.pending_staged_at = now
            s.last_seen = now

    def take_pending(self, user_id: int) -> Optional[dict[str, Any]]:
        """Pop and return the staged action (single-use), or None if none / the
        session expired / the confirm window closed. Clears a stale proposal
        rather than executing on a late "yes"."""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            s = self._sessions.get(user_id)
            if s is None or s.pending_action is None:
                return None
            if (now - s.pending_staged_at) > _CONFIRM_TTL_SECONDS:
                s.pending_action = None
                return None
            action = s.pending_action
            s.pending_action = None
            s.last_seen = now
            return action

    def stage_ingest(self, user_id: int, data: dict[str, Any]) -> None:
        """Park a parsed document's candidate specs awaiting 'create them'."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.pending_ingest = dict(data)
            s.pending_ingest_staged_at = now
            s.last_seen = now

    def _ingest_if_fresh(self, s: Optional["_Session"], now: float) -> Optional[dict[str, Any]]:
        """Return the staged ingest if it's within the confirm window, else clear and return None."""
        if s is None or s.pending_ingest is None:
            return None
        if (now - s.pending_ingest_staged_at) > _CONFIRM_TTL_SECONDS:
            s.pending_ingest = None
            return None
        return s.pending_ingest

    def peek_ingest(self, user_id: int) -> Optional[dict[str, Any]]:
        """Return the staged ingest without consuming it (None if none/expired)."""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            return self._ingest_if_fresh(self._sessions.get(user_id), now)

    def take_ingest(self, user_id: int) -> Optional[dict[str, Any]]:
        """Pop and return the staged ingest (single-use)."""
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            s = self._sessions.get(user_id)
            data = self._ingest_if_fresh(s, now)
            if data is None:
                return None
            s.pending_ingest = None
            s.last_seen = now
            return data

    def reset(self, user_id: int) -> None:
        """Wipe a user's session entirely (e.g. when they clear the chat)."""
        with self._lock:
            self._sessions.pop(user_id, None)

    def _all_sessions_for_test(self) -> dict[int, _Session]:
        with self._lock:
            return dict(self._sessions)

    def _clear_all_for_test(self) -> None:
        with self._lock:
            self._sessions.clear()


# Module-level singleton.
store = _Store()


__all__ = ["store"]
