"""Browser flows for the organization features: branding, invitations, webhooks, members,
custom fields, saved views, 2FA, email change, data export, account deletion, public sign-up
with verification, and isolation between two organizations.

One self-hosted stack on a throwaway SQLite DB. Mail never leaves the process: the stack runs
through a tiny launcher that records every message to a file, so the tests can follow the links
and codes a real inbox would receive. Each flow signs in through the API (like the other ui
suites), dismisses the welcome dialog, and then drives the real UI with role/label selectors.

Runs against the current UI. Skips cleanly without Playwright/Chromium.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api", reason="Playwright is required only for UI tests")

import pyotp  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from tests.browser_support import dismiss_welcome, launch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADMIN_EMAIL = "admin@orgflows.local"
PASSWORD = "UiTest1234"
PUBLIC_IP_URL = "https://93.184.216.34/hooks/bug-hunter"  # a literal public IP: no DNS in tests

LAUNCHER = """
import json, os, sys
sys.path.insert(0, os.environ["BH_ROOT"])
import app.email_service as es

def deliver(subject, to, body, from_address=None):
    with open(os.environ["MAIL_SINK"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"subject": subject, "to": list(to), "body": body}) + "\\n")
    return True

es.deliver = deliver
import uvicorn
from app.main import app
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="warning")
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Stack:
    def __init__(self, base: str, db: Path, sink: Path):
        self.base, self.db, self.sink = base, db, sink

    def mails(self, to: str | None = None) -> list[dict]:
        if not self.sink.exists():
            return []
        rows = [json.loads(line) for line in self.sink.read_text(encoding="utf-8").splitlines() if line]
        return [m for m in rows if to is None or to.lower() in [t.lower() for t in m["to"]]]

    def wait_mail(self, to: str, subject_part: str = "", after: int = 0) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            hits = [m for m in self.mails(to) if subject_part.lower() in m["subject"].lower()]
            if len(hits) > after:
                return hits[-1]
            time.sleep(0.2)
        raise AssertionError(f"no mail to {to} matching {subject_part!r}")

    def link(self, to: str, page_name: str, after: int = 0) -> str:
        mail = self.wait_mail(to, after=after)
        found = re.search(rf"https?://[^\s\"'<>]*{page_name}[^\s\"'<>]*token=[\w\-.~%]+", mail["body"])
        assert found, mail["body"]
        return found.group(0)

    def sql(self, statement: str, *args) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute(statement, args)
            con.commit()
        finally:
            con.close()


