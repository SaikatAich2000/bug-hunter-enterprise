"""Boot-time alignment of pre-existing agile data with the Jira hierarchy
(app/agile/upgrade.py). Rows are written with Core inserts, bypassing the
flush-time rules, exactly as data from before the upgrade looks. The retired
Collection and Feature tables are no longer part of the schema, so the tests
create them (and the two bugs columns that pointed at them) the way an old
database has them."""
from __future__ import annotations

from itertools import count

from sqlalchemy import Column, Integer, MetaData, String, Table, inspect, select, text

from tests.conftest import default_org_id

_keys = count(1)


def _project_key():
    return f"LEG{next(_keys)}"


_retired = MetaData()
COLLECTIONS = Table("collections", _retired,
                    Column("id", Integer, primary_key=True), Column("project_id", Integer, nullable=False),
                    Column("name", String(120), nullable=False))
FEATURES = Table("features", _retired,
                 Column("id", Integer, primary_key=True), Column("project_id", Integer, nullable=False),
                 Column("epic_id", Integer, nullable=False), Column("title", String(200), nullable=False))
COLLECTION_ITEMS = Table("collection_items", _retired,
                         Column("collection_id", Integer, primary_key=True),
                         Column("work_item_id", Integer, primary_key=True))


def create_retired_schema(conn):
    _retired.create_all(conn)
    columns = {c["name"] for c in inspect(conn).get_columns("bugs")}
    for column in ("collection_id", "feature_id"):
        if column not in columns:
            conn.execute(text(f"ALTER TABLE bugs ADD COLUMN {column} INTEGER"))


def _link(conn, column, item_id, group_id):
    conn.execute(text(f"UPDATE bugs SET {column} = :g WHERE id = :i"), {"g": group_id, "i": item_id})


def _retired_link(conn, column, item_id):
    return conn.execute(text(f"SELECT {column} FROM bugs WHERE id = :i"), {"i": item_id}).scalar()


def _insert(conn, table, **values):
    return conn.execute(table.insert().values(**values).returning(table.c.id)).scalar_one()


def _bug(conn, project_id, title, item_type, **extra):
    from app.models import Bug

    values = {"project_id": project_id, "title": title, "item_type": item_type, "status": "New",
              "priority": "Medium", "environment": "DEV", "description": "", "version": 1,
              "time_spent_minutes": 0, "acceptance_criteria": "", "flagged": False,
              "ready_for_sprint": False, "blocked": False, "blocked_reason": "", "mandatory": False}
    values.update(extra)
    return _insert(conn, Bug.__table__, **values)


def _legacy_world(conn):
    from app.models import Board, EpicDetail, Project, Sprint

    create_retired_schema(conn)
    project = _insert(conn, Project.__table__, org_id=default_org_id(), key=_project_key(), name="Legacy", description="", color="#c9764f",
                      agile_enabled=True, agile_feature_flags={})
    board = _insert(conn, Board.__table__, project_id=project, name="B", name_normalized="b",
                    is_default=True, board_type="scrum", filter_json={}, estimation_mode="story_points",
                    swimlane_mode="none", card_fields_json=[], wip_enforcement="off",
                    card_color_scheme="none", timezone="UTC", working_weekdays=[1, 2, 3, 4, 5],
                    working_hours_per_day=8, version=1)
    sprint = _insert(conn, Sprint.__table__, board_id=board, project_id=project, name="S1",
                     name_normalized="s1", goal="", state="active", sequence_number=1, position=0, version=1)
    epic = _bug(conn, project, "Old epic", "Epic", sprint_id=sprint)
    conn.execute(EpicDetail.__table__.insert().values(
        epic_id=epic, color="", sprint_id=sprint, health="unknown", summary_note="", archived=False, version=1,
    ))
    collection = _insert(conn, COLLECTIONS, project_id=project, name="Q4 Goals")
    from app.models import Bug

    _link(conn, "collection_id", epic, collection)
    feature = _insert(conn, FEATURES, project_id=project, epic_id=epic, title="Checkout")
    story = _bug(conn, project, "Story under feature", "Story", sprint_id=sprint,
                 rank_scope=f"backlog:{project}", rank="i")
    _link(conn, "feature_id", story, feature)
    stray = _bug(conn, project, "Collected bug", "Bug")
    conn.execute(COLLECTION_ITEMS.insert().values(collection_id=collection, work_item_id=stray))
    sub = _bug(conn, project, "Sub under story", "Sub-task", parent_id=story, display_id=None)
    orphan = _bug(conn, project, "Orphan sub-task", "Sub-task")
    under_epic = _bug(conn, project, "Sub-task under an epic", "Sub-task", parent_id=epic)
    standard_with_parent = _bug(conn, project, "Task with a parent", "Task", parent_id=story)
    conn.execute(Bug.__table__.update().where(Bug.__table__.c.id == sub).values(display_id=f"TASK-{sub}"))
    return {"project": project, "sprint": sprint, "epic": epic, "story": story, "stray": stray,
            "sub": sub, "orphan": orphan, "under_epic": under_epic,
            "standard_with_parent": standard_with_parent, "collection": collection, "feature": feature}


