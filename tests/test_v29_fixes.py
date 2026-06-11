"""Regression tests for the v2.9 follow-up bug fixes.

The five fixes in this slice were:

  1. Reports chip checkbox UI redesigned — native checkbox hidden, custom
     indicator span rendered.
  2. Comment-attachment Delete button no longer submits the enclosing
     bug form (must carry type="button").
  3. Comment composer files-only no longer leaks as a bug-level
     attachment — postComment() rejects with a clear toast and the user
     is directed to the dedicated bug-level uploader.
  4. Reports view theme uses the app's theme tokens instead of white
     panel fallbacks.
  5. Version bumped 2.8 -> 2.9 everywhere.

The frontend invariants can't run through a TestClient (no DOM), so we
assert them by sniffing the source files — cheap, deterministic and
catches accidental regressions when someone refactors the SPA.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_JS = REPO_ROOT / "app" / "static" / "app.js"
INDEX_HTML = REPO_ROOT / "app" / "static" / "index.html"
STYLES_CSS = REPO_ROOT / "app" / "static" / "styles.css"


# ---------------------------------------------------------------------------
# Fix 5: version
# ---------------------------------------------------------------------------
def test_app_version_is_29():
    from app import __version__
    assert __version__ == "2.10"


def test_config_default_app_version_is_29():
    # The config default is what runs when no APP_VERSION env var is
    # set — production deployments override via env, but the baked-in
    # default must still be current so /api/health reports the right
    # version locally.
    from app.config import get_settings
    get_settings.cache_clear()
    assert get_settings().APP_VERSION == "2.10"


# ---------------------------------------------------------------------------
# Fix 2: delete-attachment button must NOT submit the bug form
# ---------------------------------------------------------------------------
def test_delete_attachment_button_has_type_button():
    """A button inside <form> with no `type` attribute defaults to
    `submit` and triggers a form submit on click. The fix added
    type="button" so deleting a comment's attachment no longer also
    saves the bug and closes the modal."""
    src = APP_JS.read_text(encoding="utf-8")
    # Locate the renderAttachmentCard string template; verify the
    # button it emits carries type="button".
    assert 'data-act="delete-attachment"' in src
    # Walk every occurrence — every render of delete-attachment must be
    # type="button".
    needle = 'data-act="delete-attachment"'
    pos = 0
    occurrences = 0
    while True:
        idx = src.find(needle, pos)
        if idx == -1:
            break
        # Look back ~80 chars from the data-act for `type="button"`.
        prefix = src[max(0, idx - 80): idx]
        if "<button" in prefix:
            assert 'type="button"' in prefix, (
                f"delete-attachment button at offset {idx} lacks "
                f"type=\"button\"; context: {prefix!r}"
            )
            occurrences += 1
        pos = idx + len(needle)
    assert occurrences >= 1, "no rendered delete-attachment button found"


# ---------------------------------------------------------------------------
# Fix 3: comment composer files-only must NOT silently become bug-level
# ---------------------------------------------------------------------------
def test_post_comment_rejects_files_without_body():
    """The postComment() function used to allow files-only by quietly
    routing those uploads to the bug-level endpoint. v2.9 keeps them
    strictly separate — files attached in the comment composer must
    accompany comment text, or the composer refuses with a clear
    toast pointing the user at the bug-level uploader."""
    src = APP_JS.read_text(encoding="utf-8")
    # The new code path must reject files-only:
    # Internal uses `files`; enterprise uses `staged` — either is fine
    # as long as a body-empty AND files-attached check is present.
    has_reject_branch = (
        "if (!body && files.length > 0)" in src
        or "if (!body && staged.length > 0)" in src
    )
    assert has_reject_branch, "postComment is missing the files-only reject branch"
    # The toast must mention the proper uploader so users know where to go.
    assert "Add attachment" in src
    # The old "commentId = body ? ... : null" branch that produced
    # bug-level attachments must be gone — the new code always creates
    # a comment first.
    assert "const commentId = body ? await _postCommentCreate" not in src


# ---------------------------------------------------------------------------
# Fix 1: chip checkbox UI — custom indicator span + hidden native checkbox
# ---------------------------------------------------------------------------
def test_reports_chip_indicator_span_is_rendered():
    """The chip indicator (the little checkmark dot) lives in its own
    span. CSS hides the native checkbox via absolute positioning +
    opacity:0 and shows the indicator span only when checked, via
    :has(input:checked). If either side breaks, the chip falls back to
    an ugly raw checkbox — what the user complained about."""
    js = APP_JS.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert "reports-chip-indicator" in js, "_chipNode no longer emits the indicator span"
    assert ".reports-chip-indicator" in css, "indicator selector missing from CSS"
    # The native checkbox must be visually hidden so the indicator is
    # what the user sees.
    assert ".reports-chip input[type=\"checkbox\"]" in css
    assert "opacity: 0" in css
    # The :has() selector drives the "checked" visual state.
    assert ".reports-chip:has(input:checked)" in css


# ---------------------------------------------------------------------------
# Fix 4: Reports view uses theme tokens, not literal white
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("selector_block,must_contain", [
    (".reports-side {", "var(--bg-elev)"),
    (".reports-main {", "var(--bg-elev)"),
    (".reports-table thead th {", "var(--bg-elev-3)"),
    (".reports-summary-card {", "var(--bg-elev-2)"),
])
def test_reports_view_uses_theme_tokens(selector_block, must_contain):
    """Each Reports block must use the app's theme variables so the
    panel matches the dark/light theme. White fallbacks (`#fff`) were
    the visual bug the user reported."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    start = css.find(selector_block)
    assert start != -1, f"selector {selector_block!r} missing from CSS"
    block = css[start:start + 800]
    assert must_contain in block, (
        f"{selector_block} no longer uses {must_contain}; got block: {block[:200]!r}"
    )
    # And no hard-coded white panel background.
    assert "background: #fff" not in block
    assert "background: white" not in block


