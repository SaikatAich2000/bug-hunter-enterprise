"""Security regression tests for v2.8 (enterprise port).

Covers the eight hardening items shipped after the OWASP audit:

  G1 — login timing equality (no user-enumeration via response latency)
  G2 — CSV formula injection guard on bug export
  G3 — global request body size middleware
  G4 — X-Forwarded-For trust gate on audit IP
  G5 — masked email in INFO-level logs
  T3 — per-account lockout after N failed logins
  T4 — HaveIBeenPwned breach check on password set
  T6 — EXIF / metadata strip on uploaded images

The enterprise build uses the signup flow rather than a bootstrap admin,
so credentials are ``admin@acme.test`` / ``TestPass1!`` (supplied by the
``admin_client`` fixture in conftest.py).

Module-level state used by the in-memory features (account lockout, HIBP
backend) is reset between cases via the ``reset_security_state`` fixture
so the suite is order-independent.
"""
from __future__ import annotations

import io
import logging
from unittest import mock

import pytest


# Same creds the admin_client fixture in conftest.py signs up with.
ADMIN_EMAIL = "admin@acme.test"
ADMIN_PASSWORD = "TestPass1!"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_security_state():
    """Wipe per-process security state between tests. The account
    lockout buckets are in-memory; without this, a failed-login burst
    in one test could lock the same email out of a later test."""
    yield
    try:
        from app import account_lockout
        account_lockout._reset_for_tests()
    except ImportError:
        pass


