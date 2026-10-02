"""Boot-time alignment of existing agile data with the Jira hierarchy.

Runs under init_db's migration lock, inside a SAVEPOINT, on every boot.
Every step is idempotent: once the data is aligned nothing matches again.

1. Sub-tasks get their own display id prefix (SUB-) so they are never
   mistaken for Tasks (TASK-). Ids frozen by a type conversion are kept.
2. Standard issues parented to something (only Sub-tasks have parents) and
   links to "epics" that are not Epics are cleared.
3. Sub-tasks without a valid parent become Tasks (Jira has no orphan
   Sub-tasks); the others take their parent's epic and sprint.
4. Epics leave sprints (Epics are not planned into sprints).
5. Every standard issue is ranked in the list it sits in (sprint or backlog),
   Epics in their project's Epic list; Sub-tasks carry no rank.
6. Databases from before the Jira rebuild may still hold the retired
   Collection and Feature tables. Those levels become labels on the items they
   grouped (a Story also takes its Feature's Epic), then the old links are
   cleared so a label removed later is not re-added. The tables are no longer
   part of the schema: they are read only when present and left in place.
"""
from __future__ import annotations

import logging

from sqlalchemy import and_, bindparam, exists, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.agile.itemtypes import STANDARD_TYPES

logger = logging.getLogger("bug_hunter.agile.upgrade")

_STATEMENTS: list[tuple[str, str]] = [
    ("parent links on standard issues",
     "UPDATE bugs SET parent_id = NULL WHERE item_type IN :standard AND parent_id IS NOT NULL"),
    ("parent links on epics",
     "UPDATE bugs SET parent_id = NULL, epic_id = NULL "
     "WHERE item_type = 'Epic' AND (parent_id IS NOT NULL OR epic_id IS NOT NULL)"),
    ("orphan sub-tasks",
     "UPDATE bugs SET item_type = 'Task', parent_id = NULL "
     "WHERE item_type = 'Sub-task' AND (parent_id IS NULL OR parent_id NOT IN ("
     "SELECT p.id FROM bugs p WHERE p.item_type IN :standard AND p.project_id = bugs.project_id))"),
    ("epic links to non-epics",
     "UPDATE bugs SET epic_id = NULL WHERE item_type IN :standard AND epic_id IS NOT NULL "
     "AND epic_id NOT IN (SELECT e.id FROM bugs e WHERE e.item_type = 'Epic' AND e.project_id = bugs.project_id)"),
    ("sub-task display ids",
     "UPDATE bugs SET display_id = 'SUB-' || id "
     "WHERE item_type = 'Sub-task' AND display_id = 'TASK-' || id"),
    ("sub-tasks following their parent",
     "UPDATE bugs SET "
     "sprint_id = (SELECT p.sprint_id FROM bugs p WHERE p.id = bugs.parent_id), "
     "epic_id = (SELECT p.epic_id FROM bugs p WHERE p.id = bugs.parent_id) "
     "WHERE item_type = 'Sub-task' AND parent_id IS NOT NULL AND ("
     "COALESCE(sprint_id, -1) <> COALESCE((SELECT p.sprint_id FROM bugs p WHERE p.id = bugs.parent_id), -1) "
     "OR COALESCE(epic_id, -1) <> COALESCE((SELECT p.epic_id FROM bugs p WHERE p.id = bugs.parent_id), -1))"),
    ("sub-task ranks",
     "UPDATE bugs SET rank = NULL, rank_scope = NULL "
     "WHERE item_type = 'Sub-task' AND (rank IS NOT NULL OR rank_scope IS NOT NULL)"),
    ("epics in sprints",
     "UPDATE bugs SET sprint_id = NULL WHERE item_type = 'Epic' AND sprint_id IS NOT NULL"),
    ("epic sprint links",
     "UPDATE epic_details SET sprint_id = NULL WHERE sprint_id IS NOT NULL"),
    ("retired workflow statuses",
     "UPDATE workflow_statuses SET is_active = FALSE "
     "WHERE work_item_type IN ('Collection', 'Feature') AND is_active = TRUE"),
]

