"""Behavioral safety: cache-control headers, session invalidation, attachment serving,
LIKE-wildcard escaping, reporter permissions, filter case-insensitivity, and input validation.
"""
from __future__ import annotations

import io
import re
from pathlib import Path


def _make_user(c, name="Some User", email="user@x.com", role="user", password="TestUserPwd9X"):
    body = {"name": name, "email": email, "role": role, "password": password}
    # Tag to every project so non-admins keep the flat "see everything" model.
    pids = [p["id"] for p in c.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = c.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(c, name="P"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(c, project_id, title="A bug for tests", **extra):
    body = {"project_id": project_id, "title": title,
            "priority": "Medium", "environment": "DEV"}
    body.update(extra)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- Cache-Control middleware ---
class TestCacheControl:
    def test_html_pages_are_never_cached(self, client):
        r = client.get("/login.html")
        assert r.status_code == 200
        cc = r.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"login.html missing no-store: {cc!r}"

    def test_root_redirect_is_not_cached(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        cc = r.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"root redirect missing no-store: {cc!r}"

    def test_api_responses_are_not_cached(self, admin_client):
        r = admin_client.get("/api/health")
        assert r.status_code == 200
        cc = r.headers.get("Cache-Control", "")
        assert "no-store" in cc, f"/api/health missing no-store: {cc!r}"

    def test_static_assets_are_cached_long(self, client):
        # Long-cache applies to Vite's content-hashed bundles; pull a real hashed URL from index.html.
        index = (
            Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")
        m = re.search(r"/static/assets/[\w.-]+\.(?:js|css)", index)
        assert m, "no hashed asset URL found in built index.html"
        r = client.get(m.group(0))
        assert r.status_code == 200
        cc = r.headers.get("Cache-Control", "")
        assert "max-age=31536000" in cc, f"static asset not long-cached: {cc!r}"
        assert "immutable" in cc, f"static asset not marked immutable: {cc!r}"

    def test_health_exposes_asset_version(self, client):
        r = client.get("/api/health")
        body = r.json()
        assert "asset_version" in body
        assert isinstance(body["asset_version"], str)
        assert len(body["asset_version"]) > 0

    def test_html_contains_asset_version_in_static_urls(self, client):
        r = client.get("/login.html")
        body = r.text
        assert "__ASSET_VERSION__" not in body, "asset version placeholder leaked into HTML"
        # Cache-busting: hashed bundle filenames + server-substituted ?v= on the favicon.
        assert "/static/assets/login-" in body, "hashed login bundle not referenced"
        assert "/static/icon.png?v=" in body, "favicon asset-version query missing"

    def test_html_url_changes_when_asset_version_changes(self, client):
        # Mutate asset_version between requests; the HTML must reflect each value.
        from app.main import app
        original_version = app.state.asset_version
        try:
            app.state.asset_version = "before123"
            r1 = client.get("/login.html")
            app.state.asset_version = "after4567"
            r2 = client.get("/login.html")
            assert "?v=before123" in r1.text
            assert "?v=after4567" in r2.text
            assert "?v=before123" not in r2.text
        finally:
            app.state.asset_version = original_version


# --- Session invalidation on password change / reset ---
class TestSessionInvalidation:
    def test_password_change_invalidates_other_sessions(self, admin_client, client):
        """Device A's password change kicks device B out on its next request."""
        # admin_client is device A; device_b gets its own client + cookie.
        from fastapi.testclient import TestClient

        from app.main import app

        device_b = TestClient(app)
        r = device_b.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        assert r.status_code == 200
        assert device_b.get("/api/auth/me").status_code == 200

        # Device A changes the password.
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234",
            "new_password": "NewBetter999",
        })
        assert r.status_code == 204

        # Device A's cookie is re-issued, so it stays logged in.
        assert admin_client.get("/api/auth/me").status_code == 200

        # Device B's old cookie must be invalid now.
        r = device_b.get("/api/auth/me")
        assert r.status_code == 401, \
            "session NOT invalidated for other devices on password change"

    def test_password_change_with_wrong_current_returns_400(self, admin_client):
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "wrong", "new_password": "Whatever123",
        })
        assert r.status_code == 400

    def test_password_change_with_empty_current_returns_422(self, admin_client):
        """Empty current_password is rejected by the schema (min_length=1) before bcrypt."""
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "", "new_password": "Whatever123",
        })
        assert r.status_code == 422

    def test_admin_password_reset_invalidates_target_user_sessions(self, admin_client):
        """Admin resetting another user's password invalidates that user's sessions."""
        from fastapi.testclient import TestClient

        from app.main import app
        u = _make_user(admin_client, "Victim", "victim@x.com", password="OldPass11")
        victim = TestClient(app)
        r = victim.post("/api/auth/login", json={
            "email": "victim@x.com", "password": "OldPass11",
        })
        assert r.status_code == 200
        assert victim.get("/api/auth/me").status_code == 200
        # Admin resets victim's password.
        r = admin_client.put(f"/api/users/{u['id']}", json={"password": "NewPass22"})
        assert r.status_code == 200
        assert victim.get("/api/auth/me").status_code == 401

    def test_admin_deactivation_invalidates_user_sessions(self, admin_client):
        from fastapi.testclient import TestClient

        from app.main import app
        u = _make_user(admin_client, "Sleepy", "sleepy@x.com", password="ZzzPass11")
        sleepy = TestClient(app)
        sleepy.post("/api/auth/login", json={
            "email": "sleepy@x.com", "password": "ZzzPass11",
        })
        assert sleepy.get("/api/auth/me").status_code == 200
        # Deactivating the account must immediately invalidate the session.
        admin_client.put(f"/api/users/{u['id']}", json={"is_active": False})
        assert sleepy.get("/api/auth/me").status_code == 401


