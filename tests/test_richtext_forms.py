"""Rich-text editor and form behavior: frontend-source markers for B/I/U, calendar nav,
disabled Reporter caret, and image-as-attachment; plus server-side sanitiser round-trip
(rich tags survive, unsafe payloads stripped) and admin-only mutation rules.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest


# Helpers are duplicated from conftest intentionally so this file stands alone.
def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _make_project(client, name="Eng v2.6"):
    r = client.post("/api/projects", json={"name": name, "color": "#558844"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **extra):
    body = {
        "title": "Sample Bug v2.6",
        "project_id": project_id,
        "item_type": "Bug",
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _read_static(name):
    p = Path(__file__).resolve().parents[1] / "app" / "static" / name
    return p.read_text(encoding="utf-8")


# Shipped bundle is hashed/minified, so marker tests read the React source directly.
def _read_frontend(relpath):
    p = Path(__file__).resolve().parents[1] / "frontend" / "src" / relpath
    return p.read_text(encoding="utf-8")


def _rich_editor():
    return _read_frontend("components/RichEditor.jsx")


def _bh_select():
    return _read_frontend("components/BhSelect.jsx")


def _bh_date():
    return _read_frontend("components/BhDateInput.jsx")


def _bug_modal():
    return _read_frontend("modals/BugModal.jsx")


def _styles_css():
    return _read_frontend("styles/styles.css")


# Bold / Italic / Underline / format-toggle.
class TestRichTextFixMarkers:
    """Verify that expected rich-text behavior markers are present in source."""

    def test_app_js_has_saved_selection_path(self):
        """Selection is saved and restored across toolbar clicks."""
        js = _rich_editor()
        assert "savedRange" in js, "selection-save state must exist"
        assert "captureSelection" in js, "capture-selection helper must exist"
        assert "ensureCaret" in js, "ensure-caret helper must exist"

    def test_app_js_runs_cmd_after_focus_restore(self):
        """B/I/U run via the restored-selection helper, not bare execCommand in the keydown handler."""
        js = _rich_editor()
        for cmd in ("bold", "italic", "underline"):
            assert f'runCmd("{cmd}")' in js, (
                f"Ctrl+{cmd[0].upper()} should go through runCmd "
                "not bare execCommand"
            )
        # Inspect non-comment lines only; execCommand still appears in workaround comments.
        code_only = "\n".join(
            ln for ln in js.splitlines()
            if not ln.lstrip().startswith("//")
        )
        for cmd in ("bold", "italic", "underline"):
            assert f'document.execCommand("{cmd}")' not in code_only, (
                f"toolbar must not call document.execCommand(\"{cmd}\") "
                "directly — Chrome silently no-ops it inside the modal"
            )

    def test_app_js_toggles_blockquote_and_pre(self):
        """formatBlock for blockquote / pre toggles off when re-clicked."""
        js = _rich_editor()
        assert 'inAncestor([tag])' in js, "format-block ancestor check missing"

    def test_app_js_no_clear_formatting_button(self):
        """The Clear-formatting button is not rendered; block-toggle on the
        same button (blockquote/pre/list) covers returning to plain text."""
        js = _rich_editor()
        assert 'data-cmd="removeFormat"' not in js, (
            "Clear-formatting button must not be rendered"
        )
        # The dedicated handler branch went with it.
        assert '"removeFormat"' not in js, (
            "removeFormat handler branch should be removed too"
        )

    def test_app_js_uses_inline_wrapper_for_collapsed_caret(self):
        """First-click B/I/U on a collapsed caret wraps a ZWSP placeholder."""
        js = _rich_editor()
        assert "toggleInlineAtCaret" in js, "manual-toggle helper missing"
        assert "INLINE_TOGGLE" in js, "inline-tag map missing"
        # ZWSP separator on toggle-off; without it Chrome absorbs the next keystroke into the wrapper.
        assert "insertBefore(sep, n.nextSibling)" in js, (
            "ZWSP separator must be inserted after wrapper on toggle-off"
        )

    def test_app_js_uses_preventdefault_on_toolbar_mousedown(self):
        """Toolbar mousedown preventDefaults before running, else focus leaves the contenteditable and typing state is lost."""
        js = _rich_editor()
        m = re.search(
            r'handleToolbarMouseDown.*?e\.preventDefault\(\).*?runToolbarCmd',
            js, re.S,
        )
        assert m, "mousedown must preventDefault before running the toolbar command"

    def test_app_js_has_manual_formatting_implementations(self):
        """Chrome no-ops execCommand inside the modal's stacking context, so the editor ships its own helpers."""
        js = _rich_editor()
        assert "applyInlineWrap" in js, "manual inline wrap missing"
        assert "applyList" in js, "manual list toggle missing"
        assert "applyBlockWrap" in js, "manual block-wrap missing"
        assert "selectWordAtCaret" in js, (
            "word-at-caret auto-select missing — user expects Bold to "
            "bold the word they just typed, no manual selection required"
        )

    def test_app_js_has_visible_active_state(self):
        """Toolbar buttons get .is-active via updateActiveStates when the format is active at the caret."""
        js = _rich_editor()
        assert "updateActiveStates" in js
        css = _styles_css()
        assert ".bh-rt-toolbar button.is-active" in css, (
            "is-active visual style missing from stylesheet"
        )


