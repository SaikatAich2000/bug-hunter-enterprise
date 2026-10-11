"""Pytest harness: the app is imported once per session and the database is reset per test.

Re-importing app.* for every test leaked roughly 12 MB per test (engines, mappers,
routers) and cost most of the suite's wall-clock. Instead:

* the environment is pinned before anything imports app.*;
* one temp SQLite file (or one throwaway PostgreSQL database when
  BH_TEST_DB=postgres and BH_TEST_POSTGRES_URL are set) serves the whole process;
* ``_fresh_db`` truncates every table and ``client``/``db_session`` re-run the
  normal startup, so each test sees a freshly installed instance;
* ``_isolate`` rolls module-level state (rate limiters, caches, singletons), the
  Settings class and sys.modules back to their post-import baseline, so tests
  that purge or reload app.* themselves cannot leak into their neighbours.

xdist workers each load this file, so each gets its own database.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import uuid
from collections import deque
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = ["tests.forensics_plugin"]

BOOTSTRAP_EMAIL = "admin@test.local"
BOOTSTRAP_PASSWORD = "Admin1234"


def _test_app_version() -> str:
    """APP_VERSION from the environment or .env, else a clearly non-release sentinel.

    Tests compare against the live setting and never assert a literal release number.
    """
    value = os.environ.get("APP_VERSION", "").strip()
    if value:
        return value
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_VERSION="):
                value = line.split("=", 1)[1].strip().strip("\"'")
                if value:
                    return value
    except OSError:
        pass
    return "0.0.0-test"


TEST_APP_VERSION = _test_app_version()

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="bh-tests-"))
_PG_ADMIN_URL = os.environ.get("BH_TEST_POSTGRES_URL", "")
_USE_POSTGRES = os.environ.get("BH_TEST_DB", "").lower() == "postgres" and bool(_PG_ADMIN_URL)
_PG_DB_NAME = f"bh_test_{uuid.uuid4().hex[:10]}"


def _create_postgres_database() -> str:
    from sqlalchemy import create_engine, text

    admin = create_engine(_PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{_PG_DB_NAME}"'))
    admin.dispose()
    return f"{_PG_ADMIN_URL.rpartition('/')[0]}/{_PG_DB_NAME}"


def _drop_postgres_database() -> None:
    from sqlalchemy import create_engine, text

    admin = create_engine(_PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{_PG_DB_NAME}" WITH (FORCE)'))
    admin.dispose()


# Hard-set rather than setdefault: config.py loads .env, and the suite must never
# depend on a developer's local settings (cloud layers, SigNoz export, bcrypt cost).
_TEST_ENV = {
    "DATABASE_URL": (
        _create_postgres_database() if _USE_POSTGRES else f"sqlite:///{(_SESSION_DIR / 'test.db').as_posix()}"
    ),
    "EMAIL_BACKEND": "disabled",
    "SESSION_SECRET": "test_secret_for_tests_only",
    "BOOTSTRAP_ADMIN_EMAIL": BOOTSTRAP_EMAIL,
    "BOOTSTRAP_ADMIN_PASSWORD": BOOTSTRAP_PASSWORD,
    "BOOTSTRAP_ADMIN_NAME": "Test Admin",
    "APP_NAME": "Bug Hunter",
    "APP_VERSION": TEST_APP_VERSION,
    "PASSWORD_BREACH_CHECK_ENABLED": "false",  # no real HaveIBeenPwned calls
    "WEB_PUSH_ENABLED": "false",
    "AUTO_LOGIN_ENABLED": "false",  # most tests assume normal login gating
    # Sign-up is off by default in the product; the suite's helpers create organizations
    # through it. Verification has its own tests (tests/test_identity.py) that switch it on.
    "ALLOW_PUBLIC_SIGNUP": "true",
    "EMAIL_VERIFICATION_REQUIRED": "false",
    "SLEUTH_CLOUD_ENABLED": "0",
    "SLEUTH_RETRIEVAL_ENABLED": "0",
    "SLEUTH_VERIFY_ANSWERS": "0",
    "SLEUTH_AGENT_ENABLED": "0",
    "SLEUTH_EVAL_ENABLED": "0",
    "SLEUTH_RAG_ENABLED": "0",
    "SLEUTH_LLM_MODEL_PATH": str(_SESSION_DIR / "no-model.gguf"),  # keeps the local LLM unavailable
    "BCRYPT_TEST_ROUNDS": "4",  # production cost would dominate wall-clock
    "EMAIL_DIGEST_ENABLED": "false",  # when on, notify_* emails defer to the batch job
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",  # never reach a live collector
    "LOG_FORMAT": "text",
}
os.environ.update(_TEST_ENV)

# Settings are read once at import, but a few modules re-read these per call, and
# unit tests without a fixture expect the developer's own values. So they are set
# for the import and re-applied by `_fresh_db`, not left in the environment.
_FIXTURE_ENV = {
    k: _TEST_ENV[k]
    for k in (
        "DATABASE_URL", "EMAIL_BACKEND", "SESSION_SECRET", "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_NAME", "APP_NAME",
        "PASSWORD_BREACH_CHECK_ENABLED", "WEB_PUSH_ENABLED", "AUTO_LOGIN_ENABLED",
    )
}


@atexit.register
def _cleanup_session_resources() -> None:
    database = sys.modules.get("app.database")
    if database is not None:
        database.engine.dispose()
    if _USE_POSTGRES:
        try:
            _drop_postgres_database()
        except Exception:  # best effort: a CI service container is discarded anyway
            pass
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# Baseline capture and rollback of in-process state
# ---------------------------------------------------------------------------
_CONTAINERS = (dict, list, set, deque)
_SCALARS = (int, float, str, bytes, bool, type(None))
_baseline: dict = {}
_MISSING = object()


def _copy_container(value):
    return value.copy()


def _restore_container(live, snapshot) -> None:
    if isinstance(live, dict):
        live.clear()
        live.update(snapshot)
    elif isinstance(live, list):
        live[:] = snapshot
    elif isinstance(live, set):
        live.clear()
        live.update(snapshot)
    else:  # deque
        live.clear()
        live.extend(snapshot)


def _is_app_instance(value) -> bool:
    cls = type(value)
    return (
        cls.__module__.startswith("app.")
        and hasattr(value, "__dict__")
        and not isinstance(value, type)
    )


def _capture_module_state(module) -> dict:
    """Containers and scalars bound at module level, plus containers on app-owned singletons."""
    state: dict = {
        "bindings": {k: v for k, v in vars(module).items() if not k.startswith("__")},
        "containers": {}, "scalars": {}, "objects": {},
    }
    for name, value in list(vars(module).items()):
        if name.startswith("__"):
            continue
        if isinstance(value, _CONTAINERS):
            state["containers"][name] = (value, _copy_container(value))
        elif isinstance(value, _SCALARS):
            state["scalars"][name] = value
        elif _is_app_instance(value):
            attrs = {
                k: (v, _copy_container(v))
                for k, v in list(vars(value).items())
                if isinstance(v, _CONTAINERS)
            }
            scalars = {k: v for k, v in vars(value).items() if isinstance(v, _SCALARS)}
            if attrs or scalars:
                state["objects"][name] = (value, attrs, scalars)
    return state


def _restore_pairs(pairs: dict) -> None:
    for live, snapshot in pairs.values():
        try:
            if live != snapshot:
                _restore_container(live, snapshot)
        except Exception:  # elements with exotic __eq__: restore unconditionally
            _restore_container(live, snapshot)


def _restore_module_state(module, state: dict) -> None:
    # Undoes importlib.reload() and direct `module.attr = fake` assignments.
    for name, value in state["bindings"].items():
        if vars(module).get(name, _MISSING) is not value:
            setattr(module, name, value)
    _restore_pairs(state["containers"])
    for name, value in state["scalars"].items():
        if vars(module).get(name, value) != value:
            setattr(module, name, value)
    for obj, attrs, scalars in state["objects"].values():
        _restore_pairs(attrs)
        for name, value in scalars.items():
            if vars(obj).get(name, value) != value:
                setattr(obj, name, value)


def _import_all_app_modules() -> None:
    """Import every app module up front so the baseline covers lazily-imported ones."""
    import importlib
    import pkgutil

    import app

    for info in pkgutil.walk_packages(app.__path__, "app."):
        if ".static" in info.name:
            continue
        try:
            importlib.import_module(info.name)
        except Exception:  # optional-dependency modules may legitimately fail to import
            pass


def _settings_snapshot(settings_cls) -> dict:
    return {
        k: v for k, v in vars(settings_cls).items()
        if not k.startswith("__") and not isinstance(v, (property, classmethod, staticmethod))
        and not callable(v)
    }


@pytest.fixture(scope="session", autouse=True)
def _app_runtime():
    """Import the app once and record the baseline every test is rolled back to."""
    import app.main as main  # noqa: F401  (side effect: builds the app against the session DB)

    _import_all_app_modules()
    for key in _FIXTURE_ENV:
        os.environ.pop(key, None)
    from app.config import Settings, get_settings

    _baseline["modules"] = {
        name: mod for name, mod in sys.modules.items() if name == "app" or name.startswith("app.")
    }
    _baseline["state"] = {
        name: _capture_module_state(mod) for name, mod in _baseline["modules"].items()
    }
    _baseline["settings_cls"] = Settings
    _baseline["settings_attrs"] = _settings_snapshot(Settings)
    _baseline["settings_obj"] = get_settings()
    _baseline["settings_obj_dict"] = dict(vars(get_settings()))
    _baseline["overrides"] = dict(main.app.dependency_overrides)
    _baseline["routes"] = list(main.app.router.routes)
    yield
    _baseline.clear()


def _restore_sys_modules() -> None:
    """Undo tests that purge or re-import app.* themselves, disposing the engines they created."""
    live = {n: m for n, m in sys.modules.items() if n == "app" or n.startswith("app.")}
    baseline = _baseline["modules"]
    if all(live.get(n) is m for n, m in baseline.items()) and len(live) == len(baseline):
        return
    for name, mod in live.items():
        if baseline.get(name) is not mod and name == "app.database":
            try:
                mod.engine.dispose()
            except Exception:
                pass
    for name in live:
        if name not in baseline:
            del sys.modules[name]
    for name, mod in baseline.items():
        sys.modules[name] = mod
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(baseline[parent], child, mod)


def _restore_settings() -> None:
    cls = _baseline["settings_cls"]
    saved = _baseline["settings_attrs"]
    for key, value in saved.items():
        if vars(cls).get(key, value) is not value:
            setattr(cls, key, value)
    for key in set(_settings_snapshot(cls)) - set(saved):
        delattr(cls, key)
    # Tests that call get_settings.cache_clear() would otherwise leave later tests
    # patching one Settings instance while modules that captured another keep reading
    # it; pin the cache back to the baseline instance (and drop attribute shadows
    # monkeypatch leaves on it when it undoes an instance-level patch).
    obj = _baseline["settings_obj"]
    obj_dict = _baseline["settings_obj_dict"]
    if vars(obj) != obj_dict:
        vars(obj).clear()
        vars(obj).update(obj_dict)
    config = _baseline["modules"]["app.config"]
    if config.get_settings() is not obj:
        config.get_settings.cache_clear()
        factory = config.Settings
        config.Settings = lambda: obj  # get_settings() resolves Settings at call time
        try:
            config.get_settings()
        finally:
            config.Settings = factory


@pytest.fixture(autouse=True)
def _isolate(_app_runtime):
    """Roll every kind of leaked in-process state back after each test."""
    yield
    _restore_sys_modules()
    for name, mod in _baseline["modules"].items():
        _restore_module_state(mod, _baseline["state"][name])
    _restore_settings()
    main = _baseline["modules"]["app.main"]
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(_baseline["overrides"])
    if main.app.router.routes != _baseline["routes"]:  # a test registered a throwaway route
        main.app.router.routes[:] = _baseline["routes"]
        main.app.openapi_schema = None
    _baseline["modules"]["app.database"].engine.dispose()


# ---------------------------------------------------------------------------
# Database reset and the public fixtures
# ---------------------------------------------------------------------------
def _truncate_all_tables(engine) -> None:
    from sqlalchemy import inspect, text

    tables = inspect(engine).get_table_names()
    if not tables:
        return
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            for table in tables:
                conn.execute(text(f'DELETE FROM "{table}"'))
            if conn.execute(text("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'")).first():
                conn.execute(text("DELETE FROM sqlite_sequence"))
            conn.commit()
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")
    else:
        quoted = ", ".join(f'"{t}"' for t in tables)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


def _drop_foreign_tables(engine, base) -> None:
    """Drop tables the models do not own, so a legacy-schema test cannot leak into the next."""
    from sqlalchemy import inspect, text

    owned = set(base.metadata.tables) | {"app_migrations"}
    extra = [t for t in inspect(engine).get_table_names() if t not in owned]
    if not extra:
        return
    suffix = "" if engine.dialect.name == "sqlite" else " CASCADE"
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        for table in extra:
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}"{suffix}'))
        conn.commit()
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")


@pytest.fixture
def _fresh_db(_isolate, monkeypatch):
    """Empty every table (schema kept) so the test sees a freshly installed database."""
    from app.database import Base, engine

    for key, value in _FIXTURE_ENV.items():
        monkeypatch.setenv(key, value)

    engine.dispose()  # drop pooled connections a previous test may have left mid-transaction
    _drop_foreign_tables(engine, Base)  # e.g. legacy tables a migration test created
    Base.metadata.create_all(bind=engine)  # recreate anything a test dropped
    _truncate_all_tables(engine)
    yield


@pytest.fixture
def client(_fresh_db):
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
def db_session(_fresh_db):
    """Direct database session for testing model/helper functions (no API layer)."""
    from app.database import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    yield db
    db.close()