# --- XSS-safe attachment serving ---
class TestAttachmentSafety:
    def test_html_attachment_forced_to_octet_stream(self, admin_client):
        p = _make_project(admin_client, "ATX-1")
        bug = _make_bug(admin_client, p["id"])
        evil = b"<html><script>alert(1)</script></html>"
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("evil.html", io.BytesIO(evil), "text/html")},
        )
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert d.status_code == 200
        ct = d.headers.get("content-type", "")
        cd = d.headers.get("content-disposition", "")
        # Active content must be downgraded to octet-stream and forced to attachment.
        assert ct.startswith("application/octet-stream"), f"unsafe ct: {ct!r}"
        assert "attachment" in cd.lower(), f"not forced to attachment: {cd!r}"
        assert d.headers.get("X-Content-Type-Options") == "nosniff"
        assert "default-src 'none'" in (d.headers.get("Content-Security-Policy") or "")
        assert d.headers.get("X-Frame-Options") == "DENY"

    def test_svg_attachment_forced_to_octet_stream(self, admin_client):
        p = _make_project(admin_client, "ATX-2")
        bug = _make_bug(admin_client, p["id"])
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("img.svg", io.BytesIO(svg), "image/svg+xml")},
        )
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        ct = d.headers.get("content-type", "")
        cd = d.headers.get("content-disposition", "")
        assert ct.startswith("application/octet-stream")
        assert "attachment" in cd.lower()

    def test_javascript_attachment_forced_to_octet_stream(self, admin_client):
        p = _make_project(admin_client, "ATX-3")
        bug = _make_bug(admin_client, p["id"])
        js = b"alert(document.cookie)"
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("evil.js", io.BytesIO(js), "application/javascript")},
        )
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert d.headers.get("content-type", "").startswith("application/octet-stream")
        assert "attachment" in (d.headers.get("content-disposition") or "").lower()

    def test_safe_image_still_served_inline(self, admin_client):
        p = _make_project(admin_client, "ATX-4")
        bug = _make_bug(admin_client, p["id"])
        # 8-byte PNG magic suffices; we only check the type is preserved for non-active formats.
        png = b"\x89PNG\r\n\x1a\n"
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("ok.png", io.BytesIO(png), "image/png")},
        )
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        # PNG is not active content, so the type is kept as-is and served inline.
        assert d.headers.get("content-type", "").startswith("image/png")
        assert "inline" in (d.headers.get("content-disposition") or "").lower()

    def test_filename_with_quotes_does_not_break_header(self, admin_client):
        """A double-quote/CRLF in a filename must be sanitised out of Content-Disposition."""
        p = _make_project(admin_client, "ATX-5")
        bug = _make_bug(admin_client, p["id"])
        bad_name = 'inj"ect\r\nX-Evil: 1.txt'
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": (bad_name, io.BytesIO(b"hi"), "text/plain")},
        )
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert d.status_code == 200
        cd = d.headers.get("content-disposition", "")
        # CR, LF, and quotes must be stripped to prevent header injection.
        assert "\r" not in cd
        assert "\n" not in cd
        assert d.headers.get("X-Evil") is None

    def test_download_advertises_accept_ranges(self, admin_client):
        """Full (200) response advertises Accept-Ranges so the browser will seek."""
        p = _make_project(admin_client, "ATX-6")
        bug = _make_bug(admin_client, p["id"])
        blob = bytes(range(256)) * 4  # 1024 bytes
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("clip.mp4", io.BytesIO(blob), "video/mp4")},
        )
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert d.status_code == 200
        assert d.headers.get("Accept-Ranges") == "bytes"
        assert d.headers.get("Content-Length") == str(len(blob))
        assert d.content == blob

    def test_download_serves_partial_range(self, admin_client):
        """A Range request returns 206 with the exact slice and a correct Content-Range."""
        p = _make_project(admin_client, "ATX-7")
        bug = _make_bug(admin_client, p["id"])
        blob = bytes(range(256)) * 4  # 1024 bytes
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("clip.mp4", io.BytesIO(blob), "video/mp4")},
        )
        att_id = r.json()["id"]
        url = f"/api/bugs/{bug['id']}/attachments/{att_id}/download"

        # Mid-file range.
        d = admin_client.get(url, headers={"Range": "bytes=100-199"})
        assert d.status_code == 206
        assert d.headers.get("Content-Range") == f"bytes 100-199/{len(blob)}"
        assert d.headers.get("Content-Length") == "100"
        assert d.headers.get("Accept-Ranges") == "bytes"
        assert d.content == blob[100:200]

        # Open-ended range (start to EOF).
        d2 = admin_client.get(url, headers={"Range": "bytes=1000-"})
        assert d2.status_code == 206
        assert d2.headers.get("Content-Range") == f"bytes 1000-{len(blob) - 1}/{len(blob)}"
        assert d2.content == blob[1000:]

        # Suffix range (last N bytes).
        d3 = admin_client.get(url, headers={"Range": "bytes=-50"})
        assert d3.status_code == 206
        assert d3.content == blob[-50:]

    def test_download_unsatisfiable_or_malformed_range_serves_full(self, admin_client):
        """A malformed or out-of-bounds Range falls back to the full 200 body."""
        p = _make_project(admin_client, "ATX-8")
        bug = _make_bug(admin_client, p["id"])
        blob = b"0123456789"
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("clip.mp4", io.BytesIO(blob), "video/mp4")},
        )
        att_id = r.json()["id"]
        url = f"/api/bugs/{bug['id']}/attachments/{att_id}/download"

        for bad in ("bytes=999-1500", "bytes=abc-def", "kilobytes=0-1", "bytes=5-2", "bytes=-"):
            d = admin_client.get(url, headers={"Range": bad})
            assert d.status_code == 200, bad
            assert d.content == blob, bad
            assert d.headers.get("Accept-Ranges") == "bytes"


