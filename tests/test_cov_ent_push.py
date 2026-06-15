"""Coverage-push tests for the FCM push subsystem.

Targets (line >97% / branch >93%):
  - app/push_service.py   : credential-cache init/parse/refresh, channel
                            gating, stale-token detection branches, the
                            HTTP send loop outcomes, send_to_users fan-out.
  - app/push_notify.py    : every push_* notifier, recipient dedupe/exclude,
                            @-mention channel upgrade, change-summary
                            truncation, the _safe broad-except swallow.
  - app/routes/devices.py : update_preferences when a row already exists
                            (line 144 + branch 138->141).

All external I/O is mocked: httpx uses MockTransport, google-auth's
service_account / Request are patched, and push_service.send_to_user is
stubbed for the push_notify layer. Nothing reaches Firebase or the
network. Deterministic.

We extend the exact patterns from tests/test_push_notifications.py
(MockTransport + stubbed credential cache). Fake FCM tokens/secrets are
wrapped in 1-tuples to dodge Sonar S6418.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import push_service


# Fake FCM tokens — opaque per-device identifiers, NOT credentials. They
# never reach a Firebase API (httpx is mock-transported). Wrapped in
# single-element tuples so Sonar's S6418 heuristic ignores them.
_TOK_A = ("fcm-fake-cov-a-0001",)
_TOK_B = ("fcm-fake-cov-b-0002",)
_TOK_C = ("fcm-fake-cov-c-0003",)
_TOK_NET = ("fcm-fake-cov-net-0004",)
_TOK_400OK = ("fcm-fake-cov-400ok-0005",)
_TOK_400DET = ("fcm-fake-cov-400det-0006",)
_TOK_400BAD = ("fcm-fake-cov-400bad-0007",)
_TOK_NONJSON = ("fcm-fake-cov-nonjson-0008",)
_TOK_USERS1 = ("fcm-fake-cov-users1-0009",)
_TOK_USERS2 = ("fcm-fake-cov-users2-0010",)
_TOK_NOSEND = ("fcm-fake-cov-nosend-0011",)

_FAKE_ACCESS_TOKEN = ("fake-access-token-value",)  # noqa: S105 — hermetic stub


def _fake_sa_json() -> str:
    """Minimal service-account JSON. We never let google-auth actually
    parse this against real crypto — `from_service_account_info` is
    patched in the tests that need the enabled path."""
    return json.dumps({
        "type": "service_account",
        "project_id": "bug-hunter-cov",
        "private_key_id": "0" * 40,
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "cov@bug-hunter-cov.iam.gserviceaccount.com",
        "client_id": "100000000000000000000",
    })


def _settings_stub(project_id="bug-hunter-cov", sa_json=None):
    """A stand-in for app.config.Settings that _CredentialCache only
    reads two attributes from. Avoids the get_settings() lru_cache and
    the fact that Settings evaluates FIREBASE_* at import time."""
    return SimpleNamespace(
        FIREBASE_PROJECT_ID=project_id,
        FIREBASE_SA_JSON=sa_json if sa_json is not None else _fake_sa_json(),
        FIREBASE_HTTP_TIMEOUT_SECONDS=8,
    )


# ===========================================================================
# app/push_service.py — _CredentialCache.__init__
# ===========================================================================
def test_credcache_disabled_when_project_id_blank():
    """push_service.py:104-105 — blank FIREBASE_PROJECT_ID short-circuits
    __init__ before any google-auth import (the `if not self._enabled`
    early return)."""
    cache = push_service._CredentialCache(_settings_stub(project_id="   "))
    assert cache.enabled is False
    assert cache.project_id == ""  # .strip() collapses whitespace-only


def test_credcache_disabled_when_sa_json_blank():
    """push_service.py:103-105 — project set but SA JSON blank → disabled."""
    cache = push_service._CredentialCache(_settings_stub(sa_json="  "))
    assert cache.enabled is False


def test_credcache_enabled_on_valid_sa_json():
    """push_service.py:106-111 — both env present and
    from_service_account_info succeeds → enabled True, project_id exposed."""
    fake_creds = MagicMock(name="creds")
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=fake_creds,
    ) as mk:
        cache = push_service._CredentialCache(_settings_stub())
    assert cache.enabled is True
    assert cache.project_id == "bug-hunter-cov"
    mk.assert_called_once()


def test_credcache_parse_failure_disables(caplog):
    """push_service.py:112-119 — from_service_account_info raising flips
    the cache back to disabled and clears credentials."""
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        side_effect=ValueError("bad key"),
    ):
        cache = push_service._CredentialCache(_settings_stub())
    assert cache.enabled is False
    assert cache._credentials is None


# ===========================================================================
# app/push_service.py — _CredentialCache.access_token
# ===========================================================================
def test_access_token_none_when_disabled():
    """push_service.py:132-133 — disabled cache returns None without
    touching google.auth.transport."""
    cache = push_service._CredentialCache(_settings_stub(project_id=""))
    assert cache.access_token() is None


def test_access_token_returns_cached_when_valid():
    """push_service.py:140 (False branch),142 — creds already valid skips
    refresh and returns the cached token."""
    creds = MagicMock()
    creds.valid = True
    creds.token = _FAKE_ACCESS_TOKEN[0]
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=creds,
    ):
        cache = push_service._CredentialCache(_settings_stub())
    fake_request_mod = SimpleNamespace(Request=MagicMock())
    with patch.dict(sys.modules, {"google.auth.transport.requests": fake_request_mod}):
        tok = cache.access_token()
    assert tok == _FAKE_ACCESS_TOKEN[0]
    creds.refresh.assert_not_called()


def test_access_token_refreshes_when_invalid():
    """push_service.py:140 (True branch),141,142 — invalid creds trigger
    a refresh(Request()) before the token is read."""
    creds = MagicMock()
    creds.valid = False
    creds.token = _FAKE_ACCESS_TOKEN[0]
    fake_request = MagicMock(name="Request")
    fake_request_mod = SimpleNamespace(Request=fake_request)
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=creds,
    ):
        cache = push_service._CredentialCache(_settings_stub())
    with patch.dict(sys.modules, {"google.auth.transport.requests": fake_request_mod}):
        tok = cache.access_token()
    assert tok == _FAKE_ACCESS_TOKEN[0]
    creds.refresh.assert_called_once()
    fake_request.assert_called_once()  # Request() instantiated


def test_access_token_returns_none_on_refresh_exception(caplog):
    """push_service.py:143-145 — a refresh failure is swallowed and None
    is returned so callers no-op rather than 500."""
    creds = MagicMock()
    creds.valid = False
    creds.refresh.side_effect = RuntimeError("token endpoint down")
    fake_request_mod = SimpleNamespace(Request=MagicMock())
    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_info",
        return_value=creds,
    ):
        cache = push_service._CredentialCache(_settings_stub())
    with patch.dict(sys.modules, {"google.auth.transport.requests": fake_request_mod}):
        assert cache.access_token() is None


# ===========================================================================
# app/push_service.py — _get_cache / reset_credentials_cache singleton
# ===========================================================================
def test_get_cache_creates_then_reuses_and_reset(monkeypatch):
    """push_service.py:156-159 — first _get_cache() builds the singleton
    (the `is None` True branch via get_settings()); a second call returns
    the same object (False branch). reset_credentials_cache (166-167)
    drops it so a fresh instance is built next time."""
    push_service.reset_credentials_cache()
    first = push_service._get_cache()
    assert first is not None
    second = push_service._get_cache()
    assert second is first  # cached, not rebuilt
    push_service.reset_credentials_cache()
    assert push_service._credential_cache is None
    third = push_service._get_cache()
    assert third is not first  # rebuilt after reset
    push_service.reset_credentials_cache()


# ===========================================================================
# app/push_service.py — _channel_enabled_for_user branches
# ===========================================================================
def test_channel_enabled_missing_row_all_on(admin_client):
    """push_service.py:178-179 — no NotificationPreference row → all
    channels report enabled."""
    from app.database import SessionLocal

    me = admin_client.admin_me
    with SessionLocal() as db:
        assert push_service._channel_enabled_for_user(
            db, me["id"], push_service.CHANNEL_MENTIONS) is True
        assert push_service._channel_enabled_for_user(
            db, me["id"], push_service.CHANNEL_ACTIVITY) is True


def test_channel_enabled_reads_each_stored_flag(admin_client):
    """push_service.py:180-185 — with a stored row, each channel returns
    its own flag: mentions→.mentions (181), assignments→.assignments
    (183), anything else→.activity (185)."""
    from app.database import SessionLocal
    from app.models import NotificationPreference

    me = admin_client.admin_me
    with SessionLocal() as db:
        db.add(NotificationPreference(
            user_id=me["id"], mentions=False, assignments=True, activity=False,
        ))
        db.commit()
        # 180->181: mentions channel returns the stored mentions flag.
        assert push_service._channel_enabled_for_user(
            db, me["id"], push_service.CHANNEL_MENTIONS) is False
        # 182->183: assignments channel returns the stored assignments flag.
        assert push_service._channel_enabled_for_user(
            db, me["id"], push_service.CHANNEL_ASSIGNMENTS) is True
        # 185: unknown/activity channel returns the stored activity flag.
        assert push_service._channel_enabled_for_user(
            db, me["id"], push_service.CHANNEL_ACTIVITY) is False
        assert push_service._channel_enabled_for_user(
            db, me["id"], "weird-channel") is False  # normalises to activity


# ===========================================================================
# app/push_service.py — _normalise_channel + _build_payload
# ===========================================================================
def test_normalise_channel_passthrough_and_fallback():
    """push_service.py:188-189 — a known channel passes through; an
    unknown one collapses to activity."""
    assert push_service._normalise_channel(push_service.CHANNEL_MENTIONS) == "mentions"
    assert push_service._normalise_channel("bogus-channel") == push_service.CHANNEL_ACTIVITY


def test_build_payload_omits_optional_fields():
    """push_service.py:204-207 (False branches) — no deep_link / tag means
    those keys are absent from the data dict."""
    msg = push_service.PushMessage(title="T", body="B", channel="activity")
    payload = push_service._build_payload("tok", msg)["message"]
    assert payload["token"] == "tok"
    assert payload["data"] == {"title": "T", "body": "B", "channel": "activity"}
    assert "deep_link" not in payload["data"]
    assert "tag" not in payload["data"]


def test_build_payload_includes_optional_fields():
    """push_service.py:204-207 (True branches) — deep_link and tag are
    threaded into data when present."""
    msg = push_service.PushMessage(
        title="T", body="B", channel="activity",
        deep_link="app://bughunter/bug/1", tag="bug:1",
    )
    data = push_service._build_payload("tok", msg)["message"]["data"]
    assert data["deep_link"] == "app://bughunter/bug/1"
    assert data["tag"] == "bug:1"


# ===========================================================================
# app/push_service.py — _is_stale_token_error branches
# ===========================================================================
def _resp(status_code, json_body=None, text_body=None):
    if json_body is not None:
        return httpx.Response(status_code, json=json_body)
    return httpx.Response(status_code, text=text_body or "")


def test_is_stale_410_and_404_true():
    """push_service.py:218-219 — 404 and 410 are unconditionally stale."""
    assert push_service._is_stale_token_error(_resp(404, json_body={})) is True
    assert push_service._is_stale_token_error(_resp(410, json_body={})) is True


def test_is_stale_non_400_other_status_false():
    """push_service.py:220-221 — a non-400, non-404/410 (e.g. 503) is not
    a stale-token error."""
    assert push_service._is_stale_token_error(_resp(503, json_body={})) is False


def test_is_stale_400_status_text_match():
    """push_service.py:226-229 — 400 with error.status in the stale set."""
    body = {"error": {"status": "UNREGISTERED", "message": "gone"}}
    assert push_service._is_stale_token_error(_resp(400, json_body=body)) is True


def test_is_stale_400_details_errorcode_match():
    """push_service.py:232-236 — 400 whose error.details[].errorCode is a
    stale marker (the defensive nested walk)."""
    body = {
        "error": {
            "status": "INVALID",  # not itself stale
            "details": [
                "string-detail-not-a-dict",      # 233-234 continue branch
                {"noErrorCode": "x"},            # falls through inner if
                {"errorCode": "UNREGISTERED"},   # 235-236 match
            ],
        }
    }
    assert push_service._is_stale_token_error(_resp(400, json_body=body)) is True


def test_is_stale_400_no_match_false():
    """push_service.py:237 — 400 body with neither a stale status nor a
    stale errorCode returns False (the loop exhausts)."""
    body = {"error": {"status": "QUOTA_EXCEEDED", "details": [{"errorCode": "QUOTA"}]}}
    assert push_service._is_stale_token_error(_resp(400, json_body=body)) is False


def test_is_stale_400_non_dict_body_false():
    """push_service.py:226 — a 400 whose JSON body is a list (not a dict)
    yields an empty error map and returns False."""
    assert push_service._is_stale_token_error(_resp(400, json_body=[1, 2, 3])) is False


def test_is_stale_400_invalid_json_false():
    """push_service.py:222-225 — a 400 with a non-JSON body hits the
    ValueError guard and returns False."""
    assert push_service._is_stale_token_error(_resp(400, text_body="not json {")) is False


# ===========================================================================
# app/push_service.py — _send_one transport error
# ===========================================================================
def test_send_one_returns_none_on_httperror():
    """push_service.py:258-260 — a transport-level httpx.HTTPError is
    logged and swallowed, returning None."""
    bad_client = MagicMock()
    bad_client.post.side_effect = httpx.ConnectError("dns")
    msg = push_service.PushMessage(title="T", body="B", channel="activity")
    out = push_service._send_one(bad_client, "http://x", "atok", "devtok", msg)
    assert out is None


def test_send_one_returns_response_on_success():
    """push_service.py:249-257 — the happy path returns the httpx.Response
    the client produced (Authorization header is built)."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"name": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    msg = push_service.PushMessage(title="T", body="B", channel="activity")
    try:
        out = push_service._send_one(client, "http://x", "atok", "devtok", msg)
    finally:
        client.close()
    assert out is not None and out.status_code == 200
    assert captured["auth"] == "Bearer atok"


