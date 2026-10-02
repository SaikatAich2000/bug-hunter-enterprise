"""Targeted coverage for small defensive/edge branches in the infra layer.
Lockout bucket reclamation, XFF empty-input guard, animated-image preservation, scheduler re-entry.
"""
from __future__ import annotations

from collections import deque

from app import account_lockout, image_strip, scheduler
from app.auth import trusted_forwarded_ip


# account_lockout._reclaim_buckets
def test_reclaim_deletes_idle_bucket():
    account_lockout._reset_for_tests()
    # Idle: unlocked (locked_until 0) and no in-window failures → reclaimed.
    account_lockout._buckets["idle@x"] = account_lockout._Bucket(
        fails=deque(), locked_until=0.0)
    account_lockout._reclaim_buckets(1000.0)
    assert "idle@x" not in account_lockout._buckets
    account_lockout._reset_for_tests()


def test_reclaim_evicts_soonest_expiring_when_all_locked(monkeypatch):
    account_lockout._reset_for_tests()
    monkeypatch.setattr(account_lockout, "_LOCKOUT_BUCKETS_MAX", 2)
    # Two locked buckets (locked_until in the future, recent fails so neither is idle).
    account_lockout._buckets["a@x"] = account_lockout._Bucket(
        fails=deque([999.0]), locked_until=5000.0)
    account_lockout._buckets["b@x"] = account_lockout._Bucket(
        fails=deque([999.0]), locked_until=6000.0)
    account_lockout._reclaim_buckets(1000.0)
    # No idle buckets: reclaim falls back to evicting the soonest-to-expire (a@x).
    assert "a@x" not in account_lockout._buckets
    assert "b@x" in account_lockout._buckets
    account_lockout._reset_for_tests()


# auth.trusted_forwarded_ip — empty input guard (line 79)
def test_trusted_forwarded_ip_empty_returns_none():
    assert trusted_forwarded_ip("", 1) is None
    assert trusted_forwarded_ip("   ,  ,", 1) is None
    assert trusted_forwarded_ip(None, 1) is None


# image_strip — animated/multi-frame images are preserved untouched (85-86)
def test_strip_preserves_animated_image(monkeypatch):
    from PIL import Image

    class _FakeAnimated:
        size = (4, 4)
        is_animated = True
        n_frames = 3

    # Saving multi-frame would drop all but the first frame; must return original bytes.
    monkeypatch.setattr(Image, "open", lambda src: _FakeAnimated())
    data = b"GIF89a-fake-animated-bytes"
    assert image_strip.strip_image_metadata(data, "image/gif") == data


# scheduler.start() re-entry guard (193-194)
def test_scheduler_start_reentry_is_noop(monkeypatch):
    class _FakeTask:
        def done(self):
            return False

    fake = _FakeTask()
    monkeypatch.setattr(scheduler, "_task", fake)
    scheduler.start()  # already running: should log and return without replacing _task
    assert scheduler._task is fake
