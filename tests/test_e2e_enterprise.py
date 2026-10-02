"""Browser end-to-end journeys of the enterprise edition, on the same real-server harness as
test_e2e_browser.py: sign-up, two-factor sign-in, account settings, organization settings
(branding, invitations, webhooks), accepting an invitation, project members and custom fields,
saved views, and the public pages (with axe-core on every new screen).

Run: pytest tests/test_e2e_enterprise.py -m ui
"""
from __future__ import annotations

import hashlib
import itertools
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api", reason="Playwright is required for the browser suite")

import pyotp
from playwright.sync_api import expect

from tests.test_e2e_browser import (  # noqa: F401  (fixtures are shared with the main suite)
    ADMIN_EMAIL,
    AXE,
    NAV,
    THEMES,
    Problems,
    _axe,
    _database,
    _fmt,
    _free_port,
    _home,
    _serve,
    admin_state,
    browser,
    browser_name,
    user_state,
)

PASSWORD = "E2eNewPass42"
EXPECTED_404 = "console: Failed to load resource: the server responded with a status of 404 (Not Found)"
EXPECTED_422 = "console: Failed to load resource: the server responded with a status of 422 (Unprocessable Entity)"
ACCENT_VAR = "getComputedStyle(document.documentElement).getPropertyValue('--accent-fill').trim()"
_client_numbers = itertools.count(1)


@pytest.fixture(scope="module")
def server(browser_name):
    """The main suite's real server, behind a trusted proxy hop: every browser context presents
    its own client address, so the sign-up and login rate limits (which stay on) count each test
    separately instead of the whole module."""
    workdir = Path(tempfile.mkdtemp(prefix="bh-e2e-ent-"))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with _database(workdir) as database_url:
        yield from _serve(workdir, port, base, database_url, {"TRUST_PROXY_FORWARDED_FOR": "true"})


@pytest.fixture
def open_page(browser):
    """Factory: a fresh browser context with its own client address, optionally signed in."""
    contexts = []

    def _open(state=None, viewport=None, bypass_csp=False, theme=None):
        n = next(_client_numbers)
        ctx = browser.new_context(
            viewport=viewport or {"width": 1366, "height": 850}, storage_state=state, bypass_csp=bypass_csp,
            extra_http_headers={"X-Forwarded-For": f"10.{n // 250}.{n % 250}.7"},
        )
        if theme:
            ctx.add_init_script(f"localStorage.setItem('theme', '{theme}')")
        contexts.append(ctx)
        pg = ctx.new_page()
        pg.set_default_timeout(15000)
        return pg, Problems(pg)

    yield _open
    for ctx in contexts:
        ctx.close()


def _signup(pg, base, org="Globex", email=None, name="Gina Owner"):
    email = email or f"owner-{uuid.uuid4().hex[:8]}@globex.test"
    pg.goto(f"{base}/signup.html")
    pg.fill('input[name="organization_name"]', org)
    pg.fill('input[name="name"]', name)
    pg.fill('input[name="email"]', email)
    pg.fill('input[name="password"]', PASSWORD)
    pg.fill('input[name="confirm_password"]', PASSWORD)
    pg.click("#signupSubmit")
    pg.wait_for_url(f"{base}/")
    expect(pg.locator("#accountName")).not_to_have_text("")
    # Callers often reload or navigate next; WebKit reports every request that a navigation
    # cancels as a page error, so let the home page finish loading first.
    pg.wait_for_load_state("networkidle")
    return email


def _open_account(pg, tab=None):
    pg.click("#profileBtn")
    pg.click("#accountSettingsBtn")
    expect(pg.locator("#modalAccount")).to_be_visible()
    if tab:
        pg.click(f"#accountTab-{tab}")
        expect(pg.locator(f"#accountTab-{tab}")).to_have_attribute("aria-selected", "true")


def _open_org(pg, tab=None):
    pg.click(f'{NAV}[data-view="organization"]')
    expect(pg.locator("#viewOrganization")).to_be_visible()
    if tab:
        pg.click(f"#orgTab-{tab}")
        expect(pg.locator(f"#orgTab-{tab}")).to_have_attribute("aria-selected", "true")