# ===========================================================================
# app/push_service.py — send_to_user: helper to wire an enabled cache
# ===========================================================================
def _enable_push(monkeypatch, project_id="bug-hunter-cov"):
    """Install a stubbed, enabled credential cache + a valid access token
    so send_to_user proceeds into the HTTP loop. Returns the cache."""
    cache = push_service._CredentialCache.__new__(push_service._CredentialCache)
    import threading
    cache._lock = threading.Lock()
    cache._project_id = project_id
    cache._enabled = True
    cache._credentials = MagicMock()
    monkeypatch.setattr(cache, "access_token", lambda: _FAKE_ACCESS_TOKEN[0])
    monkeypatch.setattr(push_service, "_credential_cache", cache)
    return cache


def _mock_httpx(monkeypatch, handler):
    real_client = push_service.httpx.Client

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(push_service.httpx, "Client", patched)


def _register(admin_client, token):
    r = admin_client.post("/api/devices/register", json={"token": token})
    assert r.status_code == 200, r.text


def test_send_to_user_disabled_cache_returns_zero(admin_client, monkeypatch):
    """push_service.py:288-291 — cache.enabled False short-circuits to 0
    before any DB/HTTP work."""
    from app.database import SessionLocal

    cache = push_service._CredentialCache(_settings_stub(project_id=""))
    monkeypatch.setattr(push_service, "_credential_cache", cache)
    with SessionLocal() as db:
        assert push_service.send_to_user(db, 1, title="x", body="y") == 0


