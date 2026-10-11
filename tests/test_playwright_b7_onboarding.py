"""Playwright flows for B.7: multi-step sign-up with email verification, the first-admin setup wizard,
role-specific tours that anchor to real elements, organization deletion and custom-field values.

Self-hosted stack on a throwaway SQLite DB with a tiny in-process SMTP sink, so the real verification
link is read from the mail the server sends. Skips cleanly without Playwright/Chromium.
Tests share one stack and run in file order.
"""
from __future__ import annotations

import email
import os
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api", reason="Playwright is required only for UI tests")

from playwright.sync_api import expect, sync_playwright  # noqa: E402

from tests.browser_support import dismiss_welcome, launch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADMIN_EMAIL = "admin@b7.local"
ADMIN_PASSWORD = "UiTest1234"
MAILS: list[str] = []  # decoded bodies of everything the server mailed


class _Sink(socketserver.StreamRequestHandler):
    def handle(self):
        w = lambda s: (self.wfile.write(s.encode() + b"\r\n"), self.wfile.flush())  # noqa: E731
        w("220 sink")
        while line := self.rfile.readline():
            cmd = line.decode(errors="replace").strip().upper()
            if cmd.startswith("DATA"):
                w("354 go")
                raw = b""
                while (row := self.rfile.readline()) not in (b".\r\n", b""):
                    raw += row
                msg = email.message_from_bytes(raw)
                body = msg.get_payload(decode=True) or b""
                MAILS.append(body.decode("utf-8", "replace"))
                w("250 ok")
            elif cmd.startswith("QUIT"):
                w("221 bye")
                return
            elif cmd.startswith("EHLO") or cmd.startswith("HELO"):
                w("250 sink")
            else:
                w("250 ok")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def stack():
    smtp_port, port = _free_port(), _free_port()
    sink = socketserver.ThreadingTCPServer(("127.0.0.1", smtp_port), _Sink)
    sink.daemon_threads = True
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{Path(tmp.name) / 'ui.db'}",
        "EMAIL_BACKEND": "smtp", "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp_port),
        "SMTP_USE_TLS": "false", "APP_BASE_URL": base, "ALLOW_PUBLIC_SIGNUP": "true", "EMAIL_VERIFICATION_REQUIRED": "true",
        "SESSION_SECRET": "ui_test_secret_only",
        "BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL, "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "BOOTSTRAP_ADMIN_NAME": "B7 Admin", "APP_NAME": "Bug Hunter",
        "APP_VERSION": os.environ.get("APP_VERSION") or "0.0.0-test",
        "PASSWORD_BREACH_CHECK_ENABLED": "false", "WEB_PUSH_ENABLED": "false",
        "AUTO_LOGIN_ENABLED": "false", "EMAIL_DIGEST_ENABLED": "false",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.fail("self-hosted UI stack did not become healthy in time")
        yield base
    finally:
        proc.kill()
        sink.shutdown()
        tmp.cleanup()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = launch(p)
        yield b
        b.close()


def _api(pg, method, path, body=None):
    return pg.evaluate(
        """async ([m, p, b]) => { const r = await fetch('/api' + p, {method: m, credentials: 'include',
            headers: {'Content-Type': 'application/json'}, body: b ? JSON.stringify(b) : undefined});
            let j = null; try { j = await r.json(); } catch (e) {} return {status: r.status, json: j}; }""",
        [method, path, body],
    )


def _login(browser, base, email_addr, password, **ctx):
    context = browser.new_context(viewport={"width": 1440, "height": 900}, **ctx)
    pg = context.new_page()
    pg.goto(f"{base}/login.html")
    pg.wait_for_load_state("networkidle")
    res = _api(pg, "POST", "/auth/login", {"email": email_addr, "password": password})
    assert res["status"] == 200, res
    return context, pg


def _open_app(pg, base):
    pg.goto(f"{base}/")
    pg.wait_for_selector("#newBugBtn", state="visible", timeout=20000)


def _verify_link() -> str:
    deadline = time.time() + 20
    while time.time() < deadline:
        for body in reversed(MAILS):
            m = re.search(r"http\S+/verify-email\.html\?token=[\w\-]+", body)
            if m:
                return m.group(0)
        time.sleep(0.3)
    pytest.fail("no verification email arrived")


