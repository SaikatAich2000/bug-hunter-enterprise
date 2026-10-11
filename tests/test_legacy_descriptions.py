"""Descriptions, goals and notes are plain text, stored exactly as typed.

3.x stored item descriptions as sanitized HTML. On upgrade, a one-time data migration copies
each HTML description into description_legacy_html (so the formatting stays recoverable) and
stores its plain-text form, which is what the UI shows. It also decodes sprint goals, blocked
reasons and epic notes that older versions stored HTML-escaped. It runs once: text typed later
(``</div>``, ``&amp;``) is never touched again.
"""
from sqlalchemy import text

LEGACY_HTML = "<p>Steps:</p><ol><li>Open <b>Reports</b></li><li>Click export</li></ol>"


def _project(client, name):
    r = client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bug(client, pid, description):
    r = client.post("/api/bugs", json={"title": "Legacy row", "project_id": pid, "item_type": "Bug",
                                        "description": description})
    assert r.status_code == 201, r.text
    return r.json()


def _sql(statement, **params):
    from app.database import SessionLocal
    with SessionLocal() as db:
        result = db.execute(text(statement), params)
        rows = result.all() if result.returns_rows else None
        db.commit()
        return rows


def _row(bug_id):
    return _sql("SELECT description, description_legacy_html FROM bugs WHERE id = :i", i=bug_id)[0]


def _as_before_upgrade():
    """The state a pre-upgrade database is in: the one-time migration has not run."""
    _sql("DELETE FROM app_migrations WHERE name = 'plain_text_storage_v1'")


def test_upgrade_keeps_legacy_html_and_stores_its_plain_text(admin_client):
    from app.database import init_db

    bug = _bug(admin_client, _project(admin_client, "Legacy desc"), "placeholder")
    _sql("UPDATE bugs SET description = :d, description_legacy_html = NULL WHERE id = :i",
         d=LEGACY_HTML, i=bug["id"])
    _as_before_upgrade()

    init_db()
    description, legacy = _row(bug["id"])
    assert legacy == LEGACY_HTML
    assert description == "Steps:\nOpen Reports\nClick export"
    assert admin_client.get(f"/api/bugs/{bug['id']}").json()["description"] == description

    init_db()  # later boots change nothing
    assert _row(bug["id"]) == (description, LEGACY_HTML)


def test_text_typed_after_the_upgrade_is_never_rewritten(admin_client):
    from app.database import init_db

    pid = _project(admin_client, "Plain desc")
    typed = "Wrap it in </div> and <b>bold</b>\n\nCompare a < b && c > d &amp; more"
    bug = _bug(admin_client, pid, typed)
    assert bug["description"] == typed
    init_db()
    assert _row(bug["id"]) == (typed, None)
    assert admin_client.get(f"/api/bugs/{bug['id']}").json()["description"] == typed


def test_escaped_goals_and_notes_are_decoded_once(admin_client):
    from app.database import init_db

    pid = _project(admin_client, "Escaped")
    assert admin_client.post(f"/api/agile/projects/{pid}/enable", json={}).status_code == 200
    board = admin_client.get(f"/api/agile/projects/{pid}/settings").json()["board_id"]
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board}", json={
        "name": "Escapes", "goal": "Ship a > b & c",
    }).json()
    assert sprint["goal"] == "Ship a > b & c"  # stored as typed now
    _sql("UPDATE sprints SET goal = :g WHERE id = :i", g="Ship a &gt; b &amp; c", i=sprint["id"])
    _as_before_upgrade()
    init_db()
    assert _sql("SELECT goal FROM sprints WHERE id = :i", i=sprint["id"])[0][0] == "Ship a > b & c"
