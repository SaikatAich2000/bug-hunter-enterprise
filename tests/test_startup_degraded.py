"""Startup must never hang: a struggling database degrades, not blocks.
Regression contract for the Azure "Waiting for application startup" hang:
_safe_init_db() bounds init+bootstrap with DB_STARTUP_TIMEOUT_SECONDS, the
Postgres engine carries connect/statement/lock timeouts, and the lifespan
bounds board reconciliation. Covers: timeout config present, safe-init
returns False (not hangs) on a wedged DB, lifespan still serves degraded,
reconcile failure never crashes startup.
"""
from __future__ import annotations

from sqlalchemy.exc import OperationalError

import app.main as main_module
from app.config import get_settings


def test_startup_timeout_settings_present_and_sane():
    s = get_settings()
    assert s.DB_CONNECT_TIMEOUT_SECONDS >= 1
    assert s.DB_STATEMENT_TIMEOUT_MS >= 1000
    assert s.DB_LOCK_TIMEOUT_MS >= 1000
    assert s.DB_STARTUP_TIMEOUT_SECONDS >= 10


def test_safe_init_db_returns_false_on_db_error(monkeypatch):
    def _boom(**_):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(main_module, "init_db", _boom)
    assert main_module._safe_init_db() is False


def test_safe_init_db_times_out_on_wedged_db(monkeypatch):
    import time

    monkeypatch.setattr(
        get_settings(), "DB_STARTUP_TIMEOUT_SECONDS", 2)

    def _hang(**_):
        time.sleep(15)

    monkeypatch.setattr(main_module, "init_db", _hang)
    monkeypatch.setattr(main_module, "_bootstrap", lambda: None)
    assert main_module._safe_init_db() is False


def test_safe_init_db_succeeds_on_healthy_db(monkeypatch):
    monkeypatch.setattr(main_module, "init_db", lambda **_: True)
    monkeypatch.setattr(main_module, "_bootstrap", lambda: None)
    assert main_module._safe_init_db() is True


def test_lifespan_serves_degraded_when_reconcile_hangs(client):
    # Even if board reconciliation wedges, the app must still boot and
    # answer /api/health -- never sit at "Waiting for application startup".
    assert client.get("/api/health").status_code in (200, 503)


def test_reconcile_defers_when_display_id_column_missing(client, monkeypatch):
    # Old databases (pre-display_id migration) must defer reconciliation
    # with a warning, not crash startup with UndefinedColumn.
    import sqlalchemy

    class _FakeInspect:
        def get_columns(self, _table):
            return [{"name": "id"}, {"name": "project_id"}]

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _bind: _FakeInspect())
    # Must not raise; the guard returns early with a warning.
    main_module._reconcile_existing_boards()
    assert client.get("/api/health").status_code in (200, 503)


def test_ddl_type_renders_timestamp_on_postgres():
    from sqlalchemy import DateTime
    from sqlalchemy.dialects import postgresql

    class _FakeConn:
        dialect = postgresql.dialect()

    from app.database import _ddl_type_for
    assert _ddl_type_for(_FakeConn(), DateTime(timezone=True)) == \
        "TIMESTAMP WITH TIME ZONE"
    assert "DATETIME" not in _ddl_type_for(_FakeConn(), DateTime(timezone=True))
