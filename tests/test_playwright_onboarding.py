"""Playwright flows for A.5 (motion, reduced motion) and A.6 (welcome, tour, help center).

Self-hosted stack on a throwaway SQLite DB, like test_playwright_project_state.py. Skips cleanly
without Playwright/Chromium. The tests share one admin account, so they run in file order:
the welcome/tour flow needs the account to be brand new.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api", reason="Playwright is required only for UI tests")

from playwright.sync_api import expect, sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ADMIN_EMAIL = "admin@onboard.local"
ADMIN_PASSWORD = "UiTest1234"

PREFS_JS = """async (body) => {
    const r = await fetch('/api/me/preferences', {
        method: body ? 'PUT' : 'GET',
        headers: {'Content-Type': 'application/json'},
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'include'
    });
    return r.json();
}"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def stack():
    port = _free_port()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{Path(tmp.name) / 'ui.db'}",
        "EMAIL_BACKEND": "disabled",
        "SESSION_SECRET": "ui_test_secret_only",
        "BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "BOOTSTRAP_ADMIN_NAME": "UI Admin",
        "APP_NAME": "Bug Hunter",
        "APP_VERSION": os.environ.get("APP_VERSION") or "0.0.0-test",
        "PASSWORD_BREACH_CHECK_ENABLED": "false",
        "WEB_PUSH_ENABLED": "false",
        "AUTO_LOGIN_ENABLED": "false",
        "EMAIL_DIGEST_ENABLED": "false",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
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
        yield base
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
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _open(browser, stack, **ctx):
    """A logged-in page on the SPA, with its own browser context."""
    context = browser.new_context(viewport={"width": 1440, "height": 900}, **ctx)
    pg = context.new_page()
    pg.goto(f"{stack}/login.html")
    pg.wait_for_load_state("networkidle")
    status = pg.evaluate(
        """async ([email, password]) => (await fetch('/api/auth/login', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email, password}), credentials: 'include'})).status""",
        [ADMIN_EMAIL, ADMIN_PASSWORD],
    )
    assert status == 200
    pg.goto(f"{stack}/")
    pg.wait_for_selector("#newBugBtn", state="visible", timeout=20000)
    return context, pg


def _account_menu(pg, item_id):
    pg.click("#profileBtn")
    pg.click(item_id)


def test_welcome_then_resumable_tour(browser, stack):
    context, pg = _open(browser, stack)
    try:
        welcome = pg.locator("#welcomeModal")
        expect(welcome).to_be_visible()
        # Focus is inside the dialog and Maybe later only hides it for now.
        pg.get_by_role("button", name="Maybe later").click()
        expect(welcome).to_be_hidden()
        pg.reload()
        expect(pg.locator("#welcomeModal")).to_be_visible()

        pg.get_by_role("button", name="Take the tour").click()
        tour = pg.get_by_role("dialog", name="Work items")
        expect(tour).to_be_visible()
        pg.get_by_role("button", name="Next").click()
        expect(pg.get_by_role("dialog", name="Sprints")).to_be_visible()
        pg.keyboard.press("Escape")  # pauses the tour
        expect(pg.locator(".tour-card")).to_have_count(0)

        # State is stored on the server: after a reload there is no welcome and no tour.
        pg.reload()
        pg.wait_for_selector("#newBugBtn", state="visible")
        expect(pg.locator("#welcomeModal")).to_be_hidden()
        expect(pg.locator(".tour-card")).to_have_count(0)
        prefs = pg.evaluate(PREFS_JS, None)
        assert prefs["welcome_seen"] is True
        assert (prefs["tour_status"], prefs["tour_step"]) == ("dismissed", 1)

        # Resume from the account menu continues at the saved step, then finish.
        _account_menu(pg, "#productTourBtn")
        expect(pg.get_by_text("Step 2 of 9")).to_be_visible()
        for _ in range(7):
            pg.get_by_role("button", name="Next").click()
        pg.get_by_role("button", name="Finish").click()
        expect(pg.locator(".tour-card")).to_have_count(0)
        assert pg.evaluate(PREFS_JS, None)["tour_status"] == "done"
    finally:
        context.close()


def test_help_center_guides_hints_and_empty_state(browser, stack):
    context, pg = _open(browser, stack)
    try:
        # Empty events list offers the guide.
        pg.click('.nav-btn[data-view="events"]')
        expect(pg.locator("#eventsEmpty")).to_be_visible()
        pg.locator("#eventsEmpty").get_by_role("button", name="Read the guide").click()
        expect(pg.get_by_role("heading", level=1, name="Creating and tracking items")).to_be_visible()

        # Search, open a guide, follow an in-guide link.
        pg.fill("#helpSearch", "digest")
        pg.get_by_role("button", name="Notifications and push").click()
        expect(pg.get_by_role("heading", level=1, name="Notifications and push")).to_be_visible()
        pg.fill("#helpSearch", "")
        pg.get_by_role("button", name="Admin setup").click()
        pg.locator(".help-md").get_by_role("link", name="Roles and permissions").first.click()
        expect(pg.get_by_role("heading", level=1, name="Roles and permissions")).to_be_visible()

        # Contextual ? on a complex field opens the matching guide.
        pg.click('.nav-btn[data-view="list"]')
        pg.click("#newBugBtn")
        pg.wait_for_selector("#formBug", state="visible")
        pg.get_by_role("button", name="Help: Environment").click()
        expect(pg.get_by_text("Where the problem was seen")).to_be_visible()
        pg.keyboard.press("Escape")
        expect(pg.get_by_text("Where the problem was seen")).to_have_count(0)
        pg.get_by_role("button", name="Help: Environment").click()
        pg.locator(".help-hint-more").click()
        expect(pg.get_by_role("heading", level=1, name="Creating and tracking items")).to_be_visible()

        # Reachable from the account menu too.
        pg.click('.nav-btn[data-view="sprints"]')
        _account_menu(pg, "#helpCenterBtn")
        expect(pg.locator("#viewHelp")).to_be_visible()
    finally:
        context.close()


def test_motion_and_reduced_motion(browser, stack):
    context, pg = _open(browser, stack)
    try:
        pg.click("#newBugBtn")
        pg.wait_for_selector("#modalBug", state="visible")
        assert pg.evaluate(
            "getComputedStyle(document.querySelector('#modalBug .modal-card')).animationName"
        ) == "bh-pop-in"
        pg.click("#modalBug .modal-close")
        # The exit animation keeps it up briefly, then it hides.
        expect(pg.locator("#modalBug")).to_be_hidden()
    finally:
        context.close()

    context, pg = _open(browser, stack, reduced_motion="reduce")
    try:
        pg.click("#newBugBtn")
        pg.wait_for_selector("#modalBug", state="visible")
        assert pg.evaluate(
            "getComputedStyle(document.querySelector('#modalBug .modal-card')).animationName"
        ) == "none"
        assert pg.evaluate(
            "getComputedStyle(document.querySelector('#modalBug .modal-card')).transitionDuration"
        ) in ("0s", "0s, 0s")
        pg.click("#modalBug .modal-close")
        # No exit animation at all: hidden well inside the 200ms an exit would take.
        pg.wait_for_timeout(60)
        assert pg.evaluate("document.querySelector('#modalBug')?.hidden ?? true") is True
    finally:
        context.close()
