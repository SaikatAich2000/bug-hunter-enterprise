"""Web push (FCM) tests; app.fcm_transport.send is mocked, no live Firebase.
Config endpoint, subscribe/unsubscribe, push_to_users, dead-token pruning, request-time immediacy.
"""
from __future__ import annotations

BOOTSTRAP_EMAIL = "admin@test.local"


# -- helpers -----------------------------------------------------------------
def _enable_push(monkeypatch, on=True):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "WEB_PUSH_ENABLED", on)
    monkeypatch.setattr(s, "FIREBASE_API_KEY", "test-api-key" if on else "")
    monkeypatch.setattr(s, "FIREBASE_VAPID_KEY", "test-vapid-key" if on else "")


def _capture_fcm(monkeypatch, dead=None):
    """Patch the FCM transport to record calls and return the given dead tokens."""
    calls: list[dict] = []

    def fake_send(tokens, *, title, body, url="", data=None, channel="", data_only=False):
        calls.append({"tokens": list(tokens), "title": title, "body": body, "url": url})
        return list(dead or [])

    monkeypatch.setattr("app.fcm_transport.send", fake_send)
    return calls


def _session():
    from app.database import SessionLocal
    return SessionLocal()


def _uid(db, email: str) -> int:
    from sqlalchemy import select

    from app.models import User
    return db.scalar(select(User).where(User.email == email)).id


