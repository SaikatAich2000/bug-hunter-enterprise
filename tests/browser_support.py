"""Shared Playwright launch settings for the browser (`ui`) suites.

PW_BROWSERS          comma list of engines, default "chromium"
PW_EXECUTABLE_PATH   use an installed browser binary instead of Playwright's download
PW_HEADED=1          show the browser window while debugging
"""
from __future__ import annotations

import os

BROWSERS = [b.strip() for b in os.environ.get("PW_BROWSERS", "chromium").split(",") if b.strip()]


def launch(playwright, name: str = "chromium"):
    options: dict = {"headless": os.environ.get("PW_HEADED", "") != "1"}
    executable = os.environ.get("PW_EXECUTABLE_PATH", "").strip()
    if executable:
        options["executable_path"] = executable
    return getattr(playwright, name).launch(**options)


def dismiss_welcome(page) -> None:
    """Mark the first-login welcome as seen for the signed-in user and close it if it is showing.

    Every browser suite that signs in as a fresh user calls this, so the dialog never covers the
    page under test. The page must already be on the app origin with a session."""
    page.evaluate(
        """() => fetch('/api/me/preferences', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({welcome_seen: true}), credentials: 'include'})"""
    )
    later = page.get_by_role("button", name="Maybe later")
    if later.count():
        later.first.click()