@pytest.fixture(scope="module")
def stack():
    port = _free_port()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmp_path = Path(tmp.name)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    db, sink = tmp_path / "flows.db", tmp_path / "mail.jsonl"
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{db}", "EMAIL_BACKEND": "console", "MAIL_SINK": str(sink),
        "BH_ROOT": str(ROOT), "PORT": str(port), "APP_BASE_URL": base,
        "SESSION_SECRET": "ui_test_secret_only", "BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "BOOTSTRAP_ADMIN_PASSWORD": PASSWORD, "BOOTSTRAP_ADMIN_NAME": "Flow Admin",
        "APP_NAME": "Bug Hunter", "APP_VERSION": os.environ.get("APP_VERSION") or "0.0.0-test",
        "PASSWORD_BREACH_CHECK_ENABLED": "false", "WEB_PUSH_ENABLED": "false",
        "AUTO_LOGIN_ENABLED": "false", "EMAIL_DIGEST_ENABLED": "false",
        "TRUST_PROXY_FORWARDED_FOR": "true", "TRUST_PROXY_HOP_COUNT": "1",
        "ALLOW_PUBLIC_SIGNUP": "true", "EMAIL_VERIFICATION_REQUIRED": "true",
    })
    proc = subprocess.Popen([sys.executable, str(launcher)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("self-hosted UI stack did not become healthy in time")
        yield Stack(base, db, sink)
    finally:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        tmp.cleanup()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = launch(p)
        yield b
        b.close()


FETCH = """async ([method, path, body]) => {
    const r = await fetch('/api' + path, {method, credentials: 'include',
        headers: {'Content-Type': 'application/json'}, body: body === null ? undefined : JSON.stringify(body)});
    let data = null;
    try { data = await r.json(); } catch (e) { /* empty body */ }
    return {status: r.status, data};
}"""


def call(page, method: str, path: str, body=None):
    """An API call with the page's session; returns (status, json)."""
    res = page.evaluate(FETCH, [method, path, body])
    return res["status"], res["data"]


_ips = iter(range(1, 250))


def new_ctx(browser, **kw):
    """A browser context with its own client address: the per-IP login throttles (8 a minute)
    stay switched on, and the flows still sign in more often than that."""
    return browser.new_context(extra_http_headers={"X-Forwarded-For": f"10.9.8.{next(_ips)}"}, **kw)


def sign_in(browser, stack: Stack, email: str, password: str = PASSWORD, *, landing: str = "/"):
    """New browser context signed in as ``email``, welcome dismissed, app loaded."""
    ctx = new_ctx(browser, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(f"{stack.base}/login.html")
    page.wait_for_load_state("networkidle")
    status, body = call(page, "POST", "/auth/login", {"email": email, "password": password})
    assert status == 200, f"login of {email} failed ({status}) {body}"
    dismiss_welcome(page)
    page.goto(f"{stack.base}{landing}")
    page.wait_for_selector("#newBugBtn, #viewOrganization, .nav-btn", state="attached", timeout=20000)
    return ctx, page


def open_organization(page, tab: str | None = None):
    page.locator(".chrome .nav-btn[data-view='organization']").click()
    page.wait_for_selector("#viewOrganization")
    if tab:
        page.get_by_role("tab", name=tab).click()


def confirm(page, label: str):
    """Press the button of the confirm dialog (not a same-named button behind it)."""
    page.locator("#modalConfirm").get_by_role("button", name=label, exact=True).click()


def toast(page, text: str):
    expect(page.get_by_text(text).first).to_be_visible()


def make_user(admin_page, name: str, email: str, role: str = "user", project_ids=()):
    status, data = call(admin_page, "POST", "/users", {
        "name": name, "email": email, "role": role, "password": PASSWORD, "project_ids": list(project_ids)})
    assert status == 201, data
    return data["id"]


@pytest.fixture(scope="module")
def admin(browser, stack):
    ctx, page = sign_in(browser, stack, ADMIN_EMAIL)
    yield page
    ctx.close()


# ---------------------------------------------------------------------------------------------
# General + Branding
# ---------------------------------------------------------------------------------------------
def test_general_and_branding(admin, stack):
    open_organization(admin, "General")
    name = admin.get_by_label("Name", exact=False).first
    expect(name).to_be_enabled()
    name.fill("Flow Works Ltd")
    admin.get_by_label("Description").fill("Where the flows live")
    admin.get_by_role("button", name="Save").click()
    toast(admin, "Organization updated")
    status, org = call(admin, "GET", "/organization")
    assert (status, org["name"], org["description"]) == (200, "Flow Works Ltd", "Where the flows live")

    admin.get_by_role("tab", name="Branding").click()
    expect(admin.get_by_label("Accent colour").first).to_be_enabled()
    admin.get_by_label("Logo file").set_input_files({
        "name": "logo.png", "mimeType": "image/png",
        # a 1x1 transparent PNG
        "buffer": bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"),
    })
    expect(admin.get_by_alt_text("Current logo")).to_be_visible()
    admin.locator("#brandAccent").fill("#aa2200")
    admin.get_by_label("Email From address").fill("Flow Works <bugs@flowworks.test>")
    admin.get_by_role("button", name="Save").click()
    toast(admin, "Branding updated")
    status, brand = call(admin, "GET", "/branding")
    assert status == 200
    assert brand["accent_color"] == "#aa2200"
    assert brand["email_from_override"] == "Flow Works <bugs@flowworks.test>"
    assert brand["logo_data_url"].startswith("data:image/png")

    # a bad colour is refused by the form before anything is sent
    admin.locator("#brandAccent").fill("not-a-colour")
    admin.get_by_role("button", name="Save").click()
    toast(admin, "Enter the accent colour as a hex value")
    assert call(admin, "GET", "/branding")[1]["accent_color"] == "#aa2200"

    # survives a reload
    admin.reload()
    open_organization(admin, "Branding")
    expect(admin.locator("#brandAccent")).to_have_value("#aa2200")
    expect(admin.get_by_alt_text("Current logo")).to_be_visible()


# ---------------------------------------------------------------------------------------------
# Invitations: manager limits, lead, send, accept, revoke, expired, used
# ---------------------------------------------------------------------------------------------
def test_invitations_end_to_end(admin, browser, stack):
    open_organization(admin, "Invitations")
    admin.get_by_label("Email", exact=False).first.fill("newbie@flows.test")
    admin.get_by_role("button", name="Send invitation").click()
    toast(admin, "Invitation sent to newbie@flows.test")
    expect(admin.get_by_role("row", name=re.compile("newbie@flows.test.*pending"))).to_be_visible()
    link = stack.link("newbie@flows.test", "accept-invite")

    # accept in another browser, as the invitee
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(link)
    page.get_by_label("Your name", exact=False).fill("Newbie")
    page.locator("input[name='password']").fill(PASSWORD)
    page.locator("#acceptConfirmPassword").fill(PASSWORD)
    page.get_by_role("button", name=re.compile("Create|Join|Accept", re.I)).click()
    page.wait_for_url(lambda u: "accept-invite" not in u, timeout=20000)
    me = call(page, "GET", "/auth/me")
    assert me[1] and me[1].get("email") == "newbie@flows.test", me
    ctx.close()

    # a link works once
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(link)
    expect(page.get_by_role("alert").first).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Create|Join|Accept", re.I))).to_have_count(0)
    ctx.close()

    # revoke from the UI; the link then reports it as no longer valid
    admin.get_by_role("tab", name="Invitations").click()
    admin.get_by_label("Email", exact=False).first.fill("revoked@flows.test")
    admin.get_by_role("button", name="Send invitation").click()
    toast(admin, "Invitation sent to revoked@flows.test")
    revoked_link = stack.link("revoked@flows.test", "accept-invite")
    admin.get_by_role("row", name=re.compile("revoked@flows.test")).get_by_role("button", name="Revoke").click()
    confirm(admin, "Revoke")
    toast(admin, "Invitation revoked")
    expect(admin.get_by_role("row", name=re.compile("revoked@flows.test.*revoked"))).to_be_visible()
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(revoked_link)
    expect(page.get_by_role("alert").first).to_be_visible()
    ctx.close()

    # expired: age the row in the database
    admin.get_by_label("Email", exact=False).first.fill("late@flows.test")
    admin.get_by_role("button", name="Send invitation").click()
    toast(admin, "Invitation sent to late@flows.test")
    late_link = stack.link("late@flows.test", "accept-invite")
    stack.sql("UPDATE invitations SET expires_at = '2001-01-01 00:00:00' WHERE lower(email) = 'late@flows.test'")
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(late_link)
    expect(page.get_by_role("alert").first).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Create|Join|Accept", re.I))).to_have_count(0)
    ctx.close()


def test_manager_and_lead_limits_on_invitations(admin, browser, stack):
    status, project = call(admin, "POST", "/projects", {"name": "Invite Land", "color": "#336699"})
    assert status == 201
    make_user(admin, "Mina Manager", "mina@flows.test", "manager", [project["id"]])
    lead_id = make_user(admin, "Lena Lead", "lena@flows.test", "user", [project["id"]])
    assert call(admin, "PUT", f"/projects/{project['id']}/members/{lead_id}", {"role": "lead"})[0] == 200

    ctx, mgr = sign_in(browser, stack, "mina@flows.test")
    open_organization(mgr)
    # managers see only the Invitations tab and cannot offer the admin role
    expect(mgr.get_by_role("tab")).to_have_count(1)
    expect(mgr.get_by_role("tab", name="Invitations")).to_be_visible()
    options = mgr.get_by_label("Role").first.locator("option")
    assert [o.strip() for o in options.all_text_contents()] == ["User", "Manager"]
    status, data = call(mgr, "POST", "/invitations", {"email": "sneaky@flows.test", "role": "admin"})
    assert status == 403, data
    assert call(mgr, "GET", "/webhooks")[0] == 403
    assert call(mgr, "PUT", "/organization", {"name": "Hijacked"})[0] == 403
    ctx.close()

    # a project lead is a plain user for the organization: no Organization page, no invitations
    ctx, lead = sign_in(browser, stack, "lena@flows.test")
    expect(lead.locator(".chrome .nav-btn[data-view='organization']")).to_have_count(0)
    assert call(lead, "POST", "/invitations", {"email": "x@flows.test"})[0] == 403
    assert call(lead, "GET", "/invitations")[0] == 403
    ctx.close()


# ---------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------
def test_webhook_lifecycle(admin, stack):
    open_organization(admin, "Webhooks")
    admin.get_by_label("Name", exact=False).first.fill("Deploy hook")
    admin.get_by_label("URL", exact=False).fill(PUBLIC_IP_URL)
    admin.get_by_role("button", name="Create webhook").click()
    notice = admin.get_by_role("alert").filter(has_text="Signing secret for Deploy hook")
    expect(notice).to_be_visible()
    first_secret = notice.locator(".ent-secret").inner_text()
    assert len(first_secret) >= 20
    notice.get_by_role("button", name="I saved it").click()
    expect(notice).to_have_count(0)
    hook = admin.locator(".ent-hook", has_text="Deploy hook")
    expect(hook).to_contain_text("active")

    # private targets are refused, and nothing is created
    for private in ("http://169.254.169.254/latest/meta-data", "http://100.100.100.200/x",
                    "http://127.0.0.1:8080/x", "http://10.0.0.5/x"):
        admin.get_by_label("Name", exact=False).first.fill("Evil hook")
        admin.get_by_label("URL", exact=False).fill(private)
        admin.get_by_role("button", name="Create webhook").click()
        expect(admin.locator(".toast, [role='alert'], [role='status']").filter(
            has_text=re.compile("private|not allowed|public|refus|invalid|local", re.I)).first).to_be_visible()
    expect(admin.locator(".ent-hook", has_text="Evil hook")).to_have_count(0)

    hook.get_by_role("button", name="Test").click()
    toast(admin, "Test ping queued")

    hook.get_by_role("button", name="Pause").click()
    expect(hook).to_contain_text("paused")
    hook.get_by_role("button", name="Resume").click()
    expect(hook).to_contain_text("active")

    hook.get_by_role("button", name="Rotate secret").click()
    confirm(admin, "Rotate")
    notice = admin.get_by_role("alert").filter(has_text="Signing secret for Deploy hook")
    expect(notice).to_be_visible()
    assert notice.locator(".ent-secret").inner_text() != first_secret
    notice.get_by_role("button", name="I saved it").click()

    hook.get_by_role("button", name="Delete").click()
    confirm(admin, "Delete")
    toast(admin, "Webhook deleted")
    expect(admin.locator(".ent-hook", has_text="Deploy hook")).to_have_count(0)
    assert call(admin, "GET", "/webhooks")[1] == []


# ---------------------------------------------------------------------------------------------
# Members + last-lead safeguard, custom fields (project dialog), saved views
# ---------------------------------------------------------------------------------------------
def _open_project(page, name: str):
    page.locator("#projectList .side-item", has_text=name).locator("[data-act='open-project']").click()
    page.wait_for_selector("#projectMembers", state="visible")


def test_members_last_lead_safeguard_and_custom_fields(admin, browser, stack):
    status, project = call(admin, "POST", "/projects", {"name": "Crew Project", "color": "#2255aa"})
    assert status == 201
    pid = project["id"]
    make_user(admin, "Pat Person", "pat@flows.test", "user")
    make_user(admin, "Lee Leader", "lee@flows.test", "user")
    admin.reload()
    admin.wait_for_selector("#newBugBtn", state="visible", timeout=20000)

    _open_project(admin, "Crew Project")
    members = admin.locator("#projectMembers")
    # add Lee as lead and Pat as member through the form
    members.get_by_label("Person to add").select_option(label="Lee Leader (lee@flows.test)") \
        if False else None
    for who, role in (("Lee Leader", "Lead"), ("Pat Person", "Member")):
        select = members.get_by_label("Person to add")
        value = select.locator("option", has_text=who).first.get_attribute("value")
        select.select_option(value)
        members.get_by_label("Role of the new member").select_option(label=role)
        members.get_by_role("button", name=re.compile("Add")).click()
        expect(members.locator(".ent-member", has_text=who)).to_be_visible()

    # the last lead can be neither demoted nor removed
    lee = members.locator(".ent-member", has_text="Lee Leader")
    lee.get_by_label("Project role of Lee Leader").select_option("member")
    expect(admin.get_by_text(re.compile("last project lead", re.I)).first).to_be_visible()
    expect(lee.get_by_label("Project role of Lee Leader")).to_have_value("lead")
    lee.get_by_role("button", name="Remove").click()
    confirm(admin, "Remove")
    expect(admin.get_by_text(re.compile("last project lead", re.I)).first).to_be_visible()
    expect(members.locator(".ent-member", has_text="Lee Leader")).to_have_count(1)

    # with a second lead, the first can step down
    pat = members.locator(".ent-member", has_text="Pat Person")
    pat.get_by_label("Project role of Pat Person").select_option("lead")
    expect(pat.get_by_label("Project role of Pat Person")).to_have_value("lead")
    lee.get_by_label("Project role of Lee Leader").select_option("member")
    expect(lee.get_by_label("Project role of Lee Leader")).to_have_value("member")

    # custom fields: a lead (not an admin) adds, then deletes, one
    fields = admin.locator("#projectCustomFields")
    fields.get_by_label("Field name").fill("Customer tier")
    fields.get_by_label("Field type").select_option(label="Choice")
    fields.get_by_label("Choices, comma-separated").fill("gold, silver")
    fields.get_by_role("button", name="Add field").click()
    expect(fields.locator(".ent-member", has_text="Customer tier")).to_contain_text("gold, silver")
    admin.keyboard.press("Escape")

    ctx, lead = sign_in(browser, stack, "pat@flows.test")
    _open_project(lead, "Crew Project")
    lead_fields = lead.locator("#projectCustomFields")
    lead_fields.get_by_label("Field name").fill("Region")
    lead_fields.get_by_role("button", name="Add field").click()
    expect(lead_fields.locator(".ent-member", has_text="Region")).to_be_visible()
    lead_fields.locator(".ent-member", has_text="Region").get_by_role("button", name="Delete").click()
    confirm(lead, "Delete")
    expect(lead_fields.locator(".ent-member", has_text="Region")).to_have_count(0)
    ctx.close()
    assert [f["name"] for f in call(admin, "GET", f"/projects/{pid}/custom-fields")[1]] == ["Customer tier"]


def test_saved_views(admin, browser, stack):
    status, _ = call(admin, "POST", "/users", {
        "name": "Vera Viewer", "email": "vera@flows.test", "role": "user", "password": PASSWORD})
    assert status == 201
    admin.reload()
    admin.wait_for_selector("#newBugBtn", state="visible", timeout=20000)
    views = admin.locator("#savedViews")
    views.get_by_role("button", name="Save view").click()
    views.get_by_label("View name").fill("Everyone's board")
    views.get_by_label("Share with everyone").check()
    views.get_by_role("button", name="Save", exact=True).click()
    toast(admin, "View saved")
    expect(views.locator("option", has_text="Everyone's board")).to_have_count(1)

    views.get_by_role("button", name="Save view").click()
    views.get_by_label("View name").fill("Mine only")
    views.get_by_role("button", name="Save", exact=True).click()
    toast(admin, "View saved")

    ctx, vera = sign_in(browser, stack, "vera@flows.test")
    options = vera.locator("#savedViews option").all_text_contents()
    assert any("Everyone's board" in o for o in options)
    assert not any("Mine only" in o for o in options)
    # a viewer cannot delete someone else's view
    shared_id = next(v["id"] for v in call(admin, "GET", "/saved-views")[1] if v["name"] == "Everyone's board")
    assert call(vera, "DELETE", f"/saved-views/{shared_id}")[0] in (403, 404)
    ctx.close()

    views.locator("select").select_option(label="🌐 Everyone's board")
    views.get_by_role("button", name="Delete view").click()
    confirm(admin, "Delete")
    expect(views.locator("option", has_text="Everyone's board")).to_have_count(0)


# ---------------------------------------------------------------------------------------------
# 2FA, email change
# ---------------------------------------------------------------------------------------------
def _open_account(page, tab: str):
    page.get_by_role("button", name="Account menu").click()
    page.get_by_role("menuitem", name="Account settings").click()
    page.get_by_role("tab", name=tab).click()


def test_two_factor_enable_and_disable(admin, browser, stack):
    make_user(admin, "Tessa Totp", "tessa@flows.test")

    ctx, page = sign_in(browser, stack, "tessa@flows.test")
    _open_account(page, "Security")
    page.get_by_role("button", name="Enable 2FA").click()
    secret = page.locator("code").filter(has_text=re.compile(r"^[A-Z2-7]{16,}$")).first.inner_text()
    page.get_by_role("textbox", name=re.compile("^Code")).fill(pyotp.TOTP(secret).now())
    page.get_by_role("button", name="Turn on").click()
    expect(page.get_by_role("alert").filter(has_text="recovery")).to_be_visible()
    page.get_by_role("button", name="I saved them").click()
    expect(page.locator("p.hint", has_text="Two-factor authentication is")).to_contain_text("on")
    ctx.close()

    # sign-in now stops at the code step
    other = new_ctx(browser)
    login = other.new_page()
    login.goto(f"{stack.base}/login.html")
    status, data = call(login, "POST", "/auth/login", {"email": "tessa@flows.test", "password": PASSWORD})
    assert status == 200 and data.get("pending_token"), data
    other.close()

    # turn it off again from a session that is already signed in
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(f"{stack.base}/login.html")
    status, data = call(page, "POST", "/auth/login", {"email": "tessa@flows.test", "password": PASSWORD})
    status, _ = call(page, "POST", "/auth/login/totp", {
        "pending_token": data["pending_token"], "code": pyotp.TOTP(secret).at(time.time() + 30)})
    assert status == 200
    dismiss_welcome(page)
    page.goto(stack.base + "/")
    page.wait_for_selector("#newBugBtn", state="visible", timeout=20000)
    _open_account(page, "Security")
    page.get_by_label("Password (needed to change this)").fill(PASSWORD)
    page.get_by_role("button", name="Turn off 2FA").click()
    expect(page.get_by_role("button", name="Enable 2FA")).to_be_visible()
    ctx.close()


def test_email_change_needs_password_and_a_mailed_code(admin, browser, stack):
    make_user(admin, "Erin Email", "erin@flows.test")

    ctx, page = sign_in(browser, stack, "erin@flows.test")
    _open_account(page, "Profile")
    page.get_by_label("New email", exact=False).fill("erin.new@flows.test")
    page.locator("#profileEmailPassword").fill("WrongPassword1")
    page.get_by_role("button", name=re.compile("Send|Request|Continue", re.I)).first.click()
    assert not stack.mails("erin.new@flows.test")
    page.locator("#profileEmailPassword").fill(PASSWORD)
    page.get_by_role("button", name=re.compile("Send|Request|Continue", re.I)).first.click()
    mail = stack.wait_mail("erin.new@flows.test")
    code = re.search(r"\b(\d{6})\b", mail["body"]).group(1)
    page.get_by_role("textbox", name=re.compile("^6-digit code")).fill(code)
    page.get_by_role("button", name=re.compile("Confirm|Verify|Change", re.I)).last.click()
    deadline = time.time() + 10
    while time.time() < deadline and call(page, "GET", "/auth/me")[1]["email"] != "erin.new@flows.test":
        time.sleep(0.3)
    assert call(page, "GET", "/auth/me")[1]["email"] == "erin.new@flows.test"
    # the old address was told
    assert any("erin@flows.test" in m["to"] for m in stack.mails("erin@flows.test"))
    ctx.close()


# ---------------------------------------------------------------------------------------------
# Data export and account deletion
# ---------------------------------------------------------------------------------------------
def test_data_export_and_account_deletion(admin, browser, stack):
    make_user(admin, "Dana Departing", "dana@flows.test")

    ctx, page = sign_in(browser, stack, "dana@flows.test")
    _open_account(page, "Privacy")
    with page.expect_download() as info:
        page.get_by_role("button", name="Download my data").click()
    exported = json.loads(Path(info.value.path()).read_text(encoding="utf-8"))
    assert "dana@flows.test" in json.dumps(exported)
    assert "admin@orgflows.local" not in json.dumps(exported)

    page.get_by_label("Your password", exact=False).fill(PASSWORD)
    page.get_by_role("button", name="Delete my account").click()
    confirm(page, "Delete my account")
    page.wait_for_url(re.compile("login"), timeout=20000)
    ctx.close()

    other = new_ctx(browser)
    check = other.new_page()
    check.goto(f"{stack.base}/login.html")
    assert call(check, "POST", "/auth/login", {"email": "dana@flows.test", "password": PASSWORD})[0] == 401
    other.close()


# ---------------------------------------------------------------------------------------------
# Public sign-up with verification, and isolation between two organizations
# ---------------------------------------------------------------------------------------------
def test_public_signup_requires_verification(browser, stack):
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(f"{stack.base}/signup.html")
    page.get_by_label("Organization name", exact=False).fill("Verified Co")
    page.get_by_label("Your name", exact=False).fill("Vic Founder")
    page.get_by_label("Work email", exact=False).fill("vic@verified.test")
    page.locator("input[name='password']").fill(PASSWORD)
    page.locator("#signupConfirmPassword").fill(PASSWORD)
    page.get_by_role("button", name="Create organization").click()
    page.wait_for_url(re.compile("verify-email"), timeout=20000)

    # no session yet: signing in is refused until the address is verified
    status, _ = call(page, "POST", "/auth/login", {"email": "vic@verified.test", "password": PASSWORD})
    assert status in (401, 403)

    page.goto(stack.link("vic@verified.test", "verify-email"))
    expect(page.get_by_text("Your email is confirmed")).to_be_visible()
    status, _ = call(page, "POST", "/auth/login", {"email": "vic@verified.test", "password": PASSWORD})
    assert status == 200
    ctx.close()


def _signup_and_verify(browser, stack, org: str, email: str):
    ctx = new_ctx(browser)
    page = ctx.new_page()
    page.goto(f"{stack.base}/login.html")
    status, data = call(page, "POST", "/auth/signup", {
        "name": org + " Owner", "email": email, "password": PASSWORD, "organization_name": org})
    assert status in (201, 202), data
    if status == 202:
        link = stack.link(email, "verify-email")
        token = link.rsplit("token=", 1)[1]
        assert call(page, "POST", "/auth/verify-email", {"token": token})[0] in (200, 204)
    ctx.close()


def test_two_organizations_never_see_each_other(admin, browser, stack):
    # Organization A (the bootstrap one): a project, a bug, a webhook, an invitation
    status, project = call(admin, "POST", "/projects", {"name": "Alpha Secret Project", "color": "#aa2200"})
    assert status == 201
    status, bug = call(admin, "POST", "/bugs", {
        "project_id": project["id"], "title": "Alpha secret bug", "description": "alpha only"})
    assert status == 201
    assert call(admin, "POST", "/webhooks", {"name": "Alpha hook", "url": PUBLIC_IP_URL})[0] in (200, 201)
    assert call(admin, "POST", "/invitations", {"email": "alpha-invitee@flows.test"})[0] == 201

    _signup_and_verify(browser, stack, "Beta Org", "boss@beta.test")
    ctx, beta = sign_in(browser, stack, "boss@beta.test")
    beta.wait_for_selector("#projectList", state="attached")
    expect(beta.locator("#projectList")).not_to_contain_text("Alpha Secret Project")
    assert call(beta, "GET", f"/bugs/{bug['id']}")[0] == 404
    assert call(beta, "GET", f"/projects/{project['id']}")[0] == 404
    assert "Alpha secret bug" not in json.dumps(call(beta, "GET", "/bugs")[1])
    open_organization(beta, "Webhooks")
    expect(beta.get_by_text("No webhooks yet.")).to_be_visible()
    expect(beta.locator("body")).not_to_contain_text("Alpha hook")
    beta.get_by_role("tab", name="Invitations").click()
    expect(beta.get_by_text("No invitations yet.")).to_be_visible()
    # the name of the other organization is not even revealed
    assert "Flow" not in json.dumps(call(beta, "GET", "/organization")[1])
    ctx.close()
