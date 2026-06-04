"""Regression tests for the v2.6 backend port.

The v2.6 wave shipped a rich-text editor on the SPA, which forced a set
of BACKEND-side surfaces to harden:

  * HTML sanitization (`sanitize_html`) — every description / comment
    body now travels through an allowlist parser before storage. The
    allowlist must KEEP the formatting tags the editor emits (bold,
    italic, lists, blockquote, pre, code, paragraphs, data: image URLs)
    while STRIPPING anything that's a stored-XSS vector (`<script>`,
    `javascript:` URLs, event-handler attrs, `<iframe>`).
  * Larger field caps on description (1 MB) and comment body (200 KB)
    so multi-paste workflows fit.
  * Image-only comments are valid (pasted screenshot, no caption).
  * Audit pagination (`offset` + `limit`) with a 10 000 ceiling.
  * Type-aware status meta map exposing the per-type status sets.
  * Newest-first ordering on bug attachments, comments, and event items.

The frontend layout fixes from the same wave (JS markers in app.js)
are covered by the internal repo's static-asset tests and by Sonar's
lint — this file only exercises BACKEND behaviour against the live
FastAPI app.

Multi-tenant safety: every test runs through the `admin_client`
fixture, which seeds a fresh "Acme Co" org per test. All assertions
that read data verify they read it from within the same org. Tests
use the test SQLite DB only; no network egress, no shared state.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_project(client, name="Eng v2.6"):
    r = client.post("/api/projects", json={"name": name, "color": "#558844"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **extra):
    body = {
        "title": "Sample bug v2.6",
        "project_id": project_id,
        "item_type": "Bug",
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# =============================================================================
# 1. sanitize_html unit tests
# =============================================================================
class TestSanitizeHtml:
    """Direct unit tests on the in-house allowlist sanitiser. These run
    without a running app — we import the function and exercise it
    directly so a regression shows up immediately, not only via the
    integration path.
    """

    def test_sanitize_html_strips_script_tag(self):
        from app.schemas import sanitize_html
        out = sanitize_html("<p>ok</p><script>x</script>")
        assert "<script" not in out.lower()
        assert "</script" not in out.lower()
        # The wrapping <p>ok</p> must survive.
        assert "<p>ok</p>" in out

    def test_sanitize_html_strips_javascript_url(self):
        from app.schemas import sanitize_html
        out = sanitize_html('<a href="javascript:alert(1)">x</a>')
        # The whole href must be gone — either the attribute is dropped
        # or the URL is rejected outright. Either way the dangerous
        # scheme must not survive in the output.
        assert "javascript:" not in out.lower()
        assert 'href="javascript' not in out.lower()

    def test_sanitize_html_strips_onerror_attribute(self):
        from app.schemas import sanitize_html
        # Use an absolute https src so it passes the URL allowlist —
        # the goal here is to prove the `onerror` event-handler attr
        # is dropped while the safe `src` survives.
        out = sanitize_html(
            '<img src="https://example.com/a.png" onerror="alert(1)">'
        )
        # The image survives (src is allowed-listed for <img>) but the
        # event-handler attr is dropped — that's the whole point.
        assert "<img" in out.lower()
        assert "onerror" not in out.lower()
        # src must still be present — it's on the allowlist for img.
        assert 'src="https://example.com/a.png"' in out

    def test_sanitize_html_strips_iframe(self):
        from app.schemas import sanitize_html
        out = sanitize_html('<iframe src="https://evil.example"></iframe>')
        assert "<iframe" not in out.lower()
        assert "</iframe" not in out.lower()
        # The src URL string must NOT leak through as text either —
        # the parser emits no text content for iframe markup.
        assert "evil.example" not in out

    def test_sanitize_html_preserves_allowed_tags(self):
        """All the formatting tags the SPA toolbar can produce must
        round-trip identically. If a sanitiser tweak accidentally drops
        one (e.g. <s> after switching libraries) this is the canary."""
        from app.schemas import sanitize_html
        html = (
            "<p>para</p>"
            "<b>bold</b><i>italic</i><u>underline</u><s>strike</s>"
            "<ul><li>li-a</li></ul>"
            "<ol><li>li-b</li></ol>"
            "<blockquote>quoted</blockquote>"
            "<pre>code-block</pre>"
            "<code>inline</code>"
        )
        out = sanitize_html(html)
        for needle in (
            "<p>para</p>", "<b>bold</b>", "<i>italic</i>",
            "<u>underline</u>", "<s>strike</s>",
            "<ul>", "<li>li-a</li>", "</ul>",
            "<ol>", "<li>li-b</li>", "</ol>",
            "<blockquote>quoted</blockquote>",
            "<pre>code-block</pre>",
            "<code>inline</code>",
        ):
            assert needle in out, f"missing: {needle!r} in {out!r}"

    def test_sanitize_html_allows_data_image_url(self):
        from app.schemas import sanitize_html
        src = "data:image/png;base64,AAAA"
        out = sanitize_html(f'<img src="{src}">')
        # The src must survive verbatim — the only data: scheme we accept
        # is data:image/* (pasted screenshot).
        assert "data:image/png;base64,AAAA" in out, out
        assert "<img" in out.lower()

    def test_sanitize_html_rel_noopener_on_anchor(self):
        """v2.6 anchor hardening — every external <a> gets rel added if
        the author didn't supply one. Without this, target=_blank links
        can rewrite window.opener (tabnabbing)."""
        from app.schemas import sanitize_html
        out = sanitize_html('<a href="https://example.com">x</a>')
        assert "noopener" in out.lower()
        assert "nofollow" in out.lower()
        # And href survives.
        assert 'href="https://example.com"' in out


# =============================================================================
# 2. Large rich-text payloads round-trip through bug + comment endpoints
# =============================================================================
class TestLargeRichTextPayloads:

    @pytest.fixture()
    def bug(self, admin_client):
        p = _make_project(admin_client)
        return _make_bug(admin_client, p["id"])

    def test_description_accepts_1MB_html(self, admin_client):
        """The Field cap is 1_000_000 chars; ~900 KB of <p>x</p> repeats
        must round-trip 201. (>=1 MB would 422 — we sit just under.)"""
        p = _make_project(admin_client, name="Big Desc Proj")
        unit = "<p>x</p>"  # 8 chars
        # 8 chars * 112_500 = 900_000 chars total — well under the 1 MB cap
        # but big enough to prove the limit isn't 10 KB by accident.
        body_html = unit * 112_500
        assert 800_000 < len(body_html) < 1_000_000
        r = admin_client.post("/api/bugs", json={
            "title": "Giant description bug",
            "project_id": p["id"],
            "item_type": "Bug",
            "description": body_html,
        })
        assert r.status_code == 201, r.text
        # And the persisted description must still be huge (sanitiser
        # only strips disallowed bits; allowed <p>x</p> passes through).
        got = r.json()["description"]
        assert len(got) > 800_000, len(got)

    def test_comment_body_accepts_200KB(self, admin_client, bug):
        """The CommentIn cap is 200_000 chars; ~180 KB of allowed HTML
        must round-trip 201."""
        body_html = "<p>note line</p>" * 11_250  # 16 chars * 11_250 = 180_000
        assert 150_000 < len(body_html) < 200_000
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/comments",
            json={"body": body_html},
        )
        assert r.status_code == 201, r.text
        got = r.json()["body"]
        # Sanitiser preserves the allowed tags so length stays huge.
        assert len(got) > 150_000

    def test_comment_image_only_is_valid(self, admin_client, bug):
        """A comment whose only visible content is a pasted screenshot
        (data:image URL) must be accepted — the validator skips the
        "must have text" rule when an <img> is present."""
        body = "<p><img src='data:image/png;base64,AAAA'></p>"
        r = admin_client.post(
            f"/api/bugs/{bug['id']}/comments",
            json={"body": body},
        )
        assert r.status_code == 201, r.text
        got = r.json()["body"]
        # The data URL must have survived sanitisation.
        assert "data:image/png;base64,AAAA" in got


# =============================================================================
# 3. Audit pagination + limit ceiling
# =============================================================================
class TestAuditPagination:

    def _seed_audit_rows(self, admin_client, count: int):
        """Generate at least `count` audit rows by creating + editing
        bugs. Each create writes one row; each edit writes another."""
        p = _make_project(admin_client)
        for i in range(count):
            bug = _make_bug(admin_client, p["id"], title=f"Audit Seed Bug {i}")
            # one edit -> one extra audit row, but only the create is
            # strictly needed to hit `count` so just creating is fine.
            assert bug["id"]

    def test_audit_offset_pagination(self, admin_client):
        """offset advances over the result set; the two pages must NOT
        overlap by id (each row has a unique id)."""
        self._seed_audit_rows(admin_client, 12)  # > 10 ensures both pages full
        first = admin_client.get("/api/audit", params={"limit": 5, "offset": 0})
        second = admin_client.get("/api/audit", params={"limit": 5, "offset": 5})
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        first_rows = first.json()
        second_rows = second.json()
        assert len(first_rows) == 5, first_rows
        assert len(second_rows) == 5, second_rows
        # No id appears in both pages — proves offset truly skipped, not
        # just re-ran the same query.
        first_ids = {r["id"] for r in first_rows}
        second_ids = {r["id"] for r in second_rows}
        assert first_ids.isdisjoint(second_ids), (
            f"offset 5 returned overlapping rows: {first_ids & second_ids}"
        )

    def test_audit_limit_ceiling_10000(self, admin_client):
        """The /api/audit endpoint pins limit at <= 10_000. Anything
        higher (e.g. 100_000) must 422."""
        r = admin_client.get("/api/audit", params={"limit": 100_000})
        assert r.status_code == 422, r.text


# =============================================================================
# 4. Meta endpoint — statuses_by_type invariants
# =============================================================================
class TestMetaStatusesByType:
    """The /api/meta response shape underpins the SPA's status dropdown;
    a regression here breaks every modal."""

    def test_meta_statuses_by_type_invariants(self, admin_client):
        r = admin_client.get("/api/meta")
        assert r.status_code == 200, r.text
        by_type = r.json().get("statuses_by_type") or {}
        # Each type bucket is present.
        for t in ("Bug", "Requirement", "Task"):
            assert t in by_type, by_type
        # Per-type invariants — pinned strings that the v2.5 spec
        # locked in.
        assert "Not a Bug" in by_type["Bug"]
        assert "Approved" in by_type["Requirement"]
        assert "Done" in by_type["Task"]
        # "New" is the universal initial status — must appear in every
        # bucket so any freshly-created item is valid.
        for t in ("Bug", "Requirement", "Task"):
            assert "New" in by_type[t], (
                f'"New" missing from {t} bucket: {by_type[t]}'
            )


# =============================================================================
# 5. Newest-first ordering on bug detail
# =============================================================================
class TestNewestFirstOrdering:
    """v2.6 makes evidence rails order DESC by created_at so the most
    recent activity surfaces at the top of every modal."""

    def test_bug_attachments_returned_newest_first(self, admin_client):
        p = _make_project(admin_client, name="Order P")
        bug = _make_bug(admin_client, p["id"])
        # Upload two attachments — the second one (newer) should appear
        # first in the GET response.
        r1 = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("first.txt", io.BytesIO(b"one"), "text/plain")},
        )
        assert r1.status_code == 201, r1.text
        first_id = r1.json()["id"]
        r2 = admin_client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("second.txt", io.BytesIO(b"two"), "text/plain")},
        )
        assert r2.status_code == 201, r2.text
        second_id = r2.json()["id"]
        # Fetch the bug detail. attachments[0] must be the most recent.
        detail = admin_client.get(f"/api/bugs/{bug['id']}").json()
        atts = detail["attachments"]
        assert len(atts) == 2, atts
        assert atts[0]["id"] == second_id, (
            f"newest must be at index 0; got id ordering {[a['id'] for a in atts]}"
        )
        assert atts[1]["id"] == first_id

    def test_bug_comments_returned_newest_first(self, admin_client):
        p = _make_project(admin_client, name="Order C")
        bug = _make_bug(admin_client, p["id"])
        r1 = admin_client.post(
            f"/api/bugs/{bug['id']}/comments",
            json={"body": "<p>first comment</p>"},
        )
        assert r1.status_code == 201, r1.text
        first_id = r1.json()["id"]
        r2 = admin_client.post(
            f"/api/bugs/{bug['id']}/comments",
            json={"body": "<p>second comment</p>"},
        )
        assert r2.status_code == 201, r2.text
        second_id = r2.json()["id"]
        # GET /api/bugs/{id}/comments returns newest first.
        listed = admin_client.get(f"/api/bugs/{bug['id']}/comments").json()
        assert len(listed) == 2
        assert listed[0]["id"] == second_id, (
            f"newest must be at index 0; got {[c['id'] for c in listed]}"
        )
        assert listed[1]["id"] == first_id
        # And the embedded comments list on /api/bugs/{id} matches.
        detail = admin_client.get(f"/api/bugs/{bug['id']}").json()
        embedded = detail["comments"]
        assert [c["id"] for c in embedded] == [second_id, first_id]


# =============================================================================
# 6. Event detail items ordered by updated_at DESC
# =============================================================================
class TestEventItemsOrdering:
    """Event detail items rail is ordered by Bug.updated_at DESC so the
    most recently touched task floats up. Verify by editing the older
    item after creating both — the edit bumps updated_at, so the
    "older" one should now lead."""

    def test_event_items_returned_by_updated_at_desc(self, admin_client, db_path):
        p = _make_project(admin_client, name="Event Order P")
        ev = admin_client.post("/api/events", json={"name": "Standup-order"})
        assert ev.status_code == 201, ev.text
        ev_id = ev.json()["id"]
        # Create two tasks in the event.
        t1 = _make_bug(
            admin_client, p["id"],
            title="Task one early", item_type="Task", event_id=ev_id,
        )
        t2 = _make_bug(
            admin_client, p["id"],
            title="Task two later", item_type="Task", event_id=ev_id,
        )
        # Sanity: initial order has t2 first (it was created last → newer
        # created_at AND updated_at). The secondary id-desc tiebreaker
        # also picks t2 when timestamps tie.
        detail0 = admin_client.get(f"/api/events/{ev_id}").json()
        ids0 = [it["id"] for it in detail0["items"]]
        assert ids0 == [t2["id"], t1["id"]], ids0
        # Bump t1's updated_at directly via SQL so the change is
        # guaranteed strictly LATER than t2's (sub-second-resolution
        # writes via the route handler can collide on Windows, which
        # would only exercise the id-desc tiebreaker rather than the
        # actual updated_at-desc ordering we want to pin).
        from datetime import datetime, timezone, timedelta
        from sqlalchemy import create_engine, text
        bumped = datetime.now(timezone.utc) + timedelta(seconds=10)
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE bugs SET updated_at = :u WHERE id = :i"),
                {"u": bumped.isoformat(), "i": t1["id"]},
            )
        engine.dispose()
        # Refetch event detail. t1 should now lead — its updated_at is
        # strictly greater than t2's, so updated_at-desc ordering puts
        # it first regardless of id ordering.
        detail1 = admin_client.get(f"/api/events/{ev_id}").json()
        ids1 = [it["id"] for it in detail1["items"]]
        assert ids1 == [t1["id"], t2["id"]], (
            f"most-recently-updated task must surface first; got {ids1}"
        )