# --- LIKE-wildcard escape ---
class TestLikeWildcardEscape:
    def test_user_search_with_percent_does_not_match_everyone(self, admin_client):
        _make_user(admin_client, "Alpha", "alpha@x.com")
        _make_user(admin_client, "Beta", "beta@x.com")
        # No user has a literal '%' in their name, so the result must be empty.
        r = admin_client.get("/api/users?q=%25")
        users = r.json()
        assert len(users) == 0, f"LIKE wildcard not escaped: q=% returned {len(users)} users"

    def test_user_search_with_underscore_does_not_match_everyone(self, admin_client):
        _make_user(admin_client, "ZetaName", "zeta@x.com")
        # No user has a literal '_' in their name, so the result must be empty.
        r = admin_client.get("/api/users?q=_")
        users = r.json()
        assert len(users) == 0, f"LIKE wildcard not escaped: q=_ returned {len(users)} users"

    def test_user_search_with_literal_percent_in_name_works(self, admin_client):
        # A name that actually contains '%' should be findable by exact search.
        _make_user(admin_client, "100% Smith", "p1@x.com")
        _make_user(admin_client, "Plain Doe", "p2@x.com")
        r = admin_client.get("/api/users?q=%25")  # %25 = literal '%'
        users = r.json()
        names = {u["name"] for u in users}
        assert "100% Smith" in names
        assert "Plain Doe" not in names

    def test_bug_search_with_underscore_does_not_match_everything(self, admin_client):
        p = _make_project(admin_client, "WL-1")
        _make_bug(admin_client, p["id"], title="No special chars here")
        r = admin_client.get("/api/bugs?q=_")
        items = r.json()["items"]
        assert len(items) == 0, "LIKE wildcard not escaped in bug search"


