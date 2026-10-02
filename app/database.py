"""SQLAlchemy engine, session factory, and base class.

Same models run on Postgres (prod) and SQLite (tests / local dev).
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncGenerator, Callable

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.agile import integrity as agile_integrity
from app.config import get_settings

logger = logging.getLogger("bug_hunter.database")

# Rows a brand-new database is seeded with: `_bootstrap()` (app/main.py) creates
# this project, and `_seed_global_workflow_statuses` below fills the workflow
# baseline. Kept as constants so scripts/clean_db.py preserves exactly what a
# first launch creates instead of carrying a second hardcoded copy.
DEFAULT_PROJECT_NAME = "General"
DEFAULT_PROJECT_DESCRIPTION = "Default project for uncategorized bugs"
DEFAULT_PROJECT_COLOR = "#c9764f"


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _build_engine(url: str) -> Engine:
    """Create an engine with sensible per-backend tweaks."""
    if url.startswith("sqlite"):
        # check_same_thread=False: FastAPI shares connections across threads.
        # NOTE: deliberately NO poolclass here. StaticPool (one shared connection
        # for every thread) was tried and is wrong for this suite: threaded
        # tests open SessionLocal() in several workers, and interleaved use of
        # one SQLite connection corrupts the flush/lastrowid handshake
        # (sqlalchemy FlushError "did not produce a new primary key result").
        # Tests use file-backed SQLite (tmp_path/*.db), which every pooled
        # connection sees identically, so the default pool is correct here.
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            future=True,
        )

        # SQLite defaults FKs off; enable so CASCADE / SET NULL fire.
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            # WAL + busy_timeout: concurrent readers/writer, wait instead of
            # "database is locked". SQLite (dev/test) only.
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
            # NORMAL is the recommended durability level under WAL.
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

        return eng

    # Postgres: env-tunable pool; pre_ping tolerates docker-compose start order.
    # connect_timeout + statement/lock timeouts keep a struggling database
    # from hanging application startup forever: lifespan must fail fast
    # (and log) instead of sitting at "Waiting for application startup"
    # while Azure kills the replica. Tunable without a code change.
    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=_settings.DB_POOL_SIZE,
        max_overflow=_settings.DB_MAX_OVERFLOW,
        # Recycle before proxy/idle timeouts reap; fail fast on pool exhaustion.
        pool_recycle=1800,
        pool_timeout=30,
        # TCP connect to a firewalled/slow Postgres must fail fast.
        connect_args={
            "connect_timeout": _settings.DB_CONNECT_TIMEOUT_SECONDS,
            "options": (
                f"-c statement_timeout={int(_settings.DB_STATEMENT_TIMEOUT_MS)} "
                f"-c lock_timeout={int(_settings.DB_LOCK_TIMEOUT_MS)}"
            ),
        },
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _pg_utc_session(dbapi_conn, _):  # pragma: no cover - exercised only on PG
        # UTC session TZ keeps func.date() consistent with Python-side .date();
        # otherwise timeline day buckets can shift by a day.
        cur = dbapi_conn.cursor()
        cur.execute("SET TIME ZONE 'UTC'")
        cur.close()

    return eng


_settings = get_settings()
engine: Engine = _build_engine(_settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
# Work-item hierarchy rules and change log, applied on every flush.
agile_integrity.register(SessionLocal)


# Connections kept free for sessions opened outside get_db (health checks,
# background tasks, the HTML pages' session check).
_RESERVED_CONNECTIONS = 2


def _request_session_slots() -> int:
    """How many requests may hold a pooled session at the same time."""
    pool = engine.pool
    size = pool.size() if callable(getattr(pool, "size", None)) else 5
    overflow = max(0, getattr(pool, "_max_overflow", 0))
    return max(1, size + overflow - _RESERVED_CONNECTIONS)


# asyncio primitives bind to the loop that first uses them, and tests run many
# loops, so each running loop gets its own semaphore.
_slots_by_loop: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _session_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _slots_by_loop.get(loop)
    if sem is None:
        sem = _slots_by_loop[loop] = asyncio.Semaphore(_request_session_slots())
    return sem


async def get_db() -> AsyncGenerator[Session, None]:
    """FastAPI dependency: yield a session, always close it.

    Admission control against a pool deadlock (found by a 100-user spike
    under the 0.5 CPU profile): a request makes several trips through the
    worker-thread pool (sync dependencies, then the sync handler) and holds
    its connection in between. With more requests than connections, every
    worker ended up blocked waiting for a connection while the requests
    holding all of them waited for a worker, until the 30 s pool timeout.
    A request now takes a slot here, on the event loop and without holding a
    thread, before it opens a session, and at most as many requests as the
    pool can serve hold one. Teardown also runs on the event loop.
    close() rolls back uncommitted work, so no half-applied write leaks back
    to the pool.
    """
    async with _session_slots():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()


# --- Audit bridge: mirror Activity rows into the logging stream -------------
# The audit trail itself stays in the activity_log table; these listeners emit
# one log record per committed Activity so the same trail reaches SigNoz when
# OTEL_EXPORTER_OTLP_ENDPOINT is enabled (app/telemetry.py). Registered on this
# module's sessionmaker (not the Session class), so per-test engine re-imports
# never stack duplicate listeners.

@event.listens_for(SessionLocal, "after_flush")
def _stash_new_activity(session: Session, _flush_context) -> None:
    from app.models import Activity
    pending = [obj for obj in session.new if isinstance(obj, Activity)]
    if pending:
        session.info.setdefault("_bh_audit_pending", []).extend(pending)


@event.listens_for(SessionLocal, "after_commit")
def _emit_stashed_activity(session: Session) -> None:
    audit = logging.getLogger("bug_hunter.audit")
    for activity in session.info.pop("_bh_audit_pending", ()):
        audit.info(
            "audit %s %s#%s by %s: %s",
            activity.action, activity.entity_type, activity.entity_id,
            activity.actor_name, activity.detail,
        )


@event.listens_for(SessionLocal, "after_rollback")
def _drop_stashed_activity(session: Session) -> None:
    # Rolled-back writes never happened; they must not appear in the log stream.
    session.info.pop("_bh_audit_pending", None)


def _column_names(inspector, table: str) -> set[str]:
    """Existing column names for `table`, or empty set if it doesn't exist."""
    from sqlalchemy.exc import NoSuchTableError
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except NoSuchTableError:
        return set()
    except SQLAlchemyError:
        # An empty set makes the reconcilers skip the table, so say so.
        logger.warning("Could not read columns of %s; skipping it this boot", table, exc_info=True)
        return set()