def test_signup_steps_verification_wizard_and_org_deletion(browser, stack):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    pg = context.new_page()
    try:
        pg.goto(f"{stack}/signup.html")
        pg.get_by_label("Organization name").fill("Wizard Co")
        pg.get_by_role("button", name="Continue").click()
        heading = pg.get_by_role("heading", name="Tell us about you")
        expect(heading).to_be_focused()
        pg.get_by_label("Your name").fill("Wanda Wiz")
        pg.get_by_label("Work email").fill("wanda@wizard.test")
        pg.get_by_role("button", name="Continue").click()
        pg.locator("input[name=password]").fill("Wizard1234")
        pg.locator("input[name=confirm_password]").fill("Wizard1234")
        pg.get_by_role("button", name="Create organization").click()
        expect(pg.get_by_role("heading", name="Check your inbox")).to_be_visible()
        # Unconfirmed accounts cannot sign in.
        assert _api(pg, "POST", "/auth/login", {"email": "wanda@wizard.test", "password": "Wizard1234"})["status"] != 200
        # Resend is throttled with a visible countdown.
        pg.get_by_role("button", name="Send a new link").click()
        expect(pg.get_by_role("button", name=re.compile(r"Send a new link \(\d+s\)"))).to_be_disabled()

        pg.goto(_verify_link())
        expect(pg.get_by_role("heading", name="Your email is confirmed")).to_be_visible()
        # The link works once.
        pg.reload()
        expect(pg.get_by_role("heading", name="This link did not work")).to_be_visible()
        pg.goto(f"{stack}/login.html")
        assert _api(pg, "POST", "/auth/login", {"email": "wanda@wizard.test", "password": "Wizard1234"})["status"] == 200

        _open_app(pg, stack)
        expect(pg.locator("#welcomeModal")).to_be_visible()
        pg.get_by_role("button", name="Set up my organization").click()
        wiz = pg.locator("#setupWizard")
        expect(wiz.get_by_role("heading", name=re.compile("Step 1 of 5"))).to_be_visible()
        wiz.get_by_label("Organization name").fill("Wizard Works")
        wiz.get_by_placeholder("#6366f1").fill("#336699")
        wiz.get_by_role("button", name="Save and continue").click()
        expect(wiz.get_by_role("heading", name=re.compile("Step 2 of 5"))).to_be_focused()
        wiz.get_by_label("Project name").fill("Launch")
        wiz.get_by_role("button", name="Create project").click()
        expect(wiz.get_by_role("heading", name=re.compile("Step 3 of 5"))).to_be_visible()

        # Close halfway: it resumes at the same step after a reload.
        pg.keyboard.press("Escape")
        pg.reload()
        pg.wait_for_selector("#newBugBtn", state="visible")
        expect(pg.locator("#setupWizard").get_by_role("heading", name=re.compile("Step 3 of 5"))).to_be_visible()
        pg.locator("#setupWizard").get_by_label(re.compile("Email addresses")).fill("teammate@wizard.test")
        pg.get_by_role("button", name="Send invitations").click()
        expect(pg.locator("#setupWizard").get_by_role("heading", name=re.compile("Step 4 of 5"))).to_be_visible()
        pg.get_by_role("button", name="Continue").click()
        pg.get_by_role("button", name="Finish").click()
        expect(pg.locator("#setupWizard")).to_be_hidden()
        prefs = _api(pg, "GET", "/me/preferences")["json"]
        assert prefs["setup_status"] == "done"
        assert _api(pg, "GET", "/organization")["json"]["name"] == "Wizard Works"
        assert _api(pg, "GET", "/branding")["json"]["accent_color"] == "#336699"
        assert [p["name"] for p in _api(pg, "GET", "/projects")["json"]] == ["General", "Launch"]
        assert len(_api(pg, "GET", "/invitations")["json"]) == 1

        # Organization deletion: dry run, then password + typed name.
        pg.click('.nav-btn[data-view="organization"]')
        pg.get_by_role("button", name="Show what will be deleted").click()
        expect(pg.get_by_role("heading", name="This will permanently delete")).to_be_visible()
        pg.get_by_label(re.compile("Your password")).fill("Wizard1234")
        pg.get_by_label(re.compile("Type the organization name")).fill("wizard works")
        expect(pg.get_by_role("button", name="Delete organization forever")).to_be_disabled()
        pg.get_by_label(re.compile("Type the organization name")).fill("Wizard Works")
        pg.get_by_role("button", name="Delete organization forever").click()
        pg.wait_for_url(re.compile("login.html"))
        assert _api(pg, "POST", "/auth/login", {"email": "wanda@wizard.test", "password": "Wizard1234"})["status"] != 200
    finally:
        context.close()