# --- sign-up and the organization badge ----------------------------------------------------------


def test_signup_creates_an_organization_and_signs_in(server, open_page):
    pg, problems = open_page()
    _signup(pg, server["base"], org="Globex Signup")
    expect(pg.locator("#orgBadge")).to_contain_text("Globex Signup")
    # the new organization starts with its General project and sees nothing of the seeded one
    expect(pg.locator("#projectList")).to_contain_text("General")
    expect(pg.locator("body")).not_to_contain_text("E2E Project")
    problems.assert_clean()


def test_signup_validates_passwords_in_the_page(server, open_page):
    pg, problems = open_page()
    pg.goto(f"{server['base']}/signup.html")
    pg.fill('input[name="organization_name"]', "Mismatch Inc")
    pg.fill('input[name="name"]', "Mia")
    pg.fill('input[name="email"]', f"mia-{uuid.uuid4().hex[:6]}@mismatch.test")
    pg.fill('input[name="password"]', PASSWORD)
    pg.fill('input[name="confirm_password"]', PASSWORD + "x")
    pg.click("#signupSubmit")
    expect(pg.locator('[role="alert"]')).to_contain_text("don't match")
    assert pg.url.endswith("/signup.html")
    problems.assert_clean()


def test_login_page_offers_signup(server, open_page):
    pg, _ = open_page()
    pg.goto(f"{server['base']}/login.html")
    expect(pg.locator("#showSignup")).to_be_visible()


# --- account settings ----------------------------------------------------------------------------


def test_profile_name_can_be_changed(server, open_page):
    pg, problems = open_page()
    _signup(pg, server["base"], org="Rename Co")
    _open_account(pg)
    pg.get_by_role("textbox", name="Name *").fill("Gina Renamed")
    pg.get_by_role("button", name="Save", exact=True).click()
    expect(pg.locator("#accountName")).to_have_text("Gina Renamed")
    problems.assert_clean()


def test_two_factor_enrolment_then_two_step_login_and_disable(server, open_page):
    base = server["base"]
    pg, problems = open_page()
    email = _signup(pg, base, org="Twofa Co")
    _open_account(pg, "security")
    pg.get_by_role("button", name="Enable 2FA").click()
    expect(pg.get_by_role("img", name="Authenticator setup QR code")).to_be_visible()
    secret = pg.locator("p.hint code").inner_text().strip()
    assert len(secret) >= 16, secret

    pg.get_by_role("textbox", name="Code *").fill(pyotp.TOTP(secret).now())
    pg.get_by_role("button", name="Turn on").click()
    codes = pg.locator(".ent-codes code")
    expect(codes.first).to_be_visible()
    assert codes.count() >= 8
    pg.get_by_role("button", name="I saved them").click()
    problems.assert_clean()

    # sign out, then in again: password first, then the code of the *next* time step (the
    # confirming code is spent)
    pg.keyboard.press("Escape")
    pg.click("#profileBtn")
    pg.locator('.profile-menu button:has-text("Log out")').click()
    pg.locator('#modalConfirm button:has-text("Log out")').click()
    pg.wait_for_url(f"{base}/login.html*")
    pg.fill('input[name="email"]', email)
    pg.fill('input[name="password"]', PASSWORD)
    pg.click('#loginForm button[type="submit"]')
    expect(pg.locator("#totpForm")).to_be_visible()
    # a wrong code stays on the step
    pg.fill('#totpForm input[name="code"]', "000000")
    pg.click('#totpForm button[type="submit"]')
    expect(pg.locator("#totpForm .auth-alert")).to_contain_text("Invalid code")
    pg.fill('#totpForm input[name="code"]', pyotp.TOTP(secret).at(time.time() + 30))
    pg.click('#totpForm button[type="submit"]')
    pg.wait_for_url(f"{base}/")
    expect(pg.locator("#accountName")).not_to_have_text("")

    _open_account(pg, "security")
    pg.locator("#securityPassword").fill(PASSWORD)
    pg.get_by_role("button", name="Turn off 2FA").click()
    expect(pg.get_by_role("button", name="Enable 2FA")).to_be_visible()