def _ddl_type_for(conn, coltype) -> str:
    """Render a column type for the live backend.

    SQLAlchemy's DATETIME renders on Postgres, where the type is
    TIMESTAMP -- emitting DATETIME aborts the statement (and, before
    per-column SAVEPOINTs, poisoned the whole reconciliation
    transaction). Route through the connection dialect and rewrite
    DATETIME to TIMESTAMP so old databases accept the ADD COLUMN.
    """
    try:
        ddl_type = str(coltype.compile(dialect=conn.dialect)).upper()
    except SQLAlchemyError:
        ddl_type = str(coltype).upper()
    if "DATETIME" in ddl_type:
        ddl_type = ddl_type.replace("DATETIME", "TIMESTAMP")
    return ddl_type


def _add_column_safely(conn, sql: str) -> None:
    """Run one additive ALTER inside a SAVEPOINT; failures are logged, not fatal."""
    from sqlalchemy import text
    try:
        with conn.begin_nested():
            conn.execute(text(sql))
    except SQLAlchemyError:
        logger.warning("Additive column migration skipped (already applied or "
                       "failed): %s", sql)


def _add_missing_columns(conn) -> None:
    """ALTER-ADD columns the model declares that an existing DB lacks.

    create_all() never alters existing tables; each new column is nullable or
    defaulted so no backfill is needed. Runs before the index pass.
    """
    from sqlalchemy import inspect
    # Inspect through the migration's own connection: a separate connection
    # can't see this transaction's changes and, on PostgreSQL, waits on the
    # locks its ALTERs hold until lock_timeout, which used to make the
    # generic pass skip the table silently.
    inspector = inspect(conn)

    bug_cols = _column_names(inspector, "bugs")
    if bug_cols and "item_type" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN item_type VARCHAR(20) "
            "NOT NULL DEFAULT 'Bug'")
    if bug_cols and "event_id" not in bug_cols:
        # No FK at ALTER level (SQLite can't add one); ORM enforces integrity.
        _add_column_safely(conn, "ALTER TABLE bugs ADD COLUMN event_id INTEGER")

    # bugs.version — optimistic-concurrency counter; DEFAULT 1, no backfill.
    if bug_cols and "version" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")

    # notifications.emailed_at — nullable; digest lookback window skips the
    # historical NULL backlog. tz-aware type on Postgres to match the ORM.
    notif_cols = _column_names(inspector, "notifications")
    if notif_cols and "emailed_at" not in notif_cols:
        tstype = (
            "TIMESTAMP WITH TIME ZONE"
            if engine.dialect.name == "postgresql"
            else "TIMESTAMP"
        )
        _add_column_safely(conn,
            f"ALTER TABLE notifications ADD COLUMN emailed_at {tstype}")

    # events.project_id — optional owning project; nullable, no FK at ALTER
    # level (see event_id note). NULL rows stay admin-only until assigned.
    event_cols = _column_names(inspector, "events")
    if event_cols and "project_id" not in event_cols:
        _add_column_safely(conn, "ALTER TABLE events ADD COLUMN project_id INTEGER")

    _add_missing_org_columns(conn, inspector)
    _widen_secret_columns(conn, inspector)
    _add_missing_bug_display_id(conn, bug_cols)
    _add_missing_agile_columns(conn, inspector, bug_cols)
    _add_missing_sprint_columns(conn, inspector)
    _add_missing_epic_detail_columns(conn, inspector)
    _add_missing_git_columns(conn, inspector)
    _add_missing_columns_from_metadata(conn)


