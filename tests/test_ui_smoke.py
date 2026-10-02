"""End-to-end UI smoke tests: boots the FastAPI app on a random port and
drives a real Chromium via Playwright through the main user flows."""
import contextlib
import os
import socket
import sys
import tempfile
import threading
import time

import pytest

# Mark the whole module `ui` so CI can skip the browser suite with `-m "not ui"`.
pytestmark = pytest.mark.ui


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Boot the FastAPI app on a random port in a background thread."""
    db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    db.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{db.name}"
    os.environ["SESSION_SECRET"] = "ui_smoke"
    os.environ["EMAIL_BACKEND"] = "disabled"
    os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@ui.test"
    os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminUI1234"
    os.environ["BOOTSTRAP_ADMIN_NAME"] = "UI Admin"
    # Disable HIBP outbound calls. This server starts before any function-scoped
    # monkeypatch runs, so without this the create-user flow would block on a
    # live network round-trip.
    os.environ["PASSWORD_BREACH_CHECK_ENABLED"] = "false"
    # Fresh import after env vars are in place.
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]

    import uvicorn

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Poll until the server is ready to accept connections.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("server didn't start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
    with contextlib.suppress(Exception):
        os.unlink(db.name)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _login(page, base_url, email="admin@ui.test", password="AdminUI1234"):
    page.goto(f"{base_url}/login.html")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/", timeout=5000)
    # Wait for the SPA to finish booting. #newBugBtn is in the static HTML so it
    # appears immediately; it's not a reliable ready signal. Wait for network idle
    # (boot fires several /api calls) and for the account name to be populated.
    page.wait_for_load_state("networkidle", timeout=10000)
    page.wait_for_function(
        "() => document.getElementById('accountName')?.textContent?.trim().length > 0",
        timeout=5000,
    )


# When the inline comment <form> is nested inside the bug-modal <form>, the
# HTML5 spec silently closes the outer form. The Create button ends up
# parent-less and clicking it does nothing. This test catches that regression.
def test_create_bug_button_actually_creates(live_server, browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)

    # Create a project — the bug form requires at least one.
    page.click("#newProjectBtn")
    page.wait_for_selector("#modalProject", state="visible", timeout=2000)
    page.fill('#formProject input[name="name"]', "UI Smoke Project")
    page.click('#formProject button[type="submit"]')
    page.wait_for_selector("#modalProject", state="hidden", timeout=3000)

    # Now open the bug create modal
    page.click("#newBugBtn")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)

    # Create button must be inside the bug form. Use getAttribute('id') rather
    # than f.id because HTMLFormElement's named-property accessor can resolve
    # `form.id` to a child <input name="id"> instead of the element's actual id.
    enclosing_form_id = page.evaluate("""
        () => {
            const btn = document.getElementById('bugSubmitBtn');
            const f = btn ? btn.closest('form') : null;
            return f ? f.getAttribute('id') : null;
        }
    """)
    assert enclosing_form_id == "formBug", \
        f"Create button is not inside formBug! Got: {enclosing_form_id!r}"

    # Reporter must be disabled (pre-filled to the logged-in user, not editable).
    is_disabled = page.evaluate("""
        () => document.querySelector('#formBug select[name="reporter_id"]').disabled
    """)
    assert is_disabled, "Reporter select must be disabled"

    # The selected option must show the logged-in user's name.
    reporter_text = page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="reporter_id"]');
            return sel.options[sel.selectedIndex]?.text;
        }
    """)
    assert "UI Admin" in reporter_text, f"Reporter should show 'UI Admin', got: {reporter_text!r}"

    # Fill the form and submit.
    page.fill('#formBug input[name="title"]', "Smoke-test bug from Playwright")
    page.fill('#formBug textarea[name="description"]', "Created by an automated UI test.")

    # bubbles:true is required. Real change events bubble, and the React
    # frontend uses delegated listeners at the root. A non-bubbling synthetic
    # event never reaches the onChange handler. The old vanilla app read the
    # value at submit time, which hid this requirement.
    page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="project_id"]');
            sel.value = sel.options[1].value;  // skip the placeholder
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """)

    # Intercept the network request to confirm the form submission fires.
    with page.expect_response(lambda r: r.url.endswith("/api/bugs") and r.request.method == "POST") as resp_info:
        page.click("#bugSubmitBtn")
    resp = resp_info.value
    assert resp.status == 201, f"POST /api/bugs returned {resp.status}: {resp.text()}"

    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)

    # Bug should now be visible in the list.
    page.wait_for_selector("text=Smoke-test bug from Playwright", timeout=3000)

    ctx.close()


def test_open_bug_shows_inline_comments_section(live_server, browser):
    """Opening a bug row shows the inline comments section in the modal.

    Previously comments lived in a separate detail modal with tabs; this
    guards the unified-modal behavior."""
    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)

    # Relies on the bug created by the previous test. The fixture is module-scoped,
    # so the DB is shared; for full isolation you'd seed via the API instead.
    page.click("text=Smoke-test bug from Playwright")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)

    # Comments section must be visible when opening an existing bug (not a new one).
    is_hidden = page.evaluate("""
        () => document.getElementById('bugCommentsSection').hidden
    """)
    assert not is_hidden, "Comments section should be visible when opening an existing bug"

    # The comment-post button must be type="button", not type="submit",
    # so it doesn't trigger the outer form.
    btn_type = page.evaluate("() => document.getElementById('commentPostBtn').type")
    assert btn_type == "button"

    ctx.close()


def test_v3_shell_account_menu_and_bell_top_right(live_server, browser):
    """Shell chrome regression guard: notification bell, bell alignment, and profile menu.

    Three behaviors are locked in:
    1. Clicking the bell renders the notification panel.
    2. The bell stays right-aligned on all views (a CSS `~` sibling rule
       could push it left on non-list views when a hidden search box matched).
    3. Account controls (change-password / theme / log out) live in the
       top-right profile menu, not the sidebar.
    """
    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)

    # Account name must be in the top-right profile button; sidebar logout is gone.
    assert page.locator("#profileBtn #accountName").count() == 1, \
        "Account name should be inside the top-right profile button"
    assert page.locator(".sidebar #logoutBtn").count() == 0, \
        "Log out must no longer live in the sidebar"

    # The bell's left edge must be well into the right half of the viewport on
    # non-list views (the CSS bug put it near x=0). Target the desktop chrome bar
    # explicitly — the nav also lives in the mobile drawer which comes first in
    # the DOM, so an unscoped selector would resolve to the wrong hidden element.
    page.click('.chrome .nav-btn[data-view="events"]')
    page.wait_for_selector("#viewEvents, .view", timeout=3000)
    box = page.locator("#notifBtn").bounding_box()
    vw = page.evaluate("() => window.innerWidth")
    assert box, f"Bell should be right-aligned on the Events view (x={box and box['x']}, vw={vw})"
    assert box["x"] > vw / 2, \
        f"Bell should be right-aligned on the Events view (x={box and box['x']}, vw={vw})"

    # Bell click opens the notification panel.
    page.click("#notifBtn")
    page.wait_for_selector(".notif-panel", state="visible", timeout=2000)
    assert page.locator(".notif-panel .notif-panel-title").inner_text().strip() == "Notifications"
    page.keyboard.press("Escape")

    # Profile menu must expose change-password, theme, and log out.
    page.click("#profileBtn")
    for ctrl in ("#changePasswordBtn", "#themeBtn", "#logoutBtn"):
        page.wait_for_selector(f".profile-menu {ctrl}", state="visible", timeout=2000)

    # Theme toggle must flip the document's data-theme attribute.
    before = page.evaluate("() => document.documentElement.dataset.theme || 'dark'")
    page.click("#themeBtn")
    after = page.evaluate("() => document.documentElement.dataset.theme || 'dark'")
    assert before != after, f"Theme toggle should flip the theme (was {before}, now {after})"

    ctx.close()


def test_session_revoke_kicks_user_out_promptly(live_server, browser):
    """Admin revokes a session and the affected user is redirected to /login.html.

    Two pieces must work together:
    - Frontend: api() catches 401, calls bounceToLogin() (location.replace).
      The periodic /me poll handles the idle case.
    - Backend: _has_valid_session() must check the sessions table, not just the
      cookie signature. If it doesn't, /login.html bounces back to / in a loop
      (a known redirect-loop regression).
    """
    import httpx
    admin = httpx.Client(base_url=live_server)
    admin.post("/api/auth/login",
               json={"email": "admin@ui.test", "password": "AdminUI1234"})
    admin.post("/api/users", json={
        "name": "Victim", "email": "victim@ui.test",
        "role": "user", "password": "Victim1234",
    })

    victim_ctx = browser.new_context()
    victim_page = victim_ctx.new_page()
    _login(victim_page, live_server, "victim@ui.test", "Victim1234")
    assert victim_page.url == f"{live_server}/", \
        f"Victim should be on / before revoke, was at {victim_page.url}"

    # Revoke via the API (deterministic). This is equivalent to clicking Revoke
    # in the Sessions panel; the UI path is covered by test_role_policy.py.
    sessions = admin.get("/api/sessions").json()
    victim_session = next(
        s for s in sessions if s.get("user_email") == "victim@ui.test"
    )
    r = admin.delete(f"/api/sessions/{victim_session['id']}")
    assert r.status_code == 200

    # Reload triggers boot() -> /me -> 401 -> location.replace. On the server,
    # _has_valid_session must return False for the revoked cookie so /login.html
    # doesn't immediately bounce back to /.
    victim_page.reload()
    try:
        victim_page.wait_for_url("**/login.html", timeout=5000)
    except Exception:
        pass
    assert "/login.html" in victim_page.url, \
        f"Victim was not redirected after reload. URL: {victim_page.url}\n" \
        f"This means the known redirect-loop regression is back: the " \
        f"_has_valid_session() function in app/main.py is treating a " \
        f"revoked cookie as valid and bouncing /login.html back to /."

    victim_ctx.close()
    admin.close()


# Navigation regression guards.
#
# Saving an item or clicking a KPI from a non-list view must not bounce the user
# to Work Items. The bad setView("list") calls lived in BugModal (create + edit),
# ProjectModal (save), and KpiStrip (click).
#
# The shell mounts only the active view, so #viewList is absent from the DOM when
# the app is on Events. A count of 0 is therefore a precise "did not redirect"
# assertion; checking #viewEvents/#viewAnalytics visibility confirms we stayed put.
def _api_admin(live_server):
    """Return an authenticated httpx client for seeding test data via the API."""
    import httpx
    creds = {
        "email": os.environ["BOOTSTRAP_ADMIN_EMAIL"],
        "password": os.environ["BOOTSTRAP_ADMIN_PASSWORD"],
    }
    c = httpx.Client(base_url=live_server)
    c.post("/api/auth/login", json=creds)
    return c


@pytest.fixture(scope="module")
def nav_page(live_server, browser):
    """Shared logged-in page for the navigation-regression tests.

    Login is rate-limited to 8 requests per 60s per IP, and the rest of the
    module already uses most of that budget. These tests don't revoke sessions
    and always re-enter through the view nav, so one shared page is both safe
    and rate-limit friendly. Fixtures are seeded once via a single API client."""
    admin = _api_admin(live_server)
    admin.post("/api/projects", json={"name": "Event Task Project", "color": "#3366ff"})
    admin.post("/api/events", json={"name": "Sprint Planning Regression"})
    admin.close()

    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)
    yield page
    ctx.close()


def test_create_task_inside_event_stays_in_event(nav_page):
    """Creating a task from inside an event must keep the user in the event's
    detail panel, not bounce to Work Items."""
    page = nav_page

    # Scope the card click to the visible grid: the event name also appears in the
    # (hidden) detail header, so an unscoped text= selector hits the wrong node.
    page.click('.chrome .nav-btn[data-view="events"]')
    page.wait_for_selector("#viewEvents", state="visible", timeout=3000)
    page.click("#eventsGrid >> text=Sprint Planning Regression")
    page.wait_for_selector("#eventsDetailMode", state="visible", timeout=3000)

    # "+ Add Task" opens the bug modal pre-seeded with the current event and Task type.
    page.click("#addItemToEventBtn")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)
    page.fill('#formBug input[name="title"]', "Task created inside the event")
    page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="project_id"]');
            const opt = [...sel.options].find(o => o.text === 'Event Task Project');
            sel.value = opt.value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """)
    with page.expect_response(
        lambda r: r.url.endswith("/api/bugs") and r.request.method == "POST"
    ) as ri:
        page.click("#bugSubmitBtn")
    assert ri.value.status == 201, f"create task failed: {ri.value.text()}"

    # After the modal closes, we must still be inside the event.
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)
    assert page.locator("#viewList").count() == 0, (
        "BUG: creating a task inside an event redirected to the Work Items list. "
        "The forced setView('list') in BugModal.onSubmit is back."
    )
    assert page.locator("#viewEvents").is_visible(), \
        "Should still be on the Events view after adding a task"
    assert page.locator("#eventsDetailMode").is_visible(), \
        "Should still be in the event's detail panel (not the event card grid)"
    # Task also shows up in the event's item table.
    page.wait_for_selector(
        "#eventDetailItems >> text=Task created inside the event", timeout=4000
    )