def test_notification_preferences_are_saved(server, open_page):
    pg, problems = open_page()
    _signup(pg, server["base"], org="Prefs Co")
    _open_account(pg, "notifications")
    box = pg.get_by_role("checkbox", name="Other activity")
    expect(box).to_be_checked()
    with pg.expect_response(lambda r: r.url.endswith("/api/notifications/preferences") and r.request.method == "PUT"):
        box.uncheck()
    pg.reload()
    _open_account(pg, "notifications")
    expect(pg.get_by_role("checkbox", name="Other activity")).not_to_be_checked()
    problems.assert_clean()


def test_account_deletion_needs_the_password_and_removes_the_account(server, open_page):
    base = server["base"]
    pg, problems = open_page()
    email = _signup(pg, base, org="Leaver Co")
    # a second admin, so the first may leave
    res = pg.context.request.post(f"{base}/api/users", data={
        "name": "Second", "email": f"second-{uuid.uuid4().hex[:6]}@leaver.test", "role": "admin", "password": PASSWORD})
    assert res.status == 201, res.text()
    _open_account(pg, "privacy")
    pg.locator("#deleteAccountPassword").fill(PASSWORD)
    pg.get_by_role("button", name="Delete my account").click()
    pg.locator('#modalConfirm button:has-text("Delete my account")').click()
    pg.wait_for_url(f"{base}/login.html*")
    login = pg.context.request.post(f"{base}/api/auth/login", data={"email": email, "password": PASSWORD})
    assert login.status == 401


def test_data_export_downloads_a_json_file(server, open_page):
    pg, _ = open_page()
    _signup(pg, server["base"], org="Export Co")
    _open_account(pg, "privacy")
    with pg.expect_download() as info:
        pg.get_by_role("button", name="Download my data").click()
    assert info.value.suggested_filename.endswith(".json")


# --- organization settings -----------------------------------------------------------------------


def _wait_for_accent(pg, wanted):
    """Poll the page's accent variable (wait_for_function evaluates a string, which the CSP forbids)."""
    for _ in range(60):
        if (pg.evaluate(ACCENT_VAR) == wanted) is True:
            return
        pg.wait_for_timeout(100)
    raise AssertionError(f"--accent-fill never became {wanted!r}; it is {pg.evaluate(ACCENT_VAR)!r}")


def test_branding_accent_is_applied_to_the_whole_app(server, open_page, admin_state):
    pg, problems = open_page(admin_state)
    _home(pg, server["base"])
    before = pg.evaluate(ACCENT_VAR)
    assert before != "rgb(204, 51, 0)"
    _open_org(pg, "branding")
    pg.get_by_placeholder("#6366f1").fill("#cc3300")
    pg.get_by_role("button", name="Save", exact=True).click()
    expect(pg.locator(".toast, #toasts")).to_contain_text("Branding updated")
    _wait_for_accent(pg, "rgb(204, 51, 0)")
    # leave the shared admin organization as it was
    pg.get_by_placeholder("#6366f1").fill("")
    pg.get_by_role("button", name="Save", exact=True).click()
    _wait_for_accent(pg, before)
    problems.assert_clean()


def test_organization_details_can_be_renamed(server, open_page):
    pg, problems = open_page()
    _signup(pg, server["base"], org="Before Rename")
    _open_org(pg, "general")
    pg.get_by_role("textbox", name="Name *").fill("After Rename")
    pg.get_by_role("button", name="Save", exact=True).click()
    expect(pg.locator("#orgBadge")).to_contain_text("After Rename")
    problems.assert_clean()


