"""Browser end-to-end workflows against a real server process.

Boots uvicorn in a subprocess on a throwaway SQLite database, seeds data through
the public API, then drives a real browser through the main user journeys. Every
test also fails on uncaught page errors, browser console errors and HTTP 5xx
responses, so a view that "renders" while throwing in the background still fails.

Browsers: PW_BROWSERS=chromium (default), or e.g. PW_BROWSERS=chromium,firefox,webkit.
Database: SQLite by default; with BH_TEST_POSTGRES_URL (an admin URL, see
          test_postgres_migration.py) each server gets a temporary PostgreSQL
          database that is dropped afterwards.
Run:      pytest tests/test_e2e_browser.py -m ui
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui
pytest.importorskip("playwright.sync_api", reason="Playwright is required for the browser suite")

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ADMIN_EMAIL = "admin@e2e.test"
ADMIN_PASSWORD = "E2eAdminPass42"
USER_EMAIL = "user@e2e.test"
USER_PASSWORD = "E2eUserPass42"
BROWSERS = [b.strip() for b in os.environ.get("PW_BROWSERS", "chromium").split(",") if b.strip()]

# Views and the element each one renders. `list` is the landing view.
ADMIN_VIEWS = {
    "list": "#viewList",
    "sprints": "#viewSprints",
    "events": "#viewEvents",
    "analytics": "#viewAnalytics",
    "reports": "#viewReports",
    "audit": "#viewAudit",
    "sessions": "#viewSessions",
    "organization": "#viewOrganization",
}
ADMIN_ONLY_OR_MANAGER = ("reports", "audit", "sessions", "organization")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module", params=BROWSERS)
def browser_name(request):
    return request.param


@pytest.fixture(scope="module")
def server(browser_name):
    """A real uvicorn process with its own database, plus seeded fixtures.

    One server per browser keeps each browser's run inside the login rate
    limit (8 attempts/minute/IP) without weakening it.
    """
    workdir = Path(tempfile.mkdtemp(prefix="bh-e2e-"))
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    with _database(workdir) as database_url:
        yield from _serve(workdir, port, base, database_url)


@contextlib.contextmanager
def _database(workdir: Path):
    admin_url = os.environ.get("BH_TEST_POSTGRES_URL", "")
    if not admin_url:
        yield f"sqlite:///{(workdir / 'e2e.db').as_posix()}"
        return
    from sqlalchemy import create_engine, text

    name = f"bh_e2e_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield f"{admin_url.rpartition('/')[0]}/{name}"
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _serve(workdir: Path, port: int, base: str, database_url: str, extra_env: dict | None = None):
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "APP_ENV": "development",
        "APP_VERSION": os.environ.get("APP_VERSION") or "0.0.0-test",
        "APP_BASE_URL": base,
        "SESSION_SECRET": "e2e-session-secret-0123456789abcdef0123456789",
        "COOKIE_SECURE": "false",
        "BOOTSTRAP_ADMIN_EMAIL": ADMIN_EMAIL,
        "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "BOOTSTRAP_ADMIN_NAME": "E2E Admin",
        "AUTO_LOGIN_ENABLED": "false",
        "EMAIL_BACKEND": "disabled",
        "PASSWORD_BREACH_CHECK_ENABLED": "false",
        "WEB_PUSH_ENABLED": "false",
        "SLEUTH_CLOUD_ENABLED": "0",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "PYTHONUTF8": "1",
        **(extra_env or {}),
    }
    log = open(workdir / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early, see {workdir / 'server.log'}")
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("server did not become healthy within 60s")
        yield {"base": base, "database_url": database_url, **_seed(base)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def _seed(base: str) -> dict:
    """Create the data every journey needs, through the real API."""
    with httpx.Client(base_url=base, timeout=30) as c:
        c.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).raise_for_status()
        project = c.post("/api/projects", json={"name": "E2E Project"}).json()
        pid = project["id"]
        board = c.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
        today = date.today()
        sprint = c.post(f"/api/agile/sprints?board_id={board}", json={
            "name": "E2E Sprint", "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=13)).isoformat(),
        }).json()
        story = c.post("/api/agile/work-items", json={
            "project_id": pid, "title": "E2E drag me story", "item_type": "Story",
            "story_points": 3, "sprint_id": sprint["id"],
        })
        story.raise_for_status()
        c.post(f"/api/agile/sprints/{sprint['id']}/start",
               json={"version": sprint["version"]}).raise_for_status()
        bug = c.post("/api/bugs", json={
            "title": "E2E deep linked bug", "project_id": pid, "item_type": "Bug",
            "priority": "High", "environment": "DEV",
        })
        bug.raise_for_status()
        event = c.post("/api/events", json={
            "name": "E2E Standup", "description": "E2E event deep link target", "project_id": pid,
        })
        event.raise_for_status()
        c.post("/api/users", json={
            "name": "E2E User", "email": USER_EMAIL, "password": USER_PASSWORD,
            "role": "user", "project_ids": [pid],
        }).raise_for_status()
        columns = c.get(f"/api/agile/boards/{board}/view", params={"sprint_id": sprint["id"]}).json()["columns"]
    return {
        "project_id": pid, "board_id": board, "sprint_id": sprint["id"],
        "story": story.json(), "bug": bug.json(), "event": event.json(),
        "columns": {col["category"]: col["name"] for col in columns},
    }


@pytest.fixture(scope="module")
def browser(browser_name):
    with sync_playwright() as p:
        b = getattr(p, browser_name).launch()
        yield b
        b.close()


def _session_state(base: str, email: str, password: str) -> dict:
    """Log in through the API once and return a Playwright storage state."""
    with httpx.Client(base_url=base, timeout=30) as c:
        c.post("/api/auth/login", json={"email": email, "password": password}).raise_for_status()
        cookies = [
            {"name": ck.name, "value": ck.value, "domain": "127.0.0.1", "path": "/",
             "httpOnly": True, "secure": False, "sameSite": "Lax", "expires": -1}
            for ck in c.cookies.jar
        ]
    return {"cookies": cookies, "origins": []}


@pytest.fixture(scope="module")
def admin_state(server):
    return _session_state(server["base"], ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def user_state(server):
    return _session_state(server["base"], USER_EMAIL, USER_PASSWORD)


class Problems:
    """Uncaught errors, console errors and 5xx responses seen by one page."""

    def __init__(self, page):
        self.items: list[str] = []
        page.on("pageerror", lambda exc: self.items.append(f"pageerror: {exc}"))
        page.on("console", self._console)
        page.on("response", lambda r: r.status >= 500 and self.items.append(f"HTTP {r.status} {r.url}"))

    def _console(self, msg):
        if msg.type != "error":
            return
        text = msg.text
        # A 401/403 on purpose (logged-out probe, forbidden check) is logged by
        # the browser as a failed resource; the response itself is asserted.
        if "Failed to load resource" in text and ("401" in text or "403" in text):
            return
        self.items.append(f"console: {text}")

    def assert_clean(self):
        assert self.items == [], "\n".join(self.items)


@pytest.fixture
def open_page(browser):
    """Factory: a fresh browser context, optionally already signed in."""
    contexts = []

    def _open(state=None, viewport=None, bypass_csp=False, theme=None):
        # bypass_csp only for injecting axe-core; the app's CSP blocks inline scripts.
        ctx = browser.new_context(viewport=viewport or {"width": 1366, "height": 850},
                                  storage_state=state, bypass_csp=bypass_csp)
        if theme:
            ctx.add_init_script(f"localStorage.setItem('theme', '{theme}')")
        contexts.append(ctx)
        pg = ctx.new_page()
        pg.set_default_timeout(15000)
        return pg, Problems(pg)

    yield _open
    for ctx in contexts:
        ctx.close()


def _admin_api(base: str, state: dict) -> httpx.Client:
    """An API client on the module's admin session: seeding data costs no
    extra login, so the suite stays inside the login rate limit."""
    client = httpx.Client(base_url=base, timeout=30)
    for cookie in state["cookies"]:
        client.cookies.set(cookie["name"], cookie["value"], domain="127.0.0.1", path="/")
    return client


def _home(pg, base):
    pg.goto(f"{base}/")
    expect(pg.locator("#accountName")).not_to_have_text("")


NAV = 'header nav[aria-label="Main sections"] .nav-btn'



def _login(pg, base, email=ADMIN_EMAIL, password=ADMIN_PASSWORD, *, expect_url=None):
    if "/login" not in pg.url:
        pg.goto(f"{base}/login.html")
    pg.fill('input[name="email"]', email)
    pg.fill('input[name="password"]', password)
    pg.click('button[type="submit"]')
    pg.wait_for_url(expect_url or f"{base}/")
    expect(pg.locator("#accountName")).not_to_have_text("")


def _drag(pg, start, end, steps=20):
    """Pointer drag at human speed.

    dnd-kit resolves the drop target once per animation frame; WebKit needs
    frames to pass between moves or the drop lands on nothing.
    """
    pg.mouse.move(*start)
    pg.mouse.down()
    for i in range(1, steps + 1):
        pg.mouse.move(start[0] + (end[0] - start[0]) * i / steps, start[1] + (end[1] - start[1]) * i / steps)
        pg.wait_for_timeout(25)
    pg.mouse.up()
    # Like a person, act again once the card has settled: the drop animation
    # ends (the drag preview is gone), and with it dnd-kit's short window that
    # swallows clicks right after a drag.
    expect(pg.locator(".ag-row-overlay, .ag-card-overlay")).to_have_count(0)


def _open_sprints(pg, project_name="E2E Project", tab=None):
    pg.click('.nav-btn[data-view="sprints"]')
    expect(pg.locator("#viewSprints")).to_be_visible()
    pg.locator("#viewSprints .sprints-controls .bh-sel-btn").click()
    pg.locator(f'.bh-sel-pop .bh-sel-row:has-text("{project_name}")').click()
    if tab:
        pg.click(f"#sprints-tab-{tab}")
        expect(pg.locator(f"#sprints-tab-{tab}")).to_have_attribute("aria-selected", "true")


def _center(locator):
    box = locator.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_deep_link_survives_login_and_opens_the_item(server, open_page):
    """An emailed /#bug=N link: bounce to login, come back, open that item."""
    pg, problems = open_page()
    base, bug = server["base"], server["bug"]
    pg.goto(f"{base}/#bug={bug['id']}")
    # Server-side redirect to the login page; the browser keeps the fragment.
    pg.wait_for_url(f"{base}/login.html#bug={bug['id']}")
    _login(pg, base, expect_url=f"{base}/**")
    expect(pg.locator('#modalBug input[name="title"]')).to_have_value(bug["title"])
    assert pg.evaluate("location.hash") == "", "deep-link hash should be consumed"
    problems.assert_clean()