def _add_missing_columns_from_metadata(conn) -> None:
    """Generic additive reconciliation: add any ORM column the DB still lacks.

    Catches columns added to models after this deployment's database was
    created but that the per-table helpers above don't know about yet (e.g.
    ``boards.display_id`` on a volume provisioned by an earlier release).
    Strictly additive: columns are added nullable, or non-nullable only when
    the model supplies a safe scalar default, and no existing data is touched.
    """
    from sqlalchemy import inspect
    # Fresh inspector: the per-table helpers above have just added columns.
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = _column_names(inspector, table.name)
        if not present:
            continue
        for column in table.columns:
            if column.name in present:
                continue
            # Compute a safe server default; refuse anything that would need a
            # backfill or a non-scalar default (those need a bespoke migration).
            default_sql = None
            if column.nullable:
                default_sql = None
            else:
                py_default = getattr(column.default, "arg", None)
                if isinstance(py_default, bool):
                    default_sql = "FALSE" if not py_default else "TRUE"
                elif isinstance(py_default, int):
                    default_sql = str(py_default)
                elif isinstance(py_default, float):
                    default_sql = str(py_default)
                elif isinstance(py_default, str):
                    default_sql = f"'{py_default.replace(chr(39), chr(39) * 2)}'"
                if default_sql is None:
                    logger.warning(
                        "Column %s.%s is missing and has no safe default; "
                        "skipping (needs a bespoke migration)",
                        table.name, column.name,
                    )
                    continue
            ddl_type = _ddl_type_for(conn, column.type)
            clause = f" {ddl_type}" if default_sql is None else \
                f" {ddl_type} NOT NULL DEFAULT {default_sql}"
            _add_column_safely(
                conn, f"ALTER TABLE {table.name} ADD COLUMN {column.name}{clause}"
            )


# Tables that belong to an organization directly (everything else is scoped through them).
_ORG_TABLES = ("users", "projects", "events", "activity_log")


def _add_missing_org_columns(conn, inspector) -> None:
    """Add the nullable ``org_id`` column to tables of a database that predates multi-tenancy
    (a single-tenant Bug Hunter); ``_adopt_single_tenant_data`` fills it afterwards."""
    for table in _ORG_TABLES:
        cols = _column_names(inspector, table)
        if cols and "org_id" not in cols:
            _add_column_safely(conn, f"ALTER TABLE {table} ADD COLUMN org_id INTEGER")


def _adopt_single_tenant_data(conn) -> None:
    """Give a pre-tenant database its one organization.

    Rows without an organization (everything, on a database from the single-tenant edition)
    join BOOTSTRAP_ORG_NAME, created when there is no organization yet. Rows that already belong
    to an organization are never touched, so this is a no-op on every later boot.
    """
    from sqlalchemy import select

    from app.models import Organization
    from app.tenancy import unique_slug

    orgs = Organization.__table__
    orphan_tables = []
    for name in _ORG_TABLES:
        table = Base.metadata.tables[name]
        try:
            if conn.execute(select(table.c.org_id).where(table.c.org_id.is_(None)).limit(1)).first():
                orphan_tables.append(name)
        except SQLAlchemyError:
            logger.warning("Could not check %s for rows without an organization", name)
    if not orphan_tables:
        return
    org_id = conn.execute(select(orgs.c.id).order_by(orgs.c.id).limit(1)).scalar()
    if org_id is None:
        org_name = (_settings.BOOTSTRAP_ORG_NAME or "Default Organization")[:120]
        org_id = conn.execute(orgs.insert().values(
            name=org_name, slug=unique_slug(conn, org_name), description="",
        ).returning(orgs.c.id)).scalar_one()
    for name in orphan_tables:
        table = Base.metadata.tables[name]
        moved = conn.execute(
            table.update().where(table.c.org_id.is_(None)).values(org_id=org_id)
        ).rowcount
        logger.info("Multi-tenant upgrade: %d row(s) of %s joined organization #%d", moved, name, org_id)