def test_invitation_is_listed_and_can_be_revoked(server, open_page):
    pg, problems = open_page()
    _signup(pg, server["base"], org="Invite Co")
    _open_org(pg, "invitations")
    pg.get_by_role("textbox", name="Email *").fill("newcomer@invite.test")
    pg.get_by_role("button", name="Send invitation").click()
    row = pg.locator("tr", has_text="newcomer@invite.test")
    expect(row).to_contain_text("pending")
    pg.get_by_role("button", name="Revoke").click()
    pg.locator('#modalConfirm button:has-text("Revoke")').click()
    expect(row).to_contain_text("revoked")
    problems.assert_clean()


def test_webhook_secret_is_shown_once(server, open_page):
    pg, problems = open_page()
    _signup(pg, server["base"], org="Hooks Co")
    _open_org(pg, "webhooks")
    pg.get_by_role("textbox", name="Name *").fill("CI")
    pg.get_by_role("textbox", name="URL *").fill("https://hooks.example.com/e2e")
    pg.get_by_role("button", name="Create webhook").click()
    secret = pg.locator(".ent-secret")
    expect(secret).to_be_visible()
    assert len(secret.inner_text().strip()) >= 24
    pg.get_by_role("button", name="I saved it").click()
    expect(secret).to_have_count(0)
    expect(pg.locator(".ent-hook")).to_contain_text("CI")
    # a private target is refused with a readable message
    pg.get_by_role("textbox", name="URL *").fill("http://127.0.0.1/hook")
    pg.get_by_role("textbox", name="Name *").fill("Local")
    pg.get_by_role("button", name="Create webhook").click()
    expect(pg.locator(".toast, #toasts")).to_contain_text("public host")
    # the refusal is an expected 422; Chromium and WebKit log it as a failed resource, Firefox does not
    assert set(problems.items) <= {EXPECTED_422}


def test_plain_users_have_no_organization_screen(server, open_page, user_state):
    pg, problems = open_page(user_state)
    _home(pg, server["base"])
    expect(pg.locator('.nav-btn[data-view="organization"]')).to_have_count(0)
    assert pg.evaluate("async () => (await fetch('/api/invitations')).status") == 403
    assert pg.evaluate("async () => (await fetch('/api/webhooks')).status") == 403
    problems.assert_clean()


# --- accepting an invitation ---------------------------------------------------------------------


def _plant_invitation(server, token, email, org_email=ADMIN_EMAIL, project_id=None):
    """Insert an invitation with a known token straight into the SQLite database."""
    url = server["database_url"]
    if not url.startswith("sqlite:///"):
        pytest.skip("planting an invitation needs the SQLite test database")
    con = sqlite3.connect(Path(url.removeprefix("sqlite:///")))
    try:
        org_id = con.execute("SELECT org_id FROM users WHERE email = ?", (org_email,)).fetchone()[0]
        now = datetime.now(timezone.utc)
        con.execute(
            "INSERT INTO invitations (org_id, email, role, token_hash, invited_by_name, initial_project_ids,"
            " expires_at, created_at) VALUES (?, ?, 'user', ?, 'E2E Admin', ?, ?, ?)",
            (org_id, email, hashlib.sha256(token.encode()).hexdigest(),
             "" if project_id is None else str(project_id),
             (now + timedelta(days=7)).isoformat(sep=" "), now.isoformat(sep=" ")),
        )
        con.commit()
    finally:
        con.close()


def test_accepting_an_invitation_joins_the_organization(server, open_page):
    base = server["base"]
    token = uuid.uuid4().hex
    email = f"joiner-{uuid.uuid4().hex[:6]}@e2e.test"
    _plant_invitation(server, token, email, project_id=server["project_id"])
    pg, problems = open_page()
    pg.goto(f"{base}/accept-invite.html?token={token}")
    expect(pg.locator("#acceptForm")).to_contain_text("E2E Admin")
    expect(pg.locator("#acceptForm")).to_contain_text(email)
    pg.fill('input[name="name"]', "Joe Joiner")
    pg.fill('input[name="password"]', PASSWORD)
    pg.fill('input[name="confirm_password"]', PASSWORD)
    pg.click("#acceptSubmit")
    pg.wait_for_url(f"{base}/")
    expect(pg.locator("#accountName")).to_have_text("Joe Joiner")
    # joined the listed project, and has no organization screen as a plain user
    expect(pg.locator("#projectList")).to_contain_text("E2E Project")
    expect(pg.locator('.nav-btn[data-view="organization"]')).to_have_count(0)
    problems.assert_clean()


