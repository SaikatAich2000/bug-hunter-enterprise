"""Outbound webhooks: admin API, signing, event matching, SSRF guard and auto-suspension."""
from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from tests.conftest import new_client, sign_up

PASSWORD = "Passw0rd!x"
PUBLIC_URL = "https://hooks.example.com/bug-hunter"


class _Outbox(list):
    """Recorded deliveries; ``state`` steers what the fake endpoint answers."""

    def __init__(self):
        super().__init__()
        self.state = {"status": 200, "raise": None}


@pytest.fixture
def outbox(monkeypatch):
    """Swap the delivery module's HTTP client (not httpx itself, which the TestClient uses) and
    its DNS check; collect what would have been sent."""
    import types

    import app.webhooks_delivery as wd

    sent = _Outbox()

    class FakeClient:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, url, content=None, headers=None):
            if sent.state["raise"]:
                raise sent.state["raise"]
            sent.append({"url": url, "body": content, "headers": headers})
            return httpx.Response(sent.state["status"], request=httpx.Request("POST", url))

    monkeypatch.setattr(wd, "httpx", types.SimpleNamespace(
        Client=FakeClient, Timeout=httpx.Timeout, HTTPError=httpx.HTTPError))
    monkeypatch.setattr(wd, "check_destination", lambda url: None)
    return sent


def _create(c, **over):
    body = {"name": "Hook", "url": PUBLIC_URL, "events": "*", **over}
    return c.post("/api/webhooks", json=body)


# --- admin API -----------------------------------------------------------------------------------