def test_edit_task_from_event_stays_in_event(nav_page):
    """Saving an edit to a task opened from the event table must keep the user
    in the event (the edit path had the same forced-navigation bug as create)."""
    page = nav_page

    page.click('.chrome .nav-btn[data-view="events"]')
    page.wait_for_selector("#viewEvents", state="visible", timeout=3000)
    # The shared page may already be in the event detail from the previous test;
    # go back to the card grid so the event card is clickable.
    if page.locator("#eventsDetailMode").is_visible():
        page.click("#eventBackBtn")
        page.wait_for_selector("#eventsListMode", state="visible", timeout=3000)
    page.click("#eventsGrid >> text=Sprint Planning Regression")
    page.wait_for_selector("#eventsDetailMode", state="visible", timeout=3000)

    # Open the task that the previous test created, via the event's item table.
    page.click("#eventDetailItems >> text=Task created inside the event")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)
    page.fill('#formBug input[name="title"]', "Task edited inside the event")
    with page.expect_response(
        lambda r: "/api/bugs/" in r.url and r.request.method == "PUT"
    ) as ri:
        page.click("#bugSubmitBtn")
    assert ri.value.status == 200, f"edit task failed: {ri.value.text()}"

    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)
    assert page.locator("#viewList").count() == 0, \
        "BUG: editing a task from an event redirected to the Work Items list"
    assert page.locator("#eventsDetailMode").is_visible(), \
        "Should still be in the event's detail panel after an edit"