def test_a_bad_invitation_link_explains_itself(server, open_page):
    pg, problems = open_page()
    pg.goto(f"{server['base']}/accept-invite.html?token=nope")
    expect(pg.locator('[role="alert"]')).to_be_visible()
    expect(pg.locator("#acceptForm")).to_have_count(0)
    pg.goto(f"{server['base']}/accept-invite.html")
    expect(pg.locator('[role="alert"]')).to_contain_text("missing")
    # the unknown token is an expected 404; Chromium and WebKit log it as a failed resource, Firefox does not
    assert set(problems.items) <= {EXPECTED_404}


# --- projects: members and custom fields ---------------------------------------------------------


def test_project_members_and_custom_fields_end_to_end(server, open_page):
    base = server["base"]
    pg, problems = open_page()
    _signup(pg, base, org="Fields Co")
    api = pg.context.request
    assert api.post(f"{base}/api/users", data={
        "name": "Helper", "email": f"helper-{uuid.uuid4().hex[:6]}@fields.test", "role": "user",
        "password": PASSWORD}).status == 201
    pg.reload()
    pg.locator('[data-act="open-project"]').first.click()
    expect(pg.locator("#modalProject")).to_be_visible()

    # members: add Helper as a lead
    pg.get_by_label("Person to add").select_option(index=1)
    pg.get_by_label("Role of the new member").select_option("lead")
    pg.get_by_role("button", name="Add", exact=True).click()
    expect(pg.locator("#projectMembers")).to_contain_text("Helper")

    # a required custom field
    pg.get_by_label("Field name").fill("Customer")
    pg.get_by_role("checkbox", name="Required").check()
    pg.get_by_role("button", name="Add field").click()
    expect(pg.locator("#projectCustomFields")).to_contain_text("Customer")
    pg.keyboard.press("Escape")

    # a new item must fill the required field
    pg.click('button:has-text("New")')
    expect(pg.locator("#modalBug")).to_be_visible()
    expect(pg.locator("#bugCustomFields")).to_be_visible()
    pg.fill('input[name="title"]', "Needs a customer")
    pg.locator('#modalBug button[type="submit"]').first.click()
    expect(pg.locator(".toast, #toasts")).to_contain_text("required field")
    pg.get_by_role("textbox", name="Customer *").fill("Initech")
    pg.locator('#modalBug button[type="submit"]').first.click()
    expect(pg.locator("body")).to_contain_text("Needs a customer")
    problems.assert_clean()


# --- saved views ---------------------------------------------------------------------------------


def test_saved_view_round_trip(server, open_page, admin_state):
    pg, problems = open_page(admin_state)
    _home(pg, server["base"])
    name = f"High priority {uuid.uuid4().hex[:4]}"
    pg.locator('#filterBar [data-filter="priority"], #filterBar button:has-text("Priorities")').first.click()
    pg.get_by_role("option", name="High").or_(pg.locator(".ms-row:has-text('High')")).first.click()
    pg.keyboard.press("Escape")
    pg.get_by_role("button", name="Save view", exact=True).click()
    pg.get_by_label("View name").fill(name)
    pg.get_by_role("button", name="Save", exact=True).click()
    expect(pg.get_by_role("combobox", name="Saved views")).to_contain_text(name)

    pg.click("#clearFiltersBtn")
    pg.get_by_role("combobox", name="Saved views").select_option(label=name)
    expect(pg.locator("#bugTable")).to_contain_text("E2E deep linked bug")
    pg.get_by_role("button", name="Delete view").click()
    pg.locator('#modalConfirm button:has-text("Delete")').click()
    expect(pg.get_by_role("combobox", name="Saved views")).not_to_contain_text(name)
    problems.assert_clean()


