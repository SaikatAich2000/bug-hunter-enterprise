"""Contract for scripts/clean_db.py: a database reset keeps exactly what a first
launch creates (bootstrap admin with its credentials, the seeded default project,
the workflow baseline) and removes everything else, leaving a database the app
can still boot, log in and write to.
"""
from __future__ import annotations


def _counts(db, models):
    return {m.__tablename__: db.query(m).count() for m in models}


def test_clean_db_keeps_first_launch_baseline_and_drops_business_data(admin_client):
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import (
        Activity,
        Bug,
        Comment,
        Project,
        Session,
        User,
        WorkflowStatus,
        WorkflowTransition,
    )
    from scripts.clean_db import clean_database, resolve_baseline_project, resolve_baseline_user

    settings = get_settings()

    # --- business data on top of the bootstrap baseline -----------------------
    general = admin_client.get("/api/projects").json()[0]
    second = admin_client.post("/api/projects", json={
        "name": "Cleanup Me", "color": "#112233", "agile_enabled": True,
    })
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]

    assert admin_client.post("/api/users", json={
        "name": "Temp User", "email": "temp@test.local", "role": "user", "password": "TempPass123",
    }).status_code == 201

    bug = admin_client.post("/api/bugs", json={
        "project_id": general["id"], "title": "Business row", "item_type": "Bug",
    })
    assert bug.status_code == 201, bug.text
    bug_id = bug.json()["id"]
    assert admin_client.post(f"/api/bugs/{bug_id}/comments", json={"body": "hello"}).status_code == 201

    with SessionLocal() as db:
        kept_user = resolve_baseline_user(db)
        kept_project = resolve_baseline_project(db)
        assert kept_user is not None
        assert kept_user.email.lower() == settings.BOOTSTRAP_ADMIN_EMAIL.lower()
        assert kept_project is not None
        assert kept_project.id == general["id"]
        baseline_hash = kept_user.password_hash
        global_statuses = db.query(WorkflowStatus).filter(WorkflowStatus.scope_key == "global").count()
        transitions = db.query(WorkflowTransition).count()
        assert global_statuses > 0
        assert db.query(Bug).count() >= 1
        assert db.query(Project).count() == 2

    # --- dry run changes nothing ---------------------------------------------
    with SessionLocal() as db:
        report = clean_database(db, dry_run=True)
        planned = dict((name, deleted) for name, deleted, _ in report)
        assert planned.get("bugs", 0) == 1
        assert planned.get("projects", 0) == 1
        assert db.query(Bug).count() == 1, "dry run must not delete anything"
        assert db.query(Project).count() == 2

    # --- real run -------------------------------------------------------------
    with SessionLocal() as db:
        clean_database(db, dry_run=False)

    with SessionLocal() as db:
        # Baseline survives, credentials intact.
        users = db.query(User).all()
        assert len(users) == 1
        assert users[0].email.lower() == settings.BOOTSTRAP_ADMIN_EMAIL.lower()
        assert users[0].password_hash == baseline_hash
        assert users[0].role == "admin"
        assert users[0].is_active is True

        projects = db.query(Project).all()
        assert [p.id for p in projects] == [general["id"]]
        assert projects[0].name == "General"

        assert db.query(WorkflowStatus).filter(WorkflowStatus.scope_key == "global").count() == global_statuses
        assert db.query(WorkflowTransition).count() == transitions

        # Business data is gone; the API-created second project took its board
        # and work items with it.
        gone = _counts(db, (Bug, Comment, Activity, Session, WorkflowStatus))
        assert gone["bugs"] == 0
        assert gone["comments"] == 0
        assert gone["activity_log"] == 0
        assert gone["sessions"] == 0
        assert gone["workflow_statuses"] == global_statuses
        assert db.query(Project).filter(Project.id == second_id).count() == 0

    # --- the reset database still works end to end ---------------------------
    # Sessions were cleared, so the old cookie is gone: sign in again with the
    # preserved bootstrap credentials, then write a row.
    relogin = admin_client.post("/api/auth/login", json={
        "email": settings.BOOTSTRAP_ADMIN_EMAIL,
        "password": settings.BOOTSTRAP_ADMIN_PASSWORD,
    })
    assert relogin.status_code == 200, relogin.text
    assert relogin.json()["role"] == "admin"

    recreated = admin_client.post("/api/bugs", json={
        "project_id": general["id"], "title": "After the reset", "item_type": "Bug",
    })
    assert recreated.status_code == 201, recreated.text


def test_clean_db_accepts_azure_style_url_and_never_prints_password():
    import pytest

    from scripts.clean_db import (
        mask_database_url,
        normalize_cleanup_database_url,
    )

    # The Azure-style URL the operator pastes is normalized to the installed
    # psycopg v3 dialect. No real connection is opened here.
    assert normalize_cleanup_database_url(
        "postgresql://u@host:5432/db?sslmode=require"
    ) == "postgresql+psycopg://u@host:5432/db?sslmode=require"
    assert normalize_cleanup_database_url(
        "postgresql+psycopg2://u@host:5432/db?sslmode=require"
    ) == "postgresql+psycopg://u@host:5432/db?sslmode=require"
    assert normalize_cleanup_database_url(
        "postgresql+psycopg://u@host:5432/db?sslmode=require"
    ) == "postgresql+psycopg://u@host:5432/db?sslmode=require"

    masked = mask_database_url(
        "postgresql+psycopg://dbuser:example-pass@db.example.com:5432/testtracker?sslmode=require"
    )
    assert masked == (
        "postgresql+psycopg://dbuser:***@db.example.com:5432/testtracker?sslmode=require"
    )
    assert "DBtest21" not in masked
    assert mask_database_url("") == "<redacted>"
    assert mask_database_url("not-a-url") == "<redacted>"

    for bad in ("", "   ", "mysql://u@host/db"):
        with pytest.raises(ValueError):
            normalize_cleanup_database_url(bad)


def test_clean_db_cli_database_url_overrides_environment(
    tmp_path, monkeypatch, capsys
):
    from sqlalchemy import create_engine

    from app.models import Base
    from scripts import clean_db

    dry_target = tmp_path / "cli-dry.db"
    dry_url = f"sqlite:///{dry_target.as_posix()}"
    empty_engine = create_engine(dry_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(empty_engine)
    empty_engine.dispose()

    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{(tmp_path / 'must-not-be-used.db').as_posix()}",
    )

    rc = clean_db.main(["--database-url", dry_url])
    assert rc == 0
    out = capsys.readouterr().out
    assert dry_target.exists()
    assert (tmp_path / "must-not-be-used.db").exists() is False
    assert "Dry run - nothing was deleted" in out
    assert "DBtest21" not in out