def test_description_is_plain_text_but_comments_keep_formatting(server, open_page, admin_state):
    """Descriptions are stored as plain text, so their editor offers no formatting."""
    pg, problems = open_page(admin_state)
    _home(pg, server["base"])
    pg.goto(f"{server['base']}/#bug={server['bug']['id']}")
    desc = pg.locator('#modalBug .bh-rt-wrap:has([aria-label="Description"])')
    comment = pg.locator('#modalBug .bh-rt-wrap:has([aria-label="New comment"])')
    expect(desc.locator(".bh-rt-editor")).to_be_visible()
    expect(desc.locator(".bh-rt-toolbar")).to_be_hidden()
    expect(comment.locator(".bh-rt-toolbar")).to_be_visible()
    problems.assert_clean()


def test_login_rejects_offsite_next(server, open_page):
    """next=/%09/evil.example must not leave the site after login."""
    pg, problems = open_page()
    base = server["base"]
    pg.goto(f"{base}/login.html?next=%2F%09%2Fevil.example%2F")
    _login(pg, base)
    assert pg.url.startswith(base), pg.url
    problems.assert_clean()


def test_every_admin_view_renders(server, open_page, admin_state):
    pg, problems = open_page(admin_state)
    _home(pg, server["base"])
    for view, selector in ADMIN_VIEWS.items():
        pg.click(f'{NAV}[data-view="{view}"]')
        expect(pg.locator(selector)).to_be_visible()
    problems.assert_clean()