def test_kpi_click_on_analytics_stays_on_analytics(nav_page):
    """Clicking a KPI tile on Analytics must filter in place, not redirect to Work Items."""
    page = nav_page

    page.click('.chrome .nav-btn[data-view="analytics"]')
    page.wait_for_selector("#viewAnalytics", state="visible", timeout=3000)

    page.click('.kpi[data-kpi="open"]')
    # Must still be on Analytics, and the clicked tile must be marked active.
    assert page.locator("#viewList").count() == 0, \
        "BUG: a KPI click on Analytics redirected to the Work Items list"
    assert page.locator("#viewAnalytics").is_visible(), \
        "Clicking a KPI on Analytics must keep the user on Analytics"
    page.wait_for_selector('.kpi[data-kpi="open"].active', timeout=2000)


def test_project_save_keeps_current_view(nav_page):
    """Saving a project from a non-list view must keep the user on that view."""
    page = nav_page

    page.click('.chrome .nav-btn[data-view="analytics"]')
    page.wait_for_selector("#viewAnalytics", state="visible", timeout=3000)

    page.click("#newProjectBtn")
    page.wait_for_selector("#modalProject", state="visible", timeout=2000)
    page.fill('#formProject input[name="name"]', "Stay-Put Project")
    page.click('#formProject button[type="submit"]')
    page.wait_for_selector("#modalProject", state="hidden", timeout=3000)

    assert page.locator("#viewList").count() == 0, \
        "BUG: saving a project redirected to the Work Items list"
    assert page.locator("#viewAnalytics").is_visible(), \
        "Saving a project should keep the user on the Analytics view"