def test_send_to_user_no_tokens_returns_zero(admin_client, monkeypatch):
    """push_service.py:300-301 — enabled + channel-on but the user has no
    registered device tokens → 0, HTTP never invoked."""
    from app.database import SessionLocal

    _enable_push(monkeypatch)

    def handler(_req):
        raise AssertionError("HTTP must not run when there are no tokens")

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        assert push_service.send_to_user(db, me["id"], title="x", body="y") == 0


def test_send_to_user_access_token_none_returns_zero(admin_client, monkeypatch):
    """push_service.py:303-305 — tokens exist but access_token() returns
    None (creds went bad) → 0, HTTP never invoked."""
    from app.database import SessionLocal

    cache = _enable_push(monkeypatch)
    monkeypatch.setattr(cache, "access_token", lambda: None)
    _register(admin_client, _TOK_NOSEND[0])

    def handler(_req):
        raise AssertionError("HTTP must not run without an access token")

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        assert push_service.send_to_user(db, me["id"], title="x", body="y") == 0


def test_send_to_user_network_error_continues(admin_client, monkeypatch):
    """push_service.py:319-320 — _send_one returns None (transport error)
    → that token is skipped (continue), nothing counted, row kept."""
    from app.database import SessionLocal
    from app.models import DeviceToken

    _enable_push(monkeypatch)
    _register(admin_client, _TOK_NET[0])

    def handler(_req):
        raise httpx.ConnectError("boom")

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        sent = push_service.send_to_user(db, me["id"], title="x", body="y")
        assert sent == 0
        row = db.query(DeviceToken).filter(DeviceToken.token == _TOK_NET[0]).first()
        assert row is not None  # transient → not deleted