def test_event_deep_link_opens_event_detail(server, open_page, admin_state):
    pg, problems = open_page(admin_state)
    base, event = server["base"], server["event"]
    _home(pg, base)
    pg.goto(f"{base}/#event={event['id']}")
    expect(pg.locator("#viewEvents .event-detail-desc")).to_have_text(event["description"])
    problems.assert_clean()


def test_board_drag_moves_card_and_persists(server, open_page, admin_state):
    pg, problems = open_page(admin_state)
    base, story, cols = server["base"], server["story"], server["columns"]
    _home(pg, base)
    _open_sprints(pg, tab="board")
    source = pg.locator('.ag-cell[data-category="todo"]')
    target = pg.locator('.ag-cell[data-category="in_progress"]')
    card = source.locator(f'.ag-card:has-text("{story["title"]}")')
    expect(card).to_be_visible()
    b = target.bounding_box()
    with pg.expect_response(lambda r: f"/work-items/{story['id']}/transition" in r.url) as resp:
        _drag(pg, _center(card), (b["x"] + b["width"] / 2, b["y"] + 40))
    assert resp.value.ok, f"transition returned {resp.value.status}: {resp.value.text()}"
    expect(target.locator(f'.ag-card:has-text("{story["title"]}")')).to_be_visible()
    status = pg.evaluate("async (id) => (await (await fetch(`/api/bugs/${id}`)).json()).status", story["id"])
    assert status == cols["in_progress"], status
    problems.assert_clean()