def assert_aligned(conn, w):
    """The state every legacy row must be in after the upgrade. Shared with
    the PostgreSQL run in test_postgres_migration.py."""
    from app.models import Bug, EpicDetail, Label, WorkItemLabel

    bugs = Bug.__table__
    rows = {r.id: r for r in conn.execute(select(bugs)).all()}
    # Epics leave sprints, on both the item and its detail row.
    assert rows[w["epic"]].sprint_id is None
    assert conn.execute(select(EpicDetail.__table__.c.sprint_id)).scalar() is None
    # A Story under a Feature takes the Feature's Epic.
    assert rows[w["story"]].epic_id == w["epic"]
    assert _retired_link(conn, "feature_id", w["story"]) is None
    # Sub-tasks follow their parent and get their own id prefix.
    assert (rows[w["sub"]].sprint_id, rows[w["sub"]].epic_id) == (w["sprint"], w["epic"])
    assert rows[w["sub"]].display_id == f"SUB-{w['sub']}"
    # Sub-tasks without a valid (standard) parent become Tasks.
    for key in ("orphan", "under_epic"):
        assert rows[w[key]].item_type == "Task" and rows[w[key]].parent_id is None
    # Only Sub-tasks have parents.
    assert rows[w["standard_with_parent"]].parent_id is None
    # Every standard issue is ranked in the list it sits in.
    assert rows[w["story"]].rank_scope == f"sprint:{w['sprint']}"
    assert rows[w["stray"]].rank_scope == f"backlog:{w['project']}" and rows[w["stray"]].rank
    # Collections and Features became labels on what they grouped.
    labels = {r.display_name: r for r in conn.execute(select(Label.__table__)).all()}
    assert set(labels) == {"Q4 Goals", "Checkout"}
    links = {(r.work_item_id, r.label_id) for r in conn.execute(select(WorkItemLabel.__table__)).all()}
    assert (w["epic"], labels["Q4 Goals"].id) in links
    assert (w["stray"], labels["Q4 Goals"].id) in links
    assert (w["story"], labels["Checkout"].id) in links
    assert labels["Q4 Goals"].usage_count == 2 and labels["Checkout"].usage_count == 1
    assert conn.execute(select(COLLECTION_ITEMS)).all() == []
    assert _retired_link(conn, "collection_id", w["epic"]) is None


def test_upgrade_aligns_legacy_agile_data(db_session):
    from app.agile.upgrade import align_agile_hierarchy
    from app.database import engine

    with engine.begin() as conn:
        w = _legacy_world(conn)
    for _ in range(2):  # the second boot must change nothing
        with engine.begin() as conn:
            align_agile_hierarchy(conn)
    with engine.connect() as conn:
        assert_aligned(conn, w)


def test_upgrade_keeps_a_removed_label_removed(db_session):
    """Once converted, the retired link is cleared, so a label an owner removes
    later is not put back on the next boot."""
    from app.agile.upgrade import align_agile_hierarchy
    from app.database import engine
    from app.models import WorkItemLabel

    with engine.begin() as conn:
        _legacy_world(conn)
    with engine.begin() as conn:
        align_agile_hierarchy(conn)
    with engine.begin() as conn:
        conn.execute(WorkItemLabel.__table__.delete())
    with engine.begin() as conn:
        align_agile_hierarchy(conn)
    with engine.connect() as conn:
        assert conn.execute(select(WorkItemLabel.__table__)).all() == []


def _snapshot(conn):
    from app.models import Bug, Label, WorkItemLabel

    return [
        sorted(tuple(r) for r in conn.execute(select(table)).all())
        for table in (Bug.__table__, Label.__table__, WorkItemLabel.__table__)
    ]


def test_second_boot_changes_no_row(db_session):
    from app.agile.upgrade import align_agile_hierarchy
    from app.database import engine

    with engine.begin() as conn:
        _legacy_world(conn)
    with engine.begin() as conn:
        align_agile_hierarchy(conn)
    with engine.connect() as conn:
        first = _snapshot(conn)
    with engine.begin() as conn:
        align_agile_hierarchy(conn)
    with engine.connect() as conn:
        assert _snapshot(conn) == first


