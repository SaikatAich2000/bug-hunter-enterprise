"""SQLAlchemy engine, session factory, and base class.

We use SQLAlchemy 2.x so the same models work on Postgres (production)
and SQLite (tests / local dev fallback).
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _build_engine(url: str) -> Engine:
    """Create an engine with sensible per-backend tweaks."""
    if url.startswith("sqlite"):
        # check_same_thread=False so FastAPI can pass connections between
        # the request handler and dependency-injected helpers.
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            future=True,
        )

        # SQLite ships with FK enforcement OFF by default. We turn it on
        # for every new connection so ON DELETE CASCADE / SET NULL clauses
        # actually fire — without this, deleting a user wouldn't clean up
        # bug_assignees rows on SQLite.
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        return eng

    # Postgres / others — Postgres enforces FKs natively. Use a small
    # connection pool that respects docker-compose start ordering via pre_ping.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


_settings = get_settings()
engine: Engine = _build_engine(_settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables if they don't exist, AND create any missing indexes on
    existing tables. Idempotent — safe to call on every boot.

    Why the second pass? SQLAlchemy's `create_all()` skips tables that
    already exist, including their indexes. That means new indexes added
    in a later release would never appear on a long-running production
    database — the schema would silently lag behind the model. We close
    that gap by inspecting existing indexes per table after `create_all()`
    and issuing `CREATE INDEX IF NOT EXISTS` for any that the model
    declares but the database lacks.

    This is strictly ADDITIVE: nothing is dropped, altered, or renamed,
    and existing data is never touched. Both SQLite and Postgres
    natively support `CREATE INDEX IF NOT EXISTS`, so no per-dialect
    branching is needed.
    """
    # Local import avoids circular import at module load.
    from app import models  # noqa: F401  (registers tables on Base.metadata)
    from sqlalchemy import inspect

    Base.metadata.create_all(bind=engine)

    # Second pass: add any indexes the model declares but the DB is missing.
    # This is what makes new composite indexes show up on an upgraded DB.
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            try:
                existing = {idx["name"] for idx in inspector.get_indexes(table.name)}
            except Exception:
                # If the table doesn't exist for any reason, create_all
                # would have made it on the line above; either way, nothing
                # to compare against. Skip cleanly.
                continue
            for idx in table.indexes:
                if idx.name and idx.name not in existing:
                    # Use SQLAlchemy's own DDL — emits dialect-correct
                    # CREATE INDEX with the right column escaping for
                    # whichever backend is active.
                    idx.create(bind=conn, checkfirst=True)