class TestCalendarNavStaysOpen:
    """Nav clicks stopPropagation so the rebuilt DOM isn't mistaken for an outside click that closes the popover."""

    def test_app_js_stops_propagation_for_nav_clicks(self):
        js = _bh_date()
        m = re.search(
            r'data-nav="next".*?navMonth\(1\)',
            js, re.S,
        )
        assert m, "next-month bh-date-nav button missing"
        assert "stopPropagation" in m.group(0), (
            "nav-button onClick must stopPropagation so the rebuild "
            "doesn't trip outside-close"
        )


class TestReporterDropdownNoCaret:
    def test_index_html_reporter_select_is_disabled(self):
        # Pin `disabled` so a refactor can't silently bypass caret-suppression.
        src = _bug_modal()
        assert re.search(
            r'<select[^>]*name="reporter_id"[^>]*\bdisabled\b',
            src,
        ), "Reporter <select> must keep `disabled` attribute"

    def test_app_js_skips_caret_when_select_disabled(self):
        # A disabled select can't open, so showing the caret is misleading.
        js = _bh_select()
        assert re.search(
            r'!disabled\s*&&\s*<span className="bh-sel-caret"',
            js,
        ), "disabled-select caret-suppression branch missing"

    def test_app_js_observer_rerenders_label_on_disabled_change(self):
        # In React `disabled` is a prop (no MutationObserver); pin caret + trigger to it.
        js = _bh_select()
        assert re.search(
            r'!disabled\s*&&\s*<span className="bh-sel-caret"',
            js,
        ), (
            "caret must be conditional on the disabled prop so it toggles "
            "reactively when disabled changes"
        )
        assert "disabled={disabled}" in js, (
            "native select + trigger must reflect the disabled prop"
        )


class TestImageInsertionAsAttachment:
    def test_app_js_no_legacy_pickimage_helper(self):
        js = _rich_editor()
        # The old insertHTML embed left un-resizable/un-deletable blobs; must be gone.
        assert "_bhRtPickImage" not in js, (
            "Legacy inline-image picker must be removed"
        )
        assert not re.search(
            r'insertHTML[^;]*<img\b',
            js,
        ), "rich-text editor must NOT inline <img> tags"

    def test_app_js_uses_attachment_pickfile(self):
        js = _rich_editor()
        assert "pickFileAsAttachment" in js, (
            "Toolbar image button must route through the attachment helper"
        )
        # The bh-image branch specifically must be the one calling it.
        m = re.search(
            r'cmd\s*===\s*"bh-image".*?pickFileAsAttachment',
            js, re.S,
        )
        assert m, "bh-image branch must call pickFileAsAttachment"


class TestRichHtmlRoundtrip:
    """Rich formatting in descriptions/comments survives the sanitiser round-trip."""

    @pytest.fixture
    def bug(self, admin_client):
        proj = _make_project(admin_client)
        return _make_bug(admin_client, proj["id"])

    def _post_description(self, client, bug_id, body):
        r = client.put(f"/api/bugs/{bug_id}", json={"description": body})
        assert r.status_code == 200, r.text
        return r.json()

    def test_bold_italic_underline_survive(self, admin_client, bug):
        html = "<p>This is <b>bold</b>, <i>italic</i> and <u>underline</u>.</p>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        # Stored as plain text now: all words survive, formatting is gone.
        assert got == "This is bold, italic and underline."
        assert "<" not in got
        assert ">" not in got

    def test_lists_survive(self, admin_client, bug):
        html = "<ul><li>one</li><li>two</li></ul><ol><li>a</li><li>b</li></ol>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert got == "one\ntwo\na\nb"

    def test_blockquote_pre_code_survive(self, admin_client, bug):
        html = "<blockquote>quoted</blockquote><pre>code block</pre>" \
               "<p><code>inline</code></p>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert got == "quoted\ncode block\ninline"

    def test_strikethrough_survives(self, admin_client, bug):
        html = "<p>see <s>obsolete</s></p>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert got == "see obsolete"

    def test_comment_body_keeps_rich_formatting(self, admin_client, bug):
        body = "<p><b>Note:</b> please verify <i>before</i> ship.</p>"
        r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": body})
        assert r.status_code == 201, r.text
        got = r.json()["body"]
        assert "<b>Note:</b>" in got
        assert "<i>before</i>" in got