def test_board_keyboard_opens_and_moves_cards_through_the_menu(server, open_page, admin_state):
    """Keyboard users: Enter on a card's title opens the issue; the card's ⋯ menu
    (Enter to open, arrows, Enter) moves it to another column. Keys on the
    menu never reach the card, so no drag starts and the issue stays closed."""
    base = server["base"]
    name = f"E2E Keyboard {int(time.time() * 1000)}"
    with _admin_api(base, admin_state) as c:
        pid = c.post("/api/projects", json={"name": name}).json()["id"]
        board = c.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
        sprint = c.post(f"/api/agile/sprints?board_id={board}", json={"name": "Keyboard Sprint"}).json()
        story = c.post("/api/agile/work-items", json={
            "project_id": pid, "title": "Keyboard story", "item_type": "Story", "sprint_id": sprint["id"],
        }).json()
        c.post(f"/api/agile/sprints/{sprint['id']}/start", json={"version": sprint["version"]}).raise_for_status()

    pg, problems = open_page(admin_state)
    _home(pg, base)
    _open_sprints(pg, project_name=name, tab="board")
    card = pg.locator(f'.ag-card[data-issue-id="{story["id"]}"]')
    card.locator(".ag-card-title").focus()
    pg.keyboard.press("Enter")
    expect(pg.locator('#modalBug input[name="title"]')).to_have_value("Keyboard story")
    pg.locator("#modalBug .modal-close").click()
    expect(pg.locator("#modalBug")).to_be_hidden()

    menu_button = card.locator('button[aria-label^="Actions for "]')
    menu_button.focus()
    pg.keyboard.press("Enter")
    menu = pg.locator('.ag-menu[role="menu"]')
    expect(menu).to_be_visible()
    target = menu.locator('[role="menuitem"]:has-text("(In Progress)")').first
    for _ in range(10):
        if pg.evaluate("document.activeElement?.textContent") == target.text_content():
            break
        pg.keyboard.press("ArrowDown")
    with pg.expect_response(lambda r: f"/work-items/{story['id']}/transition" in r.url) as resp:
        pg.keyboard.press("Enter")
    assert resp.value.ok, resp.value.text()
    expect(pg.locator(f'.ag-cell[data-category="in_progress"] .ag-card[data-issue-id="{story["id"]}"]')).to_be_visible()
    expect(pg.locator("#modalBug")).to_be_hidden()
    expect(pg.locator(".ag-card-overlay")).to_have_count(0)
    problems.assert_clean()


