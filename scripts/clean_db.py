"""Reset the database to its first-launch baseline.

Deletes every user/business row and keeps exactly what `init_db()` and
`_bootstrap()` create on an empty database:

  * the bootstrap admin account (`BOOTSTRAP_ADMIN_EMAIL`) — the row, its
    password hash and role are untouched, so the credentials in `.env` keep
    working;
  * that account's organization, and its seeded default project
    (`app.database.DEFAULT_PROJECT_NAME`);
  * the global workflow statuses and their default transitions (the workflow
    baseline every board is built from).

Everything else goes: work items, sprints, boards, taxonomy (collections,
epics, features, versions, components, labels), comments, attachments, events,
notifications, push subscriptions, chat history, git configurations, quick
filters, idempotency keys, password-reset tokens, the audit trail and all
sessions (everyone is logged out and signs back in with the kept account).

Usage (dry-run first — nothing is deleted without --yes):

    # Docker/app container database, from DATABASE_URL in the environment:
    python scripts/clean_db.py               # show what would be deleted
    python scripts/clean_db.py --yes         # delete it

    # Stand-alone Azure/container use (Docker is not required):
    #
    #   $env:DATABASE_URL="postgresql+psycopg://USER:PASSWORD@HOST:5432/DB?sslmode=require"
    #   python scripts/clean_db.py --database-url "$env:DATABASE_URL" --yes
    #
    # The URL may also be supplied with --database-url. DATABASE_URL in the
    # environment is used when the flag is absent. URLs using
    # postgresql:// or postgresql+psycopg2:// are normalized to the installed
    # psycopg v3 driver.
    # The DATABASE_URL *value* is only read when main() runs, from
    # --database-url first and the DATABASE_URL env var second, so neither the
    # module import nor pytest collection touches a database.

`down.sh --clean-db` runs this inside a one-off container, dry-run first, then
asks for confirmation. Prefer it over `--wipe-db`, which drops the whole
Postgres volume (baseline included) and forces a re-seed from `.env`.

On Postgres each id sequence is pointed at the rows that remain, so the first
row created after a clean gets the number a fresh database would hand out.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, func, not_, select, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.config import _normalize_database_url, get_settings  # noqa: E402
from app.database import DEFAULT_PROJECT_NAME, _build_engine  # noqa: E402
from app.models import (  # noqa: E402
    ROLE_ADMIN,
    Base,
    Organization,
    Project,
    User,
    WorkflowStatus,
    WorkflowTransition,
)

log = logging.getLogger("clean_db")

#: Workflow statuses with this scope are seeded at boot, i.e. baseline data.
GLOBAL_SCOPE = "global"

#: Tables that hold nothing but baseline rows, so none of their rows are removed.
_KEEP_WHOLE_TABLES = frozenset({WorkflowTransition.__table__.name})

#: Never print or log a URL containing credentials.
_REDACTED_URL = "<redacted>"


def mask_database_url(url: str) -> str:
    """Return a log-safe rendering of a database URL (password removed)."""
    if not url:
        return _REDACTED_URL
    scheme, sep, rest = url.partition("://")
    if not sep:
        return _REDACTED_URL
    login, at_sep, host = rest.rpartition("@")
    if not at_sep:
        return f"{scheme}://{rest}"
    if ":" in login:
        return f"{scheme}://{login.split(':', 1)[0]}:***@{host}"
    return f"{scheme}://{login}@{host}"


def normalize_cleanup_database_url(url: str) -> str:
    """Normalize an operator-supplied URL the same way the app normalizes env.

    ``postgresql://`` and ``postgresql+psycopg2://`` are rewritten to the
    installed psycopg v3 dialect. Rejects empty values and URLs that do not
    point at a Postgres server or SQLite database.
    """
    if not url or not str(url).strip():
        raise ValueError("A database URL is required (--database-url or DATABASE_URL).")
    normalized = _normalize_database_url(str(url).strip())
    scheme = normalized.split("://", 1)[0].split("+", 1)[0].lower()
    if scheme not in ("postgres", "postgresql", "sqlite"):
        raise ValueError(
            f"Unsupported database URL scheme {scheme!r}; expected postgresql:// or sqlite://."
        )
    return normalized


def cleanup_session_factory(database_url: str | None = None) -> sessionmaker:
    """Session factory bound to the cleanup target.

    ``database_url`` wins; otherwise the app configuration (``DATABASE_URL`` env
    or the local SQLite fallback) is used. A new engine is always built for the
    resolved URL — the app-wide ``SessionLocal`` must not be reused because it
    is bound to whatever ``DATABASE_URL`` the environment had at import time.
    No secrets are printed here — the masked host is logged by ``main``.
    """
    raw = str(database_url).strip() if database_url else None
    source = "--database-url" if raw else "app configuration"
    if not raw:
        settings = get_settings()
        raw = settings.DATABASE_URL
    normalized = normalize_cleanup_database_url(raw or "")
    log.info("Cleanup target: %s (%s)", mask_database_url(normalized), source)
    return sessionmaker(
        bind=_build_engine(normalized), autoflush=False, autocommit=False, future=True
    )


def _count(db: Session, table, where=None) -> int:
    stmt = select(func.count()).select_from(table)
    if where is not None:
        stmt = stmt.where(where)
    return int(db.scalar(stmt) or 0)


def resolve_baseline_user(db: Session) -> User | None:
    """The account to keep: the `.env` bootstrap email, else the oldest admin.

    Falling back to the oldest admin (then the oldest account of any role) means
    a renamed bootstrap account can never lock the operator out. Returns None
    only when the table is empty — then the next boot re-seeds it from `.env`.
    """
    email = (get_settings().BOOTSTRAP_ADMIN_EMAIL or "").strip()
    if email:
        row = db.scalar(
            select(User).where(func.lower(User.email) == email.lower()).order_by(User.id)
        )
        if row is not None:
            return row
    row = db.scalar(select(User).where(User.role == ROLE_ADMIN).order_by(User.id))
    if row is not None:
        return row
    return db.scalar(select(User).order_by(User.id))


def resolve_baseline_project(db: Session, org_id: int | None = None) -> Project | None:
    """The project to keep: the seeded default project, else the oldest one (of the kept
    account's organization when there is one)."""
    scope = [] if org_id is None else [Project.org_id == org_id]
    row = db.scalar(
        select(Project)
        .where(func.lower(Project.name) == DEFAULT_PROJECT_NAME.lower(), *scope)
        .order_by(Project.id)
    )
    if row is not None:
        return row
    return db.scalar(select(Project).where(*scope).order_by(Project.id))


def _keep_condition(
    table, kept_user_id: int | None, kept_project_id: int | None, kept_org_id: int | None,
):
    """Rows to keep for `table`, or None when every row goes."""
    if table.name == Organization.__table__.name:
        return None if kept_org_id is None else table.c.id == kept_org_id
    if table.name == User.__table__.name:
        return None if kept_user_id is None else table.c.id == kept_user_id
    if table.name == Project.__table__.name:
        return None if kept_project_id is None else table.c.id == kept_project_id
    if table.name == WorkflowStatus.__table__.name:
        return table.c.scope_key == GLOBAL_SCOPE
    return None


def _restart_sequences(db: Session) -> int:
    """Point Postgres id sequences at the surviving rows (best effort).

    A fresh database hands out id 1 for its first row; without this, Postgres
    keeps counting from the last id the deleted rows used. SQLite already
    restarts at max(id)+1 once a table is empty, so it needs nothing.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return 0
    restarted = 0
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in _KEEP_WHOLE_TABLES:
            continue
        pks = list(table.primary_key.columns)
        if len(pks) != 1:
            continue
        pk = pks[0].name
        try:
            seq = db.scalar(
                text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": table.name, "c": pk}
            )
            if not seq:
                continue
            with db.begin_nested():
                # Table/column names come from our own metadata, never from input;
                # they are still quoted so a reserved-word name can't break the SQL.
                # Empty table -> COALESCE gives 1 with is_called=false, so the next
                # insert is id 1, exactly like a first launch.
                quote = db.get_bind().dialect.identifier_preparer.quote
                col, tbl = quote(pk), quote(table.name)
                db.execute(text(
                    f"SELECT setval(CAST(:seq AS regclass), "  # nosec B608 - quoted metadata identifiers
                    f"COALESCE((SELECT MAX({col}) FROM {tbl}), 1), "
                    f"EXISTS (SELECT 1 FROM {tbl}))"
                ), {"seq": seq})
            restarted += 1
        except SQLAlchemyError:
            log.warning("Could not restart the id sequence for %s; leaving it as is.", table.name)
    return restarted


def clean_database(db: Session, *, dry_run: bool = False) -> list[tuple[str, int, int]]:
    """Delete every non-baseline row.

    Returns ``(table, deleted, kept)`` per table. Nothing is written when
    ``dry_run`` is true; a real run is one transaction.
    """
    kept_user = resolve_baseline_user(db)
    kept_org_id = kept_user.org_id if kept_user else None
    kept_project = resolve_baseline_project(db, kept_org_id)
    kept_user_id = kept_user.id if kept_user else None
    kept_project_id = kept_project.id if kept_project else None

    report: list[tuple[str, int, int]] = []
    # Reversed dependency order: children before the rows they point at.
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in _KEEP_WHOLE_TABLES:
            report.append((table.name, 0, _count(db, table)))
            continue
        keep = _keep_condition(table, kept_user_id, kept_project_id, kept_org_id)
        total = _count(db, table)
        kept = _count(db, table, keep) if keep is not None else 0
        deleted = total - kept
        if not dry_run and deleted:
            # Remove everything EXCEPT the rows the keep-condition matches.
            db.execute(delete(table) if keep is None else delete(table).where(not_(keep)))
        report.append((table.name, deleted, kept))

    if not dry_run:
        db.commit()
        _restart_sequences(db)
        db.commit()
    return report


def _print_report(report: list[tuple[str, int, int]], *, dry_run: bool) -> None:
    verb = "would be deleted" if dry_run else "deleted"
    print(f"{'table':<28}{verb:>18}{'kept':>8}")
    print("-" * 54)
    for name, deleted, kept in report:
        if deleted or kept:
            print(f"{name:<28}{deleted:>18}{kept:>8}")
    print("-" * 54)
    print(f"total {verb}: {sum(d for _, d, _ in report)}")


def _describe_baseline(db: Session) -> None:
    user = resolve_baseline_user(db)
    project = resolve_baseline_project(db, user.org_id if user else None)
    settings = get_settings()
    print("Baseline kept (what a first launch creates):")
    print(f"  admin   : {user.email if user else '(none - re-seeded from .env on next boot)'}")
    print(f"  project : {project.name if project else '(none - re-seeded on next boot)'}")
    print(
        "  env     : "
        f"BOOTSTRAP_ADMIN_EMAIL={'set' if settings.BOOTSTRAP_ADMIN_EMAIL else 'unset'}, "
        f"BOOTSTRAP_ADMIN_PASSWORD={'set' if settings.BOOTSTRAP_ADMIN_PASSWORD else 'unset'}"
    )
    print("  workflow: global statuses and their default transitions are preserved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset the database to its first-launch baseline."
    )
    parser.add_argument("--yes", action="store_true",
                        help="actually delete (without it this is a dry run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="force a dry run even if --yes is given")
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "SQLAlchemy URL for the database to clean. Useful for Azure "
            "deployments where Docker is unavailable; falls back to DATABASE_URL "
            "from the environment (or the local SQLite fallback). The password "
            "is never printed."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    dry_run = args.dry_run or not args.yes
    sessions = cleanup_session_factory(args.database_url)
    with sessions() as db:
        print("")
        _describe_baseline(db)
        print("")
        report = clean_database(db, dry_run=dry_run)
        _print_report(report, dry_run=dry_run)
        print("")
        if dry_run:
            print("Dry run - nothing was deleted. Re-run with --yes to apply.")
        else:
            print("Done. Every session was cleared, so all users sign in again.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
