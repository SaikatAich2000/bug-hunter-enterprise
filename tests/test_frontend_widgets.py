"""Source-level guards for frontend behavior TestClient can't reach.
Chip checkbox, delete-attachment type, files-only comment reject, theme tokens, app version.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Pin fix-markers in the readable React source (the built bundle is hashed/minified).
FRONTEND = REPO_ROOT / "frontend" / "src"
RICH_EDITOR = FRONTEND / "components" / "RichEditor.jsx"
BUG_MODAL = FRONTEND / "modals" / "BugModal.jsx"
BUG_HELPERS = FRONTEND / "modals" / "bug" / "helpers.jsx"
REPORTS_VIEW = FRONTEND / "views" / "ReportsView.jsx"
SIDEBAR = FRONTEND / "shell" / "Sidebar.jsx"
# NAV_ITEMS is shared by TopChrome and Sidebar so the two nav surfaces can't drift.
TOPCHROME = FRONTEND / "shell" / "TopChrome.jsx"
NAVITEMS = FRONTEND / "shell" / "navItems.js"
TYPES = FRONTEND / "types.js"
FORMAT_TS = FRONTEND / "lib" / "format.js"
STYLES_CSS = FRONTEND / "styles" / "styles.css"


# Application branding
def test_config_app_branding_comes_from_environment(monkeypatch):
    """Product name and version have no code-level fallback values."""
    import importlib

    import dotenv

    import app.config as config

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("APP_NAME", "Test Tracker")
    monkeypatch.setenv("APP_VERSION", "9.9")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.get_settings().APP_NAME == "Test Tracker"
        assert reloaded.get_settings().APP_VERSION == "9.9"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# Delete-attachment button must not submit the bug form
def test_delete_attachment_button_has_type_button():
    """A bare button in a form defaults to submit; type=button stops delete from saving the bug."""
    src = BUG_HELPERS.read_text(encoding="utf-8")
    assert 'data-act="delete-attachment"' in src
    needle = 'data-act="delete-attachment"'
    pos = 0
    occurrences = 0
    while True:
        idx = src.find(needle, pos)
        if idx == -1:
            break
        # Walk back to the opening <button and confirm type="button" appears before data-act.
        start = src.rfind("<button", 0, idx)
        assert start != -1, "delete-attachment is not inside a <button>"
        btn = src[start:idx]
        assert 'type="button"' in btn, (
            f"delete-attachment button at offset {idx} lacks "
            f'type="button"; context: {btn!r}'
        )
        occurrences += 1
        pos = idx + len(needle)
    assert occurrences >= 1, "no rendered delete-attachment button found"


# Comment composer files-only must not silently become bug-level
def test_post_comment_rejects_files_without_body():
    """Files-only comment submissions are rejected with a toast (would otherwise become bug-level attachments)."""
    src = BUG_MODAL.read_text(encoding="utf-8")
    assert "if (!body && files.length > 0)" in src, (
        "postComment is missing the files-only reject branch"
    )
    assert "Add attachment" in src
    # The old three-way commentId split produced bug-level attachments; now a comment is always created first.
    assert "const commentId = body ? await _postCommentCreate" not in src


# Chip checkbox UI — custom indicator span + hidden native checkbox
def test_reports_chip_indicator_span_is_rendered():
    """Indicator span replaces the native checkbox (hidden input + :has); if either side breaks, the chip degrades."""
    js = REPORTS_VIEW.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert "reports-chip-indicator" in js, "ReportsView no longer emits the indicator span"
    assert ".reports-chip-indicator" in css, "indicator selector missing from CSS"
    assert ".reports-chip input[type=\"checkbox\"]" in css
    assert "opacity: 0" in css
    assert ".reports-chip:has(input:checked)" in css


# Reports view uses theme tokens, not literal white
@pytest.mark.parametrize("selector_block,must_contain", [
    (".reports-side {", "var(--bg-elev)"),
    (".reports-main {", "var(--bg-elev)"),
    (".reports-table thead th {", "var(--bg-elev-3)"),
    (".reports-summary-card {", "var(--bg-elev-2)"),
])
def test_reports_view_uses_theme_tokens(selector_block, must_contain):
    """Reports panels use CSS theme variables, not hard-coded white."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    start = css.find(selector_block)
    assert start != -1, f"selector {selector_block!r} missing from CSS"
    block = css[start:start + 800]
    assert must_contain in block, (
        f"{selector_block} no longer uses {must_contain}; got block: {block[:200]!r}"
    )
    assert "background: #fff" not in block
    assert "background: white" not in block