def _backfill_project_keys(conn) -> None:
    """Give projects without a key one derived from the name (unique within the organization)."""
    from sqlalchemy import select

    from app.models import Project
    from app.project_keys import derive_key

    projects = Project.__table__
    try:
        rows = conn.execute(
            select(projects.c.id, projects.c.org_id, projects.c.name, projects.c.key)
            .where(projects.c.key == "").order_by(projects.c.id)
        ).all()
        if not rows:
            return
        taken: dict[int, set[str]] = {}
        for org_id, key in conn.execute(select(projects.c.org_id, projects.c.key)).all():
            taken.setdefault(org_id, set()).add(key)
        for pid, org_id, name, _ in rows:
            base, candidate, n = derive_key(name), derive_key(name), 2
            while candidate in taken.setdefault(org_id, set()) or len(candidate) < 2:
                candidate, n = f"{base}{n}", n + 1
            taken[org_id].add(candidate)
            conn.execute(projects.update().where(projects.c.id == pid).values(key=candidate[:16]))
        logger.info("Backfilled the key of %d project(s)", len(rows))
    except SQLAlchemyError:
        logger.warning("Project key backfill skipped (table/column may be mid-migration)")


def _migrate_enterprise_tables(conn) -> None:
    """Fold the earlier enterprise edition's tables into the current ones; its own tables are
    left in place and never read again.

    - ``project_memberships`` (role lead/member) -> ``user_projects``
    - ``device_tokens`` (Android FCM tokens)     -> ``push_subscriptions``
    - role "member"                              -> "user" (users and invitations)
    Each step is idempotent: rows already present are skipped, a second boot changes nothing.
    """
    from sqlalchemy import inspect

    tables = set(inspect(conn).get_table_names())
    steps = []
    if "project_memberships" in tables:
        steps.append(("project memberships", (
            "INSERT INTO user_projects (user_id, project_id, role) "
            "SELECT pm.user_id, pm.project_id, pm.role FROM project_memberships pm "
            "WHERE NOT EXISTS (SELECT 1 FROM user_projects up "
            "WHERE up.user_id = pm.user_id AND up.project_id = pm.project_id)")))
    if "device_tokens" in tables:
        steps.append(("device tokens", (
            "INSERT INTO push_subscriptions (user_id, token, platform, user_agent, created_at, last_seen_at) "
            "SELECT d.user_id, d.token, d.platform, '', d.created_at, COALESCE(d.last_seen_at, d.created_at) "
            "FROM device_tokens d WHERE NOT EXISTS "
            "(SELECT 1 FROM push_subscriptions p WHERE p.token = d.token)")))
    steps.append(("member roles", "UPDATE users SET role = 'user' WHERE role = 'member'"))
    if "invitations" in tables:
        steps.append(("invitation roles", "UPDATE invitations SET role = 'user' WHERE role = 'member'"))
    for label, sql in steps:
        try:
            with conn.begin_nested():
                moved = conn.execute(text(sql)).rowcount
            if moved:
                logger.info("Enterprise upgrade: %s: %d row(s) converted", label, moved)
        except SQLAlchemyError:
            logger.exception("Enterprise upgrade step %r failed; it will be retried next boot", label)


