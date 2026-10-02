"""Tests for the optional in-app email-digest cron scheduler (app/scheduler.py).

Covers the standard-cron parser/matcher, the timezone resolver, the
start()/stop() control surface, and one digest tick — without depending on
wall-clock timing (the only untested line is the infinite sleep loop itself).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app import scheduler
from app.scheduler import CronSchedule, _parse_field, _resolve_tz


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# --- field parser ---------------------------------------------------------
def test_parse_field_forms():
    assert _parse_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}
    assert _parse_field("5", 0, 59) == {5}
    assert _parse_field("1-3", 0, 59) == {1, 2, 3}
    assert _parse_field("0,30", 0, 59) == {0, 30}
    assert _parse_field("*/15", 0, 59) == {0, 15, 30, 45}
    assert _parse_field("10-20/5", 0, 59) == {10, 15, 20}


@pytest.mark.parametrize("bad", ["", "99", "5-2", "*/0", "1-99"])
def test_parse_field_invalid(bad):
    with pytest.raises(ValueError):
        _parse_field(bad, 0, 59)


# --- CronSchedule.matches -------------------------------------------------
def test_daily_at_seven():
    c = CronSchedule("0 7 * * *")
    assert c.matches(_dt(2026, 6, 15, 7, 0))
    assert not c.matches(_dt(2026, 6, 15, 7, 1))
    assert not c.matches(_dt(2026, 6, 15, 8, 0))


def test_every_30_minutes():
    c = CronSchedule("*/30 * * * *")
    assert c.matches(_dt(2026, 6, 15, 3, 0))
    assert c.matches(_dt(2026, 6, 15, 3, 30))
    assert not c.matches(_dt(2026, 6, 15, 3, 15))


def test_weekdays_only():
    c = CronSchedule("0 9 * * 1-5")
    assert c.matches(_dt(2026, 6, 15, 9, 0))      # Mon 2026-06-15
    assert not c.matches(_dt(2026, 6, 20, 9, 0))  # Sat
    assert not c.matches(_dt(2026, 6, 21, 9, 0))  # Sun


def test_sunday_accepts_zero_and_seven():
    assert CronSchedule("0 0 * * 0").matches(_dt(2026, 6, 21, 0, 0))  # Sun
    assert CronSchedule("0 0 * * 7").matches(_dt(2026, 6, 21, 0, 0))  # Sun


def test_dom_dow_or_semantics():
    # When both day-of-month and day-of-week are restricted, cron matches either.
    c = CronSchedule("0 0 15 * 1")
    assert c.matches(_dt(2026, 6, 15, 0, 0))   # the 15th
    assert c.matches(_dt(2026, 6, 1, 0, 0))    # a Monday
    assert not c.matches(_dt(2026, 6, 3, 0, 0))  # neither


def test_invalid_field_count():
    with pytest.raises(ValueError):
        CronSchedule("0 7 * *")


# --- timezone resolver ----------------------------------------------------
def test_resolve_tz_default_is_utc():
    assert _resolve_tz("") is timezone.utc


def test_resolve_tz_unknown_falls_back_to_utc():
    assert _resolve_tz("Not/AZone") is timezone.utc


def test_resolve_tz_valid_returns_tzinfo():
    # On hosts without tz data this gracefully falls back to UTC; either way
    # it's a valid tzinfo and never raises.
    assert _resolve_tz("Asia/Kolkata") is not None


# --- start()/stop() control ----------------------------------------------
def test_start_noop_when_cron_empty(monkeypatch):
    monkeypatch.setattr(scheduler, "_task", None)
    scheduler.start()  # default settings: EMAIL_DIGEST_CRON is empty
    assert scheduler._task is None


def test_start_noop_when_cron_and_digest_both_off(monkeypatch):
    # Explicit, deterministic counterpart to the "default settings" test above:
    # cron empty and digest disabled together must skip the warning entirely.
    monkeypatch.setattr(scheduler, "get_settings", lambda: type(
        "S", (), {"EMAIL_DIGEST_CRON": "",
                  "EMAIL_DIGEST_ENABLED": False,
                  "EMAIL_DIGEST_TIMEZONE": "",
                  "AUDIT_RETENTION_DAYS": 0})())
    monkeypatch.setattr(scheduler, "_task", None)
    scheduler.start()
    assert scheduler._task is None


def test_start_noop_when_digest_disabled(monkeypatch):
    monkeypatch.setattr(scheduler, "get_settings", lambda: type(
        "S", (), {"EMAIL_DIGEST_CRON": "0 7 * * *",
                  "EMAIL_DIGEST_ENABLED": False,
                  "EMAIL_DIGEST_TIMEZONE": "",
                  "AUDIT_RETENTION_DAYS": 0})())
    monkeypatch.setattr(scheduler, "_task", None)
    scheduler.start()
    assert scheduler._task is None


def test_start_noop_on_invalid_cron(monkeypatch):
    monkeypatch.setattr(scheduler, "get_settings", lambda: type(
        "S", (), {"EMAIL_DIGEST_CRON": "not a cron",
                  "EMAIL_DIGEST_ENABLED": True,
                  "EMAIL_DIGEST_TIMEZONE": "",
                  "AUDIT_RETENTION_DAYS": 0})())
    monkeypatch.setattr(scheduler, "_task", None)
    scheduler.start()
    assert scheduler._task is None


def test_start_warns_when_enabled_but_cron_empty(monkeypatch, caplog):
    # Digest mode suppresses immediate emails, so "enabled but no cron" means
    # nothing is ever delivered. start() must stay a no-op but log a loud warning
    # rather than failing silently.
    monkeypatch.setattr(scheduler, "get_settings", lambda: type(
        "S", (), {"EMAIL_DIGEST_CRON": "",
                  "EMAIL_DIGEST_ENABLED": True,
                  "EMAIL_DIGEST_TIMEZONE": "",
                  "AUDIT_RETENTION_DAYS": 0})())
    monkeypatch.setattr(scheduler, "_task", None)
    with caplog.at_level("WARNING", logger="bug_hunter.scheduler"):
        scheduler.start()
    assert scheduler._task is None
    assert any("EMAIL_DIGEST_CRON is empty" in r.message for r in caplog.records)


def test_stop_noop_when_never_started(monkeypatch):
    monkeypatch.setattr(scheduler, "_task", None)
    asyncio.run(scheduler.stop())  # must not raise


# --- one digest tick ------------------------------------------------------
def test_tick_runs_when_schedule_matches(monkeypatch):
    seen = {}

    def fake_run():
        seen["ran"] = True
        return {"emails_sent": 2, "operations": 5, "users": 2}

    monkeypatch.setattr(scheduler, "_run_digest_once", fake_run)
    c = CronSchedule("* * * * *")
    stats = asyncio.run(scheduler._tick(c, timezone.utc, now=_dt(2026, 6, 15, 7, 0)))
    assert seen.get("ran")
    assert stats["emails_sent"] == 2


def test_tick_skips_when_no_match(monkeypatch):
    monkeypatch.setattr(scheduler, "_run_digest_once",
                        lambda: pytest.fail("must not run when schedule misses"))
    c = CronSchedule("0 7 * * *")
    assert asyncio.run(scheduler._tick(c, timezone.utc, now=_dt(2026, 6, 15, 8, 0))) is None


def test_tick_swallows_run_failure(monkeypatch):
    def boom():
        raise RuntimeError("smtp down")
    monkeypatch.setattr(scheduler, "_run_digest_once", boom)
    c = CronSchedule("* * * * *")
    assert asyncio.run(scheduler._tick(c, timezone.utc, now=_dt(2026, 6, 15, 7, 0))) is None