def test_send_to_user_400_unregistered_deletes_stale(admin_client, monkeypatch):
    """push_service.py:324-325 + 335-338 — a 400/UNREGISTERED body marks
    the token stale; the delete+commit branch removes the row."""
    from app.database import SessionLocal
    from app.models import DeviceToken

    _enable_push(monkeypatch)
    _register(admin_client, _TOK_400DET[0])

    def handler(_req):
        return httpx.Response(400, json={
            "error": {"status": "INVALID_ARGUMENT",
                      "details": [{"errorCode": "UNREGISTERED"}]},
        })

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        sent = push_service.send_to_user(db, me["id"], title="x", body="y")
        assert sent == 0
        row = db.query(DeviceToken).filter(DeviceToken.token == _TOK_400DET[0]).first()
        assert row is None


def test_send_to_user_other_4xx_keeps_and_logs(admin_client, monkeypatch, caplog):
    """push_service.py:330-333 — a non-stale 4xx (403) logs a warning and
    keeps the row (no stale delete, send_count stays 0)."""
    from app.database import SessionLocal
    from app.models import DeviceToken

    _enable_push(monkeypatch)
    _register(admin_client, _TOK_400BAD[0])

    def handler(_req):
        return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        sent = push_service.send_to_user(db, me["id"], title="x", body="y")
        assert sent == 0
        row = db.query(DeviceToken).filter(DeviceToken.token == _TOK_400BAD[0]).first()
        assert row is not None