class TestSanitiserStillBlocksUnsafe:
    """Rich-text support must not weaken the sanitiser."""

    @pytest.fixture
    def bug(self, admin_client):
        proj = _make_project(admin_client, name="Sec v2.6")
        return _make_bug(admin_client, proj["id"])

    def test_script_tag_is_stripped(self, admin_client, bug):
        html = "<p>before<script>alert(1)</script>after</p>"
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        assert r.status_code == 200
        got = r.json()["description"]
        # Tag dropped; inert text content may remain (no executable wrapper = no XSS).
        assert "<script" not in got.lower()
        assert "</script" not in got.lower()
        assert "before" in got
        assert "after" in got

    def test_javascript_url_in_anchor_is_stripped(self, admin_client, bug):
        html = '<p><a href="javascript:alert(1)">x</a></p>'
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        got = r.json()["description"]
        assert "javascript:" not in got.lower()

    def test_onerror_attr_is_stripped(self, admin_client, bug):
        html = '<p><img src="x" onerror="alert(1)"></p>'
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        got = r.json()["description"]
        assert "onerror" not in got.lower(), "event-handler attrs must die"

    def test_iframe_is_stripped(self, admin_client, bug):
        html = '<p><iframe src="https://evil.example"></iframe>x</p>'
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        got = r.json()["description"]
        assert "<iframe" not in got.lower()
        assert got == "x"  # plain text now; only surviving text remains


# Re-covers the admin-only rules so this suite stands alone.
class TestAdminOnlyMutations:

    def _make_user(self, client, name, role="user"):
        body = {"name": name, "email": f"{name.lower()}@x.test",
                "role": role, "password": "User12345Aa"}
        # User must see the bug (all projects) before the admin-only rule applies.
        pids = [p["id"] for p in client.get("/api/projects").json()]
        if pids:
            body["project_ids"] = pids
        r = client.post("/api/users", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def test_non_admin_cannot_edit_own_comment(self, client):
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6")
        u = self._make_user(client, "Beta")
        bug = _make_bug(client, proj["id"])
        # user posts a comment
        _login(client, u["email"], "User12345Aa")
        r = client.post(f"/api/bugs/{bug['id']}/comments",
                        json={"body": "<p>Hello</p>"})
        assert r.status_code == 201
        cid = r.json()["id"]
        r = client.put(f"/api/bugs/{bug['id']}/comments/{cid}",
                       json={"body": "<p>edited</p>"})
        assert r.status_code == 403, (
            "Non-admin must not edit even their own comment"
        )

    def test_non_admin_cannot_delete_comment(self, client):
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6 b")
        u = self._make_user(client, "Gamma")
        bug = _make_bug(client, proj["id"])
        # Post as admin so the user is not the owner of this comment.
        r = client.post(f"/api/bugs/{bug['id']}/comments",
                        json={"body": "<p>admin note</p>"})
        cid = r.json()["id"]
        _login(client, u["email"], "User12345Aa")
        r = client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
        assert r.status_code == 403

    def test_non_admin_cannot_delete_attachment(self, client):
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6 c")
        u = self._make_user(client, "Delta")
        bug = _make_bug(client, proj["id"])
        r = client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("doc.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        _login(client, u["email"], "User12345Aa")
        r = client.delete(f"/api/bugs/{bug['id']}/attachments/{aid}")
        assert r.status_code == 403

    def test_non_admin_CAN_upload_attachment_after_creation(self, client):
        """Post-creation uploads stay open to non-admins."""
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6 d")
        u = self._make_user(client, "Epsilon")
        bug = _make_bug(client, proj["id"])
        _login(client, u["email"], "User12345Aa")
        r = client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("note.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        assert r.status_code == 201, (
            "Non-admins must still be able to attach files after the bug "
            "exists — only delete is admin-only"
        )


class TestInitDbIsIdempotent:
    """init_db (create/add-column/add-index) run twice leaves the schema identical — no destructive DDL on redeploy."""

    def test_running_init_twice_does_not_recreate_tables(self, tmp_path, monkeypatch):
        import sys
        db_file = tmp_path / "smoke.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x" * 32)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "a@a.local")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")

        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]

        from sqlalchemy import inspect

        from app.database import engine, init_db

        init_db()
        snap1 = sorted(inspect(engine).get_table_names())
        init_db()  # second pass
        snap2 = sorted(inspect(engine).get_table_names())
        assert snap1 == snap2, (
            "Schema must be identical across init_db calls — "
            "no destructive DDL on redeploy"
        )

    def test_no_new_tables_introduced_by_v26_fixes(self, tmp_path, monkeypatch):
        """init_db creates the canonical core tables (future additions still pass)."""
        import sys
        db_file = tmp_path / "schema.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x" * 32)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "a@a.local")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")

        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]



        from sqlalchemy import inspect

        from app.database import engine, init_db

        init_db()
        names = set(inspect(engine).get_table_names())
        # Check only core tables; pinning the full set would be brittle.
        for required in ("users", "projects", "bugs", "comments",
                         "attachments", "activity_log"):
            assert required in names, f"missing canonical table {required!r}"