# --- Owner can save their own bug ---
class TestReporterPermissionFix:
    def test_user_can_save_own_bug_with_unchanged_reporter_id(self, admin_client):
        p = _make_project(admin_client, "RP-1")
        owner = _make_user(admin_client, "Owner", "owner@x.com", password="TestUserPwd9X")
        bug = _make_bug(admin_client, p["id"], reporter_id=owner["id"])
        # The SPA echoes reporter_id back; the owner re-sending the same value must be allowed.
        admin_client.post("/api/auth/logout")
        admin_client.post("/api/auth/login", json={
            "email": "owner@x.com", "password": "TestUserPwd9X",
        })
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "title": "Owner edits their own bug now",
            "reporter_id": owner["id"],   # unchanged reporter — must be allowed
            "priority": "High",
        })
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "Owner edits their own bug now"
        assert r.json()["priority"] == "High"

    def test_user_cannot_change_reporter_to_someone_else(self, admin_client):
        p = _make_project(admin_client, "RP-2")
        owner = _make_user(admin_client, "Owner", "owner2@x.com", password="TestUserPwd9X")
        other = _make_user(admin_client, "Other", "other@x.com", password="TestUserPwd9X")
        bug = _make_bug(admin_client, p["id"], reporter_id=owner["id"])
        admin_client.post("/api/auth/logout")
        admin_client.post("/api/auth/login", json={
            "email": "owner2@x.com", "password": "TestUserPwd9X",
        })
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": other["id"]})
        assert r.status_code == 403


# --- Case-insensitive filters ---
class TestFilterCaseInsensitive:
    def test_status_filter_matches_lowercase(self, admin_client):
        p = _make_project(admin_client, "CF-1")
        _make_bug(admin_client, p["id"], title="filter test")
        r = admin_client.get("/api/bugs?status=new")
        items = r.json()["items"]
        assert any(b["title"] == "filter test" for b in items)

    def test_priority_filter_matches_uppercase(self, admin_client):
        p = _make_project(admin_client, "CF-2")
        _make_bug(admin_client, p["id"], title="prio test", priority="High")
        r = admin_client.get("/api/bugs?priority=HIGH")
        items = r.json()["items"]
        assert any(b["title"] == "prio test" for b in items)

    def test_invalid_status_filter_returns_400(self, admin_client):
        r = admin_client.get("/api/bugs?status=NotAStatus")
        assert r.status_code == 400