# --- public pages --------------------------------------------------------------------------------


def test_privacy_and_delete_account_pages_are_public(server, open_page):
    pg, problems = open_page()
    pg.goto(f"{server['base']}/privacy.html")
    expect(pg.get_by_role("heading", name="Privacy policy")).to_be_visible()
    expect(pg.locator("body")).to_contain_text("days")
    pg.goto(f"{server['base']}/delete-account.html")
    expect(pg.locator("#deleteForm")).to_be_visible()
    problems.assert_clean()


def test_delete_account_page_removes_the_account(server, open_page):
    base = server["base"]
    owner, _ = open_page()
    email = _signup(owner, base, org="Page Delete Co")
    assert owner.context.request.post(f"{base}/api/users", data={
        "name": "Backup", "email": f"backup-{uuid.uuid4().hex[:6]}@pd.test", "role": "admin",
        "password": PASSWORD}).status == 201
    pg, problems = open_page()
    pg.goto(f"{base}/delete-account.html")
    pg.fill('input[name="email"]', email)
    pg.fill('input[name="password"]', PASSWORD)
    pg.click("#deleteSubmit")
    expect(pg.locator("body")).to_contain_text("has been deleted")
    problems.assert_clean()


# --- accessibility of every new screen -----------------------------------------------------------


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page", ["signup.html", "accept-invite.html?token=x", "privacy.html", "delete-account.html"])
def test_public_pages_have_no_serious_a11y_violations(server, open_page, page, theme):
    pg, _ = open_page(bypass_csp=True, theme=theme)
    pg.goto(f"{server['base']}/{page}")
    pg.wait_for_load_state("networkidle")
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("tab", ["general", "branding", "invitations", "webhooks"])
def test_organization_tabs_have_no_serious_a11y_violations(server, open_page, admin_state, tab, theme):
    pg, _ = open_page(admin_state, bypass_csp=True, theme=theme)
    _home(pg, server["base"])
    _open_org(pg, tab)
    pg.wait_for_load_state("networkidle")
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("tab", ["profile", "security", "notifications", "privacy"])
def test_account_tabs_have_no_serious_a11y_violations(server, open_page, admin_state, tab, theme):
    pg, _ = open_page(admin_state, bypass_csp=True, theme=theme)
    _home(pg, server["base"])
    _open_account(pg, tab)
    pg.wait_for_load_state("networkidle")
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
def test_project_settings_and_filter_bar_have_no_serious_a11y_violations(server, open_page, admin_state, theme):
    pg, _ = open_page(admin_state, bypass_csp=True, theme=theme)
    _home(pg, server["base"])
    pg.wait_for_load_state("networkidle")
    violations = _axe(pg)
    assert violations == [], _fmt(violations)
    pg.locator('[data-act="open-project"]').first.click()
    expect(pg.locator("#projectMembers")).to_be_visible()
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


def test_phone_width_organization_and_account_have_no_horizontal_overflow(server, open_page, admin_state):
    pg, problems = open_page(admin_state, viewport={"width": 375, "height": 812})
    _home(pg, server["base"])
    pg.evaluate("document.querySelector('.nav-btn[data-view=\"organization\"]').click()")
    expect(pg.locator("#viewOrganization")).to_be_visible()
    for tab in ("general", "branding", "invitations", "webhooks"):
        pg.click(f"#orgTab-{tab}")
        overflow = pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"{tab}: scrolls horizontally by {overflow}px"
    problems.assert_clean()


def test_audit_trail_exports_csv(server, open_page, admin_state):
    pg, problems = open_page(admin_state)
    _home(pg, server["base"])
    pg.click(f'{NAV}[data-view="audit"]')
    expect(pg.locator("#viewAudit")).to_be_visible()
    with pg.expect_download() as info:
        pg.click("#auditExportBtn")
    assert info.value.suggested_filename == "audit.csv"
    problems.assert_clean()