def test_secret_is_shown_once_and_can_be_rotated(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    made = _create(c)
    assert made.status_code == 201, made.text
    secret = made.json()["secret"]
    assert len(secret) >= 24
    hook_id = made.json()["id"]

    assert c.get(f"/api/webhooks/{hook_id}").json()["secret"] is None
    assert all(h["secret"] is None for h in c.get("/api/webhooks").json())

    rotated = c.post(f"/api/webhooks/{hook_id}/rotate-secret").json()["secret"]
    assert rotated
    assert rotated != secret


def test_secret_is_stored_sealed_not_in_plain_text(client):
    from app.database import SessionLocal
    from app.models import Webhook

    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret = _create(c).json()["secret"]
    with SessionLocal() as db:
        stored = db.query(Webhook).one().secret
    # unreadable without the key, or at minimum recoverable only through the helper
    from app.secrets_box import unseal

    assert unseal(stored) == secret


def test_private_and_malformed_destinations_are_rejected(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    for url in (
        "http://localhost/hook", "http://127.0.0.1/hook", "http://10.0.0.5/hook", "http://192.168.1.1/hook",
        "http://169.254.169.254/latest/meta-data", "http://[::1]/hook", "http://2130706433/hook",
        "http://metadata.google.internal/x", "http://printer.local/x", "ftp://example.com/x", "example.com/x",
    ):
        assert _create(c, url=url).status_code == 422, url
    assert _create(c, events="bug created!").status_code == 422
    assert _create(c, name="").status_code == 422


def test_private_networks_can_be_allowed_for_on_premise_setups(client, monkeypatch):
    from app.config import get_settings

    c, _ = sign_up("Acme Corp", "owner@acme.test")
    monkeypatch.setattr(get_settings(), "WEBHOOK_ALLOW_PRIVATE_NETWORKS", True)
    assert _create(c, url="http://10.0.0.5/hook").status_code == 201


def test_only_admins_manage_webhooks(client):
    owner, _ = sign_up("Acme Corp", "owner@acme.test")
    owner.post("/api/users", json={"name": "Plain", "email": "plain@acme.test", "role": "manager",
                                   "password": PASSWORD})
    plain = new_client()
    plain.post("/api/auth/login", json={"email": "plain@acme.test", "password": PASSWORD})
    assert plain.get("/api/webhooks").status_code == 403
    assert plain.post("/api/webhooks", json={"name": "x", "url": PUBLIC_URL}).status_code == 403


def test_webhooks_never_cross_organizations(client):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")
    hook_id = _create(a).json()["id"]
    assert b.get("/api/webhooks").json() == []
    for method, path in (("GET", ""), ("PUT", ""), ("DELETE", ""), ("POST", "/rotate-secret"), ("POST", "/test")):
        res = b.request(method, f"/api/webhooks/{hook_id}{path}", json={"name": "x"} if method == "PUT" else None)
        assert res.status_code == 404, (method, path)


def test_update_and_delete(client):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    hook_id = _create(c).json()["id"]
    updated = c.put(f"/api/webhooks/{hook_id}", json={"name": "Renamed", "events": "bug.*", "is_active": False})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed"
    assert updated.json()["events"] == "bug.*"
    assert updated.json()["is_active"] is False
    assert c.delete(f"/api/webhooks/{hook_id}").status_code == 204
    assert c.get(f"/api/webhooks/{hook_id}").status_code == 404


# --- delivery ------------------------------------------------------------------------------------


def test_helpers_match_events_and_sign_bodies(client):
    from app.webhooks_delivery import matches_event, sign

    assert matches_event("*", "bug.created")
    assert matches_event("bug.*", "bug.created")
    assert matches_event("comment.added, bug.created", "bug.created")
    assert not matches_event("comment.*", "bug.created")
    assert not matches_event("bug.created", "bug.updated")
    assert sign("k", b"body") == "sha256=" + hmac.new(b"k", b"body", hashlib.sha256).hexdigest()


def test_creating_an_item_delivers_a_signed_event(client, outbox):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    secret = _create(c, events="bug.created").json()["secret"]
    pid = c.get("/api/projects").json()[0]["id"]
    assert c.post("/api/bugs", json={"project_id": pid, "title": "Boom", "description": "d"}).status_code == 201

    (call,) = outbox
    assert call["url"] == PUBLIC_URL
    assert call["headers"]["X-BugHunter-Event"] == "bug.created"
    assert call["headers"]["X-BugHunter-Signature"] == "sha256=" + hmac.new(
        secret.encode(), call["body"], hashlib.sha256).hexdigest()
    body = json.loads(call["body"])
    assert body["event"] == "bug.created"
    assert body["payload"]["bug"]["title"] == "Boom"
    assert body["delivery_id"] == call["headers"]["X-BugHunter-Delivery"]


def test_events_only_go_to_subscribed_active_hooks_of_the_same_organization(client, outbox):
    a, _ = sign_up("Acme Corp", "owner@acme.test")
    b, _ = sign_up("Other Org", "owner@other.test")
    _create(a, name="bugs", url="https://a.example.com/bugs", events="bug.*")
    _create(a, name="comments", url="https://a.example.com/comments", events="comment.*")
    _create(a, name="off", url="https://a.example.com/off", events="*", is_active=False)
    _create(b, name="theirs", url="https://b.example.com/all", events="*")

    pid = a.get("/api/projects").json()[0]["id"]
    a.post("/api/bugs", json={"project_id": pid, "title": "Only mine", "description": "d"})
    assert [c["url"] for c in outbox] == ["https://a.example.com/bugs"]


def test_a_test_ping_goes_out_even_to_an_inactive_hook(client, outbox):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    hook_id = _create(c, is_active=False, events="nothing.matches").json()["id"]
    assert c.post(f"/api/webhooks/{hook_id}/test").status_code == 202
    assert json.loads(outbox[0]["body"])["event"] == "webhook.ping"


def test_failures_are_recorded_and_ten_in_a_row_suspend_the_hook(client, outbox):
    from app.webhooks_delivery import MAX_CONSECUTIVE_FAILURES

    c, _ = sign_up("Acme Corp", "owner@acme.test")
    hook_id = _create(c).json()["id"]
    outbox.state["status"] = 500
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        c.post(f"/api/webhooks/{hook_id}/test")
    hook = c.get(f"/api/webhooks/{hook_id}").json()
    assert hook["consecutive_failures"] == MAX_CONSECUTIVE_FAILURES
    assert hook["last_error"] == "HTTP 500"
    assert hook["is_active"] is False

    # a suspended hook receives nothing until an admin re-enables it, which also clears the count
    before = len(outbox)
    pid = c.get("/api/projects").json()[0]["id"]
    c.post("/api/bugs", json={"project_id": pid, "title": "Quiet", "description": "d"})
    assert len(outbox) == before
    outbox.state["status"] = 200
    revived = c.put(f"/api/webhooks/{hook_id}", json={"is_active": True}).json()
    assert revived["consecutive_failures"] == 0
    c.post(f"/api/webhooks/{hook_id}/test")
    assert c.get(f"/api/webhooks/{hook_id}").json()["last_status_code"] == 200


def test_a_success_resets_the_failure_count_and_network_errors_are_failures(client, outbox):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    hook_id = _create(c).json()["id"]
    outbox.state["raise"] = httpx.ConnectError("refused")
    c.post(f"/api/webhooks/{hook_id}/test")
    hook = c.get(f"/api/webhooks/{hook_id}").json()
    assert hook["consecutive_failures"] == 1
    assert "ConnectError" in hook["last_error"]
    outbox.state["raise"] = None
    c.post(f"/api/webhooks/{hook_id}/test")
    hook = c.get(f"/api/webhooks/{hook_id}").json()
    assert hook["consecutive_failures"] == 0
    assert hook["last_error"] is None


def test_delivery_refuses_a_destination_that_resolves_to_a_private_address(client, monkeypatch):
    import socket

    import app.webhooks_delivery as wd

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 80))])
    with pytest.raises(wd.WebhookTargetError):
        wd.check_destination("https://rebind.example.com/hook")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    wd.check_destination("https://public.example.com/hook")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nx")))
    with pytest.raises(wd.WebhookTargetError):
        wd.check_destination("https://nx.example.com/hook")


