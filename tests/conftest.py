"""Pytest fixtures: hermetic temp-SQLite app; client (anon), admin_client, user_client."""
from __future__ import annotations

import faulthandler
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _test_app_version() -> str:
    """Resolve APP_VERSION for the suite without hardcoding a release number.

    Precedence: the real environment/.env value if present (so tests exercise
    the same version the app ships), else a clearly non-release sentinel. Tests
    must never assert a literal product version - the release-hygiene tests
    compare against the live settings value instead.
    """
    value = os.environ.get("APP_VERSION", "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_VERSION="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value:
                    return value
    except OSError:
        pass
    return "0.0.0-test"


TEST_APP_VERSION = _test_app_version()
# Modules that import app.main at collection time (before any fixture runs)
# must see a version too, so the suite never depends on a developer's .env.
os.environ.setdefault("APP_VERSION", TEST_APP_VERSION)

# Hard-set (not setdefault): config.py auto-loads .env, and cloud layers must stay off in CI.
for _flag in (
    "SLEUTH_CLOUD_ENABLED",
    "SLEUTH_RETRIEVAL_ENABLED",
    "SLEUTH_VERIFY_ANSWERS",
    "SLEUTH_AGENT_ENABLED",
    "SLEUTH_EVAL_ENABLED",
    "SLEUTH_RAG_ENABLED",
):
    os.environ[_flag] = "0"

# Fast bcrypt for the suite: hashing at production cost (rounds=12) burns most
# of CI's wall-clock (~0.3-0.6s per hash, hundreds of hashes). The app reads
# BCRYPT_TEST_ROUNDS dynamically at hash time (capped at 12), and verify works
# against any cost, so existing stored hashes stay valid.
os.environ["BCRYPT_TEST_ROUNDS"] = "4"

# Digest off: when on, immediate notify_* emails defer to the batch job.
os.environ["EMAIL_DIGEST_ENABLED"] = "false"

# SigNoz export must never fire from the suite (endpoint would be a live collector).
# load_dotenv uses override=False, so this hard-set wins over .env.
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
# Keep the console format stable regardless of a developer's .env (LOG_FORMAT=json).
os.environ["LOG_FORMAT"] = "text"


# ============================================================================
# CI forensics harness ("CI black box"): off by default, armed in CI via
#   CI_TEST_FORENSICS=1 pytest ...
#
# Two output channels with different jobs:
#   * stdout (streamed live into the Actions log) -- LOW VOLUME, ACTIONABLE
#     only: a heartbeat that fires *only* while a single test has outlived the
#     heartbeat interval, one completion line for a failed or slow test, a
#     one-line session summary, and the SIGTERM/SIGABRT thread dump a cancelled
#     runner produces. A healthy run prints a handful of lines, not one pair
#     per test (the old per-test pair was ~4,800 lines and buried the real
#     signal).
#   * ci-forensics.log -- FULL per-test detail, one file per pytest process
#     (CI_TEST_FORENSICS_LOG picks the path), uploaded as an artifact. A single
#     reused handle is opened lazily, so an event is a write, not an
#     open/write/close cycle.
#
# Why not rely on junit.xml? When the runner is killed, the pytest process
# never finishes, so junit.xml/coverage.xml are never flushed to disk.
# Streaming to stdout keeps the evidence in the Actions log either way.
# ============================================================================
_FORENSICS_ENABLED = os.environ.get("CI_TEST_FORENSICS", "") == "1"
_FORENSICS_LOG = Path(
    os.environ.get("CI_TEST_FORENSICS_LOG", "ci-forensics.log")
).resolve()
_FORENSICS_HEARTBEAT_SECONDS = max(
    15, int(os.environ.get("CI_TEST_FORENSICS_HEARTBEAT", "60") or 60)
)
_FORENSICS_SLOW_SECONDS = max(
    30, int(os.environ.get("CI_TEST_FORENSICS_SLOW", "30") or 30)
)
_forensics_lock = threading.Lock()
_forensics_current: dict[str, object] = {}
_forensics_heartbeat_stop = threading.Event()
_forensics_handle = None
_forensics_totals = {"tests": 0, "failed": 0, "slow": 0, "slowest": 0.0, "slowest_id": "-"}


def _forensics_log_line(line: str) -> None:
    """Append one line to the forensics log through a single reused handle."""
    global _forensics_handle
    try:
        if _forensics_handle is None or _forensics_handle.closed:
            _FORENSICS_LOG.parent.mkdir(parents=True, exist_ok=True)
            _forensics_handle = _FORENSICS_LOG.open("a", encoding="utf-8", buffering=1)
        _forensics_handle.write(line + "\n")
    except (OSError, ValueError):
        pass


def _forensics_close() -> None:
    """Flush and close the forensics log (called at session teardown)."""
    global _forensics_handle
    try:
        if _forensics_handle is not None and not _forensics_handle.closed:
            _forensics_handle.flush()
            _forensics_handle.close()
    except (OSError, ValueError):
        pass
    _forensics_handle = None


def _forensics_emit(kind: str, message: str, stream: bool = True) -> None:
    """Record one timestamped line; echo it to stdout only when stream=True.

    stream=False keeps per-test bookkeeping out of the job log while the full
    detail still lands in the artifact.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    line = f"[{stamp}Z] [ci-forensics pid={os.getpid()}] {kind}: {message}"
    _forensics_log_line(line)
    if not stream:
        return
    try:
        print(line, flush=True)
    except OSError:
        pass


def _forensics_heartbeat() -> None:
    """Emit a line ONLY while a single test has outlived the heartbeat interval.

    A healthy suite never trips this (every test finishes inside the window),
    so the job log stays quiet; a wedged test produces one line per interval
    naming it and how long it has held the worker.
    """
    while not _forensics_heartbeat_stop.wait(_FORENSICS_HEARTBEAT_SECONDS):
        with _forensics_lock:
            test_id = _forensics_current.get("test")
            since = _forensics_current.get("since")
        if not test_id or since is None:
            continue
        held = time.monotonic() - float(since)
        if held < _FORENSICS_HEARTBEAT_SECONDS:
            continue
        _forensics_emit("heartbeat", f"test still running after {held:.0f}s: {test_id}")


def _forensics_on_signal(signum: int, _frame) -> None:  # pragma: no cover
    """Trap: dump every thread's stack, then let the default action proceed."""
    try:
        name = signal.Signals(signum).name
    except (AttributeError, ValueError):
        name = str(signum)
    _forensics_emit("signal", f"received {name}; dumping all thread stacks")
    try:
        faulthandler.dump_traceback(file=sys.stderr)
    except (OSError, ValueError):
        pass
    # Re-raise with the default disposition so the runner still sees the
    # real exit behaviour (143 on SIGTERM, etc.) instead of a silent swallow.
    signal.signal(signum, signal.SIG_DFL)
    try:
        os.kill(os.getpid(), signum)
    except OSError:
        pass


def pytest_configure(config):
    if not _FORENSICS_ENABLED:
        return
    for signum in (signal.SIGTERM, signal.SIGABRT):
        try:
            # faulthandler first: it needs the raw frames at kill time.
            # (Guarded with getattr: python -X faulthandler pre-imports the
            # module early, which can surface a partially-initialized copy
            # missing 'register' on some interpreters.)
            faulthandler_register = getattr(faulthandler, "register", None)
            if callable(faulthandler_register):
                faulthandler_register(signum, file=sys.stderr, all_threads=True)
            previous = signal.getsignal(signum)
            if callable(previous):

                def _chained(signum_inner, frame, _prev=previous):
                    _forensics_on_signal(signum_inner, frame)
                    _prev(signum_inner, frame)

                signal.signal(signum, _chained)
            else:
                signal.signal(signum, _forensics_on_signal)
        except (OSError, ValueError, RuntimeError):
            pass
    _forensics_heartbeat_stop.clear()
    worker = threading.Thread(
        target=_forensics_heartbeat, name="ci-forensics-heartbeat", daemon=True
    )
    worker.start()
    # stream=False: with one pytest process per test file this line would
    # otherwise repeat ~105 times. The artifact keeps the process boundaries.
    _forensics_emit(
        "session-start",
        f"pid={os.getpid()} heartbeat={_FORENSICS_HEARTBEAT_SECONDS}s "
        f"slow-threshold={_FORENSICS_SLOW_SECONDS}s log={_FORENSICS_LOG}",
        stream=False,
    )


def pytest_unconfigure(config):
    if not _FORENSICS_ENABLED:
        return
    _forensics_heartbeat_stop.set()
    _forensics_emit("session-finish", "pytest session teardown reached", stream=False)
    totals = _forensics_totals
    # The one line that replaces ~4,800 per-test stdout lines.
    _forensics_emit(
        "session-summary",
        f"tests={totals['tests']} failed={totals['failed']} "
        f"slow(>={_FORENSICS_SLOW_SECONDS}s)={totals['slow']} "
        f"slowest={totals['slowest_id']} ({totals['slowest']:.1f}s)",
    )
    _forensics_close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    if not _FORENSICS_ENABLED:
        yield
        return
    test_id = getattr(item, "nodeid", str(item))
    started = time.monotonic()
    with _forensics_lock:
        _forensics_current["test"] = test_id
        _forensics_current["since"] = started
    # Artifact only: ~2,200 of these per run is pure noise in the job log.
    _forensics_emit("test-start", test_id, stream=False)
    try:
        yield
    finally:
        # Phase outcomes recorded by pytest_runtest_logreport below.
        failed = [
            phase
            for phase in ("setup", "call", "teardown")
            if _forensics_reports.pop((test_id, phase), False)
        ]
        elapsed = time.monotonic() - started
        slow = elapsed >= _FORENSICS_SLOW_SECONDS
        with _forensics_lock:
            _forensics_current.pop("test", None)
            _forensics_current.pop("since", None)
            _forensics_totals["tests"] += 1
            _forensics_totals["slow"] += 1 if slow else 0
            _forensics_totals["failed"] += 1 if failed else 0
            if elapsed > _forensics_totals["slowest"]:
                _forensics_totals["slowest"] = elapsed
                _forensics_totals["slowest_id"] = test_id
        _forensics_emit(
            f"test-end {'FAILED:' + ','.join(failed) if failed else 'ok'} "
            f"{elapsed:.1f}s{' SLOW' if slow else ''}",
            test_id,
            # Only the interesting tests reach stdout.
            stream=bool(failed) or slow,
        )


_forensics_reports: dict[tuple[str, str], bool] = {}


@pytest.hookimpl
def pytest_runtest_logreport(report) -> None:
    """Record each phase outcome; pytest_runtest_protocol reads it on test-end.

    (A plain hook, not a wrapper: it fires exactly when the setup/call/
    teardown report object exists, so no pluggy ordering pitfalls.)
    """
    if not _FORENSICS_ENABLED:
        return
    try:
        _forensics_reports[(report.nodeid, report.when)] = bool(
            getattr(report, "failed", False)
        )
    except Exception:  # never break reporting because of forensics
        pass


BOOTSTRAP_EMAIL = "admin@test.local"
BOOTSTRAP_PASSWORD = "Admin1234"


@pytest.fixture(autouse=True)
def dispose_database_engine():
    """Close pooled connections left by hermetic app re-imports."""
    yield
    database = sys.modules.get("app.database")
    if database is not None:
        database.engine.dispose()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", BOOTSTRAP_EMAIL)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", BOOTSTRAP_PASSWORD)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Test Admin")
    monkeypatch.setenv("APP_NAME", "Bug Hunter")
    monkeypatch.setenv("APP_VERSION", TEST_APP_VERSION)
    # No real HaveIBeenPwned calls; breach tests monkeypatch app.password_breach.
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    # Push off by default so no real FCM calls; push tests opt in via monkeypatch.
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")
    # Off by default regardless of the real .env — most tests assume normal
    # login-gating; auto-login tests opt in via monkeypatch.
    monkeypatch.setenv("AUTO_LOGIN_ENABLED", "false")

    # Dispose the previous engine before re-importing app modules so pooled
    # SQLite connections are closed when each isolated test database changes.
    old_database = sys.modules.get("app.database")
    if old_database is not None:
        old_database.engine.dispose()

    # Re-import so the SQLAlchemy engine picks up the overridden DATABASE_URL.
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def default_org_id() -> int:
    """Id of the suite's single organization (created on first use; the HTTP fixtures get
    theirs from the bootstrap admin). Reads and commits on its own short session."""
    from app.database import SessionLocal
    from app.models import Organization

    with SessionLocal() as s:
        org = s.query(Organization).order_by(Organization.id).first()
        if org is None:
            org = Organization(name="Test Organization", slug="test-organization")
            s.add(org)
            s.commit()
        return org.id