def _ensure_project(client) -> int:
    """Return an existing project_id or create one. Enterprise signups
    don't seed a default project, so tests that need bugs have to make
    one first."""
    existing = client.get("/api/projects").json()
    if existing:
        return existing[0]["id"]
    r = client.post("/api/projects", json={"name": "Security tests"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_bug(client, title: str) -> int:
    project_id = _ensure_project(client)
    res = client.post("/api/bugs", json={
        "project_id": project_id, "title": title,
        "description": "desc", "item_type": "Bug",
        "status": "New", "priority": "Medium", "environment": "DEV",
    })
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ---------------------------------------------------------------------------
# G1 — Login timing equality
# ---------------------------------------------------------------------------
class TestLoginTimingEquality:
    """Both the unknown-email and the wrong-password branches must run
    one bcrypt verification. Without this, an attacker can enumerate
    accounts by measuring response latency."""

    def test_unknown_email_still_runs_bcrypt(self, client):
        from app.routes import auth as auth_routes
        with mock.patch.object(auth_routes, "verify_password",
                               wraps=auth_routes.verify_password) as spy:
            res = client.post("/api/auth/login", json={
                "email": "no-such-user@example.com",
                "password": "anything-at-all-9",
            })
        assert res.status_code == 401
        assert spy.call_count == 1, (
            "Unknown-email branch did not run bcrypt — timing oracle still open"
        )
        args, _ = spy.call_args
        assert args[1] == auth_routes._DUMMY_PASSWORD_HASH

    def test_wrong_password_branch_unchanged(self, admin_client):
        from app.routes import auth as auth_routes
        admin_client.post("/api/auth/logout")
        with mock.patch.object(auth_routes, "verify_password",
                               wraps=auth_routes.verify_password) as spy:
            res = admin_client.post("/api/auth/login", json={
                "email": ADMIN_EMAIL,
                "password": "definitely-not-the-right-pwd-9",
            })
        assert res.status_code == 401
        assert spy.call_count == 1

    def test_unified_401_message_for_both_branches(self, admin_client):
        admin_client.post("/api/auth/logout")
        a = admin_client.post("/api/auth/login", json={
            "email": "no-such-user@example.com", "password": "abc12345",
        })
        b = admin_client.post("/api/auth/login", json={
            "email": ADMIN_EMAIL, "password": "wrong-password-9",
        })
        assert a.status_code == b.status_code == 401
        assert a.json()["detail"] == b.json()["detail"]

    def test_inactive_account_also_returns_401(self, admin_client):
        """v2.8 anti-enumeration: disabled accounts must return the same
        401 + same detail as wrong-password. Previously enterprise
        returned 403 'Account is disabled', which let an attacker who
        knew a valid password tell 'exists but disabled' from 'wrong
        password'."""
        # Create + deactivate a peer.
        admin_client.post("/api/users", json={
            "name": "Disabled", "email": "disabled@acme.test",
            "role": "member", "password": "DisabledPass1",
        })
        users = admin_client.get("/api/users").json()
        target = next(u for u in users if u["email"] == "disabled@acme.test")
        admin_client.put(f"/api/users/{target['id']}", json={"is_active": False})
        admin_client.post("/api/auth/logout")

        a = admin_client.post("/api/auth/login", json={
            "email": "disabled@acme.test", "password": "DisabledPass1",
        })
        b = admin_client.post("/api/auth/login", json={
            "email": ADMIN_EMAIL, "password": "WrongPass-9",
        })
        assert a.status_code == b.status_code == 401
        assert a.json()["detail"] == b.json()["detail"]


# ---------------------------------------------------------------------------
# G2 — Spreadsheet formula injection (XLSX, v2.9)
#
# Legacy CSV export retired with v2.9; the same attack surface (a bug
# title `=cmd|'/c calc.exe'!A1` executing as a formula on open) applies
# to XLSX too, defanged in app/reports/xlsx.py::_defang_formula_text.
# ---------------------------------------------------------------------------
class TestXlsxFormulaInjectionGuard:

    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
    def test_defang_helper_neutralises_formula_triggers(self, trigger):
        from app.reports.xlsx import _defang_formula_text
        assert _defang_formula_text(trigger + "cmd|calc!A1").startswith("'" + trigger)

    def test_defang_helper_passes_through_normal_text(self):
        from app.reports.xlsx import _defang_formula_text
        assert _defang_formula_text("Login button broken") == "Login button broken"

    def test_defang_helper_handles_empty_string(self):
        from app.reports.xlsx import _defang_formula_text
        assert _defang_formula_text("") == ""

    def test_export_xlsx_prefixes_malicious_title(self, admin_client):
        import io
        from openpyxl import load_workbook
        _make_bug(admin_client, "=cmd|'calc.exe'!A1")
        res = admin_client.post("/api/reports/export.xlsx", json={
            "report_key": "item_detail", "filters": {},
        })
        assert res.status_code == 200
        wb = load_workbook(io.BytesIO(res.content), read_only=True)
        found_defanged = False
        for row in wb[wb.sheetnames[0]].iter_rows(values_only=True):
            for cell in row:
                if not isinstance(cell, str):
                    continue
                if "cmd|" in cell:
                    assert cell.startswith("'="), (
                        f"Un-neutralised formula in XLSX cell: {cell!r}"
                    )
                    found_defanged = True
        assert found_defanged, "expected the malicious title to appear (defanged)"


# ---------------------------------------------------------------------------
# G3 — Body size middleware
# ---------------------------------------------------------------------------
class TestBodySizeMiddleware:

    def test_default_limit_is_at_least_50mb(self, client):
        from app.main import settings
        assert settings.MAX_REQUEST_BODY_BYTES >= 50 * 1024 * 1024

    def test_normal_request_under_limit_succeeds(self, admin_client):
        res = admin_client.get("/api/auth/me")
        assert res.status_code == 200

    def test_malformed_content_length_returns_400(self):
        """The middleware must reject a non-integer Content-Length cleanly
        rather than tracebacking inside the int() call. Exercised by
        invoking the middleware's dispatch directly."""
        import asyncio
        from starlette.requests import Request
        from app.main import BodySizeLimitMiddleware

        middleware = BodySizeLimitMiddleware(None)
        scope = {
            "type": "http", "method": "POST", "path": "/api/auth/login",
            "headers": [(b"content-length", b"not-a-number")],
            "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 0),
            "raw_path": b"/api/auth/login",
        }
        request = Request(scope)

        async def call_next(_req):  # pragma: no cover — should not run
            raise AssertionError("middleware must short-circuit before call_next")

        response = asyncio.run(middleware.dispatch(request, call_next))
        assert response.status_code == 400
        body = response.body.decode("utf-8")
        assert "Content-Length" in body or "invalid" in body.lower()

    def test_oversize_content_length_returns_413(self):
        """Direct middleware test: a Content-Length above the cap returns 413
        without reading the body."""
        import asyncio
        from starlette.requests import Request
        from app.main import BodySizeLimitMiddleware, settings

        middleware = BodySizeLimitMiddleware(None)
        oversize = settings.MAX_REQUEST_BODY_BYTES + 1
        scope = {
            "type": "http", "method": "POST", "path": "/api/bugs",
            "headers": [(b"content-length", str(oversize).encode())],
            "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 0),
            "raw_path": b"/api/bugs",
        }
        request = Request(scope)

        async def call_next(_req):  # pragma: no cover
            raise AssertionError("middleware must short-circuit before call_next")

        response = asyncio.run(middleware.dispatch(request, call_next))
        assert response.status_code == 413
        assert "too large" in response.body.decode("utf-8").lower()


# ---------------------------------------------------------------------------
# G4 — X-Forwarded-For trust gate
# ---------------------------------------------------------------------------
class TestXffTrustGate:

    def test_xff_ignored_when_trust_disabled(self, admin_client):
        """Default TRUST_PROXY_FORWARDED_FOR = False (see config.py).
        An X-Forwarded-For header on a login should NOT influence the
        session-row IP."""
        admin_client.post("/api/auth/logout")
        res = admin_client.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert res.status_code == 200
        sessions = admin_client.get("/api/sessions").json()
        assert sessions, "expected at least one session row"
        ips = [s["ip_address"] for s in sessions]
        assert "203.0.113.99" not in ips, (
            f"XFF was honoured despite TRUST_PROXY_FORWARDED_FOR=False: {ips}"
        )

    def test_client_ip_helper_honours_trust_setting(self, monkeypatch):
        """Unit-test the _client_ip helper directly to avoid spinning up a
        second TestClient with a different bootstrap. With TRUST=True and
        an XFF header, it returns the leftmost XFF entry."""
        from app.routes.auth import _client_ip
        from app.config import get_settings

        class _FakeClient:
            host = "10.0.0.1"

        class _FakeReq:
            def __init__(self, xff: str | None):
                self.headers = {"x-forwarded-for": xff} if xff else {}
                self.client = _FakeClient()

        # Disabled (default): falls back to socket client.
        settings = get_settings()
        monkeypatch.setattr(settings, "TRUST_PROXY_FORWARDED_FOR", False)
        assert _client_ip(_FakeReq("198.51.100.7, 10.0.0.1")) == "10.0.0.1"

        # Enabled: honours leftmost XFF.
        monkeypatch.setattr(settings, "TRUST_PROXY_FORWARDED_FOR", True)
        assert _client_ip(_FakeReq("198.51.100.7, 10.0.0.1")) == "198.51.100.7"


# ---------------------------------------------------------------------------
# G5 — Email masking in logs
# ---------------------------------------------------------------------------
class TestEmailMasking:

    @pytest.mark.parametrize("raw,expected", [
        ("alice@example.com",  "a***@example.com"),
        ("a@example.com",      "a***@example.com"),
        ("@example.com",       "@example.com"),
        ("",                   "***"),
        ("plain-no-at-sign",   "***"),
        ("BOB@example.com",    "B***@example.com"),
    ])
    def test_mask_helper(self, raw, expected):
        from app.routes.auth import _mask_email
        assert _mask_email(raw) == expected

    def test_inactive_login_logs_masked_email(self, admin_client, caplog):
        admin_client.post("/api/users", json={
            "name": "Disabled User", "email": "disabled-mask@acme.test",
            "role": "member", "password": "Disabled12",
        })
        users = admin_client.get("/api/users").json()
        target = next(u for u in users if u["email"] == "disabled-mask@acme.test")
        r = admin_client.put(f"/api/users/{target['id']}", json={"is_active": False})
        assert r.status_code == 200
        admin_client.post("/api/auth/logout")

        with caplog.at_level(logging.INFO, logger="bug_hunter.auth"):
            res = admin_client.post("/api/auth/login", json={
                "email": "disabled-mask@acme.test", "password": "Disabled12",
            })
        assert res.status_code == 401
        relevant = [r.message for r in caplog.records if "Login refused" in r.message]
        assert relevant, "expected the masked-email log line"
        for line in relevant:
            assert "disabled-mask@acme.test" not in line, (
                f"Raw email leaked into log: {line!r}"
            )
            assert "d***@acme.test" in line


# ---------------------------------------------------------------------------
# T3 — Account lockout
# ---------------------------------------------------------------------------
class TestAccountLockout:

    def test_unit_check_locked_passes_when_no_state(self):
        from app import account_lockout
        account_lockout.check_locked("never-seen@example.com")

    def test_unit_threshold_triggers_lockout(self):
        from app import account_lockout
        email = "victim@example.com"
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(email)
        with pytest.raises(Exception) as excinfo:
            account_lockout.check_locked(email)
        assert getattr(excinfo.value, "status_code", None) == 429
        assert "Retry-After" in (excinfo.value.headers or {})

    def test_unit_clear_resets_bucket(self):
        from app import account_lockout
        email = "transient@example.com"
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(email)
        account_lockout.clear(email)
        account_lockout.check_locked(email)

    def test_unit_unknown_email_also_counts(self):
        """Ticking only known emails would leak account existence."""
        from app import account_lockout
        ghost = "definitely-not-a-real-user@example.com"
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(ghost)
        with pytest.raises(Exception) as excinfo:
            account_lockout.check_locked(ghost)
        assert getattr(excinfo.value, "status_code", None) == 429

    def test_unit_disabled_when_limit_is_zero(self, monkeypatch):
        from app import account_lockout
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 0)
        for _ in range(50):
            account_lockout.record_failure("anyone@example.com")
        account_lockout.check_locked("anyone@example.com")

    def test_http_login_429_after_threshold(self, admin_client):
        """Drive the lockout from the route layer."""
        admin_client.post("/api/auth/logout")
        codes = []
        for _ in range(15):
            r = admin_client.post("/api/auth/login", json={
                "email": ADMIN_EMAIL, "password": "Wrong-pwd-9",
            })
            codes.append(r.status_code)
        assert 429 in codes, f"Expected 429 somewhere in {codes}"

    def test_successful_login_clears_lockout(self, admin_client):
        from app import account_lockout
        admin_client.post("/api/auth/logout")
        for _ in range(3):
            account_lockout.record_failure(ADMIN_EMAIL)
        res = admin_client.post("/api/auth/login", json={
            "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
        })
        assert res.status_code == 200
        account_lockout.check_locked(ADMIN_EMAIL)


# ---------------------------------------------------------------------------
# T3 — Account lockout (edge cases for coverage)
# ---------------------------------------------------------------------------
class TestAccountLockoutEdgeCases:

    def test_env_int_falls_back_on_garbage(self, monkeypatch):
        from app.account_lockout import _env_int
        monkeypatch.setenv("BH_BOGUS_INT_VAR", "not-a-number")
        assert _env_int("BH_BOGUS_INT_VAR", 42) == 42

    def test_env_int_uses_value(self, monkeypatch):
        from app.account_lockout import _env_int
        monkeypatch.setenv("BH_GOOD_INT_VAR", "99")
        assert _env_int("BH_GOOD_INT_VAR", 0) == 99

    def test_old_failures_get_evicted(self, monkeypatch):
        import time
        from app import account_lockout
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_WINDOW_SECONDS", 0.05)
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 3)
        account_lockout._reset_for_tests()
        for _ in range(2):
            account_lockout.record_failure("evictee@x.com")
        time.sleep(0.07)
        account_lockout.record_failure("evictee@x.com")
        bucket = account_lockout._buckets.get("evictee@x.com")
        assert bucket is not None
        assert len(bucket.fails) == 1
        account_lockout.check_locked("evictee@x.com")

    def test_bucket_dict_cap_drops_oldest_entry(self, monkeypatch):
        from app import account_lockout
        monkeypatch.setattr(account_lockout, "_LOCKOUT_BUCKETS_MAX", 5)
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 10)
        account_lockout._reset_for_tests()
        for i in range(7):
            account_lockout.record_failure(f"user{i}@x.com")
        assert len(account_lockout._buckets) <= 5

    def test_clear_unknown_email_is_noop(self):
        from app import account_lockout
        account_lockout._reset_for_tests()
        account_lockout.clear("never-recorded@x.com")
        assert "never-recorded@x.com" not in account_lockout._buckets