def _mk_user(admin_client, name: str, email: str) -> int:
    r = admin_client.post("/api/users", json={
        "name": name, "email": email, "role": "user", "password": "User12345",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _register(user_id: int, token: str):
    from app import push_service
    db = _session()
    push_service.register(db, user_id=user_id, token=token)
    db.commit()
    db.close()


# -- /api/push/config --------------------------------------------------------
def test_config_reports_disabled_when_off(admin_client, monkeypatch):
    # Force off so the test is hermetic regardless of the host's .env.
    _enable_push(monkeypatch, on=False)
    r = admin_client.get("/api/push/config")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_config_exposes_public_values_when_enabled(admin_client, monkeypatch):
    _enable_push(monkeypatch, on=True)
    j = admin_client.get("/api/push/config").json()
    assert j["enabled"] is True
    assert j["api_key"] == "test-api-key"
    assert j["vapid_key"] == "test-vapid-key"


# -- service worker (served from the root scope) -----------------------------
def test_service_worker_noop_when_disabled(client, monkeypatch):
    _enable_push(monkeypatch, on=False)  # hermetic regardless of host .env
    r = client.get("/firebase-messaging-sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "not configured" in r.text  # no firebase init when push is off


def test_service_worker_injects_config_when_enabled(client, monkeypatch):
    _enable_push(monkeypatch, on=True)
    r = client.get("/firebase-messaging-sw.js")
    assert r.status_code == 200
    assert r.headers.get("Service-Worker-Allowed") == "/"
    assert "firebase.initializeApp" in r.text
    assert "test-api-key" in r.text
    assert "onBackgroundMessage" in r.text


# -- subscribe / unsubscribe -------------------------------------------------
def test_subscribe_requires_auth(client):
    assert client.post("/api/push/subscribe", json={"token": "t1"}).status_code == 401
    assert client.post("/api/push/unsubscribe", json={"token": "t1"}).status_code == 401


def test_subscribe_then_unsubscribe(admin_client):
    db = _session()
    admin_id = _uid(db, BOOTSTRAP_EMAIL)
    db.close()

    r = admin_client.post("/api/push/subscribe", json={"token": "tok-abc", "platform": "web"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    from sqlalchemy import select

    from app.models import PushSubscription
    db = _session()
    sub = db.scalar(select(PushSubscription).where(PushSubscription.token == "tok-abc"))
    assert sub is not None
    assert sub.user_id == admin_id
    assert sub.platform == "web"
    db.close()

    assert admin_client.post("/api/push/unsubscribe", json={"token": "tok-abc"}).json()["ok"] is True
    db = _session()
    assert db.scalar(select(PushSubscription).where(PushSubscription.token == "tok-abc")) is None
    db.close()


def test_subscribe_same_token_upserts(admin_client):
    admin_client.post("/api/push/subscribe", json={"token": "dup"})
    admin_client.post("/api/push/subscribe", json={"token": "dup", "platform": "web"})

    from sqlalchemy import func, select

    from app.models import PushSubscription
    db = _session()
    n = db.scalar(
        select(func.count()).select_from(PushSubscription)
        .where(PushSubscription.token == "dup")
    )
    db.close()
    assert n == 1  # upserted, not duplicated


def test_unsubscribe_is_user_scoped(admin_client):
    # Admin registers a token, then another user attempts to remove it.
    admin_client.post("/api/push/subscribe", json={"token": "admins-token"})
    _mk_user(admin_client, "Mallory", "mallory@test.local")
    admin_client.post("/api/auth/logout")
    admin_client.post("/api/auth/login", json={"email": "mallory@test.local", "password": "User12345"})

    r = admin_client.post("/api/push/unsubscribe", json={"token": "admins-token"})
    assert r.json()["ok"] is False  # wrong owner, nothing removed

    from sqlalchemy import select

    from app.models import PushSubscription
    db = _session()
    assert db.scalar(select(PushSubscription).where(PushSubscription.token == "admins-token")) is not None
    db.close()


# -- push_service.push_to_users ----------------------------------------------
def test_push_to_users_noop_when_disabled(admin_client, monkeypatch):
    _enable_push(monkeypatch, on=False)  # hermetic regardless of host .env
    calls = _capture_fcm(monkeypatch)
    from app import push_service
    db = _session()
    admin_id = _uid(db, BOOTSTRAP_EMAIL)
    db.close()
    _register(admin_id, "tok")

    assert push_service.push_to_users([admin_id], title="x", body="y") == 0
    assert calls == []  # transport is never called when push is disabled


def test_push_to_users_sends_and_prunes_dead_tokens(admin_client, monkeypatch):
    _enable_push(monkeypatch, on=True)
    calls = _capture_fcm(monkeypatch, dead=["dead-tok"])
    from app import push_service
    db = _session()
    admin_id = _uid(db, BOOTSTRAP_EMAIL)
    db.close()
    _register(admin_id, "live-tok")
    _register(admin_id, "dead-tok")

    delivered = push_service.push_to_users([admin_id], title="T", body="B", url="/#bug=1")
    assert len(calls) == 1
    assert set(calls[0]["tokens"]) == {"live-tok", "dead-tok"}
    assert delivered == 1  # 2 tokens sent, 1 reported dead

    from sqlalchemy import select

    from app.models import PushSubscription
    db = _session()
    assert db.scalar(select(PushSubscription).where(PushSubscription.token == "dead-tok")) is None
    assert db.scalar(select(PushSubscription).where(PushSubscription.token == "live-tok")) is not None
    db.close()


# -- immediacy: push fires during the request, not in a cron/digest ----------
def test_operation_fires_push_immediately(admin_client, monkeypatch):
    _enable_push(monkeypatch, on=True)
    calls = _capture_fcm(monkeypatch)
    xid = _mk_user(admin_client, "Xavier", "xavier@test.local")
    _register(xid, "xavier-token")

    proj = admin_client.post("/api/projects", json={"name": "Push Proj"}).json()
    r = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Push me", "priority": "Medium",
        "environment": "DEV", "assignee_ids": [xid],
    })
    assert r.status_code == 201, r.text

    # BackgroundTasks run before TestClient returns, so the push arrives synchronously.
    pushed = [c for c in calls if "xavier-token" in c["tokens"]]
    assert pushed, calls
    assert "Push me" in pushed[0]["body"]
    assert pushed[0]["url"].startswith("/#bug=")


def test_actor_does_not_get_their_own_push(admin_client, monkeypatch):
    _enable_push(monkeypatch, on=True)
    calls = _capture_fcm(monkeypatch)
    db = _session()
    admin_id = _uid(db, BOOTSTRAP_EMAIL)
    db.close()
    _register(admin_id, "admin-token")

    # Admin is actor and sole assignee; self-exclusion empties the list, no push.
    proj = admin_client.post("/api/projects", json={"name": "Solo"}).json()
    admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Just me", "priority": "Low",
        "environment": "DEV",
    })
    assert all("admin-token" not in c["tokens"] for c in calls)


def test_bulk_delete_fires_push_immediately(admin_client, monkeypatch):
    """Bulk delete must push to assignees like a single delete; the bulk path was previously silent."""
    _enable_push(monkeypatch, on=True)
    calls = _capture_fcm(monkeypatch)
    yid = _mk_user(admin_client, "Yolanda", "yolanda@test.local")
    _register(yid, "yolanda-token")

    proj = admin_client.post("/api/projects", json={"name": "Bulk Push"}).json()
    bug = admin_client.post("/api/bugs", json={
        "project_id": proj["id"], "title": "Bulk push me", "priority": "Medium",
        "environment": "DEV", "assignee_ids": [yid],
    }).json()
    calls.clear()  # discard the creation push; only inspect the bulk-delete push

    r = admin_client.post("/api/bugs/bulk", json={"action": "delete", "ids": [bug["id"]]})
    assert r.status_code == 200, r.text

    pushed = [c for c in calls if "yolanda-token" in c["tokens"]]
    assert pushed, calls
    assert "deleted" in pushed[0]["title"].lower()
