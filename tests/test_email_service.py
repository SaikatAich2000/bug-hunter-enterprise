"""Rendering + dispatch tests for app/email_service.py.
Every notify_* body builder, recipient helpers, and SMTP transport against a fake smtplib.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from app import email_service as es

# Settings binds os.getenv at class-definition time, so monkeypatch get_settings directly.


def _use_backend(monkeypatch, backend: str, **over) -> None:
    cfg = SimpleNamespace(
        EMAIL_BACKEND=backend,
        EMAIL_FROM="Test Tracker <bot@bh.local>",
        APP_NAME="Test Tracker",
        APP_BASE_URL="http://bh.local:8765",
        SMTP_HOST="mail.bh.local",
        SMTP_PORT=587,
        SMTP_USERNAME="",
        SMTP_PASSWORD="",
        SMTP_USE_TLS=False,
        SMTP_USE_SSL=False,
        SMTP_TIMEOUT=10,
        # Digest off so these tests exercise the immediate-send path.
        EMAIL_DIGEST_ENABLED=False,
        EMAIL_DIGEST_LOOKBACK_HOURS=26,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    monkeypatch.setattr(es, "get_settings", lambda: cfg)


def _user(uid: int, name: str, email: str) -> es.UserSnapshot:
    return es.UserSnapshot(id=uid, name=name, email=email)


def _bug(**over) -> es.BugSnapshot:
    base = {
        "id": 7, "title": "Login crash", "project_name": "Web", "status": "New",
        "priority": "High", "environment": "PROD", "description": "Boom on submit.",
        "reporter": _user(1, "Reporter", "rep@bh.local"),
        "assignees": (_user(2, "Dev A", "deva@bh.local"), _user(3, "Dev B", "devb@bh.local")),
    }
    base.update(over)
    return es.BugSnapshot(**base)


def _event(**over) -> es.EventSnapshot:
    base = {
        "id": 4, "name": "Sprint Standup", "description": "Daily sync",
        "scheduled_for": "2026-06-13",
        "managers": (_user(2, "Meg", "meg@bh.local"),),
    }
    base.update(over)
    return es.EventSnapshot(**base)


# Recipient selection
def test_recipients_dedup_and_exclude_actor():
    dup = _user(2, "Dev A", "deva@bh.local")
    bug = _bug(assignees=(dup, _user(2, "Dev A copy", "DEVA@bh.local"), _user(9, "", "")))
    # Dedup is case-insensitive; the no-email assignee is dropped.
    out = es._recipients(bug, exclude_user_id=None)
    assert out == ["rep@bh.local", "deva@bh.local"]
    # Excluding the reporter removes them from the list.
    assert "rep@bh.local" not in es._recipients(bug, exclude_user_id=1)


def test_recipients_no_reporter():
    bug = _bug(reporter=None, assignees=())
    assert es._recipients(bug, exclude_user_id=None) == []


def test_event_recipients_dedup_and_exclude():
    ev = _event(managers=(
        _user(2, "Meg", "meg@bh.local"),
        _user(2, "Meg again", "MEG@bh.local"),
        _user(5, "NoMail", ""),
    ))
    assert es._event_recipients(ev, exclude_user_id=None) == ["meg@bh.local"]
    assert es._event_recipients(ev, exclude_user_id=2) == []


# Console backend — every notify_* body builder runs end to end
def test_console_renders_every_notification(monkeypatch, caplog):
    _use_backend(monkeypatch, "console")
    bug = _bug(item_type="Task", event_name="Standup")
    ev = _event()
    with caplog.at_level(logging.INFO, logger="bug_hunter.email"):
        es.notify_bug_created(bug, actor_user_id=None)
        es.notify_bug_updated(bug, [("status", "New", "In Progress")], "Alice", actor_user_id=1)
        es.notify_assignment(bug, [_user(8, "New Dev", "newdev@bh.local")], "Alice")
        es.notify_comment_added(bug, "Bob", 99, "Looks bad")
        es.notify_event_created(ev, "Alice", actor_user_id=None)
        es.notify_event_updated(ev, [("name", "Old", "New")], "Alice", actor_user_id=None)
        es.notify_event_deleted(ev, "Alice", actor_user_id=None)
        es.notify_password_reset("user@bh.local", "User", "http://bh.local/reset?t=abc")
    blob = caplog.text
    assert "New task #7" in blob
    assert "Task #7 updated" in blob
    assert "assigned to task #7" in blob
    assert "New comment on task #7" in blob
    assert "New event #4" in blob
    assert "Event #4 updated" in blob
    assert "Event #4 deleted" in blob
    assert "Reset your password" in blob


def test_notify_short_circuits_when_no_recipients(monkeypatch, caplog):
    _use_backend(monkeypatch, "console")
    empty = _bug(reporter=None, assignees=())
    ev = _event(managers=())
    with caplog.at_level(logging.INFO, logger="bug_hunter.email"):
        es.notify_bug_created(empty, actor_user_id=None)
        es.notify_bug_updated(empty, [], "A", 1)          # no changes
        es.notify_bug_updated(empty, [("s", "a", "b")], "A", 1)  # no recipients
        es.notify_comment_added(empty, "Bob", None, "hi")
        es.notify_event_created(ev, "A", None)
        es.notify_event_updated(ev, [], "A", None)        # no changes
        es.notify_event_deleted(ev, "A", None)
        es.notify_password_reset("", "Nobody", "url")     # no email
    assert not [r for r in caplog.records if "console-email" in (r.message or "")]


def test_deliver_disabled_is_noop(monkeypatch, caplog):
    _use_backend(monkeypatch, "disabled")
    with caplog.at_level(logging.INFO, logger="bug_hunter.email"):
        es.deliver("Subject", ["a@b.local"], "body")
    assert not any("console-email" in (r.message or "") for r in caplog.records)


def test_deliver_drops_blank_recipients(monkeypatch, caplog):
    _use_backend(monkeypatch, "console")
    with caplog.at_level(logging.INFO, logger="bug_hunter.email"):
        es.deliver("Subject", ["", "   ", None], "body")  # type: ignore[list-item]
    assert not any("console-email" in (r.message or "") for r in caplog.records)


# SMTP transport (fake smtplib — no network)
class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.sent, self.logged_in, self.started_tls, self.ehlo_count = [], False, False, 0
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.ehlo_count += 1

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, pw):
        self.logged_in = True

    def send_message(self, msg):
        self.sent.append(msg)


_CRED = "s3cr3t"  # placeholder value to exercise the auth branch


def test_smtp_starttls_path(monkeypatch):
    monkeypatch.setattr(_FakeSMTP, "instances", [])
    monkeypatch.setattr(es.smtplib, "SMTP", _FakeSMTP)
    _use_backend(
        monkeypatch, "smtp",
        SMTP_HOST="mail.bh.local", SMTP_PORT=587, SMTP_USE_TLS=True,
        SMTP_USE_SSL=False, SMTP_USERNAME="bot", SMTP_PASSWORD=_CRED,
    )
    es.deliver("Hi", ["a@b.local"], "body")
    assert len(_FakeSMTP.instances) == 1
    inst = _FakeSMTP.instances[0]
    assert inst.started_tls
    assert inst.logged_in
    assert len(inst.sent) == 1


def test_smtp_ssl_path(monkeypatch):
    monkeypatch.setattr(_FakeSMTP, "instances", [])
    monkeypatch.setattr(es.smtplib, "SMTP_SSL", _FakeSMTP)
    _use_backend(
        monkeypatch, "smtp",
        SMTP_HOST="mail.bh.local", SMTP_PORT=465, SMTP_USE_SSL=True,
        SMTP_USERNAME="bot", SMTP_PASSWORD=_CRED,
    )
    es.deliver("Hi", ["a@b.local"], "body")
    assert _FakeSMTP.instances[0].sent
    assert _FakeSMTP.instances[0].logged_in


def test_smtp_missing_host_is_dropped(monkeypatch, caplog):
    _use_backend(monkeypatch, "smtp", SMTP_HOST="")
    with caplog.at_level(logging.WARNING, logger="bug_hunter.email"):
        es.deliver("Hi", ["a@b.local"], "body")
    assert any("SMTP_HOST is empty" in (r.message or "") for r in caplog.records)


def test_smtp_swallows_transport_error(monkeypatch, caplog):
    import smtplib

    def _boom(*a, **k):
        raise smtplib.SMTPException("nope")

    monkeypatch.setattr(es.smtplib, "SMTP", _boom)
    _use_backend(monkeypatch, "smtp", SMTP_HOST="mail.bh.local", SMTP_USE_SSL=False)
    with caplog.at_level(logging.ERROR, logger="bug_hunter.email"):
        es.deliver("Hi", ["a@b.local"], "body")  # must not raise
    assert any("Failed to send email" in (r.message or "") for r in caplog.records)
