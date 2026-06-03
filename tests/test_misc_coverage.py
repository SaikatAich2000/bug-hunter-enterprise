"""Coverage padding for the smaller / nearly-covered modules.

Targets specific uncovered branches in:
  - app/auth.py (session helper functions)
  - app/observability.py (metrics middleware paths)
  - app/chatbot/router.py (rate limit + download endpoints)
  - app/chatbot/excel.py (workbook builder + staging cache)
  - app/database.py (idempotency + reconciliation passes)
  - app/main.py (bootstrap reset-password path, has_valid_session)
  - app/routes/saved_views.py (update + delete branches)
  - app/routes/totp.py (regenerate, status branches)
  - app/routes/dsar.py (export branches)
  - app/routes/branding.py (validation branches)
  - app/routes/custom_fields.py (manage + delete + bulk-set)
  - app/routes/webhooks.py (test endpoint, suspension reset)
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PASS = "TestPass1!"


def _signup(client, org="Acme", name="Alice Admin", email="alice@a.test"):
    r = client.post("/api/auth/signup", json={
        "organization_name": org, "name": name,
        "email": email, "password": PASS,
    })
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# app/auth.py — session helpers
# ---------------------------------------------------------------------------
class TestAuthSessionHelpers:
    def test_expired_session_row_rejects_request(self, client):
        """Hits _delete_expired_session via the request path."""
        _signup(client)
        # /me works while session is valid.
        assert client.get("/api/auth/me").status_code == 200
        # Now flip the SessionRow row to expired and try again.
        from app.database import SessionLocal
        from app.models import Session as SessionRow
        db = SessionLocal()
        try:
            rows = db.query(SessionRow).all()
            assert rows, "Expected at least one session row after signup"
            for s in rows:
                s.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
        finally:
            db.close()
        # Now the session is dead — _delete_expired_session should fire
        # and the helper returns False → 401.
        r = client.get("/api/auth/me")
        assert r.status_code == 401
        # Session row should be gone.
        db = SessionLocal()
        try:
            assert db.query(SessionRow).count() == 0
        finally:
            db.close()

    def test_last_seen_bump_is_throttled(self, client):
        """Two adjacent requests should not both touch last_seen_at."""
        _signup(client)
        from app.database import SessionLocal
        from app.models import Session as SessionRow

        # Drive an initial /me to populate last_seen_at, then grab it.
        client.get("/api/auth/me")
        db = SessionLocal()
        try:
            sess = db.query(SessionRow).first()
            assert sess is not None
            first_seen = sess.last_seen_at
        finally:
            db.close()
        # Immediately repeat the call. Throttle is 60 s so the second
        # call should NOT update last_seen_at.
        client.get("/api/auth/me")
        db = SessionLocal()
        try:
            sess2 = db.query(SessionRow).first()
            assert sess2 is not None
            # Must be unchanged (or at most equal — same row).
            assert sess2.last_seen_at == first_seen
        finally:
            db.close()

    def test_validate_session_row_user_mismatch(self, client):
        """Tamper the session row's user_id → _validate_session_row
        returns False → request is unauthenticated.

        We can't point user_id at a nonexistent row (FK on), so we
        insert a second user and re-target the session at them — the
        cookie's signed user_id no longer matches.
        """
        _signup(client)
        from app.auth import hash_password
        from app.database import SessionLocal
        from app.models import Session as SessionRow, User
        db = SessionLocal()
        try:
            # Insert another user in the same org so the FK still holds.
            me = db.query(User).filter(User.email == "alice@a.test").one()
            decoy = User(
                org_id=me.org_id, name="Decoy",
                email="decoy@a.test",
                role="member", is_active=True,
                password_hash=hash_password(PASS),
            )
            db.add(decoy)
            db.commit()
            sess = db.query(SessionRow).first()
            assert sess is not None
            sess.user_id = decoy.id
            db.commit()
        finally:
            db.close()
        r = client.get("/api/auth/me")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# app/observability.py — metrics endpoints + middleware paths
# ---------------------------------------------------------------------------
class TestObservabilityMetrics:
    def test_metrics_requires_token_when_set(self, db_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        monkeypatch.setenv("METRICS_ENABLED", "true")
        monkeypatch.setenv("METRICS_TOKEN", "open-sesame")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            # No bearer → 401
            r = c.get("/api/metrics")
            assert r.status_code == 401
            # Bad bearer → 403
            r = c.get("/api/metrics", headers={"authorization": "Bearer wrong"})
            assert r.status_code == 403
            # Good bearer → 200
            r = c.get("/api/metrics", headers={"authorization": "Bearer open-sesame"})
            assert r.status_code == 200
            assert "bh_http_requests_total" in r.text

    def test_render_prometheus_includes_events(self, app_env):
        from app import observability
        observability.record_event("login_failure", 2)
        observability.record_event("login_success", 1)
        # Push a synthetic request into the histogram so we exercise
        # the bucket emission loop.
        observability._record_request("/x", 200, 12.5)
        observability._record_request("/x", 200, 5000.0)
        text = observability.render_prometheus()
        assert "bh_events_total" in text
        assert "login_failure" in text
        assert "bh_http_request_duration_ms_bucket" in text
        assert 'route="/x"' in text

    def test_observability_middleware_echoes_request_id(self, client):
        r = client.get("/api/health", headers={"x-request-id": "my-trace-id"})
        assert r.status_code == 200
        assert r.headers.get("X-Request-ID") == "my-trace-id"

    def test_observability_middleware_generates_request_id(self, client):
        r = client.get("/api/health")
        rid = r.headers.get("X-Request-ID")
        assert rid and len(rid) <= 64

    def test_set_request_context_helpers(self, app_env):
        from app import observability
        observability.set_request_context("rid-1", user_id=7, org_id=3)
        assert observability.current_request_id() == "rid-1"


# ---------------------------------------------------------------------------
# app/chatbot/router.py — rate limit + download endpoint
# ---------------------------------------------------------------------------
class TestChatbotRouter:
    def test_download_unknown_token_404(self, client):
        _signup(client)
        r = client.get("/api/chat/download/no-such-token")
        assert r.status_code == 404

    def test_download_requires_auth(self, client):
        # No signup → request hits dependency that demands auth.
        r = client.get("/api/chat/download/anything")
        assert r.status_code == 401

    def test_download_staged_workbook(self, client):
        _signup(client)
        from app.chatbot import excel
        excel.clear_all_for_test()
        token, size = excel.stage_workbook(
            rows=[{"id": 1, "title": "Hello", "project": "Web"}],
            filename="my export.xlsx",
            description="recent bugs",
        )
        assert size > 0
        r = client.get(f"/api/chat/download/{token}")
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "my export.xlsx" in cd
        # xlsx files start with the PK zip magic.
        assert r.content[:2] == b"PK"

    def test_rate_limit_triggers(self, client, monkeypatch):
        _signup(client)
        from app.chatbot import router as chat_router
        # Crank the limit way down so a single ask trips the throttle on
        # the next call.
        monkeypatch.setattr(chat_router, "_RATE_MAX_REQUESTS", 1)
        # Reset the bucket so previous tests can't poison us.
        chat_router._rate_state.clear()

        # First ask succeeds (the executor handles unknown intents fine).
        r1 = client.post("/api/chat/ask", json={"message": "hello"})
        assert r1.status_code == 200
        # Second ask → 429
        r2 = client.post("/api/chat/ask", json={"message": "again"})
        assert r2.status_code == 429
        assert "slow down" in r2.json()["detail"].lower()

    def test_executor_exception_returns_graceful_text(self, client, monkeypatch):
        _signup(client)
        from app.chatbot import router as chat_router
        from app.chatbot import executor

        def _explode(*a, **k):
            raise RuntimeError("synthetic boom")

        monkeypatch.setattr(executor, "execute", _explode)
        chat_router._rate_state.clear()
        r = client.post("/api/chat/ask", json={"message": "x"})
        assert r.status_code == 200
        body = r.json()
        assert body["intent"] == "error"
        assert any(b["kind"] == "text" for b in body["blocks"])


# ---------------------------------------------------------------------------
# app/chatbot/excel.py — workbook builder + cache eviction
# ---------------------------------------------------------------------------
class TestChatbotExcel:
    def test_stage_and_fetch_roundtrip(self, app_env):
        from app.chatbot import excel
        excel.clear_all_for_test()
        token, _ = excel.stage_workbook(
            rows=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}],
            filename="r.xlsx",
            description="dummy",
        )
        result = excel.fetch_staged(token)
        assert result is not None
        payload, filename = result
        assert filename == "r.xlsx"
        # Should be a real xlsx file (PK zip magic).
        assert payload[:2] == b"PK"

    def test_fetch_empty_token_returns_none(self, app_env):
        from app.chatbot import excel
        assert excel.fetch_staged("") is None

    def test_fetch_unknown_token_returns_none(self, app_env):
        from app.chatbot import excel
        excel.clear_all_for_test()
        assert excel.fetch_staged("not-staged") is None

    def test_expired_entry_is_evicted(self, app_env, monkeypatch):
        from app.chatbot import excel
        excel.clear_all_for_test()
        token, _ = excel.stage_workbook(
            rows=[{"id": 1, "title": "x"}], filename="f.xlsx",
        )
        # Force the entry's expiry into the past by editing the cache
        # directly — easier than fast-forwarding time.
        payload, fn, _ = excel._cache[token]
        excel._cache[token] = (payload, fn, 0.0)
        assert excel.fetch_staged(token) is None
        assert token not in excel._cache

    def test_eviction_when_cache_full(self, app_env, monkeypatch):
        from app.chatbot import excel
        excel.clear_all_for_test()
        monkeypatch.setattr(excel, "_MAX_ENTRIES", 3)
        # Stage 4 — the oldest (smallest expiry) gets dropped.
        tokens = []
        for i in range(4):
            tok, _ = excel.stage_workbook(
                rows=[{"id": i}], filename=f"f{i}.xlsx",
            )
            tokens.append(tok)
        assert len(excel._cache) <= 3

    def test_build_workbook_handles_none_values(self, app_env):
        from app.chatbot import excel
        excel.clear_all_for_test()
        # None and missing values should not crash.
        token, _ = excel.stage_workbook(
            rows=[{"id": 1, "title": None}, {}],
            filename="x.xlsx",
            description="",  # falsy description hits the banner-fallback branch
        )
        result = excel.fetch_staged(token)
        assert result is not None
        payload, _ = result
        assert payload[:2] == b"PK"


# ---------------------------------------------------------------------------
# app/database.py — init_db idempotency, helper passes
# ---------------------------------------------------------------------------
class TestDatabaseInit:
    def test_init_db_is_idempotent(self, app_env):
        from app.database import init_db
        # Already called once via lifespan in other tests; here we
        # just invoke it directly twice.
        init_db()
        init_db()

    def test_add_missing_column_via_pragmatic_path(self, client):
        """Exercise _add_missing_column with a real model table.

        Going through `client` ensures init_db has run so the metadata
        is populated. The helper's primary ALTER path emits a portable
        DDL — for an entirely new nullable column this succeeds.
        """
        from sqlalchemy import Column, String
        from app.database import _add_missing_column, engine, Base
        # Pick a known model table (users).
        from app.models import User  # noqa: F401
        table = Base.metadata.tables["users"]
        sacrificial = Column("zz_temp_col", String(8))
        sacrificial.table = table
        with engine.begin() as conn:
            # Should NOT raise.
            _add_missing_column(conn, table, sacrificial)

    def test_reconcile_passes_run_after_init(self, client):
        """Calling the reconcile helpers directly after init_db should
        be a clean no-op."""
        from sqlalchemy import inspect
        from app.database import (
            _reconcile_columns, _reconcile_indexes, engine,
        )
        inspector = inspect(engine)
        _reconcile_columns(inspector)
        _reconcile_indexes(inspector)


# ---------------------------------------------------------------------------
# app/main.py — bootstrap admin reset-password flow + has_valid_session
# ---------------------------------------------------------------------------
class TestMainBootstrap:
    def test_bootstrap_resets_existing_admin_password(self, db_path, monkeypatch):
        """Lifespan should reset the password when RESET=true and a
        user with that email already exists."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        # First boot: signup, then we modify the user and re-boot with
        # the reset flag on.
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/auth/signup", json={
                "organization_name": "Acme", "name": "Boss",
                "email": "boss@a.test", "password": "OldPass1!",
            })
            assert r.status_code == 201

        # Now turn on bootstrap reset and rebuild the app.
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "boss@a.test")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "ResetPass1!")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_RESET_PASSWORD", "true")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings as _gs2
        _gs2.cache_clear()  # type: ignore[attr-defined]
        from app.main import app as app2
        with TestClient(app2) as c:
            # The OLD password should now be rejected; the NEW one works.
            r = c.post("/api/auth/login", json={
                "email": "boss@a.test", "password": "OldPass1!",
            })
            assert r.status_code == 401
            r = c.post("/api/auth/login", json={
                "email": "boss@a.test", "password": "ResetPass1!",
            })
            assert r.status_code == 200

    def test_bootstrap_creates_admin_when_missing(self, db_path, monkeypatch):
        """Empty DB + BOOTSTRAP_ADMIN_EMAIL → admin gets created."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "boot@a.test")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Boot1Pass!")
        monkeypatch.setenv("BOOTSTRAP_ORG_NAME", "Bootstrapped Org")

        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/auth/login", json={
                "email": "boot@a.test", "password": "Boot1Pass!",
            })
            assert r.status_code == 200, r.text

    def test_home_redirects_when_logged_out(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login.html" in r.headers["location"]

    def test_login_page_redirects_when_logged_in(self, client):
        _signup(client)
        r = client.get("/login", follow_redirects=False)
        assert r.status_code == 302

    def test_health_returns_version(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body


# ---------------------------------------------------------------------------
# app/routes/saved_views.py
# ---------------------------------------------------------------------------
class TestSavedViewsBranches:
    def test_get_view_returns_view(self, client):
        _signup(client)
        v = client.post("/api/saved-views", json={
            "name": "Mine", "filters": {"x": 1},
        }).json()
        r = client.get(f"/api/saved-views/{v['id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "Mine"
        assert r.json()["filters"] == {"x": 1}

    def test_update_view_changes_fields(self, client):
        _signup(client)
        v = client.post("/api/saved-views", json={"name": "A", "filters": {}}).json()
        r = client.put(f"/api/saved-views/{v['id']}", json={
            "name": "B", "filters": {"k": "v"}, "shared_with_org": True,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "B"
        assert body["shared_with_org"] is True

    def test_delete_returns_204(self, client):
        _signup(client)
        v = client.post("/api/saved-views", json={"name": "A", "filters": {}}).json()
        r = client.delete(f"/api/saved-views/{v['id']}")
        assert r.status_code == 204
        # Idempotent re-delete → 404
        r = client.delete(f"/api/saved-views/{v['id']}")
        assert r.status_code == 404

    def test_view_unknown_id_404(self, client):
        _signup(client)
        assert client.get("/api/saved-views/9999").status_code == 404

    def test_member_cannot_edit_other_users_view(self, client, make_invite):
        from fastapi.testclient import TestClient
        from app.main import app
        _signup(client)
        # Admin makes a shared view.
        v = client.post("/api/saved-views", json={
            "name": "Shared", "filters": {}, "shared_with_org": True,
        }).json()
        tok = make_invite(client, "mem@a.test", role="member")
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Member", "password": PASS,
            })
            # Member can SEE the shared view (200) but can't update or delete.
            r = mem.get(f"/api/saved-views/{v['id']}")
            assert r.status_code == 200
            r = mem.put(f"/api/saved-views/{v['id']}", json={"name": "Hijack"})
            assert r.status_code == 403
            r = mem.delete(f"/api/saved-views/{v['id']}")
            assert r.status_code == 403


# ---------------------------------------------------------------------------
# app/routes/totp.py — extra branches
# ---------------------------------------------------------------------------
class TestTotpBranches:
    def test_begin_when_already_enabled_returns_409(self, client):
        _signup(client)
        r = client.post("/api/auth/2fa/begin")
        secret = r.json()["secret"]
        import pyotp
        client.post("/api/auth/2fa/confirm",
                    json={"code": pyotp.TOTP(secret).now()})
        r = client.post("/api/auth/2fa/begin")
        assert r.status_code == 409

    def test_confirm_without_begin_returns_400(self, client):
        _signup(client)
        r = client.post("/api/auth/2fa/confirm", json={"code": "123456"})
        assert r.status_code == 400

    def test_regenerate_requires_enrolled(self, client):
        _signup(client)
        r = client.post("/api/auth/2fa/recovery-codes/regenerate")
        assert r.status_code == 400

    def test_regenerate_after_enrolment(self, client):
        _signup(client)
        r = client.post("/api/auth/2fa/begin")
        secret = r.json()["secret"]
        import pyotp
        client.post("/api/auth/2fa/confirm",
                    json={"code": pyotp.TOTP(secret).now()})
        r = client.post("/api/auth/2fa/recovery-codes/regenerate")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert len(body["recovery_codes"]) > 0

    def test_status_when_totp_disabled_globally(self, db_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
        monkeypatch.setenv("BCRYPT_ROUNDS", "4")
        monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "true")
        monkeypatch.setenv("CSRF_PROTECTION", "false")
        monkeypatch.setenv("TOTP_ENABLED", "false")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            r = c.post("/api/auth/signup", json={
                "organization_name": "Acme", "name": "X Yz",
                "email": "x@a.test", "password": PASS,
            })
            assert r.status_code == 201, r.text
            r = c.get("/api/auth/2fa/status")
            assert r.status_code == 200
            assert r.json()["enabled"] is False
            # Begin/confirm should both be 403 in this mode.
            assert c.post("/api/auth/2fa/begin").status_code == 403
            assert c.post("/api/auth/2fa/confirm", json={"code": "123456"}).status_code == 403


# ---------------------------------------------------------------------------
# app/routes/dsar.py — extra branches
# ---------------------------------------------------------------------------
class TestDsarBranches:
    def test_delete_wrong_password_400(self, client):
        _signup(client)
        r = client.request("DELETE", "/api/auth/account",
                           json={"password": "nope"})
        assert r.status_code == 400

    def test_data_export_unauthenticated(self, client):
        r = client.get("/api/auth/data-export")
        assert r.status_code == 401

    def test_data_export_with_assignments_and_comments(self, client):
        _signup(client)
        proj = client.post("/api/projects", json={"name": "Web"}).json()
        me = client.get("/api/auth/me").json()
        bug = client.post("/api/bugs", json={
            "project_id": proj["id"], "title": "Mine",
            "status": "New", "priority": "Low", "environment": "DEV",
            "assignee_ids": [me["id"]],
        }).json()
        client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "hi"})
        r = client.get("/api/auth/data-export")
        assert r.status_code == 200
        body = r.json()
        assert any(b["id"] == bug["id"] for b in body["bugs_reported"])
        assert any(c["body"] == "hi" for c in body["comments"])


# ---------------------------------------------------------------------------
# app/routes/branding.py — validation branches
# ---------------------------------------------------------------------------
class TestBrandingBranches:
    def test_email_override_validation(self, client):
        _signup(client)
        # Bad — contains a space.
        r = client.put("/api/branding", json={
            "email_from_override": "bad email@a.test",
        })
        assert r.status_code == 422
        # Bad — no @
        r = client.put("/api/branding", json={
            "email_from_override": "noatsign",
        })
        assert r.status_code == 422
        # Empty string → treated as None and accepted (no-op).
        r = client.put("/api/branding", json={
            "email_from_override": "",
        })
        assert r.status_code == 200

    def test_logo_too_large(self, client):
        _signup(client)
        oversize = "data:image/png;base64," + ("A" * 250_000)
        r = client.put("/api/branding", json={"logo_data_url": oversize})
        assert r.status_code == 422

    def test_set_valid_logo_and_email(self, client):
        _signup(client)
        # Minimum valid PNG data URL (any base64 payload accepted by the regex).
        ok_logo = "data:image/png;base64,iVBORw0KGgo="
        r = client.put("/api/branding", json={
            "logo_data_url": ok_logo,
            "email_from_override": "noreply@acme.test",
            "accent_color": "#abc",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["logo_data_url"] == ok_logo
        assert body["email_from_override"] == "noreply@acme.test"
        assert body["accent_color"] == "#abc"
        # PUT with no real changes triggers the "no changes" path.
        r2 = client.put("/api/branding", json={})
        assert r2.status_code == 200

    def test_member_cannot_access_branding(self, client, make_invite):
        from fastapi.testclient import TestClient
        from app.main import app
        _signup(client)
        tok = make_invite(client, "mem@a.test", role="member")
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Mem", "password": PASS,
            })
            r = mem.get("/api/branding")
            assert r.status_code == 403


# ---------------------------------------------------------------------------
# app/routes/custom_fields.py — extra branches
# ---------------------------------------------------------------------------
class TestCustomFieldsBranches:
    def _setup(self, client):
        _signup(client)
        return client.post("/api/projects", json={"name": "Web"}).json()

    def test_update_field(self, client):
        proj = self._setup(client)
        f = client.post(f"/api/projects/{proj['id']}/custom-fields", json={
            "name": "Sev", "field_type": "text",
        }).json()
        r = client.put(f"/api/projects/{proj['id']}/custom-fields/{f['id']}", json={
            "name": "Severity",
            "options": ["A", "B"],
            "is_required": True,
            "position": 2,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Severity"
        assert body["is_required"] is True

    def test_update_invalid_field_type(self, client):
        proj = self._setup(client)
        f = client.post(f"/api/projects/{proj['id']}/custom-fields", json={
            "name": "Sev", "field_type": "text",
        }).json()
        r = client.put(f"/api/projects/{proj['id']}/custom-fields/{f['id']}", json={
            "field_type": "blah",
        })
        assert r.status_code == 400

    def test_update_missing_field_404(self, client):
        proj = self._setup(client)
        r = client.put(f"/api/projects/{proj['id']}/custom-fields/9999",
                       json={"name": "x"})
        assert r.status_code == 404

    def test_delete_field(self, client):
        proj = self._setup(client)
        f = client.post(f"/api/projects/{proj['id']}/custom-fields", json={
            "name": "Sev", "field_type": "text",
        }).json()
        r = client.delete(f"/api/projects/{proj['id']}/custom-fields/{f['id']}")
        assert r.status_code == 204
        r = client.delete(f"/api/projects/{proj['id']}/custom-fields/{f['id']}")
        assert r.status_code == 404

    def test_set_values_drops_unknown_field_ids(self, client):
        proj = self._setup(client)
        f = client.post(f"/api/projects/{proj['id']}/custom-fields", json={
            "name": "T", "field_type": "text",
        }).json()
        bug = client.post("/api/bugs", json={
            "project_id": proj["id"], "title": "Bug for custom values",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        # Send a mix: one valid, one nonsense.
        r = client.put(f"/api/bugs/{bug['id']}/custom-values", json=[
            {"field_id": f["id"], "value": "real"},
            {"field_id": 99999, "value": "fake"},
        ])
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["value"] == "real"
        # Now clear all values by sending an empty list.
        r = client.put(f"/api/bugs/{bug['id']}/custom-values", json=[])
        assert r.status_code == 200
        assert r.json() == []

    def test_get_values_unknown_bug_404(self, client):
        self._setup(client)
        r = client.get("/api/bugs/99999/custom-values")
        assert r.status_code == 404

    def test_get_values_cross_org_404(self, two_orgs):
        c_a, c_b, _, _ = two_orgs
        proj = c_a.post("/api/projects", json={"name": "Web App"}).json()
        bug = c_a.post("/api/bugs", json={
            "project_id": proj["id"], "title": "Cross-org bug",
            "status": "New", "priority": "Low", "environment": "DEV",
        }).json()
        r = c_b.get(f"/api/bugs/{bug['id']}/custom-values")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# app/routes/webhooks.py — extra branches
# ---------------------------------------------------------------------------
class TestWebhooksBranches:
    def test_get_one_404_unknown(self, client):
        _signup(client)
        r = client.get("/api/webhooks/99999")
        assert r.status_code == 404

    def test_get_one_works(self, client):
        _signup(client)
        h = client.post("/api/webhooks", json={
            "name": "Slack", "url": "https://hook.example.test/abc",
        }).json()
        r = client.get(f"/api/webhooks/{h['id']}")
        assert r.status_code == 200
        assert r.json()["name"] == "Slack"

    def test_reenabling_resets_failure_counter(self, client):
        """A suspended hook gets reset when admin flips is_active=true."""
        _signup(client)
        h = client.post("/api/webhooks", json={
            "name": "Z", "url": "https://example.test/h",
        }).json()
        # Directly bump consecutive_failures via SessionLocal.
        from app.database import SessionLocal
        from app.models import Webhook
        db = SessionLocal()
        try:
            row = db.get(Webhook, h["id"])
            row.consecutive_failures = 12
            row.is_active = False
            row.last_error = "old"
            db.commit()
        finally:
            db.close()
        r = client.put(f"/api/webhooks/{h['id']}", json={"is_active": True})
        assert r.status_code == 200
        body = r.json()
        assert body["is_active"] is True
        assert body["consecutive_failures"] == 0
        assert body["last_error"] is None

    def test_test_endpoint_queues_ping(self, client, monkeypatch):
        _signup(client)
        h = client.post("/api/webhooks", json={
            "name": "Y", "url": "https://example.test/h",
        }).json()
        # Replace deliver_event so the background task is a noop.
        import app.routes.webhooks as webhooks_route
        called = {"args": None}

        def fake_deliver(org_id, event, payload):
            called["args"] = (org_id, event, payload)

        monkeypatch.setattr(webhooks_route, "deliver_event", fake_deliver)
        r = client.post(f"/api/webhooks/{h['id']}/test")
        assert r.status_code == 202
        # Background task fires on response close — the TestClient runs
        # them synchronously by the time `r` is returned.
        assert called["args"] is not None
        assert called["args"][1] == "webhook.ping"

    def test_update_url_validates(self, client):
        _signup(client)
        h = client.post("/api/webhooks", json={
            "name": "X", "url": "https://example.test/h",
        }).json()
        r = client.put(f"/api/webhooks/{h['id']}", json={
            "url": "http://localhost/x",
        })
        assert r.status_code == 422

    def test_non_admin_cannot_list(self, client, make_invite):
        from fastapi.testclient import TestClient
        from app.main import app
        _signup(client)
        tok = make_invite(client, "mem@a.test", role="member")
        with TestClient(app) as mem:
            mem.post("/api/invitations/accept", json={
                "token": tok, "name": "Member User", "password": PASS,
            })
            r = mem.get("/api/webhooks")
            assert r.status_code == 403