def test_backlog_keyboard_ranking_with_the_drag_handle(server, open_page, admin_state):
    """Keyboard ranking: Tab to an issue's drag handle, Space to lift it,
    arrow up, Space to drop. Enter on the title still opens the issue."""
    base = server["base"]
    name = f"E2E Rank Keys {int(time.time() * 1000)}"
    with _admin_api(base, admin_state) as c:
        pid = c.post("/api/projects", json={"name": name}).json()["id"]
        board = c.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
        ids = [c.post("/api/agile/work-items", json={
            "project_id": pid, "title": f"Ranked story {n}", "item_type": "Story",
        }).json()["id"] for n in (1, 2, 3)]

    pg, problems = open_page(admin_state)
    _home(pg, base)
    _open_sprints(pg, project_name=name, tab="backlog")
    rows = pg.locator(".ag-backlog-section .ag-row")
    expect(rows).to_have_count(3)
    third = pg.locator(f'.ag-row[data-issue-id="{ids[2]}"]')
    third.locator(".ag-row-title").focus()
    pg.keyboard.press("Enter")
    expect(pg.locator('#modalBug input[name="title"]')).to_have_value("Ranked story 3")
    pg.locator("#modalBug .modal-close").click()
    expect(pg.locator("#modalBug")).to_be_hidden()

    # Each key waits for dnd-kit's live announcement (what a screen-reader
    # user hears) before the next, as a person would.
    announcer = pg.locator("[role=status][aria-live]").filter(has_text="Draggable item")
    third.locator(".ag-drag-handle").focus()
    pg.keyboard.press("Space")
    expect(announcer).to_contain_text(f"over droppable area {ids[2]}")
    pg.keyboard.press("ArrowUp")
    expect(announcer).to_contain_text(f"over droppable area {ids[1]}")
    with pg.expect_response(lambda r: f"/boards/{board}/move" in r.url) as resp:
        pg.keyboard.press("Space")
    assert resp.value.ok, resp.value.text()
    expect(rows.nth(1)).to_have_attribute("data-issue-id", str(ids[2]))
    order = pg.evaluate(
        "async (b) => (await (await fetch(`/api/agile/boards/${b}/planning`)).json()).backlog.map((i) => i.id)", board,
    )
    assert order == [ids[0], ids[2], ids[1]], order
    problems.assert_clean()


def test_a_cancelled_sprint_create_never_leaks_into_the_next_edit(server, open_page, admin_state):
    """Open "+ Create issue" under a sprint, cancel, then edit an unrelated
    backlog issue and save: it must stay in the backlog."""
    base = server["base"]
    name = f"E2E Stale Pick {int(time.time() * 1000)}"
    with _admin_api(base, admin_state) as c:
        pid = c.post("/api/projects", json={"name": name}).json()["id"]
        board = c.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
        c.post(f"/api/agile/sprints?board_id={board}", json={"name": "Stale Sprint"}).raise_for_status()
        loose = c.post("/api/agile/work-items", json={
            "project_id": pid, "title": "Backlog story", "item_type": "Story",
        }).json()

    pg, problems = open_page(admin_state)
    _home(pg, base)
    _open_sprints(pg, project_name=name, tab="backlog")
    pg.locator('.ag-sprint[aria-label="Stale Sprint"] .ag-create-link').click()
    expect(pg.locator("#modalBug")).to_be_visible()
    pg.locator("#modalBug .modal-close").click()
    expect(pg.locator("#modalBug")).to_be_hidden()

    pg.locator(f'.ag-row[data-issue-id="{loose["id"]}"] .ag-row-title').click()
    title = pg.locator('#modalBug input[name="title"]')
    expect(title).to_have_value("Backlog story")
    title.fill("Backlog story, renamed")
    with pg.expect_response(lambda r: f"/api/bugs/{loose['id']}" in r.url and r.request.method == "PUT") as saved:
        pg.locator("#bugSubmitBtn").click()
    assert saved.value.ok, saved.value.text()
    expect(pg.locator("#modalBug")).to_be_hidden()
    item = pg.evaluate("async (id) => (await (await fetch(`/api/bugs/${id}`)).json())", loose["id"])
    assert (item["title"], item["sprint_id"]) == ("Backlog story, renamed", None), item
    problems.assert_clean()