# --- Activity ordering consistent ---
class TestActivityOrderingFix:
    def test_activity_order_consistent_between_endpoints(self, admin_client):
        p = _make_project(admin_client, "AO-1")
        bug = _make_bug(admin_client, p["id"])
        for s in ("In Progress", "Resolved", "Closed", "Reopened"):
            admin_client.put(f"/api/bugs/{bug['id']}", json={"status": s})
        detail = admin_client.get(f"/api/bugs/{bug['id']}").json()
        activity = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()
        d_ids = [a["id"] for a in detail["activities"]]
        a_ids = [a["id"] for a in activity]
        assert d_ids == a_ids, f"detail={d_ids} vs activity={a_ids}"


# --- Description-only update persists ---
class TestDescriptionOnlyUpdate:
    def test_description_only_update_persists(self, admin_client):
        p = _make_project(admin_client, "DO-1")
        bug = _make_bug(admin_client, p["id"], description="initial")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": "changed"})
        assert r.status_code == 200
        assert r.json()["description"] == "changed"

    def test_description_to_empty_persists(self, admin_client):
        p = _make_project(admin_client, "DO-2")
        bug = _make_bug(admin_client, p["id"], description="initial")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": ""})
        assert r.status_code == 200
        assert r.json()["description"] == ""

    def test_description_change_creates_activity(self, admin_client):
        p = _make_project(admin_client, "DO-3")
        bug = _make_bug(admin_client, p["id"], description="initial")
        admin_client.put(f"/api/bugs/{bug['id']}", json={"description": "changed"})
        acts = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()
        descs = [a for a in acts if a["action"] == "description_changed"]
        assert len(descs) == 1


# --- Search with whitespace ---
class TestSearchWhitespace:
    def test_search_strips_whitespace(self, admin_client):
        p = _make_project(admin_client, "SW-1")
        _make_bug(admin_client, p["id"], title="findable thing here")
        # Leading/trailing whitespace should be stripped before the search.
        r = admin_client.get("/api/bugs?q=  findable  ")
        items = r.json()["items"]
        assert any("findable" in b["title"] for b in items)


# --- Title min-length after strip ---
class TestTitleStripMinLength:
    def test_title_with_padded_whitespace_below_min_length_rejected(self, admin_client):
        p = _make_project(admin_client, "TS-1")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "  a  ",
            "priority": "Low", "environment": "DEV",
        })
        assert r.status_code == 422

    def test_project_name_padded_whitespace_below_min_rejected(self, admin_client):
        r = admin_client.post("/api/projects", json={"name": "  X  "})
        assert r.status_code == 422

    def test_user_name_padded_whitespace_below_min_rejected(self, admin_client):
        r = admin_client.post("/api/users", json={
            "name": " A ", "email": "shorty@x.com", "role": "user", "password": "TestUserPwd9X",
        })
        assert r.status_code == 422


# --- Reset-token invalidation on password change ---
class TestResetTokenInvalidation:
    def test_password_change_audits_token_invalidation(self, admin_client):
        admin_client.post("/api/auth/forgot-password", json={"email": "admin@test.local"})
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234", "new_password": "Whatever123",
        })
        assert r.status_code == 204
        rows = admin_client.get("/api/audit?q=password_changed").json()
        assert rows, "no password_changed audit row"
        # The audit detail should confirm that outstanding reset links were invalidated.
        recent = [r for r in rows if r["action"] == "password_changed"]
        assert recent
        assert "reset link" in recent[0]["detail"].lower() or \
               "invalidated" in recent[0]["detail"].lower()
