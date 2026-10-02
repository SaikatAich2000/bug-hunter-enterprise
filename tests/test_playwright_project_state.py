"""Playwright behavioral test for the project-state upsert fix.

Source guards (tests/test_frontend_project_state.py) pin the implementation;
this module proves the behavior end-to-end: creating a project via the UI makes
it appear exactly once in the sidebar and in the New Item project selector
without any page reload, while an unrelated form selection survives.

Runs against a self-hosted stack on a temp SQLite DB (no live data touched).
Requires Playwright + Chromium (skips cleanly when unavailable), like
test_e2e_browser.py.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright is required only for the browser UI test script",
)

from playwright.sync_api import expect, sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

ADMIN_EMAIL = "admin@uitest.local"
ADMIN_PASSWORD = "UiTest1234"
PROJECT_NAME = "Zebra Phase UI"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def stack():
    """Self-hosted app on a throwaway SQLite DB; no live data is touched."""
    port = _free_port()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    db_path = Path(tmp.name) / "ui_test.db"
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{db_path}",
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
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        import urllib.request

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
def page(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        pg = context.new_page()
        pg.goto(f"{stack}/login.html")
        pg.wait_for_load_state("networkidle")
        status = pg.evaluate(
            """async ([email, password]) => {
                const res = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({email, password}),
                    credentials: 'include'
                });
                return res.status;
            }""",
            [ADMIN_EMAIL, ADMIN_PASSWORD],
        )
        if status != 200:
            context.close()
            browser.close()
            pytest.fail(f"self-hosted stack login failed with status {status}")
        pg.goto(f"{stack}/")
        pg.wait_for_selector("#newBugBtn", state="visible", timeout=20000)
        yield pg
        context.close()
        browser.close()


def _side_items(page, name: str):
    return page.locator("#projectList .side-item", has_text=name)


def test_new_project_appears_once_without_reload(page):
    # Baseline: the target project does not exist yet.
    assert _side_items(page, PROJECT_NAME).count() == 0

    # Client state that must survive: the app remembers the type of the last
    # item *saved* as the New Item default (an unsaved selection is discarded
    # on close, by design), so save a Task first.
    page.click("#newBugBtn")
    page.wait_for_selector("#formBug", state="visible")
    page.fill("#formBug input[name='title']", "Remembered type probe")
    page.locator("#formBug select[name='item_type']").select_option("Task")
    page.click("#bugSubmitBtn")
    page.wait_for_selector("#modalBug", state="hidden")

    # Mark the window; if the app reloads, this sentinel disappears.
    page.evaluate("window.__bhNoReloadProbe = 42")

    # Create a project via the sidebar + button.
    page.click("#newProjectBtn")
    page.wait_for_selector("#formProject", state="visible")
    page.fill("#formProject input[name='name']", PROJECT_NAME)
    page.click("#formProject button[type='submit']")
    page.wait_for_selector("#formProject", state="hidden")

    # The sidebar shows the new project exactly once (deduplicated upsert),
    # without any reload.
    assert page.evaluate("window.__bhNoReloadProbe") == 42, "the page reloaded"
    assert _side_items(page, PROJECT_NAME).count() == 1

    # The New Item selector now contains the project exactly once…
    page.click("#newBugBtn")
    page.wait_for_selector("#formBug", state="visible")
    options = page.locator(
        "#formBug select[name='project_id'] option", has_text=PROJECT_NAME
    )
    assert options.count() == 1

    # …and the remembered type survives the project creation. expect() waits
    # for the form to finish seeding instead of sampling it mid-render.
    expect(page.locator("#formBug select[name='item_type']")).to_have_value("Task")
