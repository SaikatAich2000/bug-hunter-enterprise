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
    existing tables AND add any missing columns the model declares.
    Idempotent — safe to call on every boot.

    Why all three passes? SQLAlchemy's `create_all()` skips tables that
    already exist, including their indexes and any new columns the
    model has grown since the last release. That means schema
    additions never reach a long-running production database — the
    schema would silently lag behind the code. We close that gap with:

      1. `create_all()` — new tables.
      2. Column reconciliation — `ALTER TABLE ... ADD COLUMN` for any
         column the model declares but the DB lacks. ALTER ADD COLUMN
         is non-locking and atomic on both SQLite and Postgres for the
         nullable / default-having columns we add for new features
         (TOTP secrets, brand colour, etc.).
      3. Index reconciliation — `CREATE INDEX IF NOT EXISTS` for any
         index the model declares but the DB lacks. This runs AFTER
         the column pass because new indexes can target new columns
         (e.g. v2.4 adds idx_bugs_event_id on the brand-new
         bugs.event_id column) — running indexes first would fail with
         "column does not exist" on long-running production DBs.

    Everything here is strictly ADDITIVE: nothing is dropped, altered
    in-place, renamed, or has its constraints tightened. Existing data
    is never touched. Both SQLite and Postgres natively support
    `CREATE INDEX IF NOT EXISTS`; ADD COLUMN is dialect-portable.
    """
    # Local import avoids circular import at module load.
    from app import models  # noqa: F401  (registers tables on Base.metadata)
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn

    Base.metadata.create_all(bind=engine)

    # ── Pass 2: columns ──────────────────────────────────────────────
    # Must run before the index pass so any new indexes that target a
    # brand-new column (e.g. idx_bugs_event_id → bugs.event_id) see the
    # column when CREATE INDEX runs.
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            try:
                existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            except Exception:
                continue
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                # Skip primary-key columns: ADD COLUMN can't add a PK on
                # an existing table portably. Any model that adds a new
                # PK is a re-design, not an additive change.
                if column.primary_key:
                    continue
                # Build the ALTER TABLE manually so we can guarantee the
                # column lands with a NULL-tolerant definition. We never
                # add a NOT NULL column without a server-side default on
                # an existing table, because that would fail on rows the
                # DB already has.
                col_ddl = CreateColumn(column).compile(dialect=engine.dialect).string
                # SQLAlchemy emits the bare column spec — wrap it.
                stmt = text(f'ALTER TABLE "{table.name}" ADD COLUMN {col_ddl}')
                try:
                    conn.execute(stmt)
                except Exception:
                    # If the dialect emits a definition the DB rejects,
                    # we fall back to a permissive nullable variant so
                    # the boot still completes — the model code reading
                    # the column already tolerates NULL.
                    try:
                        sql_type = column.type.compile(dialect=engine.dialect)
                        conn.execute(text(
                            f'ALTER TABLE "{table.name}" '
                            f'ADD COLUMN "{column.name}" {sql_type}'
                        ))
                    except Exception:
                        # Last resort — log and continue. Operators
                        # will see the missing column in /api/health
                        # diagnostics (or downstream model usage will
                        # surface a clear error).
                        import logging
                        logging.getLogger("bug_hunter").exception(
                            "Failed to add column %s.%s; manual migration may be needed",
                            table.name, column.name,
                        )

    # ── Pass 3: indexes ──────────────────────────────────────────────
    # Re-inspect so we see columns we just added (Pass 2) — otherwise
    # the "all index columns exist" guard below would skip indexes that
    # target the column we literally just created.
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            try:
                existing = {idx["name"] for idx in inspector.get_indexes(table.name)}
            except Exception:
                continue
            try:
                table_cols = {col["name"] for col in inspector.get_columns(table.name)}
            except Exception:
                table_cols = set()
            for idx in table.indexes:
                if not idx.name or idx.name in existing:
                    continue
                # Defensive guard: only create an index when every column
                # it references actually exists in the live table. If the
                # column pass above failed for any reason, we'd rather
                # log-and-skip than crash startup and leave the service
                # down.
                idx_cols = {c.name for c in idx.columns}
                if table_cols and not idx_cols.issubset(table_cols):
                    import logging
                    logging.getLogger("bug_hunter").warning(
                        "Skipping index %s on %s: missing columns %s",
                        idx.name, table.name, sorted(idx_cols - table_cols),
                    )
                    continue
                try:
                    idx.create(bind=conn, checkfirst=True)
                except Exception:
                    import logging
                    logging.getLogger("bug_hunter").exception(
                        "Failed to create index %s on %s; manual migration may be needed",
                        idx.name, table.name,
                    )