def any_user(db):
    """The oldest user of the database (the bootstrap admin on the HTTP fixtures)."""
    from app.models import User

    return db.query(User).order_by(User.id).first()


def scope_of(db, user):
    """Project ids ``user`` may access (what the routes pass to the chatbot)."""
    from app.access import accessible_project_ids

    return accessible_project_ids(db, user)


def new_client():
    """A second TestClient on the same app, with its own cookie jar (another browser)."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def sign_up(org_name, email, password="Passw0rd!x", name="Owner"):
    """Create an organization through public sign-up; returns (client, /me body)."""
    c = new_client()
    res = c.post("/api/auth/signup", json={
        "name": name, "email": email, "password": password, "organization_name": org_name,
    })
    assert res.status_code == 201, res.text
    return c, res.json()


@pytest.fixture
def admin_client(client):
    """A TestClient with an authenticated admin session cookie."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL,
        "password": BOOTSTRAP_PASSWORD,
    })
    assert res.status_code == 200, f"admin login failed: {res.text}"
    return client


@pytest.fixture
def user_client(client):
    """TestClient logged in as a fresh regular user; separate instance so the admin cookie doesn't bleed over."""
    res = client.post("/api/auth/login", json={
        "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
    })
    assert res.status_code == 200
    res = client.post("/api/users", json={
        "name": "Regular User",
        "email": "user@test.local",
        "role": "user",
        "password": "User12345",
    })
    assert res.status_code == 201, res.text
    # Same TestClient is fine; the cookie is simply replaced.
    client.post("/api/auth/logout")
    res = client.post("/api/auth/login", json={
        "email": "user@test.local", "password": "User12345",
    })
    assert res.status_code == 200
    return client


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """Direct database session for testing model/helper functions (no API layer)."""
    db_file = tmp_path / "test_db_session.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", BOOTSTRAP_EMAIL)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", BOOTSTRAP_PASSWORD)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Test Admin")
    monkeypatch.setenv("APP_NAME", "Bug Hunter")
    monkeypatch.setenv("APP_VERSION", TEST_APP_VERSION)
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")
    monkeypatch.setenv("AUTO_LOGIN_ENABLED", "false")

    # Dispose the previous engine before replacing app modules and database URL.
    old_database = sys.modules.get("app.database")
    if old_database is not None:
        old_database.engine.dispose()

    # Re-import to pick up overridden env vars
    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    from app.config import get_settings
    get_settings.cache_clear()

    from app.database import SessionLocal, init_db
    
    # Initialize database schema
    init_db()
    
    db = SessionLocal()
    yield db
    db.close()