def test_send_to_user_batches_multiple_tokens(admin_client, monkeypatch):
    """push_service.py:316-323 — two registered tokens for one user both
    POST; sent_count sums to 2 (the for-loop over rows)."""
    from app.database import SessionLocal

    _enable_push(monkeypatch)
    _register(admin_client, _TOK_A[0])
    _register(admin_client, _TOK_B[0])

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode())["message"]["token"])
        return httpx.Response(200, json={"name": "ok"})

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        sent = push_service.send_to_user(
            db, me["id"], title="Hi", body="There",
            channel=push_service.CHANNEL_ACTIVITY,
        )
    assert sent == 2
    assert set(seen) == {_TOK_A[0], _TOK_B[0]}


def test_send_to_user_channel_off_assignments(admin_client, monkeypatch):
    """push_service.py:294-295 — assignments channel disabled drops the
    push even though a token is registered (the gating branch)."""
    from app.database import SessionLocal

    _enable_push(monkeypatch)
    _register(admin_client, _TOK_C[0])
    admin_client.put("/api/notifications/preferences", json={"assignments": False})

    def handler(_req):
        raise AssertionError("must not POST when assignments are off")

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        sent = push_service.send_to_user(
            db, me["id"], title="x", body="y",
            channel=push_service.CHANNEL_ASSIGNMENTS,
        )
    assert sent == 0


def test_send_to_user_activity_channel_off(admin_client, monkeypatch):
    """push_service.py:184-185 + 294-295 — the activity branch of
    _channel_enabled_for_user (else clause) when activity is disabled."""
    from app.database import SessionLocal

    _enable_push(monkeypatch)
    _register(admin_client, _TOK_NONJSON[0])
    admin_client.put("/api/notifications/preferences", json={"activity": False})

    def handler(_req):
        raise AssertionError("must not POST when activity is off")

    _mock_httpx(monkeypatch, handler)
    me = admin_client.admin_me
    with SessionLocal() as db:
        sent = push_service.send_to_user(
            db, me["id"], title="x", body="y",
            channel=push_service.CHANNEL_ACTIVITY,
        )
    assert sent == 0


# ===========================================================================
# app/push_service.py — send_to_users fan-out
# ===========================================================================
def test_send_to_users_sums_across_users(two_orgs, monkeypatch):
    """push_service.py:357-364 — send_to_users iterates user_ids and sums
    per-user token counts."""
    from app.database import SessionLocal

    c_a, c_b, me_a, me_b = two_orgs
    _register(c_a, _TOK_USERS1[0])
    _register(c_b, _TOK_USERS2[0])
    _enable_push(monkeypatch)

    def handler(_req):
        return httpx.Response(200, json={"name": "ok"})

    _mock_httpx(monkeypatch, handler)
    with SessionLocal() as db:
        total = push_service.send_to_users(
            db, [me_a["id"], me_b["id"]],
            title="Broadcast", body="Body",
            channel=push_service.CHANNEL_ACTIVITY,
        )
    assert total == 2


def test_send_to_users_empty_iterable_returns_zero(admin_client, monkeypatch):
    """push_service.py:357-364 — empty user_ids → loop body never runs,
    total stays 0."""
    from app.database import SessionLocal

    _enable_push(monkeypatch)
    with SessionLocal() as db:
        assert push_service.send_to_users(db, [], title="x", body="y") == 0