def test_sprint_lifecycle_plan_start_complete_and_report(server, open_page, admin_state):
    """The Jira Scrum loop in the browser: rank an issue into a future sprint
    by dragging it in the backlog, start the sprint, finish the work on the
    board, complete the sprint (open work goes back to the backlog) and read
    the sprint report."""
    base = server["base"]
    name = f"E2E Lifecycle {int(time.time() * 1000)}"
    with _admin_api(base, admin_state) as c:
        pid = c.post("/api/projects", json={"name": name}).json()["id"]
        board = c.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
        c.post(f"/api/agile/sprints?board_id={board}", json={"name": "Lifecycle Sprint"}).raise_for_status()
        done_story = c.post("/api/agile/work-items", json={
            "project_id": pid, "title": "Lifecycle finished story", "item_type": "Story", "story_points": 5,
        }).json()
        open_story = c.post("/api/agile/work-items", json={
            "project_id": pid, "title": "Lifecycle unfinished story", "item_type": "Story", "story_points": 3,
        }).json()
        # The second story goes in through the API; the first by dragging.
        sprint_id = c.get(f"/api/agile/sprints?board_id={board}").json()[0]["id"]
        c.post(f"/api/agile/boards/{board}/move",
               json={"item_ids": [open_story["id"]], "sprint_id": sprint_id}).raise_for_status()

    pg, problems = open_page(admin_state)
    _home(pg, base)
    _open_sprints(pg, project_name=name, tab="backlog")
    sprint = pg.locator('.ag-sprint[aria-label="Lifecycle Sprint"]')
    backlog = pg.locator(".ag-backlog-section")
    row = backlog.locator(f'.ag-row[data-issue-id="{done_story["id"]}"]')
    expect(row).to_be_visible()
    neighbour = sprint.locator(f'.ag-row[data-issue-id="{open_story["id"]}"]')
    expect(neighbour).to_be_visible()
    n = neighbour.bounding_box()
    with pg.expect_response(lambda r: f"/boards/{board}/move" in r.url) as resp:
        _drag(pg, _center(row), (n["x"] + n["width"] / 2, n["y"] + n["height"] - 4))
    assert resp.value.ok, f"move returned {resp.value.status}: {resp.value.text()}"
    expect(sprint.locator(f'.ag-row[data-issue-id="{done_story["id"]}"]')).to_be_visible()
    expect(backlog.locator(".ag-row")).to_have_count(0)

    sprint.locator('button:has-text("Start sprint")').click()
    dialog = pg.locator("#startSprintDialog")
    expect(dialog).to_be_visible()
    with pg.expect_response(lambda r: f"/sprints/{sprint_id}/start" in r.url) as started:
        dialog.locator('button[type="submit"]').click()
    assert started.value.ok, started.value.text()
    # Starting a sprint opens its board, like Jira.
    expect(pg.locator("#sprints-tab-board")).to_have_attribute("aria-selected", "true")
    expect(pg.locator(".ag-board-title")).to_have_text("Lifecycle Sprint")

    card = pg.locator(f'.ag-card[data-issue-id="{done_story["id"]}"]')
    done_cell = pg.locator('.ag-cell[data-category="done"]')
    d = done_cell.bounding_box()
    with pg.expect_response(lambda r: f"/work-items/{done_story['id']}/transition" in r.url) as moved:
        _drag(pg, _center(card), (d["x"] + d["width"] / 2, d["y"] + 40))
    assert moved.value.ok, moved.value.text()
    expect(done_cell.locator(f'.ag-card[data-issue-id="{done_story["id"]}"]')).to_be_visible()

    pg.locator('.ag-board-head button:has-text("Complete sprint")').click()
    complete = pg.locator("#completeSprintDialog")
    expect(complete.locator(".ag-complete-summary")).to_contain_text("1 completed issue")
    expect(complete.locator(".ag-complete-summary")).to_contain_text("1 open issue")
    with pg.expect_response(lambda r: f"/sprints/{sprint_id}/complete" in r.url) as completed:
        complete.locator('button[type="submit"]').click()
    assert completed.value.ok, completed.value.text()
    # Back on the backlog: the unfinished story returned there, the finished
    # one stayed with the closed sprint.
    expect(pg.locator("#sprints-tab-backlog")).to_have_attribute("aria-selected", "true")
    expect(backlog.locator(f'.ag-row[data-issue-id="{open_story["id"]}"]')).to_be_visible()
    expect(pg.locator(f'.ag-row[data-issue-id="{done_story["id"]}"]')).to_have_count(0)

    pg.click("#sprints-tab-reports")
    pg.click('.ag-report-link:has-text("Sprint report")')
    kpis = pg.locator(".ag-report-body .report-kpis")
    expect(kpis.locator('.report-kpi:has-text("Committed at start") .report-kpi-num')).to_have_text("8")
    expect(kpis.locator(".report-kpi").nth(1).locator(".report-kpi-num")).to_have_text("5")
    expect(kpis.locator('.report-kpi:has-text("Not completed") .report-kpi-num')).to_have_text("3")
    problems.assert_clean()


