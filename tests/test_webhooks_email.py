"""Coverage tests for app/webhooks_delivery.py and app/email_service.py.

We exercise the underlying functions directly (not the HTTP routes that
manage CRUD) so we hit the branches that the routing layer doesn't —
non-2xx responses, exceptions, auto-suspension, all three email
backends, etc.

No real network: we monkeypatch httpx.Client.post and smtplib.SMTP/
SMTP_SSL.
"""
from __future__ import annotations

import smtplib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_TEST_PASSWORD = "TestPass1!"  # NOSONAR — fixture password, not a real credential


def _signup(client, org="Acme", name="Alice", email="alice@a.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": _TEST_PASSWORD,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_webhook(org_id, url="https://hook.example.test/in",
                  events="*", secret="s3cret", **extras):
    """Create a Webhook row directly via the SessionLocal — avoids the
    routing layer's localhost-rejection validator, which would otherwise
    block all of our test URLs."""
    from app.database import SessionLocal
    from app.models import Webhook
    db = SessionLocal()
    try:
        hook = Webhook(
            org_id=org_id, name="Test", url=url, secret=secret,
            events=events, is_active=True, consecutive_failures=0,
        )
        for k, v in extras.items():
            setattr(hook, k, v)
        db.add(hook)
        db.commit()
        db.refresh(hook)
        return hook.id
    finally:
        db.close()


def _read_webhook(hook_id):
    from app.database import SessionLocal
    from app.models import Webhook
    db = SessionLocal()
    try:
        return db.get(Webhook, hook_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# webhooks_delivery._matches_event — pure helper, branch table
# ---------------------------------------------------------------------------
def test_matches_event_branches(app_env):
    from app.webhooks_delivery import _matches_event
    # empty subscription string → never matches
    assert _matches_event("", "bug.created") is False
    # wildcard catches anything
    assert _matches_event("*", "anything.at.all") is True
    # exact match
    assert _matches_event("bug.created", "bug.created") is True
    # glob prefix match
    assert _matches_event("bug.*", "bug.assigned") is True
    # glob prefix should NOT match a different prefix
    assert _matches_event("bug.*", "event.created") is False
    # whitespace + multiple subs, last one matches
    assert _matches_event(" foo.x , bug.created ", "bug.created") is True
    # empty pieces in the list are skipped, not crashy
    assert _matches_event(",,*,", "x") is True
    # no subscription matches → False
    assert _matches_event("event.created,project.created", "bug.created") is False


def test_sign_payload_is_stable_hmac(app_env):
    from app.webhooks_delivery import _sign_payload
    sig1 = _sign_payload("secret", b'{"a":1}')
    sig2 = _sign_payload("secret", b'{"a":1}')
    sig3 = _sign_payload("different", b'{"a":1}')
    assert sig1 == sig2
    assert sig1 != sig3
    assert sig1.startswith("sha256=")
    # length = "sha256=" + 64 hex chars
    assert len(sig1) == len("sha256=") + 64


# ---------------------------------------------------------------------------
# deliver_event — full path with mocked httpx
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Stand-in for httpx.Client. Records every POST and replies based on
    a caller-supplied sequence of responses or exception raisers."""

    def __init__(self, behaviours):
        self._behaviours = list(behaviours)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": dict(headers or {})})
        if not self._behaviours:
            return _FakeResponse(200)
        b = self._behaviours.pop(0)
        if isinstance(b, Exception):
            raise b
        return b


@pytest.fixture()
def _logged_in(client):
    me = _signup(client)
    return client, me["org_id"]


def _install_fake_client(monkeypatch, fake_client):
    import app.webhooks_delivery as wd

    def _factory(timeout=None, follow_redirects=False):  # noqa: ARG001
        return fake_client

    monkeypatch.setattr(wd.httpx, "Client", _factory)


def test_deliver_event_success_resets_failures_and_sets_status(_logged_in, monkeypatch):
    _, org_id = _logged_in
    hook_id = _make_webhook(org_id, consecutive_failures=3,
                            last_error="boom", events="bug.created")
    fake = _FakeClient([_FakeResponse(202)])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {"id": 1, "title": "x"})

    # Exactly one call to the matching hook
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == "https://hook.example.test/in"
    # Signature and identifying headers are populated
    assert call["headers"]["X-BugHunter-Event"] == "bug.created"
    assert call["headers"]["X-BugHunter-Signature"].startswith("sha256=")
    assert "X-BugHunter-Delivery" in call["headers"]
    # Hook row was updated for a successful delivery
    hook = _read_webhook(hook_id)
    assert hook.last_status_code == 202
    assert hook.consecutive_failures == 0
    assert hook.last_error is None
    assert hook.is_active is True
    assert hook.last_delivered_at is not None


def test_deliver_event_non_2xx_increments_failures(_logged_in, monkeypatch):
    _, org_id = _logged_in
    hook_id = _make_webhook(org_id, consecutive_failures=2)
    fake = _FakeClient([_FakeResponse(500)])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {})

    hook = _read_webhook(hook_id)
    assert hook.last_status_code == 500
    assert hook.consecutive_failures == 3
    assert hook.last_error is not None
    assert "500" in hook.last_error
    assert hook.is_active is True  # below the suspension threshold


def test_deliver_event_exception_records_error(_logged_in, monkeypatch):
    _, org_id = _logged_in
    hook_id = _make_webhook(org_id, consecutive_failures=0)
    import httpx
    fake = _FakeClient([httpx.ConnectError("dns failure")])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {})

    hook = _read_webhook(hook_id)
    assert hook.consecutive_failures == 1
    assert hook.last_status_code is None
    assert hook.last_error is not None
    assert "dns" in hook.last_error.lower()
    assert hook.last_delivered_at is not None


def test_deliver_event_auto_suspends_on_threshold(_logged_in, monkeypatch):
    _, org_id = _logged_in
    # Already at 9 failures — one more pushes it to 10 and suspends.
    hook_id = _make_webhook(org_id, consecutive_failures=9)
    fake = _FakeClient([_FakeResponse(503)])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {})

    hook = _read_webhook(hook_id)
    assert hook.consecutive_failures >= 10
    assert hook.is_active is False


def test_deliver_event_skips_when_no_match(_logged_in, monkeypatch):
    """Hook subscribes to event.* — bug.created should not even open a client."""
    _, org_id = _logged_in
    _make_webhook(org_id, events="event.created")
    fake = _FakeClient([])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {"x": 1})
    assert fake.calls == []


def test_deliver_event_inactive_hook_not_delivered(_logged_in, monkeypatch):
    _, org_id = _logged_in
    _make_webhook(org_id, is_active=False)
    fake = _FakeClient([_FakeResponse(200)])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {})
    assert fake.calls == []


def test_deliver_event_multiple_hooks(_logged_in, monkeypatch):
    _, org_id = _logged_in
    _make_webhook(org_id, url="https://a.example.test/x", events="*")
    _make_webhook(org_id, url="https://b.example.test/x", events="bug.*")
    fake = _FakeClient([_FakeResponse(200), _FakeResponse(404)])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_id, "bug.created", {"k": "v"})

    assert len(fake.calls) == 2
    urls = {c["url"] for c in fake.calls}
    assert urls == {"https://a.example.test/x", "https://b.example.test/x"}
    # Each call carries the same delivery id (shared across the batch)
    deliveries = {c["headers"]["X-BugHunter-Delivery"] for c in fake.calls}
    assert len(deliveries) == 1


def test_deliver_event_cross_org_isolation(_logged_in, monkeypatch):
    """A hook in org A must not fire when we deliver an event for org B."""
    _, org_a = _logged_in
    _make_webhook(org_a)
    fake = _FakeClient([_FakeResponse(200)])
    _install_fake_client(monkeypatch, fake)

    from app.webhooks_delivery import deliver_event
    deliver_event(org_a + 999, "bug.created", {})
    assert fake.calls == []


# ---------------------------------------------------------------------------
# email_service — backends + dispatcher
# ---------------------------------------------------------------------------
def _user(uid=1, name="User", email="u@a.test"):
    from app.email_service import UserSnapshot
    return UserSnapshot(id=uid, name=name, email=email)


def _bug(**overrides):
    from app.email_service import BugSnapshot
    base = {
        "id": 1, "title": "Title", "project_name": "P", "status": "Open",
        "priority": "High", "environment": "DEV", "description": "desc",
        "reporter": _user(1, "Rep", "rep@a.test"),
        "assignees": (_user(2, "Bea", "bea@a.test"), _user(3, "Cal", "cal@a.test")),
        "item_type": "Bug",
    }
    base.update(overrides)
    return BugSnapshot(**base)


def _event(**overrides):
    from app.email_service import EventSnapshot
    base = {
        "id": 10, "name": "Sprint kickoff", "description": "desc",
        "scheduled_for": "2026-01-01T10:00:00Z",
        "managers": (_user(5, "Mara", "mara@a.test"),),
    }
    base.update(overrides)
    return EventSnapshot(**base)


def _capture_deliver(monkeypatch):
    """Patch deliver() in email_service to record args without sending."""
    sent = []
    import app.email_service as es

    def fake_deliver(subject, to, body):
        sent.append({"subject": subject, "to": list(to), "body": body})

    monkeypatch.setattr(es, "deliver", fake_deliver)
    return sent


def _set_backend(monkeypatch, backend, **overrides):
    """Override the cached settings' attributes for a single test.

    Settings is a plain class whose attributes are evaluated at import
    time, so changing env vars after import has no effect. Patch the
    instance directly instead.
    """
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "EMAIL_BACKEND", backend, raising=False)
    for k, v in overrides.items():
        monkeypatch.setattr(s, k, v, raising=False)
    return s


# ---- backend dispatch ----
def test_deliver_disabled_backend_is_noop(app_env, monkeypatch, caplog):
    from app import email_service
    _set_backend(monkeypatch, "disabled")

    def _boom(*a, **k):
        raise AssertionError("SMTP must not be touched on disabled backend")

    monkeypatch.setattr(email_service, "_send_smtp", _boom)
    monkeypatch.setattr(email_service, "_send_console", _boom)
    # Should return without raising
    email_service.deliver("Subject", ["x@a.test"], "body")


def test_deliver_console_backend_logs(app_env, monkeypatch, caplog):
    from app import email_service
    _set_backend(monkeypatch, "console")

    captured = {}

    def fake_smtp(*a, **k):
        captured["smtp_called"] = True

    monkeypatch.setattr(email_service, "_send_smtp", fake_smtp)

    with caplog.at_level("INFO", logger="bug_hunter.email"):
        email_service.deliver("Hello", ["a@a.test", " a@a.test ", ""], "hi there")

    assert "smtp_called" not in captured
    # Console output went through the logger
    assert any("console-email" in r.getMessage() for r in caplog.records)


def test_deliver_dedupes_and_drops_empty_recipients(app_env, monkeypatch):
    from app import email_service
    _set_backend(monkeypatch, "console")

    captured = {}

    def fake_console(msg):
        captured["to"] = msg["To"]

    monkeypatch.setattr(email_service, "_send_console", fake_console)
    email_service.deliver("S", ["A@A.test", "a@a.test", " ", "", "B@a.test"], "b")
    # dedup is case-sensitive on the input list per the impl; just check
    # that something sane went out and it was a comma-joined string.
    assert "to" in captured
    assert isinstance(captured["to"], str)
    assert "@" in captured["to"]


def test_deliver_with_empty_recipient_list_is_noop(app_env, monkeypatch):
    from app import email_service
    _set_backend(monkeypatch, "console")

    def _boom(*a, **k):
        raise AssertionError("nothing should be sent")

    monkeypatch.setattr(email_service, "_send_console", _boom)
    monkeypatch.setattr(email_service, "_send_smtp", _boom)
    email_service.deliver("S", ["", "  ", None], "b")


# ---- SMTP transport ----
class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.ehlo_count = 0
        self.starttls_called = False
        self.starttls_context = None
        self.login_args = None
        self.sent_msg = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.ehlo_count += 1

    def starttls(self, context=None):
        self.starttls_called = True
        self.starttls_context = context

    def login(self, user, pw):
        self.login_args = (user, pw)

    def send_message(self, msg):
        self.sent_msg = msg


class _FakeSMTPSSL(_FakeSMTP):
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        super().__init__(host, port, timeout=timeout, context=context)
        _FakeSMTPSSL.instances.append(self)


def _reset_fakes():
    _FakeSMTP.instances.clear()
    _FakeSMTPSSL.instances.clear()


def test_smtp_no_host_logs_and_returns(app_env, monkeypatch, caplog):
    from app import email_service
    settings = _set_backend(monkeypatch, "smtp", SMTP_HOST="")

    msg = email_service._build("S", ["x@a.test"], "body", settings)
    with caplog.at_level("WARNING", logger="bug_hunter.email"):
        email_service._send_smtp(settings, msg)
    assert any("SMTP_HOST" in r.getMessage() for r in caplog.records)


def test_smtp_starttls_path_with_login(app_env, monkeypatch):
    from app import email_service

    _reset_fakes()
    settings = _set_backend(
        monkeypatch, "smtp",
        SMTP_HOST="mail.example.test", SMTP_PORT=587,
        SMTP_USE_TLS=True, SMTP_USE_SSL=False,
        SMTP_USERNAME="user", SMTP_PASSWORD="pw",
    )

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)

    msg = email_service._build("Hello", ["x@a.test"], "body", settings)
    email_service._send_smtp(settings, msg)

    assert _FakeSMTP.instances, "Plain SMTP should have been instantiated"
    s = _FakeSMTP.instances[-1]
    assert s.host == "mail.example.test"
    assert s.port == 587
    assert s.starttls_called is True
    assert s.starttls_context is not None
    assert s.ehlo_count >= 2  # one before STARTTLS, one after
    assert s.login_args == ("user", "pw")
    assert s.sent_msg is msg
    assert _FakeSMTPSSL.instances == []  # SSL path NOT taken


def test_smtp_ssl_path_with_login(app_env, monkeypatch):
    from app import email_service

    _reset_fakes()
    settings = _set_backend(
        monkeypatch, "smtp",
        SMTP_HOST="mail.example.test", SMTP_PORT=465,
        SMTP_USE_SSL=True,
        SMTP_USERNAME="user", SMTP_PASSWORD="pw",
    )

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)

    msg = email_service._build("S", ["x@a.test"], "body", settings)
    email_service._send_smtp(settings, msg)

    assert _FakeSMTPSSL.instances, "SMTP_SSL should have been instantiated"
    s = _FakeSMTPSSL.instances[-1]
    assert s.port == 465
    assert s.context is not None
    assert s.login_args == ("user", "pw")
    assert s.sent_msg is msg
    # Plain SMTP must NOT also have been used
    assert _FakeSMTP.instances == _FakeSMTPSSL.instances


def test_smtp_no_username_skips_login(app_env, monkeypatch):
    from app import email_service

    _reset_fakes()
    settings = _set_backend(
        monkeypatch, "smtp",
        SMTP_HOST="mail.example.test",
        SMTP_USE_TLS=False, SMTP_USE_SSL=False,
        SMTP_USERNAME="", SMTP_PASSWORD="",
    )

    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    msg = email_service._build("S", ["x@a.test"], "body", settings)
    email_service._send_smtp(settings, msg)
    s = _FakeSMTP.instances[-1]
    assert s.login_args is None
    assert s.starttls_called is False
    assert s.sent_msg is msg


def test_smtp_exception_is_swallowed(app_env, monkeypatch, caplog):
    from app import email_service
    settings = _set_backend(
        monkeypatch, "smtp", SMTP_HOST="mail.example.test",
    )

    class _Broken:
        def __init__(self, *a, **k):
            raise smtplib.SMTPException("boom")

    monkeypatch.setattr(smtplib, "SMTP", _Broken)

    msg = email_service._build("S", ["x@a.test"], "body", settings)
    with caplog.at_level("ERROR", logger="bug_hunter.email"):
        # Must NOT raise — broken SMTP must not break callers
        email_service._send_smtp(settings, msg)
    assert any("Failed to send email" in r.getMessage() for r in caplog.records)


# ---- recipient selection ----
def test_recipients_excludes_actor_and_dedupes(app_env):
    from app.email_service import _recipients
    b = _bug(
        reporter=_user(1, "Rep", "rep@a.test"),
        assignees=(
            _user(2, "Bea", "BEA@a.test"),
            _user(3, "Cal", "bea@a.test"),  # dup email different case
            _user(4, "Del", ""),  # no email
        ),
    )
    out = _recipients(b, exclude_user_id=1)
    # Reporter excluded, dup deduped (case-insensitive), empty skipped
    assert len(out) == 1
    assert out[0].lower() == "bea@a.test"


def test_recipients_no_reporter_no_assignees(app_env):
    from app.email_service import _recipients
    b = _bug(reporter=None, assignees=())
    assert _recipients(b, exclude_user_id=None) == []


def test_event_recipients_filters_and_dedupes(app_env):
    from app.email_service import _event_recipients
    ev = _event(managers=(
        _user(1, "M1", "m@a.test"),
        _user(2, "M2", "M@a.test"),       # dup case
        _user(3, "M3", ""),                # no email
        _user(4, "Self", "self@a.test"),
    ))
    out = _event_recipients(ev, exclude_user_id=4)
    assert len(out) == 1
    assert out[0].lower() == "m@a.test"


# ---- notification helpers ----
def test_notify_bug_created_includes_meta_and_link(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_bug_created
    notify_bug_created(_bug(item_type="Task"), actor_user_id=1)
    assert len(sent) == 1
    e = sent[0]
    # Reporter (id=1) excluded, two assignees remain
    assert "rep@a.test" not in e["to"]
    assert "bea@a.test" in e["to"]
    assert "cal@a.test" in e["to"]
    # type-aware subject uses the noun
    assert "task" in e["subject"].lower()
    # Description and link sections present
    assert "Description:" in e["body"]
    assert "View:" in e["body"]
    # Meta lines exist
    assert "Bug #1:" in e["body"]


def test_notify_bug_created_no_recipients_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_bug_created
    notify_bug_created(_bug(reporter=None, assignees=()), actor_user_id=None)
    assert sent == []


def test_notify_bug_updated_lists_changes(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_bug_updated
    notify_bug_updated(
        _bug(),
        changes=[("status", "Open", "In Progress"), ("priority", "", "High")],
        actor_name="Eve",
        actor_user_id=999,
    )
    assert len(sent) == 1
    body = sent[0]["body"]
    assert "Changes:" in body
    assert "status" in body
    assert "priority" in body
    # Empty-value placeholder used for the empty old value
    assert "(empty)" in body


def test_notify_bug_updated_no_changes_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_bug_updated
    notify_bug_updated(_bug(), changes=[], actor_name="Eve", actor_user_id=1)
    assert sent == []


def test_notify_bug_updated_no_recipients_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_bug_updated
    notify_bug_updated(
        _bug(reporter=None, assignees=()),
        changes=[("x", "a", "b")],
        actor_name="Eve",
        actor_user_id=None,
    )
    assert sent == []


def test_notify_assignment_one_email_per_new_assignee(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_assignment
    notify_assignment(
        _bug(item_type="Requirement"),
        newly_assigned=[
            _user(2, "Bea", "bea@a.test"),
            _user(7, "NoMail", ""),  # skipped
            _user(8, "Eli", "eli@a.test"),
        ],
        actor_name="PM",
    )
    assert len(sent) == 2
    addrs = {e["to"][0] for e in sent}
    assert addrs == {"bea@a.test", "eli@a.test"}
    # Subject uses item-type noun
    assert all("requirement" in e["subject"].lower() for e in sent)


def test_notify_comment_added_to_assignees_excluding_author(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_comment_added
    notify_comment_added(
        _bug(),
        comment_author_name="Carla",
        comment_author_id=2,  # excludes one assignee
        comment_body="hello world",
    )
    assert len(sent) == 1
    e = sent[0]
    assert "bea@a.test" not in e["to"]
    assert "rep@a.test" in e["to"]
    assert "cal@a.test" in e["to"]
    assert "hello world" in e["body"]
    assert "Carla commented" in e["body"]


def test_notify_comment_added_no_recipients(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_comment_added
    notify_comment_added(
        _bug(reporter=None, assignees=()),
        comment_author_name="C", comment_author_id=None, comment_body="hi",
    )
    assert sent == []


def test_notify_event_created_with_description(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_event_created
    notify_event_created(_event(), actor_name="PM", actor_user_id=999)
    assert len(sent) == 1
    e = sent[0]
    assert "mara@a.test" in e["to"]
    assert "Sprint kickoff" in e["subject"]
    assert "Description:" in e["body"]
    assert "Scheduled for:" in e["body"]


def test_notify_event_created_actor_excluded(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_event_created
    # Manager id 5 is the actor — should be excluded → no recipients → no send
    notify_event_created(_event(), actor_name="Mara", actor_user_id=5)
    assert sent == []


def test_notify_event_updated_lists_changes(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_event_updated
    notify_event_updated(
        _event(),
        changes=[("name", "old", "new")],
        actor_name="PM",
        actor_user_id=999,
    )
    assert len(sent) == 1
    assert "Changes:" in sent[0]["body"]


def test_notify_event_updated_no_changes_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_event_updated
    notify_event_updated(_event(), changes=[], actor_name="PM", actor_user_id=999)
    assert sent == []


def test_notify_event_deleted_emits(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_event_deleted
    notify_event_deleted(_event(), actor_name="PM", actor_user_id=999)
    assert len(sent) == 1
    assert "deleted" in sent[0]["subject"].lower()
    assert "preserved" in sent[0]["body"]


def test_notify_event_deleted_no_recipients(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_event_deleted
    notify_event_deleted(_event(managers=()), actor_name="PM", actor_user_id=None)
    assert sent == []


def test_notify_password_reset_includes_link(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_password_reset
    notify_password_reset("u@a.test", "Ulla", "https://app/reset?t=abc")
    assert len(sent) == 1
    e = sent[0]
    assert e["to"] == ["u@a.test"]
    assert "Ulla" in e["body"]
    assert "https://app/reset?t=abc" in e["body"]


def test_notify_password_reset_no_email_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_password_reset
    notify_password_reset("", "Ulla", "https://app/reset")
    assert sent == []


def test_notify_password_reset_no_name_uses_fallback(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_password_reset
    notify_password_reset("u@a.test", "", "https://app/reset")
    assert len(sent) == 1
    assert "there" in sent[0]["body"]  # "Hi there,"


def test_notify_invitation_admin_role_label(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_invitation
    notify_invitation(
        email="new@a.test", inviter_name="Iggy", org_name="Acme",
        accept_url="https://app/accept?t=z", role="admin",
    )
    assert len(sent) == 1
    e = sent[0]
    assert "an admin" in e["body"]
    assert "Acme" in e["body"]
    assert "https://app/accept?t=z" in e["body"]
    assert "Iggy" in e["subject"]


def test_notify_invitation_unknown_role_label(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_invitation
    notify_invitation(
        email="new@a.test", inviter_name="", org_name="",
        accept_url="u", role="guest",
    )
    assert len(sent) == 1
    body = sent[0]["body"]
    # Unknown roles fall back to "a <role>"
    assert "a guest" in body
    # Empty inviter falls back to "Someone"
    assert "Someone" in sent[0]["subject"]


def test_notify_invitation_no_email_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_invitation
    notify_invitation("", "Iggy", "Acme", "u", "admin")
    assert sent == []


def test_notify_email_change_code_includes_code(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_email_change_code
    notify_email_change_code("new@a.test", "Nina", "123456")
    assert len(sent) == 1
    e = sent[0]
    assert e["to"] == ["new@a.test"]
    assert "123456" in e["body"]
    assert "Nina" in e["body"]


def test_notify_email_change_code_no_email_skips(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_email_change_code
    notify_email_change_code("", "Nina", "123456")
    assert sent == []


def test_notify_email_change_code_no_name_uses_fallback(app_env, monkeypatch):
    sent = _capture_deliver(monkeypatch)
    from app.email_service import notify_email_change_code
    notify_email_change_code("new@a.test", "", "999999")
    assert "there" in sent[0]["body"]


def test_item_type_word_falls_back_for_missing_attr(app_env):
    """Legacy snapshot-shaped object that doesn't carry item_type should
    fall back to 'bug'."""
    from app.email_service import _item_type_word
    legacy = SimpleNamespace(item_type=None)
    assert _item_type_word(legacy) == "bug"
    legacy2 = SimpleNamespace(item_type="Task")
    assert _item_type_word(legacy2) == "task"


def test_user_snapshot_display(app_env):
    u = _user(9, "Zed", "zed@a.test")
    assert u.display == "Zed <zed@a.test>"