_STORY_EPICS_FROM_FEATURES = (
    "story epics from features",
    "UPDATE bugs SET epic_id = (SELECT f.epic_id FROM features f WHERE f.id = bugs.feature_id) "
    "WHERE feature_id IS NOT NULL AND epic_id IS NULL AND item_type IN :standard "
    "AND (SELECT f.epic_id FROM features f WHERE f.id = bugs.feature_id) IN "
    "(SELECT e.id FROM bugs e WHERE e.item_type = 'Epic' AND e.project_id = bugs.project_id)",
)


def _statement(sql: str):
    """The SQL as a bound statement; ``:standard`` expands to the standard
    issue types, so no value is ever formatted into the SQL text."""
    statement = text(sql)
    if ":standard" in sql:
        statement = statement.bindparams(bindparam("standard", value=list(STANDARD_TYPES), expanding=True))
    return statement


def _normalize_rank_scopes(conn) -> int:
    """Rank every standard issue in the list it sits in (its sprint or its
    project's backlog) and every Epic in its project's Epic list.

    A list is re-spaced (evenly spaced short tokens, current order kept, its
    misplaced or unranked issues appended in their old order) when anything
    in it is off: an issue ranked in another list or not at all, two issues
    with the same rank, or a token the current generator would not produce
    (the earlier algorithm left tokens of up to 62 characters that leave no
    room after them). Healthy lists are not touched, so a second boot changes
    nothing."""
    from app.agile.ranking import is_valid, spaced_ranks
    from app.models import Bug

    bugs = Bug.__table__
    rows = conn.execute(
        select(bugs.c.id, bugs.c.item_type, bugs.c.project_id, bugs.c.sprint_id, bugs.c.rank_scope, bugs.c.rank)
        .where(bugs.c.item_type.in_([*STANDARD_TYPES, "Epic"]))
    ).all()
    placed: dict[str, list] = {}
    misplaced: dict[str, list] = {}
    for row in rows:
        if row.item_type == "Epic":
            scope = f"epics:{row.project_id}"
        elif row.sprint_id is not None:
            scope = f"sprint:{row.sprint_id}"
        else:
            scope = f"backlog:{row.project_id}"
        bucket = placed if row.rank_scope == scope and row.rank is not None else misplaced
        bucket.setdefault(scope, []).append(row)
    updates = []
    for scope in set(placed) | set(misplaced):
        ok = sorted(placed.get(scope, []), key=lambda r: (r.rank, r.id))
        bad = sorted(misplaced.get(scope, []), key=lambda r: (r.rank is None, r.rank or "", r.id))
        ranks = [r.rank for r in ok]
        if not bad and len(set(ranks)) == len(ranks) and all(is_valid(r) for r in ranks):
            continue
        for row, token in zip(ok + bad, spaced_ranks(len(ok) + len(bad))):
            if (row.rank_scope, row.rank) != (scope, token):
                updates.append({"bid": row.id, "scope": scope, "token": token})
    if updates:
        conn.execute(
            bugs.update().where(bugs.c.id == bindparam("bid")).values(
                rank_scope=bindparam("scope"), rank=bindparam("token"),
            ),
            updates,
        )
    return len(updates)


def _label_id(conn, project_id: int, name: str) -> int:
    from app.agile.taxonomy import normalize_name
    from app.models import Label

    labels = Label.__table__
    norm = normalize_name(name)[:120]
    found = conn.execute(
        select(labels.c.id).where(labels.c.project_id == project_id, labels.c.normalized_name == norm)
    ).scalar()
    if found is not None:
        return found
    return conn.execute(
        labels.insert().values(
            project_id=project_id, normalized_name=norm, display_name=name.strip()[:120],
            color="", usage_count=0, version=1,
            created_at=func.current_timestamp(), updated_at=func.current_timestamp(),
        ).returning(labels.c.id)
    ).scalar_one()