# ---------------------------------------------------------------------------
# T4 — HIBP breach check
# ---------------------------------------------------------------------------
class TestPasswordBreachCheck:

    def test_unit_known_breached_hash_matches(self):
        from app import password_breach
        body = "1E4C9B93F3F0682250B6CF8331B7EE68FD8:3861493\n"
        with mock.patch.object(password_breach, "_fetch_range", return_value=body):
            assert password_breach.is_password_breached("password") is True

    def test_unit_unknown_password_passes(self):
        from app import password_breach
        with mock.patch.object(password_breach, "_fetch_range", return_value=""):
            assert password_breach.is_password_breached("rare-uniq-pwd-9") is False

    def test_unit_padding_count_zero_treated_as_safe(self):
        from app import password_breach
        body = "1E4C9B93F3F0682250B6CF8331B7EE68FD8:0\n"
        with mock.patch.object(password_breach, "_fetch_range", return_value=body):
            assert password_breach.is_password_breached("password") is False

    def test_unit_fail_open_on_network_error(self):
        from app import password_breach
        with mock.patch.object(password_breach, "_fetch_range", return_value=None):
            assert password_breach.is_password_breached("anything-9") is False

    def test_unit_disabled_short_circuits(self, monkeypatch):
        from app import password_breach
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
        assert password_breach.is_password_breached("password") is False

    @staticmethod
    def _force_match(pw: str) -> str:
        import hashlib
        digest = hashlib.sha1(pw.encode("utf-8")).hexdigest().upper()  # NOSONAR
        return f"{digest[5:]}:9999\n"

    def test_change_password_rejects_breached(self, admin_client, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        new_pw = "GoodFresh123"
        with mock.patch.object(
            password_breach, "_fetch_range",
            return_value=self._force_match(new_pw),
        ):
            res = admin_client.post("/api/auth/change-password", json={
                "current_password": ADMIN_PASSWORD,
                "new_password": new_pw,
            })
        assert res.status_code == 400, res.text
        assert "breach" in res.json()["detail"].lower()

    def test_create_user_rejects_breached(self, admin_client, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        new_pw = "GoodFresh123"
        with mock.patch.object(
            password_breach, "_fetch_range",
            return_value=self._force_match(new_pw),
        ):
            res = admin_client.post("/api/users", json={
                "name": "Tester", "email": "tester-breach@acme.test",
                "role": "member", "password": new_pw,
            })
        assert res.status_code == 400, res.text
        assert "breach" in res.json()["detail"].lower()

    def test_signup_rejects_breached(self, client, monkeypatch):
        """v2.8 enterprise extra: signup is a password-set path too,
        so it should also enforce the breach check."""
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        new_pw = "BreachedAtSignup1"
        with mock.patch.object(
            password_breach, "_fetch_range",
            return_value=self._force_match(new_pw),
        ):
            res = client.post("/api/auth/signup", json={
                "organization_name": "Bad-pw Co", "name": "Bob",
                "email": "bob@badpw.test", "password": new_pw,
            })
        assert res.status_code == 400, res.text
        assert "breach" in res.json()["detail"].lower()

    def test_change_password_accepts_safe_new_password(self, admin_client, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        with mock.patch.object(password_breach, "_fetch_range", return_value=""):
            res = admin_client.post("/api/auth/change-password", json={
                "current_password": ADMIN_PASSWORD,
                "new_password": "FreshSafe123",
            })
        assert res.status_code == 204


class TestPasswordBreachFetchRange:
    """Exercise the real ``_fetch_range`` body."""

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")

    @staticmethod
    def _patch_httpx_client(monkeypatch, response_status=200, response_text="",
                            raise_on_get=None):
        from app import password_breach

        class _FakeResponse:
            status_code = response_status
            text = response_text

        class _FakeClient:
            def __init__(self, **_kw):
                # No-op stand-in for httpx.Client(timeout=...).
                pass
            def __enter__(self): return self
            def __exit__(self, *_a): return False
            def get(self, _url, **_kw):
                if raise_on_get is not None:
                    raise raise_on_get
                return _FakeResponse()

        monkeypatch.setattr(password_breach.httpx, "Client", _FakeClient)

    def test_fetch_range_returns_text_on_200(self, monkeypatch):
        from app import password_breach
        self._patch_httpx_client(monkeypatch, 200, "ABCD:1\n")
        assert password_breach._fetch_range("5BAA6") == "ABCD:1\n"

    def test_fetch_range_returns_none_on_non_200(self, monkeypatch):
        from app import password_breach
        self._patch_httpx_client(monkeypatch, 503, "")
        assert password_breach._fetch_range("5BAA6") is None

    def test_fetch_range_returns_none_on_httperror(self, monkeypatch):
        import httpx
        from app import password_breach
        self._patch_httpx_client(
            monkeypatch, raise_on_get=httpx.HTTPError("simulated")
        )
        assert password_breach._fetch_range("5BAA6") is None

    def test_fetch_range_returns_none_on_oserror(self, monkeypatch):
        from app import password_breach
        self._patch_httpx_client(
            monkeypatch, raise_on_get=OSError("connection refused")
        )
        assert password_breach._fetch_range("5BAA6") is None


# ---------------------------------------------------------------------------
# T6 — EXIF strip
# ---------------------------------------------------------------------------
def _jpeg_with_exif(gps_value: str = "secret-gps-tag") -> bytes:
    from PIL import Image
    img = Image.new("RGB", (8, 8), (200, 100, 50))
    exif = img.getexif()
    exif[270] = gps_value  # 270 = ImageDescription tag
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def _png_with_text(text: str = "stash-this") -> bytes:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    img = Image.new("RGB", (8, 8), (10, 20, 30))
    meta = PngInfo()
    meta.add_text("Source", text)
    out = io.BytesIO()
    img.save(out, format="PNG", pnginfo=meta)
    return out.getvalue()


class TestExifStrip:

    def test_unit_jpeg_metadata_removed(self):
        from app.image_strip import strip_image_metadata
        marker = "GPS-LEAK-XY-ZZ"
        raw = _jpeg_with_exif(marker)
        assert marker.encode() in raw
        clean = strip_image_metadata(raw, "image/jpeg")
        assert marker.encode() not in clean

    def test_unit_png_metadata_removed(self):
        from app.image_strip import strip_image_metadata
        marker = "PNG-TEXT-LEAK-XY"
        raw = _png_with_text(marker)
        assert marker.encode() in raw
        clean = strip_image_metadata(raw, "image/png")
        assert marker.encode() not in clean

    def test_unit_non_image_passes_through(self):
        from app.image_strip import strip_image_metadata
        raw = b"%PDF-1.7\n%fake pdf bytes"
        assert strip_image_metadata(raw, "application/pdf") == raw

    def test_unit_unknown_content_type_passes_through(self):
        from app.image_strip import strip_image_metadata
        raw = b"<svg></svg>"
        assert strip_image_metadata(raw, "image/svg+xml") == raw

    def test_unit_garbage_image_passes_through(self):
        from app.image_strip import strip_image_metadata
        raw = b"this is definitely not a jpeg"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_unit_empty_bytes_returns_empty(self):
        from app.image_strip import strip_image_metadata
        assert strip_image_metadata(b"", "image/jpeg") == b""

    def test_http_upload_strips_jpeg_exif(self, admin_client):
        bug_id = _make_bug(admin_client, "EXIF carrier upload")
        marker = "FIELD-MARKER-9X"
        raw = _jpeg_with_exif(marker)
        assert marker.encode() in raw
        res = admin_client.post(
            f"/api/bugs/{bug_id}/attachments",
            files={"file": ("evidence.jpg", raw, "image/jpeg")},
        )
        assert res.status_code == 201, res.text
        att_id = res.json()["id"]
        dl = admin_client.get(f"/api/bugs/{bug_id}/attachments/{att_id}/download")
        assert dl.status_code == 200
        assert marker.encode() not in dl.content, (
            "EXIF marker survived the upload roundtrip"
        )


class TestImageStripEdgeCases:

    def test_pillow_missing_returns_original(self, monkeypatch):
        import sys
        from app.image_strip import strip_image_metadata
        monkeypatch.setitem(sys.modules, "PIL", None)
        raw = b"fake-jpeg-bytes"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_format_none_returns_original(self, monkeypatch):
        from PIL import Image as PILImage
        from app.image_strip import strip_image_metadata

        class _FakeImg:
            format = None
            info: dict = {}
            def load(self):
                # No-op stub; we only need format-is-None branching here.
                pass

        monkeypatch.setattr(PILImage, "open", lambda _src: _FakeImg())
        raw = b"any-bytes"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_save_oserror_returns_original(self, monkeypatch):
        from PIL import Image as PILImage
        from app.image_strip import strip_image_metadata

        class _BoomImg:
            format = "JPEG"
            info: dict = {}
            def load(self):
                # No-op stub — the failure under test is in save().
                pass
            def save(self, _out, **_kw): raise OSError("disk full mid-encode")

        monkeypatch.setattr(PILImage, "open", lambda _src: _BoomImg())
        raw = b"any-bytes"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_content_type_with_charset_param_still_handled(self):
        from app.image_strip import strip_image_metadata
        raw = b"not-a-jpeg"
        assert strip_image_metadata(raw, "image/jpeg; charset=binary") == raw