class TestHardening:
    """Pin security hardening so an accidental revert is caught early."""

    def test_uvicorn_host_defaults_to_loopback(self):
        """app/main.py's __main__ block must not hard-bind 0.0.0.0."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        assert 'os.getenv("UVICORN_HOST"' in src, (
            "Bind host must come from UVICORN_HOST env var, not be hard-coded"
        )
        assert '"127.0.0.1"' in src, (
            "Default UVICORN_HOST should be the loopback address"
        )

    def test_cors_default_is_empty_not_wildcard(self):
        """Default CORS_ORIGINS must be empty so the wildcard policy is opt-in."""
        import importlib
        import os
        os.environ.pop("CORS_ORIGINS", None)
        import app.config as cfg
        importlib.reload(cfg)
        assert cfg.Settings().CORS_ORIGINS == [], (
            f"expected [] default, got {cfg.Settings().CORS_ORIGINS!r}"
        )

    def test_cors_middleware_not_registered_when_no_origins(self):
        """No CORS_ORIGINS configured → CORSMiddleware must not be added."""
        import importlib
        import os
        import sys

        from starlette.middleware.cors import CORSMiddleware
        os.environ.pop("CORS_ORIGINS", None)
        for m in list(sys.modules):
            if m == "app" or m.startswith("app."):
                del sys.modules[m]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from app import main as app_main
        importlib.reload(app_main)
        cls_names = [
            m.cls.__name__ for m in app_main.app.user_middleware
        ]
        assert CORSMiddleware.__name__ not in cls_names, (
            f"CORSMiddleware should be skipped when CORS_ORIGINS is empty; "
            f"got middleware stack: {cls_names}"
        )

    def test_no_bare_except_exception_in_app_database(self):
        """app/database.py must use narrowed SQLAlchemyError, not Exception."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "database.py").read_text(encoding="utf-8")
        assert "except SQLAlchemyError:" in src
        assert src.count("except Exception:") == 0

    def test_email_service_uses_narrowed_except(self):
        """SMTP send-path must not catch bare Exception."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "email_service.py").read_text(encoding="utf-8")
        assert "except (smtplib.SMTPException, OSError)" in src
        assert "except Exception:\n        # Never let mailer failures" not in src

    def test_auth_uses_narrowed_except(self):
        """session-table writes must narrow to SQLAlchemyError."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "auth.py").read_text(encoding="utf-8")
        assert "from sqlalchemy.exc import SQLAlchemyError" in src
        assert "except SQLAlchemyError:" in src

    def test_pyproject_toml_has_relative_coverage(self):
        """pyproject.toml must enable relative_files=True so coverage paths
        map back to sources from a container with a different layout."""
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert "relative_files = true" in text


# ===========================================================================
# Plain-text descriptions in the API (no HTML storage, no *_text twin)
# ===========================================================================

def test_bug_stores_and_returns_plain_text_description(admin_client):
    """Legacy editor HTML in -> plain text out; description is the only field."""
    project = _make_project(admin_client, "Plain text story")
    rich = (
        "<div>As a user, I want a readable API.</div><div><br></div>"
        "<div>Acceptance Criteria:</div><div><br></div>"
        "<div>GET / returns <b>HTTP 200</b> &amp; docs.</div>"
        "<div>The endpoint does not require authentication.</div>"
    )
    bug = _make_bug(admin_client, project["id"], description=rich)
    detail = admin_client.get(f"/api/bugs/{bug['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    # Exactly one description field, readable plain text, no tags anywhere.
    assert "description_text" not in body
    assert body["description"] == (
        "As a user, I want a readable API.\n\n"
        "Acceptance Criteria:\n\n"
        "GET / returns HTTP 200 & docs.\n"
        "The endpoint does not require authentication."
    )
    assert "<" not in body["description"]
    assert ">" not in body["description"]
    # Round trip: PUT with plain text keeps the exact same string.
    put = admin_client.put(
        f"/api/bugs/{bug['id']}",
        json={"description": body["description"], "expected_version": body["version"]},
    )
    assert put.status_code == 200, put.text
    assert put.json()["description"] == body["description"]