def _attach(conn, label_id: int, item_ids: set[int]) -> int:
    from app.models import WorkItemLabel

    links = WorkItemLabel.__table__
    added = 0
    for item_id in sorted(item_ids):
        present = conn.execute(select(exists().where(and_(
            links.c.work_item_id == item_id, links.c.label_id == label_id,
        )))).scalar()
        if not present:
            conn.execute(links.insert().values(work_item_id=item_id, label_id=label_id))
            added += 1
    return added


def _retire_levels_to_labels(conn, tables: set[str], bug_columns: set[str]) -> int:
    from app.models import Bug, Label, WorkItemLabel

    bugs = Bug.__table__
    converted = 0
    touched_labels: set[int] = set()

    def label_items(name: str, item_ids: set[int]) -> int:
        # Labels are per project: each grouped item gets the label in its own
        # project (a collection could hold items from several projects).
        attached = 0
        by_project: dict[int, set[int]] = {}
        for row in conn.execute(select(bugs.c.id, bugs.c.project_id).where(bugs.c.id.in_(item_ids))).all():
            by_project.setdefault(row.project_id, set()).add(row.id)
        for project_id, ids in by_project.items():
            label_id = _label_id(conn, project_id, name)
            attached += _attach(conn, label_id, ids)
            touched_labels.add(label_id)
        return attached

    def ids_of(sql: str, group_id: int) -> set[int]:
        return set(conn.execute(text(sql), {"gid": group_id}).scalars())

    if "collections" in tables:
        for gid, name in conn.execute(text("SELECT id, name FROM collections")).all():
            item_ids: set[int] = set()
            if "collection_id" in bug_columns:
                item_ids |= ids_of("SELECT id FROM bugs WHERE collection_id = :gid", gid)
            if "collection_items" in tables:
                item_ids |= ids_of("SELECT work_item_id FROM collection_items WHERE collection_id = :gid", gid)
            if item_ids:
                converted += label_items(name, item_ids)
    if "features" in tables and "feature_id" in bug_columns:
        for gid, title in conn.execute(text("SELECT id, title FROM features")).all():
            item_ids = ids_of("SELECT id FROM bugs WHERE feature_id = :gid", gid)
            if item_ids:
                converted += label_items(title, item_ids)
    if "collection_id" in bug_columns:
        conn.execute(text("UPDATE bugs SET collection_id = NULL WHERE collection_id IS NOT NULL"))
    if "feature_id" in bug_columns:
        conn.execute(text("UPDATE bugs SET feature_id = NULL WHERE feature_id IS NOT NULL"))
    if "collection_items" in tables:
        conn.execute(text("DELETE FROM collection_items"))
    labels, links = Label.__table__, WorkItemLabel.__table__
    for label_id in touched_labels:
        count = conn.execute(select(func.count()).select_from(links).where(links.c.label_id == label_id)).scalar()
        conn.execute(labels.update().where(labels.c.id == bindparam("lid")).values(usage_count=count),
                     {"lid": label_id})
    return converted


def align_agile_hierarchy(conn) -> None:
    try:
        with conn.begin_nested():
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            bug_columns = {column["name"] for column in inspector.get_columns("bugs")}
            statements = list(_STATEMENTS)
            if "features" in tables and "feature_id" in bug_columns:
                statements.insert(4, _STORY_EPICS_FROM_FEATURES)
            for label, sql in statements:
                result = conn.execute(_statement(sql))
                if result.rowcount:
                    logger.info("Agile upgrade: %s: %d row(s) aligned", label, result.rowcount)
            ranked = _normalize_rank_scopes(conn)
            if ranked:
                logger.info("Agile upgrade: %d issue(s) re-ranked into their sprint or backlog", ranked)
            converted = _retire_levels_to_labels(conn, tables, bug_columns)
            if converted:
                logger.info("Agile upgrade: %d Collection/Feature membership(s) converted to labels", converted)
    except SQLAlchemyError:
        logger.exception("Agile hierarchy alignment failed; data was left unchanged")