def test_upgrade_never_links_across_projects(db_session):
    """A collection holding another project's item labels it in that item's
    own project, and a Feature whose Epic lives in another project does not
    give its Story that Epic."""
    from app.agile.upgrade import align_agile_hierarchy
    from app.database import engine
    from app.models import Bug, Label, Project, WorkItemLabel

    with engine.begin() as conn:
        w = _legacy_world(conn)
        other = _insert(conn, Project.__table__, org_id=default_org_id(), key=_project_key(), name="Other", description="", color="#c9764f",
                        agile_enabled=True, agile_feature_flags={})
        foreign = _bug(conn, other, "Other project's bug", "Bug")
        conn.execute(COLLECTION_ITEMS.insert().values(collection_id=w["collection"], work_item_id=foreign))
        feature = _insert(conn, FEATURES, project_id=other, epic_id=w["epic"], title="Cross")
        story = _bug(conn, other, "Story under a cross-project feature", "Story")
        _link(conn, "feature_id", story, feature)
    with engine.begin() as conn:
        align_agile_hierarchy(conn)
    with engine.connect() as conn:
        labels = {(r.project_id, r.display_name): r.id for r in conn.execute(select(Label.__table__)).all()}
        links = {(r.work_item_id, r.label_id) for r in conn.execute(select(WorkItemLabel.__table__)).all()}
        assert (foreign, labels[(other, "Q4 Goals")]) in links
        assert (foreign, labels[(w["project"], "Q4 Goals")]) not in links
        assert conn.execute(select(Bug.__table__.c.epic_id).where(Bug.__table__.c.id == story)).scalar() is None
        assert (story, labels[(other, "Cross")]) in links


def test_upgrade_respaces_lists_damaged_by_the_old_rank_algorithm(db_session):
    """The earlier algorithm left tokens like "zzz...zi" (62 characters) after a
    few hundred appends, and duplicates. Boot must not fail on them: the list
    is re-spaced in its current order, and new issues append after it."""
    from app.agile.ranking import MAX_LENGTH
    from app.agile.upgrade import align_agile_hierarchy
    from app.database import SessionLocal, engine
    from app.models import Bug, Project

    with engine.begin() as conn:
        project = _insert(conn, Project.__table__, org_id=default_org_id(), key=_project_key(), name="Old ranks", description="", color="#c9764f",
                          agile_enabled=True, agile_feature_flags={})
        scope = f"backlog:{project}"
        long_tail = "z" * 61 + "i"
        ranks = ["a", "m", "m", "z" * 30, long_tail]
        ids = [_bug(conn, project, f"Issue {n}", "Story", rank_scope=scope, rank=rank) for n, rank in enumerate(ranks)]
    for _ in range(2):
        with engine.begin() as conn:
            align_agile_hierarchy(conn)
    with engine.connect() as conn:
        rows = conn.execute(select(Bug.__table__.c.id, Bug.__table__.c.rank)
                            .where(Bug.__table__.c.rank_scope == scope).order_by(Bug.__table__.c.rank)).all()
    assert [r.id for r in rows] == ids  # order kept (ties by id)
    assert len({r.rank for r in rows}) == len(rows)
    assert all(len(r.rank) <= MAX_LENGTH for r in rows)
    # A new issue in that project lands at the bottom.
    with SessionLocal() as db:
        item = Bug(project_id=project, title="New issue", item_type="Story", status="New", priority="Medium",
                   environment="DEV", description="")
        db.add(item)
        db.commit()
        new_id, new_rank = item.id, item.rank
    assert new_rank > rows[-1].rank, (new_rank, rows[-1].rank)
    assert new_id not in ids


def test_appending_after_a_damaged_list_respaces_it_instead_of_failing(db_session):
    """Defence in depth for data written after the upgrade by something else:
    the flush-time append re-spaces a list that has no room left."""
    from sqlalchemy import update

    from app.database import SessionLocal, engine
    from app.models import Bug, Project

    with engine.begin() as conn:
        project = _insert(conn, Project.__table__, org_id=default_org_id(), key=_project_key(), name="No room", description="", color="#c9764f",
                          agile_enabled=True, agile_feature_flags={})
    with SessionLocal() as db:
        first = Bug(project_id=project, title="First", item_type="Story", status="New", priority="Medium",
                    environment="DEV", description="")
        db.add(first)
        db.commit()
        first_id = first.id
    with engine.begin() as conn:
        conn.execute(update(Bug.__table__).where(Bug.__table__.c.id == first_id).values(rank="z" * 61 + "i"))
    with SessionLocal() as db:
        second = Bug(project_id=project, title="Second", item_type="Story", status="New", priority="Medium",
                     environment="DEV", description="")
        db.add(second)
        db.commit()
        first_rank = db.get(Bug, first_id).rank
        assert first_rank < second.rank
        assert len(first_rank) <= 32 and len(second.rank) <= 32


def test_upgrade_runs_on_a_database_without_the_retired_tables(db_session):
    """A fresh install has no collections/features tables and no bugs columns
    pointing at them; the upgrade must simply skip that step."""
    from app.agile.upgrade import align_agile_hierarchy
    from app.database import engine
    from app.models import Project

    with engine.begin() as conn:
        assert "collections" not in inspect(conn).get_table_names()
        project = _insert(conn, Project.__table__, org_id=default_org_id(), key=_project_key(), name="Fresh", description="", color="#c9764f",
                          agile_enabled=True, agile_feature_flags={})
        _bug(conn, project, "Plain story", "Story")
    with engine.begin() as conn:
        align_agile_hierarchy(conn)
    with engine.connect() as conn:
        assert "collections" not in inspect(conn).get_table_names()