def test_no_literal_export_csv_button_in_index_html():
    """Sidebar Export CSV button is gone; VIEW_MIN_ROLE gates Reports to manager+ on both nav surfaces."""
    sidebar_src = SIDEBAR.read_text(encoding="utf-8")
    assert 'id="exportCsvBtn"' not in sidebar_src
    assert "Export CSV" not in sidebar_src
    navitems_src = NAVITEMS.read_text(encoding="utf-8")
    assert 'view: "reports"' in navitems_src, "Reports nav entry must exist in NAV_ITEMS"
    # VIEW_MIN_ROLE is the source of truth; Reports must be gated to manager+.
    types_src = TYPES.read_text(encoding="utf-8")
    m = re.search(r"VIEW_MIN_ROLE[^{]*\{(.*?)\}", types_src, re.S)
    assert m, "VIEW_MIN_ROLE map must exist in types.ts"
    assert re.search(r'reports:\s*"manager"', m.group(1)), (
        "Reports must be gated to manager+ in VIEW_MIN_ROLE"
    )
    # Both nav surfaces must consume the gate so map and buttons can't drift.
    nav_src = TOPCHROME.read_text(encoding="utf-8")
    assert "VIEW_MIN_ROLE" in nav_src, "TopChrome must consult VIEW_MIN_ROLE to gate nav buttons"
    assert "allowed(item.view)" in nav_src, "TopChrome must filter NAV_ITEMS through the role gate"
    assert "NAV_ITEMS" in nav_src, "TopChrome must render the shared NAV_ITEMS list"
    assert "NAV_ITEMS" in sidebar_src, "Sidebar (mobile drawer) must render the shared NAV_ITEMS list"
    assert "VIEW_MIN_ROLE" in sidebar_src, "Sidebar must consult VIEW_MIN_ROLE to gate its drawer nav"


# Smoke: a manager-only API still works after the version bump
def test_health_endpoint_reports_version(client):
    # /api/health reports the configured version; compare against config, not a literal.
    from app.main import settings
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["version"] == settings.APP_VERSION


def test_login_page_shows_version(client):
    """Login page renders 'Version <X>' (server-side substituted) below the sign-in card."""
    from app.main import settings
    r = client.get("/login.html")
    assert r.status_code == 200
    body = r.text
    # Placeholder substitution must have happened server-side.
    assert "__APP_VERSION__" not in body
    assert f"Version {settings.APP_VERSION}" in body
    # Styling hook must be present.
    assert 'class="auth-version"' in body


# Snapshot-based undo/redo: native contenteditable undo skips toolbar DOM mutations.
def test_rich_editor_has_snapshot_history():
    src = RICH_EDITOR.read_text(encoding="utf-8")
    # History structure (stack + index) must exist per instance. The TS-only
    # "HistoryState" interface name is gone post TS->JS migration (Slice J);
    # the behavior itself (stack/idx on historyRef) is what's actually guarded.
    assert "stack:" in src, "snapshot history state missing from RichEditor"
    assert "idx:" in src, "snapshot history state missing from RichEditor"
    # Ctrl+Z / Ctrl+Y must call our undo/redo, not the browser's native undo.
    assert "undoEdit()" in src
    assert "redoEdit()" in src
    # Toolbar mousedown must go through the snapshot wrapper.
    assert "runToolbarCmd(btn)" in src
    # setHtml must reset history so undo can't jump back to a previous bug's content.
    assert "resetHistory()" in src


def test_rich_editor_seeds_initial_snapshot():
    """First Ctrl+Z can return to the initial state: resetHistory() seeds a snapshot at mount."""
    src = RICH_EDITOR.read_text(encoding="utf-8")
    # resetHistory must seed an initial snapshot().
    start = src.find("const resetHistory")
    assert start != -1, "resetHistory helper missing"
    body = src[start:start + 400]
    assert "snapshot()" in body, "resetHistory must seed an initial snapshot()"
    # Must run at mount, inside the mount layout effect, before any user input.
    assert "resetHistory();" in src
    le = src.find("useLayoutEffect(() => {")
    assert le != -1, "mount layout effect missing"
    assert "resetHistory();" in src[le:le + 700], (
        "resetHistory() must be seeded from the mount layout effect"
    )


# formatDate must always render both date and time, no today-only shortcut.
def test_format_date_always_includes_date_and_time():
    """formatDate() must combine locale date and time; the today-only shortcut is gone."""
    src = FORMAT_TS.read_text(encoding="utf-8")
    # Slice to the next top-level export so the check spans the whole function.
    start = src.find("export function formatDate")
    assert start != -1, "formatDate helper missing"
    end = src.find("\nexport function", start + 1)
    assert end != -1
    body = src[start:end]
    assert "toLocaleDateString" in body
    assert "toLocaleTimeString" in body
    # The old "sameDay ? time : date" branch must be gone.
    assert "sameDay" not in body, (
        "formatDate still uses the today/not-today shortcut; both pieces of "
        "the timestamp must be visible everywhere"
    )
    # Rendered string must concatenate date + time.
    assert "${date}, ${time}" in body