# Feature regression guards for the linked-items picker and the unified square
# checkbox. Both reuse the shared nav_page login to stay within the rate limit.
def _seed_bug_via_page(page, title: str) -> int:
    """Create a bug via the authenticated browser session and return its id."""
    return page.evaluate(
        """async (title) => {
            const projs = await (await fetch('/api/projects', {credentials:'include'})).json();
            const r = await fetch('/api/bugs', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    project_id: projs[0].id, title,
                    priority: 'Medium', environment: 'DEV', item_type: 'Bug',
                }),
            });
            return (await r.json()).id;
        }""",
        title,
    )


def test_user_active_checkbox_is_square(nav_page):
    """The "Active" checkbox in the user-create modal must be square (the custom
    themed control), not a stretched rectangle."""
    page = nav_page
    page.click("#newUserBtn")
    page.wait_for_selector("#modalUser", state="visible", timeout=3000)

    box = page.locator('#modalUser input[name="is_active"]').bounding_box()
    assert box is not None, "Active checkbox not found"
    # Allow 1px for sub-pixel rounding.
    assert abs(box["width"] - box["height"]) <= 1, \
        f"Active checkbox is not square (rectangle bug): {box}"
    # appearance:none confirms it's the custom control, not the browser's native box.
    appearance = page.evaluate(
        """() => getComputedStyle(
            document.querySelector('#modalUser input[name="is_active"]')
        ).webkitAppearance"""
    )
    assert appearance == "none", f"checkbox is still a native control: {appearance!r}"
    page.keyboard.press("Escape")