@pytest.mark.parametrize("report", [
    "Burndown chart", "Burnup chart", "Sprint report", "Velocity chart", "Cumulative flow diagram",
    "Control chart", "Epic report", "Daily summary", "Workload",
])
def test_every_report_renders(server, open_page, admin_state, report):
    pg, problems = open_page(admin_state)
    _home(pg, server["base"])
    _open_sprints(pg, tab="reports")
    pg.click(f'.ag-report-link:has-text("{report}")')
    expect(pg.locator("#ag-report-title")).to_have_text(report)
    body = pg.locator(".ag-report-body")
    expect(body.locator("svg, table, .report-kpis, .ag-empty").first).to_be_visible()
    problems.assert_clean()


def test_regular_user_sees_no_privileged_views(server, open_page, user_state):
    pg, problems = open_page(user_state)
    _home(pg, server["base"])
    expect(pg.locator(f'{NAV}[data-view="list"]')).to_be_visible()
    for view in ADMIN_ONLY_OR_MANAGER:
        expect(pg.locator(f'.nav-btn[data-view="{view}"]')).to_have_count(0)
    status = pg.evaluate("async () => (await fetch('/api/audit')).status")
    assert status == 403, status
    problems.assert_clean()


def test_logout_ends_the_session(server, open_page):
    base = server["base"]
    # Its own session: logging out revokes it, so the shared one stays valid.
    pg, problems = open_page(_session_state(base, ADMIN_EMAIL, ADMIN_PASSWORD))
    _home(pg, base)
    pg.click("#profileBtn")
    pg.locator('.profile-menu button:has-text("Log out")').click()
    pg.locator('#modalConfirm button:has-text("Log out")').click()
    pg.wait_for_url(f"{base}/login.html*")
    assert pg.evaluate("async () => (await fetch('/api/auth/me')).status") == 401
    pg.goto(f"{base}/")
    pg.wait_for_url(f"{base}/login.html*")
    problems.assert_clean()


@pytest.mark.parametrize("view", ["list", "sprints", "events", "organization"])
def test_phone_width_has_no_horizontal_overflow(server, open_page, admin_state, view):
    pg, problems = open_page(admin_state, viewport={"width": 375, "height": 812})
    _home(pg, server["base"])
    if view != "list":
        # Narrow layouts move navigation into the sidebar drawer.
        pg.evaluate(f"document.querySelector('.nav-btn[data-view=\"{view}\"]').click()")
    expect(pg.locator(ADMIN_VIEWS[view])).to_be_visible()
    overflow = pg.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"{view}: page scrolls horizontally by {overflow}px at 375px"
    problems.assert_clean()


AXE = ROOT / "frontend" / "node_modules" / "axe-core" / "axe.min.js"
# WCAG 2.1 A/AA rule tags; "best-practice" rules are reported but not gating.
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]


def _settle(pg) -> None:
    """Let entrance animations and colour transitions finish before measuring."""
    for _ in range(3):
        pg.evaluate("""() => Promise.all(document.getAnimations()
            .filter((a) => a.effect?.getTiming().iterations !== Infinity)
            .map((a) => a.finished.catch(() => null)))""")
        pg.wait_for_timeout(150)