def test_no_literal_export_csv_button_in_index_html():
    """The sidebar Export CSV button was removed in favour of the
    Reports view. Make sure it stays gone."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="exportCsvBtn"' not in html
    assert "Export CSV" not in html
    # And the Reports nav entry IS there, gated by role.
    assert 'data-view="reports"' in html
    assert 'data-needs-role="manager"' in html


# ---------------------------------------------------------------------------
# Smoke: a manager-only API still works after the version bump
# ---------------------------------------------------------------------------
def test_health_endpoint_reports_v29(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["version"] == "2.10"


def test_login_page_shows_version_2_9(client):
    """The login page renders "Version 2.10" below the sign-in card so a
    user looking at the landing page knows which release they're on
    without having to log in first."""
    r = client.get("/login.html")
    assert r.status_code == 200
    body = r.text
    # Server-side placeholder substitution must have replaced the token.
    assert "__APP_VERSION__" not in body
    assert "Version 2.10" in body
    # And the styling hook must be there.
    assert 'class="auth-version"' in body


# ---------------------------------------------------------------------------
# Rich-text editor undo/redo: snapshot-based history so Ctrl+Z works
# across both typed characters AND toolbar formatting commands. The old
# editor relied on the browser's native contenteditable undo, which
# silently skipped every toolbar mutation because those went through
# direct DOM manipulation (not document.execCommand). Users reported
# undo/redo "not working properly at all".
# ---------------------------------------------------------------------------
def test_rich_editor_has_snapshot_history():
    src = APP_JS.read_text(encoding="utf-8")
    # The history structure must exist on every editor instance.
    assert "_history = { stack: [], idx: -1," in src, (
        "snapshot history object missing from enhanceRichEditor"
    )
    # Ctrl+Z / Ctrl+Y handlers must explicitly call our undo/redo,
    # not fall back to the browser's broken native undo.
    assert "undoEdit()" in src
    assert "redoEdit()" in src
    # The toolbar mousedown path must run through the snapshot wrapper.
    assert "_runToolbarCmd(btn)" in src
    # _bhRtSet should reset the history so undo can't jump back to a
    # previous (unrelated) bug's content.
    assert "resetHistory()" in src


def test_rich_editor_seeds_initial_snapshot():
    """The first Ctrl+Z must be able to return to the editor's initial
    state. That requires a seed snapshot taken at enhancement time."""
    src = APP_JS.read_text(encoding="utf-8")
    # The "Seed the initial state" comment + _snapshot() call sits inside
    # enhanceRichEditor before any user input can arrive.
    start = src.find("function enhanceRichEditor")
    assert start != -1
    end = src.find("\nfunction ", start + 1)
    body = src[start:end]
    assert "_snapshot()" in body
    assert "Seed the initial state" in body


# ---------------------------------------------------------------------------
# Timestamp formatter: must always render BOTH date and time everywhere
# (comments, audit log, session list, bug modal metadata). The old
# "today shortcut" hid one half of the timestamp; users wanted both.
# ---------------------------------------------------------------------------
def test_format_date_always_includes_date_and_time():
    """The formatDate() helper must combine the locale date and the
    locale time. Static-source check: verify both toLocale*() calls are
    in the same expression and the today-only shortcut is gone."""
    src = APP_JS.read_text(encoding="utf-8")
    # Find the formatDate body. We extract until the next top-level
    # `const ` declaration so the slice covers the entire arrow-function
    # body regardless of how it's formatted.
    start = src.find("const formatDate = (iso) =>")
    assert start != -1, "formatDate helper missing"
    end = src.find("\nconst ", start + 1)
    assert end != -1
    body = src[start:end]
    assert "toLocaleDateString" in body
    assert "toLocaleTimeString" in body
    # The old "sameDay ? time : date" branch must be gone.
    assert "sameDay" not in body, (
        "formatDate still uses the today/not-today shortcut; users wanted "
        "both pieces of the timestamp visible everywhere"
    )
    # The rendered string must concatenate date + time.
    assert "${datePart}, ${timePart}" in body