def test_linked_items_picker_multiselect_and_link(nav_page):
    """Linked-items picker: filter by type, multi-select two items, and link them
    in one action. Guards the multi-select UX, the type filter, and the POST
    /links API end-to-end."""
    page = nav_page
    src_id = _seed_bug_via_page(page, "ZZ Picker Source Bug")
    tgt1 = _seed_bug_via_page(page, "ZZ Picker Target Alpha")
    tgt2 = _seed_bug_via_page(page, "ZZ Picker Target Bravo")

    # Open the source bug directly via a custom event (avoids list-filter coupling).
    page.evaluate(
        "(id) => document.dispatchEvent(new CustomEvent('sleuth:open-bug', {detail: {bugId: id}}))",
        src_id,
    )
    page.wait_for_selector("#modalBug", state="visible", timeout=3000)

    # Open the picker, filter to Bugs, search, and tick both targets.
    page.click(".item-picker-trigger")
    page.click('.item-picker-tab:has-text("Bugs")')
    page.fill(".item-picker-search", "ZZ Picker Target")
    page.click('.item-picker-row:has-text("ZZ Picker Target Alpha")')
    page.click('.item-picker-row:has-text("ZZ Picker Target Bravo")')
    # Multi-select keeps the dropdown open and shows chips for each selection.
    assert page.locator(".item-picker-chips .item-picker-chip").count() == 2

    # Link — one POST per selected item.
    page.click("#bugLinkAdd .btn.primary")

    # Both targets must appear in the linked-items list.
    page.wait_for_selector(f'.bug-link-row:has-text("#{tgt1}")', timeout=4000)
    page.wait_for_selector(f'.bug-link-row:has-text("#{tgt2}")', timeout=4000)
    assert page.locator('.bug-link-row:has-text("ZZ Picker Target Alpha")').count() >= 1
    assert page.locator('.bug-link-row:has-text("ZZ Picker Target Bravo")').count() >= 1

    page.keyboard.press("Escape")


def test_reports_type_dropdown_anchors_to_trigger(nav_page):
    """The report-type dropdown must open anchored to its trigger button.

    Previously it landed hundreds of px to the right because a position:fixed
    popover was clipped by an ancestor stacking context. Portaling to <body>
    fixed the alignment."""
    page = nav_page
    page.click('.chrome .nav-btn[data-view="reports"]')
    page.wait_for_selector("#reportTypeSelect", timeout=5000)

    trigger = page.locator('.bh-sel-wrap:has(#reportTypeSelect) .bh-sel-btn')
    trigger.click()
    page.wait_for_selector(".bh-sel-pop", state="visible", timeout=2000)

    tb = trigger.bounding_box()
    pb = page.locator(".bh-sel-pop").bounding_box()
    assert tb
    assert pb
    # Left edges must align within 8px (the bug shifted it ~sidebar-width right).
    assert abs(pb["x"] - tb["x"]) <= 8, f"dropdown not left-aligned: trigger={tb}, pop={pb}"
    # Must sit directly below the trigger, or just above if the popover flips.
    below = abs(pb["y"] - (tb["y"] + tb["height"])) <= 14
    above = abs((pb["y"] + pb["height"]) - tb["y"]) <= 14
    assert below or above, f"dropdown not anchored vertically: trigger={tb}, pop={pb}"
    page.keyboard.press("Escape")


def test_bulk_bar_uses_themed_selects(nav_page):
    """The bulk action bar must use themed BhSelect controls, not native <select> boxes."""
    page = nav_page
    _seed_bug_via_page(page, "ZZ Bulk Bar Item")
    page.click('.chrome .nav-btn[data-view="list"]')
    page.wait_for_selector("#viewList", state="visible", timeout=3000)

    page.check('th.col-select input[type="checkbox"]')  # select all rows on page
    page.wait_for_selector("#bulkBar", state="visible", timeout=3000)

    assert page.locator("#bulkBar select.bulk-select").count() == 0, \
        "bulk bar still uses unthemed native <select>"
    assert page.locator("#bulkBar .bulk-select-wrap .bh-sel-btn").count() == 3, \
        "bulk bar should have 3 themed BhSelect controls (status/priority/env)"
    # Clear selection so the shared page is clean for subsequent tests.
    page.click("#bulkBar .bulk-clear")