def _axe(pg) -> list[dict]:
    """Serious/critical WCAG violations on the current page (axe-core).

    A colour sampled in the middle of a fade can look like a contrast failure,
    so a first scan that finds violations is repeated after the page has
    settled again; only violations that persist are reported.
    """
    # Let entrance transitions finish: mid-fade colours give false contrast failures.
    _settle(pg)
    pg.add_script_tag(path=str(AXE))
    run = "async (tags) => await axe.run(document, {runOnly: {type: 'tag', values: tags}})"
    result = pg.evaluate(run, AXE_TAGS)
    if any(v["impact"] in ("serious", "critical") for v in result["violations"]):
        _settle(pg)
        result = pg.evaluate(run, AXE_TAGS)
    out = []
    for v in result["violations"]:
        if v["impact"] not in ("serious", "critical"):
            continue
        nodes = []
        for n in v["nodes"][:5]:
            data = (n.get("any") or [{}])[0].get("data") or {}
            ratio = data.get("contrastRatio") if isinstance(data, dict) else None
            nodes.append(f"{n['target']} {data.get('fgColor')}/{data.get('bgColor')}={ratio}" if ratio
                         else str(n["target"]))
        out.append({"id": v["id"], "impact": v["impact"], "help": v["help"], "targets": nodes})
    return out


def _fmt(violations):
    return "\n".join(f"{v['impact']}: {v['id']} - {v['help']} {v['targets']}" for v in violations)


THEMES = ["dark", "light"]


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
def test_login_page_has_no_serious_a11y_violations(server, open_page, theme):
    pg, _ = open_page(bypass_csp=True, theme=theme)
    pg.goto(f"{server['base']}/login.html")
    expect(pg.locator('input[name="email"]').first).to_be_visible()
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("view", list(ADMIN_VIEWS))
def test_views_have_no_serious_a11y_violations(server, open_page, admin_state, view, theme):
    pg, _ = open_page(admin_state, bypass_csp=True, theme=theme)
    _home(pg, server["base"])
    pg.click(f'{NAV}[data-view="{view}"]')
    expect(pg.locator(ADMIN_VIEWS[view])).to_be_visible()
    pg.wait_for_load_state("networkidle")
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("tab", ["backlog", "board", "hierarchy", "reports", "planning", "settings"])
def test_every_sprints_tab_has_no_serious_a11y_violations(server, open_page, admin_state, theme, tab):
    """Each Sprints tab of the seeded project (an active sprint with a story,
    cards on the board, backlog rows, an epic) scanned with axe."""
    with _admin_api(server["base"], admin_state) as c:
        pid = server["project_id"]
        epics = c.get(f"/api/agile/epics?project_id={pid}").json()
        if not epics:
            epic = c.post("/api/agile/work-items", json={"project_id": pid, "title": "E2E epic", "item_type": "Epic"}).json()
            c.post("/api/agile/work-items", json={
                "project_id": pid, "title": "E2E backlog story", "item_type": "Story", "epic_id": epic["id"],
            }).raise_for_status()
    pg, _ = open_page(admin_state, bypass_csp=True, theme=theme)
    _home(pg, server["base"])
    _open_sprints(pg, tab=tab)
    pg.wait_for_load_state("networkidle")
    if tab == "board":
        expect(pg.locator(".ag-card").first).to_be_visible()
    if tab == "backlog":
        expect(pg.locator(".ag-row").first).to_be_visible()
    violations = _axe(pg)
    assert violations == [], _fmt(violations)


@pytest.mark.skipif(not AXE.exists(), reason="run `npm ci` in frontend/ for axe-core")
@pytest.mark.parametrize("theme", THEMES)
def test_item_dialog_has_no_serious_a11y_violations(server, open_page, admin_state, theme):
    pg, _ = open_page(admin_state, bypass_csp=True, theme=theme)
    _home(pg, server["base"])
    pg.goto(f"{server['base']}/#bug={server['bug']['id']}")
    expect(pg.locator('#modalBug input[name="title"]')).to_be_visible()
    violations = _axe(pg)
    assert violations == [], _fmt(violations)