def test_verify_page_without_a_valid_link(browser, stack):
    context = browser.new_context()
    pg = context.new_page()
    try:
        pg.goto(f"{stack}/verify-email.html?token=bogus")
        expect(pg.get_by_role("heading", name="This link did not work")).to_be_visible()
        expect(pg.get_by_role("button", name="Send a new link")).to_be_disabled()  # needs an address
        pg.goto(f"{stack}/verify-email.html")
        expect(pg.get_by_role("heading", name="Check your inbox")).to_be_visible()
    finally:
        context.close()


def _tour_titles(pg, total):
    """Walk the tour; every step must have a spotlight on a real element. Returns the titles."""
    titles = []
    for i in range(total):
        expect(pg.get_by_text(f"Step {i + 1} of {total}")).to_be_visible()
        expect(pg.locator(".tour-spot"), f"step {i + 1}: {pg.locator('.tour-title').inner_text()}").to_have_count(1)
        titles.append(pg.locator(".tour-title").inner_text())
        if i < total - 1:
            pg.get_by_role("button", name="Next").click()
    pg.get_by_role("button", name="Finish").click()
    return titles


@pytest.fixture(scope="module")
def role_users(browser, stack):
    """One admin sign-in (logins are rate limited) creates a manager, a project lead and a plain user."""
    ctx, admin = _login(browser, stack, ADMIN_EMAIL, ADMIN_PASSWORD)
    try:
        dismiss_welcome(admin)
        for role in ("manager", "lead", "user"):
            pid = _api(admin, "POST", "/projects", {"name": f"P-{role}"})["json"]["id"]
            uid = _api(admin, "POST", "/users", {
                "name": role.title(), "email": f"{role}@b7.local", "role": "manager" if role == "manager" else "user",
                "password": "UiTest1234", "project_ids": [pid],
            })["json"]["id"]
            if role == "lead":
                _api(admin, "PUT", f"/projects/{pid}/members/{uid}", {"role": "lead"})
    finally:
        ctx.close()


@pytest.mark.parametrize("role,total,expected_welcome", [
    ("manager", 8, "Invite your team"),
    ("lead", 6, "Manage your project"),
    ("user", 5, "Plan sprints"),
])
def test_role_welcome_and_tour_anchor_to_real_elements(browser, stack, role_users, role, total, expected_welcome):
    addr = f"{role}@b7.local"
    context, pg = _login(browser, stack, addr, "UiTest1234")
    try:
        _open_app(pg, stack)
        expect(pg.locator("#welcomeModal")).to_contain_text(expected_welcome)
        expect(pg.get_by_role("button", name="Set up my organization")).to_have_count(0)
        pg.get_by_role("button", name="Take the tour").click()
        titles = _tour_titles(pg, total)
        assert len(titles) == total
    finally:
        context.close()


def test_admin_tour_and_custom_field_values_on_items(browser, stack):
    context, pg = _login(browser, stack, ADMIN_EMAIL, ADMIN_PASSWORD)
    try:
        _open_app(pg, stack)
        # (The welcome was dismissed by the earlier fixture.) The tour starts from the account menu,
        # which also offers the wizard again to admins.
        pg.click("#profileBtn")
        expect(pg.locator("#orgSetupBtn")).to_be_visible()
        pg.click("#productTourBtn")
        titles = _tour_titles(pg, 9)
        assert "Organization" in titles and "Sessions" in titles

        pid = _api(pg, "POST", "/projects", {"name": "Fields"})["json"]["id"]
        fid = _api(pg, "POST", f"/projects/{pid}/custom-fields", {"name": "Customer", "field_type": "text"})["json"]["id"]
        pg.reload()
        pg.wait_for_selector("#newBugBtn", state="visible")
        pg.click("#newBugBtn")
        pg.wait_for_selector("#formBug", state="visible")
        pg.fill("#formBug input[name='title']", "Item with a custom value")
        pg.locator("#formBug select[name='project_id']").select_option(str(pid))
        pg.get_by_label(re.compile("Customer")).fill("Acme")
        pg.click("#bugSubmitBtn")
        pg.wait_for_selector("#modalBug", state="hidden")
        bugs = _api(pg, "GET", f"/bugs?project_id={pid}")["json"]
        items = bugs["items"] if isinstance(bugs, dict) else bugs
        bug_id = next(b["id"] for b in items if b["title"] == "Item with a custom value")
        values = _api(pg, "GET", f"/bugs/{bug_id}/custom-values")["json"]
        assert values == [{"field_id": fid, "value": "Acme"}]
    finally:
        context.close()