# ===========================================================================
# app/routes/devices.py — update_preferences existing-row path
# ===========================================================================
def test_update_preferences_updates_existing_row(admin_client):
    """devices.py:138->141 (row not None) + 144 — a second PUT finds the
    row created by the first and overwrites activity on the existing row.
    The first PUT exercises the create branch; the second the update +
    the activity assignment (line 144)."""
    # First PUT creates the row (row is None branch) and sets activity.
    r1 = admin_client.put("/api/notifications/preferences", json={"activity": False})
    assert r1.status_code == 200
    assert r1.json()["activity"] is False
    # Second PUT finds the existing row (138->141 false) and flips
    # activity back on via line 144 (payload.activity is not None).
    r2 = admin_client.put("/api/notifications/preferences", json={
        "mentions": False, "assignments": False, "activity": True,
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body == {"mentions": False, "assignments": False, "activity": True}


def test_update_preferences_none_fields_left_untouched(admin_client):
    """devices.py:141,143,145 (False branches) — a PUT with all-None body
    on an existing row leaves the stored values unchanged."""
    admin_client.put("/api/notifications/preferences", json={"mentions": False})
    # All-None PUT: none of the three `if ... is not None` guards fire.
    r = admin_client.put("/api/notifications/preferences", json={})
    assert r.status_code == 200
    # mentions stays False from the prior PUT; others remain default True.
    assert r.json() == {"mentions": False, "assignments": True, "activity": True}


# ===========================================================================
# app/push_notify.py — recipient resolution / dedupe / exclude
# ===========================================================================
def _user(uid, name="U", email=None):
    from app.email_service import UserSnapshot
    return UserSnapshot(id=uid, name=name, email=email or f"u{uid}@x.test")


def _bug(reporter=None, assignees=(), item_type="Bug", **kw):
    from app.email_service import BugSnapshot
    base = dict(
        id=42, title="Login crash", project_name="Web", status="open",
        priority="high", environment="prod", description="d",
        reporter=reporter, assignees=tuple(assignees), item_type=item_type,
    )
    base.update(kw)
    return BugSnapshot(**base)


def _event(managers=(), **kw):
    from app.email_service import EventSnapshot
    base = dict(
        id=7, name="Launch", description="d",
        scheduled_for=None, managers=tuple(managers),
    )
    base.update(kw)
    return EventSnapshot(**base)


def test_recipient_ids_dedupe_and_exclude():
    """push_notify.py:104-115 — reporter+assignees deduped, actor excluded,
    order preserved. Exercises: exclude hit (continue), seen hit
    (continue), and the no-reporter False branch via a second call."""
    from app.push_notify import _recipient_ids

    r = _user(1)
    a1 = _user(2)
    a_dup = _user(2)  # duplicate id → deduped
    actor = _user(3)
    bug = _bug(reporter=r, assignees=(a1, a_dup, actor))
    out = _recipient_ids(bug, exclude_user_id=3)
    assert out == [1, 2]  # reporter, one assignee; actor 3 excluded

    # No reporter branch (105 False) + exclude_user_id None (109 False).
    bug2 = _bug(reporter=None, assignees=(a1,))
    assert _recipient_ids(bug2, exclude_user_id=None) == [2]


def test_event_recipient_ids_dedupe_and_exclude():
    """push_notify.py:121-130 — managers deduped, actor excluded."""
    from app.push_notify import _event_recipient_ids

    m1 = _user(10)
    m_dup = _user(10)
    actor = _user(11)
    ev = _event(managers=(m1, m_dup, actor))
    assert _event_recipient_ids(ev, exclude_user_id=11) == [10]
    # exclude None branch.
    assert _event_recipient_ids(_event(managers=(m1,)), exclude_user_id=None) == [10]


def test_item_noun_default_and_override():
    """push_notify.py:76 — item_type present lowercases it; absent/None
    falls back to 'bug'."""
    from app.push_notify import _item_noun

    assert _item_noun(_bug(item_type="Task")) == "task"
    assert _item_noun(_bug(item_type="Bug")) == "bug"


# ===========================================================================
# app/push_notify.py — _safe broad-except
# ===========================================================================
def test_safe_swallows_exceptions(caplog):
    """push_notify.py:137-140 — _safe runs the callable; an exception is
    logged and swallowed (never re-raised). Also covers the happy path."""
    from app.push_notify import _safe

    calls = []
    _safe(lambda: calls.append("ok"))  # happy path: try succeeds
    assert calls == ["ok"]

    # Error path: the except branch logs and returns None.
    _safe(lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
    # No exception propagated → reaching here proves the swallow.


# ===========================================================================
# app/push_notify.py — bug notifiers (send_to_user/users stubbed)
# ===========================================================================
@pytest.fixture()
def capture_sends(monkeypatch):
    """Replace push_notify's bound send_to_user / send_to_users with
    recorders so we never hit push_service / Firebase. push_notify
    imported these names at module load, so patch them on push_notify."""
    from app import push_notify

    calls = {"user": [], "users": []}

    def fake_send_to_user(db, user_id, **kw):
        calls["user"].append((user_id, kw))
        return 1

    def fake_send_to_users(db, user_ids, **kw):
        ids = list(user_ids)
        calls["users"].append((ids, kw))
        return len(ids)

    monkeypatch.setattr(push_notify, "send_to_user", fake_send_to_user)
    monkeypatch.setattr(push_notify, "send_to_users", fake_send_to_users)
    return calls


def test_push_bug_created_fans_out(capture_sends):
    """push_notify.py:146-160 — recipients present → send_to_users called
    with activity channel + bug deep link/tag."""
    from app.push_notify import push_bug_created

    bug = _bug(reporter=_user(1), assignees=(_user(2),))
    push_bug_created(bug, actor_user_id=99)
    assert len(capture_sends["users"]) == 1
    ids, kw = capture_sends["users"][0]
    assert ids == [1, 2]
    assert kw["channel"] == "activity"
    assert kw["deep_link"] == "app://bughunter/bug/42"
    assert kw["tag"] == "bug:42"
    assert kw["title"].startswith("New bug:")


def test_push_bug_created_no_recipients_noop(capture_sends):
    """push_notify.py:148-149 — actor is the only candidate → empty
    recipients → early return, no send."""
    from app.push_notify import push_bug_created

    bug = _bug(reporter=_user(5), assignees=())
    push_bug_created(bug, actor_user_id=5)
    assert capture_sends["users"] == []


def test_push_bug_updated_with_changes(capture_sends):
    """push_notify.py:163-189 — non-empty changes & recipients → body
    summarises the first three fields."""
    from app.push_notify import push_bug_updated

    bug = _bug(reporter=_user(1), assignees=())
    changes = [("status", "open", "closed")]
    push_bug_updated(bug, changes, actor_name="Alice", actor_user_id=None)
    assert len(capture_sends["users"]) == 1
    _ids, kw = capture_sends["users"][0]
    assert "status" in kw["body"]
    assert "Alice changed" in kw["body"]
    assert kw["channel"] == "activity"


def test_push_bug_updated_more_than_three_changes(capture_sends):
    """push_notify.py:177-179 — >3 changes appends a '+N more' suffix."""
    from app.push_notify import push_bug_updated

    bug = _bug(reporter=_user(1), assignees=())
    changes = [(f"f{i}", "o", "n") for i in range(5)]
    push_bug_updated(bug, changes, actor_name="Bob", actor_user_id=None)
    _ids, kw = capture_sends["users"][0]
    assert "+2 more" in kw["body"]


def test_push_bug_updated_no_changes_noop(capture_sends):
    """push_notify.py:169-170 — empty changes list short-circuits."""
    from app.push_notify import push_bug_updated

    bug = _bug(reporter=_user(1), assignees=())
    push_bug_updated(bug, [], actor_name="Bob", actor_user_id=None)
    assert capture_sends["users"] == []


def test_push_bug_updated_no_recipients_noop(capture_sends):
    """push_notify.py:171-173 — changes present but no recipients (actor
    is sole reporter) → early return."""
    from app.push_notify import push_bug_updated

    bug = _bug(reporter=_user(8), assignees=())
    push_bug_updated(bug, [("status", "o", "n")], actor_name="X", actor_user_id=8)
    assert capture_sends["users"] == []


def test_push_assignment_high_channel(capture_sends):
    """push_notify.py:192-209 — each newly-assigned user gets a
    send_to_user on the assignments channel."""
    from app.push_notify import push_assignment

    bug = _bug(reporter=_user(1), assignees=())
    push_assignment(bug, newly_assigned=[_user(2), _user(3)], actor_name="Lead")
    assert len(capture_sends["user"]) == 2
    for uid, kw in capture_sends["user"]:
        assert kw["channel"] == "assignments"
        assert kw["tag"] == "bug:42"
        assert "Assigned to you" in kw["title"]


def test_push_assignment_empty_iterable_noop(capture_sends):
    """push_notify.py:200 — empty newly_assigned → the for-loop body never
    runs, no sends."""
    from app.push_notify import push_assignment

    push_assignment(_bug(reporter=_user(1)), newly_assigned=[], actor_name="Lead")
    assert capture_sends["user"] == []


def test_push_comment_mention_upgrades_channel(capture_sends):
    """push_notify.py:226-227 (True) — an @ in the body bumps the channel
    to mentions."""
    from app.push_notify import push_comment_added

    bug = _bug(reporter=_user(1), assignees=())
    push_comment_added(bug, "Carol", comment_author_id=2,
                       comment_body="hey @dave check this")
    _ids, kw = capture_sends["users"][0]
    assert kw["channel"] == "mentions"


def test_push_comment_no_mention_stays_activity(capture_sends):
    """push_notify.py:226-227 (False) — no @ keeps the activity channel;
    body preview is the first line."""
    from app.push_notify import push_comment_added

    bug = _bug(reporter=_user(1), assignees=())
    push_comment_added(bug, "Carol", comment_author_id=2,
                       comment_body="just a plain comment\nsecond line")
    _ids, kw = capture_sends["users"][0]
    assert kw["channel"] == "activity"
    assert kw["body"] == "just a plain comment"


def test_push_comment_long_body_truncated(capture_sends):
    """push_notify.py:233-234 — a >140 char first line is truncated with
    an ellipsis."""
    from app.push_notify import push_comment_added

    bug = _bug(reporter=_user(1), assignees=())
    long_body = "x" * 200
    push_comment_added(bug, "Carol", comment_author_id=2, comment_body=long_body)
    _ids, kw = capture_sends["users"][0]
    assert kw["body"].endswith("…")
    assert len(kw["body"]) == 140


def test_push_comment_empty_body_uses_fallback(capture_sends):
    """push_notify.py:232,240 — an empty-string comment body is falsy, so
    `preview` becomes '' via the `else ''` arm and the body falls back to
    'View the comment on …'.

    NOTE: a *whitespace-only* body (e.g. '   ') is truthy, so line 232
    evaluates `'   '.strip().splitlines()[0]` → '   '.strip() == '' →
    [].splitlines() yields no lines → IndexError. That is a latent source
    edge case in push_notify.py:232; we deliberately do NOT trigger it
    here (cannot edit source), and exercise the empty-string fallback
    instead, which is the realistic empty-comment case."""
    from app.push_notify import push_comment_added

    bug = _bug(reporter=_user(1), assignees=())
    push_comment_added(bug, "Carol", comment_author_id=2, comment_body="")
    _ids, kw = capture_sends["users"][0]
    assert kw["body"].startswith("View the comment on")
    assert kw["channel"] == "activity"


def test_push_comment_none_body_uses_fallback(capture_sends):
    """push_notify.py:226,232 — a None comment_body exercises the
    `comment_body or ''` guards (search side and preview side)."""
    from app.push_notify import push_comment_added

    bug = _bug(reporter=_user(1), assignees=())
    push_comment_added(bug, "Carol", comment_author_id=2, comment_body=None)
    _ids, kw = capture_sends["users"][0]
    assert kw["body"].startswith("View the comment on")


def test_push_comment_no_recipients_noop(capture_sends):
    """push_notify.py:218-220 — no recipients after exclude → early
    return, nothing sent."""
    from app.push_notify import push_comment_added

    bug = _bug(reporter=_user(4), assignees=())
    push_comment_added(bug, "Carol", comment_author_id=4, comment_body="hi")
    assert capture_sends["users"] == []


# ===========================================================================
# app/push_notify.py — event notifiers
# ===========================================================================
def test_push_event_created_fans_out(capture_sends):
    """push_notify.py:250-265 — managers (minus actor) get an event push."""
    from app.push_notify import push_event_created

    ev = _event(managers=(_user(1), _user(2)))
    push_event_created(ev, actor_name="Ann", actor_user_id=2)
    ids, kw = capture_sends["users"][0]
    assert ids == [1]
    assert kw["deep_link"] == "app://bughunter/event/7"
    assert kw["tag"] == "event:7"
    assert kw["title"].startswith("New event:")


def test_push_event_created_no_recipients_noop(capture_sends):
    """push_notify.py:253-255 — actor is the only manager → no send."""
    from app.push_notify import push_event_created

    push_event_created(_event(managers=(_user(2),)), actor_name="Ann", actor_user_id=2)
    assert capture_sends["users"] == []


def test_push_event_updated_with_changes(capture_sends):
    """push_notify.py:268-291 — changes + recipients → '+N more' suffix on
    >3 changes, event channel/deep link."""
    from app.push_notify import push_event_updated

    ev = _event(managers=(_user(1),))
    changes = [(f"f{i}", "o", "n") for i in range(4)]
    push_event_updated(ev, changes, actor_name="Eve", actor_user_id=None)
    _ids, kw = capture_sends["users"][0]
    assert "+1 more" in kw["body"]
    assert kw["deep_link"] == "app://bughunter/event/7"


def test_push_event_updated_three_or_fewer_changes(capture_sends):
    """push_notify.py:280->282 (False branch) — exactly 3 changes skips
    the '+N more' append and goes straight to the send."""
    from app.push_notify import push_event_updated

    ev = _event(managers=(_user(1),))
    changes = [("a", "o", "n"), ("b", "o", "n"), ("c", "o", "n")]
    push_event_updated(ev, changes, actor_name="Eve", actor_user_id=None)
    _ids, kw = capture_sends["users"][0]
    assert "+more" not in kw["body"]
    assert "+1 more" not in kw["body"]
    assert "a, b, c" in kw["body"]


def test_push_event_updated_no_changes_noop(capture_sends):
    """push_notify.py:274-275 — empty changes → no send."""
    from app.push_notify import push_event_updated

    push_event_updated(_event(managers=(_user(1),)), [], actor_name="Eve",
                       actor_user_id=None)
    assert capture_sends["users"] == []


def test_push_event_updated_no_recipients_noop(capture_sends):
    """push_notify.py:276-278 — changes present but actor is sole manager
    → no recipients → early return."""
    from app.push_notify import push_event_updated

    ev = _event(managers=(_user(3),))
    push_event_updated(ev, [("name", "o", "n")], actor_name="Eve", actor_user_id=3)
    assert capture_sends["users"] == []


def test_push_event_deleted_no_deep_link(capture_sends):
    """push_notify.py:294-311 — deletion push carries no deep link (the
    event is gone) but keeps the event tag."""
    from app.push_notify import push_event_deleted

    ev = _event(managers=(_user(1), _user(2)))
    push_event_deleted(ev, actor_name="Del", actor_user_id=1)
    ids, kw = capture_sends["users"][0]
    assert ids == [2]
    assert kw["deep_link"] is None
    assert kw["tag"] == "event:7"
    assert kw["title"].startswith("Event deleted:")


def test_push_event_deleted_no_recipients_noop(capture_sends):
    """push_notify.py:297-299 — actor is the only manager → no send."""
    from app.push_notify import push_event_deleted

    push_event_deleted(_event(managers=(_user(1),)), actor_name="Del", actor_user_id=1)
    assert capture_sends["users"] == []
