"""Per-user conversation memory for the Sleuth assistant.

Sleuth conversations are stateful: people say "close it" after viewing a
bug, or "and assign her to bug 5" after listing managers. Without
remembering recent referents the assistant feels brain-dead. This module
provides that memory — small, in-process, TTL-cleaned.

Design notes:
- Storage is a plain dict keyed by user_id. We don't persist across
  process restarts on purpose: chat memory should feel like a phone call,
  not a permanent record. If the server restarts, the conversation is
  fresh — no surprise resurrections of stale "it"s.
- Access is thread-safe via a single lock. Read-modify-writes are
  bounded (a few microseconds), so contention is negligible at our scale.
- Hard caps on total sessions (200) and per-session entry size keep RAM
  flat. On a 2 GB box, every byte matters.
- TTL is 30 minutes. After that, "it" no longer means anything; the
  user has likely moved on.

The state stored is intentionally minimal:
- last_bug_id          — for pronouns like "it", "that bug"
- last_user_id         — for "her", "him" after listing/mentioning a user
- last_filter          — the most recent ParsedQuery filter dict
                         (so "and only the criticals" can refine it)
- pending_action       — a serialised ActionPlan awaiting "yes"/"confirm"
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# Tuned for a 2 GB box: 200 sessions × <1 KB each = under 200 KB.
_MAX_SESSIONS = 200
_TTL_SECONDS = 30 * 60   # 30 minutes idle


@dataclass
class _Session:
    """The mutable state we keep for one user."""
    last_bug_id: Optional[int] = None
    last_user_id: Optional[int] = None
    last_user_name: Optional[str] = None
    last_filter: dict[str, Any] = field(default_factory=dict)
    pending_action: Optional[dict[str, Any]] = None
    last_seen: float = 0.0   # epoch seconds, for TTL eviction


class _Store:
    """Thread-safe session store.

    All mutating operations take the lock and update last_seen on the
    session in question, so a chatty user keeps their context alive
    without us doing anything special.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[int, _Session] = {}

    # -- internal --------------------------------------------------------
    def _evict_expired_locked(self, now: float) -> None:
        # Prune anything older than the TTL. Caller must hold the lock.
        dead = [uid for uid, s in self._sessions.items()
                if (now - s.last_seen) > _TTL_SECONDS]
        for uid in dead:
            self._sessions.pop(uid, None)

    def _evict_oldest_locked(self) -> None:
        # Cap total sessions: drop the least-recently-used one.
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

    # -- public ----------------------------------------------------------
    def touch(self, user_id: int) -> _Session:
        """Return (or create) a session and update last_seen."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_seen = now
            return s

    def get(self, user_id: int) -> Optional[_Session]:
        """Read a session WITHOUT extending its TTL.

        Useful for code paths that want to consult memory without
        creating a session (e.g. introspection, debug).
        """
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

    def remember_user(self, user_id: int,
                      target_user_id: int,
                      target_user_name: str) -> None:
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.last_user_id = target_user_id
            s.last_user_name = target_user_name
            s.last_seen = now

    def remember_filter(self, user_id: int, filter_dict: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            # Defensive copy — the caller may keep mutating its own copy.
            s.last_filter = dict(filter_dict)
            s.last_seen = now

    def stage_pending(self, user_id: int, action: dict[str, Any]) -> None:
        """Park an action awaiting user confirmation."""
        now = time.time()
        with self._lock:
            s = self._get_or_create_locked(user_id, now)
            s.pending_action = dict(action)
            s.last_seen = now

    def take_pending(self, user_id: int) -> Optional[dict[str, Any]]:
        """Pop and return the staged action (single-use).

        Returns None if there isn't one or the session has expired.
        """
        now = time.time()
        with self._lock:
            self._evict_expired_locked(now)
            s = self._sessions.get(user_id)
            if s is None or s.pending_action is None:
                return None
            action = s.pending_action
            s.pending_action = None
            s.last_seen = now
            return action

    def clear_pending(self, user_id: int) -> None:
        with self._lock:
            s = self._sessions.get(user_id)
            if s is not None:
                s.pending_action = None
                s.last_seen = time.time()

    def reset(self, user_id: int) -> None:
        """Wipe a user's entire session — used when they 'clear' the chat."""
        with self._lock:
            self._sessions.pop(user_id, None)

    # -- test hooks ------------------------------------------------------
    def _all_sessions_for_test(self) -> dict[int, _Session]:
        with self._lock:
            return dict(self._sessions)

    def _clear_all_for_test(self) -> None:
        with self._lock:
            self._sessions.clear()


# Module-level singleton. Importers use this directly.
store = _Store()


__all__ = ["store"]