def test_comment_update_and_delete_events_are_emitted(client, outbox):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    _create(c, events="*")
    pid = c.get("/api/projects").json()[0]["id"]
    bug_id = c.post("/api/bugs", json={"project_id": pid, "title": "First title", "description": "d"}).json()["id"]
    c.post(f"/api/bugs/{bug_id}/comments", json={"body": "hello"})
    c.put(f"/api/bugs/{bug_id}", json={"title": "Second title"})
    c.delete(f"/api/bugs/{bug_id}")
    events = [json.loads(call["body"])["event"] for call in outbox]
    assert events == ["bug.created", "comment.added", "bug.updated", "bug.deleted"]


def test_board_work_items_and_transitions_emit_events(client, outbox):
    c, _ = sign_up("Acme Corp", "owner@acme.test")
    pid = c.get("/api/projects").json()[0]["id"]
    assert c.post(f"/api/agile/projects/{pid}/enable", json={"feature_flags": {}}).status_code == 200
    _create(c, events="bug.*")

    story = c.post("/api/agile/work-items", json={"project_id": pid, "title": "Board story", "item_type": "Story"})
    assert story.status_code == 201, story.text
    created = json.loads(outbox[-1]["body"])
    assert created["event"] == "bug.created"
    assert created["payload"]["bug"]["title"] == "Board story"

    item = c.get(f"/api/bugs/{story.json()['id']}").json()
    target = "In Progress"
    before = len(outbox)
    moved = c.post(f"/api/agile/work-items/{item['id']}/transition",
                   json={"to_status": target, "version": item["version"]})
    assert moved.status_code == 200, moved.text
    assert len(outbox) == before + 1
    updated = json.loads(outbox[-1]["body"])
    assert updated["event"] == "bug.updated"
    assert updated["payload"]["changes"] == [{"field": "status", "old": item["status"], "new": target}]