def test_video_attachment_opens_custom_player_in_lightbox(nav_page):
    """A video attachment shows a poster thumbnail in the card; clicking "View"
    must open the custom player in a themed lightbox, not the browser's native
    player in a new tab. The control bar must stay visible while paused and
    while playing, and the seek bar must always be grabbable."""
    page = nav_page
    bug_id = _seed_bug_via_page(page, "ZZ Video Bug")
    # Upload a minimal fake mp4 so the attachment is recognized as video by content-type.
    page.evaluate(
        """async (id) => {
            const fd = new FormData();
            const blob = new Blob([new Uint8Array([0, 0, 0, 24])], {type: 'video/mp4'});
            fd.append('file', new File([blob], 'clip.mp4', {type: 'video/mp4'}));
            await fetch(`/api/bugs/${id}/attachments`, {method: 'POST', credentials: 'include', body: fd});
        }""",
        bug_id,
    )
    page.evaluate(
        "(id) => document.dispatchEvent(new CustomEvent('sleuth:open-bug', {detail: {bugId: id}}))",
        bug_id,
    )
    page.wait_for_selector("#modalBug", state="visible", timeout=3000)
    # Must show a poster thumbnail in the card, not an inline player.
    page.wait_for_selector(".attach-video-thumb", timeout=4000)
    assert page.locator(".vplayer").count() == 0, "player should not render inline (use the lightbox)"

    # "View" must open the lightbox player, not a raw new-tab download.
    view_btn = page.locator('.attach-actions [data-act="view-video"]').first
    assert view_btn.count() == 1, "video View action missing"
    view_btn.click()
    page.wait_for_selector(".video-lightbox .vplayer", timeout=3000)
    assert page.locator(".video-lightbox .vplayer-seek-input").count() == 1, "no seek bar"
    assert page.locator(".video-lightbox .vplayer-volume").count() == 1, "no volume control"

    # Control bar must be visible while paused.
    bar_opacity = page.eval_on_selector(".vplayer-bar", "el => getComputedStyle(el).opacity")
    assert float(bar_opacity) > 0.9, f"control bar not visible while paused (opacity {bar_opacity})"

    # Regression: "control bar not visible while playing / can't drag the seek bar".
    # The bar must stay at full opacity during playback (no auto-hide).
    page.evaluate(
        """() => {
            const v = document.querySelector('.video-lightbox .vplayer-video');
            Object.defineProperty(v, 'paused', {configurable: true, get: () => false});
            v.dispatchEvent(new Event('play'));
        }"""
    )
    page.wait_for_timeout(300)
    play_opacity = page.eval_on_selector(".vplayer-bar", "el => getComputedStyle(el).opacity")
    assert float(play_opacity) > 0.9, f"control bar faded while playing (opacity {play_opacity})"

    # Close button must dismiss the lightbox.
    page.locator(".video-lightbox-close").click()
    page.wait_for_selector(".video-lightbox", state="detached", timeout=3000)

    page.click("#modalBug .modal-close")
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)


def test_attachment_drag_and_drop_stages_file(nav_page):
    """Dropping a file on the create-mode dropzone must stage it for upload."""
    page = nav_page
    page.click("#newBugBtn")
    page.wait_for_selector("#modalBug", state="visible", timeout=3000)

    # Synthesize a native drag-and-drop sequence on the create dropzone.
    page.evaluate(
        """() => {
            const zone = document.querySelector('#bugCreateAttachSection .attach-dropzone');
            const dt = new DataTransfer();
            dt.items.add(new File(['hello'], 'dropped.txt', {type: 'text/plain'}));
            for (const t of ['dragenter', 'dragover', 'drop']) {
                const ev = new DragEvent(t, {bubbles: true, cancelable: true});
                Object.defineProperty(ev, 'dataTransfer', {value: dt});
                zone.dispatchEvent(ev);
            }
        }"""
    )
    page.wait_for_selector('#createFilePreview .attach-staged', timeout=3000)
    assert page.locator('.attach-staged-name:has-text("dropped.txt")').count() == 1, \
        "dropped file was not staged"
    page.click("#modalBug .modal-close")
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)