def _widen_secret_columns(conn, inspector) -> None:
    """The earlier edition stored TOTP and webhook secrets in 64/80-character columns; encrypted
    values need more room. PostgreSQL enforces the length (SQLite does not)."""
    if conn.dialect.name != "postgresql":
        return
    for table, column in (("users", "totp_secret"), ("webhooks", "secret")):
        for col in _columns_of(inspector, table):
            length = getattr(col["type"], "length", None)
            if col["name"] == column and length is not None and length < 255:
                _add_column_safely(conn, f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR(255)")


def _columns_of(inspector, table: str) -> list[dict]:
    try:
        return inspector.get_columns(table)
    except SQLAlchemyError:
        return []


def _add_missing_git_columns(conn, inspector) -> None:
    """Additive Git integration columns.

    ``project_git_configs.credential_encrypted`` holds a Fernet ciphertext
    (app/git/credentials.py). Additive and defaulted to ``''``, so every
    existing row keeps exactly its previous behaviour: no project credential,
    unchanged legacy global-token fallback. Nothing is rewritten or destroyed.
    """
    git_cols = _column_names(inspector, "project_git_configs")
    if git_cols and "credential_encrypted" not in git_cols:
        _add_column_safely(conn,
            "ALTER TABLE project_git_configs ADD COLUMN credential_encrypted "
            "TEXT NOT NULL DEFAULT ''")


# Agile-only additive columns. Every
# column is nullable or safely defaulted; no backfill required at boot —
# rank/rank_scope backfill happens at per-project Agile activation instead
# (app/agile/boards.py::activate_agile_for_project), never here.
def _add_missing_bug_display_id(conn, bug_cols: set[str]) -> None:
    if bug_cols and "display_id" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN display_id VARCHAR(32)")

def _backfill_bug_display_id(conn) -> None:
    """Backfill display_id for existing bugs that lack one. Deterministic from
    current item_type + numeric id. One-time no-op once all rows are populated."""
    from sqlalchemy import text
    try:
        result = conn.execute(text(
            "UPDATE bugs SET display_id = "
            "CASE item_type "
            "WHEN 'Bug' THEN 'BUG-' || id "
            "WHEN 'Requirement' THEN 'REQ-' || id "
            "WHEN 'Task' THEN 'TASK-' || id "
            "WHEN 'Sub-task' THEN 'SUB-' || id "
            "WHEN 'Story' THEN 'USRSTR-' || id "
            "WHEN 'Epic' THEN 'EPIC-' || id "
            "ELSE 'ID-' || id "
            "END "
            "WHERE display_id IS NULL"
        ))
        if result.rowcount:
            logger.info("Backfilled display_id for %d bug rows", result.rowcount)
    except SQLAlchemyError:
        logger.warning("display_id backfill skipped (table/column may be mid-migration)")


def _align_agile_hierarchy(conn) -> None:
    """Align existing agile data with the Jira hierarchy (app/agile/upgrade.py)."""
    from app.agile.upgrade import align_agile_hierarchy

    align_agile_hierarchy(conn)


def _preserve_legacy_html_descriptions(conn) -> None:
    """Copy 3.x rich-text (HTML) descriptions aside before 4.0 flattens them.

    4.0 stores and returns descriptions as plain text, and saving an item
    replaces a stored HTML description with its plain-text form. Copying every
    HTML description into description_legacy_html first keeps the formatting
    recoverable. Idempotent: rows already copied are skipped; plain-text rows
    (everything 4.0 writes) never match. Runs under the migration lock.
    """
    from sqlalchemy import text
    try:
        # SAVEPOINT: on PostgreSQL a failed statement would otherwise abort the
        # whole migration transaction. Patterns are bound parameters because
        # psycopg treats a literal % in SQL text as a placeholder.
        with conn.begin_nested():
            result = conn.execute(text(
                "UPDATE bugs SET description_legacy_html = description "
                "WHERE description_legacy_html IS NULL "
                "AND (description LIKE :closing_tag OR description LIKE :line_break)"
            ), {"closing_tag": "%</%", "line_break": "%<br%"})
        if result.rowcount:
            logger.info("Preserved %d rich-text description(s) from before 4.0", result.rowcount)
    except SQLAlchemyError:
        logger.exception("legacy description preservation failed; descriptions were not copied")


def _add_missing_agile_columns(conn, inspector, bug_cols: set[str]) -> None:
    _add_missing_project_agile_columns(conn, inspector)
    if bug_cols:
        _add_missing_bug_agile_columns(conn, bug_cols)


def _add_missing_project_agile_columns(conn, inspector) -> None:
    project_cols = _column_names(inspector, "projects")
    if not project_cols:
        return
    if "agile_enabled" not in project_cols:
        _add_column_safely(conn,
            "ALTER TABLE projects ADD COLUMN agile_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    if "agile_feature_flags" not in project_cols:
        # JSON portability contract: empty object, not a string.
        default_json = "'{}'::jsonb" if engine.dialect.name == "postgresql" else "'{}'"
        _add_column_safely(conn,
            "ALTER TABLE projects ADD COLUMN agile_feature_flags JSON "
            f"NOT NULL DEFAULT {default_json}")


def _add_missing_bug_agile_columns(conn, bug_cols: set[str]) -> None:
    int_cols = ("parent_id", "epic_id", "sprint_id",
                "original_estimate_minutes", "remaining_estimate_minutes")
    for col in int_cols:
        if col not in bug_cols:
            _add_column_safely(conn, f"ALTER TABLE bugs ADD COLUMN {col} INTEGER")
    for col in ("rank_scope", "rank"):
        if col not in bug_cols:
            _add_column_safely(conn, f"ALTER TABLE bugs ADD COLUMN {col} VARCHAR(64)")
    _add_missing_bug_agile_columns_extra(conn, bug_cols)


def _add_missing_bug_agile_columns_extra(conn, bug_cols: set[str]) -> None:
    if "story_points" not in bug_cols:
        numeric_type = "NUMERIC(6,2)" if engine.dialect.name == "postgresql" else "NUMERIC"
        _add_column_safely(conn, f"ALTER TABLE bugs ADD COLUMN story_points {numeric_type}")
    if "time_spent_minutes" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN time_spent_minutes INTEGER NOT NULL DEFAULT 0")
    if "acceptance_criteria" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN acceptance_criteria TEXT NOT NULL DEFAULT ''")
    if "resolution" not in bug_cols:
        _add_column_safely(conn, "ALTER TABLE bugs ADD COLUMN resolution VARCHAR(50)")
    if "resolved_at" not in bug_cols:
        tstype = "TIMESTAMP WITH TIME ZONE" if engine.dialect.name == "postgresql" else "TIMESTAMP"
        _add_column_safely(conn, f"ALTER TABLE bugs ADD COLUMN resolved_at {tstype}")
    if "flagged" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN flagged BOOLEAN NOT NULL DEFAULT FALSE")
    _add_missing_hierarchy_columns(conn, bug_cols)


# Agile additive columns on bugs; new tables are created by create_all().
def _add_missing_hierarchy_columns(conn, bug_cols: set[str]) -> None:
    if "owner_id" not in bug_cols:
        _add_column_safely(conn, "ALTER TABLE bugs ADD COLUMN owner_id INTEGER")
    if "ready_for_sprint" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN ready_for_sprint BOOLEAN NOT NULL DEFAULT FALSE")
    if "blocked" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN blocked BOOLEAN NOT NULL DEFAULT FALSE")
    if "blocked_reason" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT ''")
    if "mandatory" not in bug_cols:
        _add_column_safely(conn,
            "ALTER TABLE bugs ADD COLUMN mandatory BOOLEAN NOT NULL DEFAULT FALSE")
    if "start_date" not in bug_cols:
        _add_column_safely(conn, "ALTER TABLE bugs ADD COLUMN start_date VARCHAR(10)")


def _add_missing_sprint_columns(conn, inspector) -> None:
    sprint_cols = _column_names(inspector, "sprints")
    if not sprint_cols:
        return
    if "cadence" not in sprint_cols:
        _add_column_safely(conn, "ALTER TABLE sprints ADD COLUMN cadence VARCHAR(20)")


def _add_missing_epic_detail_columns(conn, inspector) -> None:
    """Add sprint_id to epic_details table to track Epic sprint assignments."""
    epic_detail_cols = _column_names(inspector, "epic_details")
    if not epic_detail_cols:
        return
    if "sprint_id" not in epic_detail_cols:
        _add_column_safely(conn, "ALTER TABLE epic_details ADD COLUMN sprint_id INTEGER")


def _add_missing_indexes(conn) -> None:
    """CREATE indexes the model declares that the DB lacks (idempotent)."""
    from sqlalchemy import Index, inspect
    inspector = inspect(conn)  # same connection as the migration (see _add_missing_columns)
    # display_id is a nullable human-facing label, backfilled on old
    # databases: NULL/duplicate values are legitimate legacy data, so these
    # indexes must never be UNIQUE -- a unique index both fails to create
    # on old data (the Azure warnings) and would reject future duplicates.
    display_id_non_unique = frozenset({
        "ix_boards_display_id", "ix_components_display_id",
        "ix_labels_display_id", "ix_versions_display_id",
        "ix_collections_display_id", "ix_features_display_id",
    })
    for table in Base.metadata.sorted_tables:
        try:
            existing = {idx["name"] for idx in inspector.get_indexes(table.name)}
        except SQLAlchemyError:
            # Table absent → nothing to compare against.
            continue
        for idx in table.indexes:
            if idx.name and idx.name not in existing:
                if idx.name in display_id_non_unique and idx.unique:
                    idx = Index(
                        idx.name,
                        *[c.name for c in idx.columns.values()],
                    )
                _create_index_safely(conn, idx, table.name)


def _create_index_safely(conn, idx, table_name: str) -> None:
    """Create one missing index inside a SAVEPOINT.

    A failure (e.g. UNIQUE index over pre-existing duplicates) is logged and
    skipped rather than crashing boot.
    """
    try:
        with conn.begin_nested():
            idx.create(bind=conn, checkfirst=True)
    except SQLAlchemyError:
        logger.warning(
            "Could not create index %s on %s -- the same index name is "
            "already used by an existing index on different columns "
            "(legacy schema drift), or the table holds legacy data the "
            "new index cannot cover. Skipping; the application does not "
            "need this duplicate.",
            idx.name, table_name,
        )


def _retire_legacy_branch_index(conn) -> None:
    """Drop the legacy whole-table unique branch index (idempotent, additive).

    The old ``idx_work_item_branches_unique`` made a removed branch's name
    permanently block a recreated branch. Branch uniqueness is now enforced by
    the partial ``idx_work_item_branches_active_unique`` (Active rows only).
    Only the index is dropped — branch rows and history are never touched.
    """
    from sqlalchemy import text
    try:
        conn.execute(text("DROP INDEX IF EXISTS idx_work_item_branches_unique"))
    except SQLAlchemyError as exc:  # pragma: no cover - dialect edge cases only
        logger.warning("Could not retire legacy branch index: %s", exc)


def init_db(on_migrated: Callable[[], None] | None = None) -> bool:
    """Create missing tables, then missing columns and indexes.

    Idempotent on every boot; nothing is dropped, renamed, or altered.
    ``on_migrated`` (first-run seeding) runs only in the process that did the
    migration, while it still holds the lock, so two replicas booting on an
    empty database never seed twice or seed before the tables exist. Returns
    True when this process ran the reconciliation.

    On Postgres the whole reconciliation runs on one connection holding a
    NOWAIT advisory lock: on a scaled-out deployment (e.g. Azure Container
    Apps with 2+ replicas) only one replica migrates at a time. A replica
    that cannot get the lock skips reconciliation and starts serving
    immediately -- it must never block startup (and the platform health
    probe) waiting on another replica's DDL, otherwise every replica sits
    at "Waiting for application startup" until the platform kills it.
    """
    # Local import avoids a circular import at module load.
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    if _settings.DATABASE_URL.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            _add_missing_columns(conn)
        with engine.begin() as conn:
            _retire_legacy_branch_index(conn)
            _add_missing_indexes(conn)
        with engine.begin() as conn:
            _seed_global_workflow_statuses(conn)
        with engine.begin() as conn:
            _adopt_single_tenant_data(conn)
            _migrate_enterprise_tables(conn)
            _backfill_project_keys(conn)
            _backfill_bug_display_id(conn)
            _preserve_legacy_html_descriptions(conn)
            _align_agile_hierarchy(conn)
        if on_migrated is not None:
            on_migrated()
        return True

    # One connection, explicit commits: the advisory lock is session-level, so
    # it stays held across the DDL commit and the seeding that follows it.
    with engine.connect() as conn:
        try:
            locked = conn.execute(
                text("SELECT pg_try_advisory_lock(72794811)")).scalar()
            conn.commit()
        except SQLAlchemyError:
            conn.rollback()
            locked = True
        if not locked:
            logger.info(
                "startup reconciliation skipped: another replica holds "
                "the migration lock; starting to serve without migrating."
            )
            return False
        try:
            Base.metadata.create_all(bind=conn)
            _add_missing_columns(conn)
            _retire_legacy_branch_index(conn)
            _add_missing_indexes(conn)
            _seed_global_workflow_statuses(conn)
            _adopt_single_tenant_data(conn)
            _migrate_enterprise_tables(conn)
            _backfill_project_keys(conn)
            _backfill_bug_display_id(conn)
            _preserve_legacy_html_descriptions(conn)
            _align_agile_hierarchy(conn)
            # All DDL commits together; seeding uses its own session and must
            # see the tables, so it runs after this commit, still under the lock.
            conn.commit()
            if on_migrated is not None:
                on_migrated()
        except BaseException:
            conn.rollback()
            raise
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(72794811)"))
                conn.commit()
            except SQLAlchemyError:
                conn.rollback()
    return True


# Explicit, source-controlled seed data for the global workflow statuses
#: (persisted_value, key, category,
# is_initial, is_terminal) per item type. Never derived/guessed at runtime —
# every persisted_value here must exactly match app.schemas.STATUSES_BY_TYPE;
# a mismatch is logged as an ERROR and that item type is skipped (not seeded)
# rather than silently invented.
#
# The same canonical status vocabulary is seeded for every work-item type so
# that any configured board status can be applied to any item. The board column
# categories (todo/in_progress/testing/done) are what drive workflow, not the
# item type. Task and Sub-task are distinct types with the same vocabulary.
_IN_PROGRESS_ROW = ("In Progress", "in_progress", "in_progress", False, False)
_TESTING_ROW = ("Testing", "testing", "testing", False, False)

# Canonical status seed rows: (persisted_value, key, category, is_initial, is_terminal).
# Applied uniformly to every work-item type.
_CANONICAL_STATUS_SEED_ROWS: list[tuple[str, str, str, bool, bool]] = [
    ("New", "new", "todo", True, False),
    _IN_PROGRESS_ROW,
    _TESTING_ROW,
    ("In Review", "in_review", "in_progress", False, False),
    ("Approved", "approved", "in_progress", False, False),
    ("Done", "done", "done", False, True),
    ("Blocked", "blocked", "in_progress", False, False),
    ("Cancelled", "cancelled", "done", False, True),
    ("Resolved", "resolved", "done", False, True),
    ("Closed", "closed", "done", False, True),
    ("Reopened", "reopened", "in_progress", False, False),
    ("Not a Bug", "not_a_bug", "done", False, True),
    ("Resolve Later", "resolve_later", "todo", False, False),
    ("Implemented", "implemented", "done", False, True),
    ("Rejected", "rejected", "done", False, True),
    ("Deferred", "deferred", "todo", False, False),
    ("Planned", "planned", "todo", False, False),
    ("Completed", "completed", "done", False, True),
]

_WORKFLOW_STATUS_SEED: dict[str, list[tuple[str, str, str, bool, bool]]] = {
    "Bug": list(_CANONICAL_STATUS_SEED_ROWS),
    "Requirement": list(_CANONICAL_STATUS_SEED_ROWS),
    "Task": list(_CANONICAL_STATUS_SEED_ROWS),
    "Epic": list(_CANONICAL_STATUS_SEED_ROWS),
    "Story": list(_CANONICAL_STATUS_SEED_ROWS),
    "Sub-task": list(_CANONICAL_STATUS_SEED_ROWS),
}


def _seed_global_workflow_statuses(conn) -> None:
    """Idempotent check-then-insert of the global (scope_key='global') workflow
    statuses. Project-scoped rows are not created here.
    """
    from sqlalchemy import select

    from app.models import WorkflowStatus
    from app.schemas import STATUSES_BY_TYPE

    try:
        existing = set(
            conn.execute(
                select(WorkflowStatus.work_item_type, WorkflowStatus.key)
                .where(WorkflowStatus.scope_key == "global")
            ).all()
        )
    except SQLAlchemyError:
        # Table doesn't exist yet on a very old snapshot mid-migration; the
        # next boot (after create_all runs) will find it.
        return

    for item_type, rows in _WORKFLOW_STATUS_SEED.items():
        declared = set(STATUSES_BY_TYPE.get(item_type, []))
        seeded_values = {r[0] for r in rows}
        if declared != seeded_values:
            logger.error(
                "Workflow-status seed drift for item_type=%s: schemas.py "
                "declares %s but the seed table has %s. Skipping seed for "
                "this type until reconciled — see "
                "scripts/agile_reconciliation_report.py.",
                item_type, sorted(declared), sorted(seeded_values),
            )
            continue
        for position, (persisted_value, key, category, is_initial, is_terminal) in enumerate(rows):
            if (item_type, key) in existing:
                continue
            try:
                with conn.begin_nested():
                    conn.execute(
                        WorkflowStatus.__table__.insert().values(
                            scope_key="global",
                            work_item_type=item_type,
                            key=key,
                            persisted_status_value=persisted_value,
                            name=persisted_value,
                            category=category,
                            color="",
                            position=position,
                            is_initial=is_initial,
                            is_terminal=is_terminal,
                            is_active=True,
                        )
                    )
            except SQLAlchemyError:
                logger.warning(
                    "Could not seed global workflow status %s/%s (already "
                    "present?) — skipping.", item_type, key,
                )
