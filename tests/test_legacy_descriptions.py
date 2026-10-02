"""3.x rich-text descriptions are preserved before 4.0 handles them as plain text.

3.x stored item descriptions as sanitized HTML; 4.0 stores and returns plain
text, and saving an item replaces the stored HTML. init_db copies every HTML
description into description_legacy_html first, so the formatting stays
recoverable whichever description format the product keeps.
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


def _row(bug_id):
    from app.database import SessionLocal
    with SessionLocal() as db:
        return db.execute(text("SELECT description, description_legacy_html FROM bugs WHERE id = :i"),
                          {"i": bug_id}).one()


def _make_legacy(bug_id):
    """Turn a row into what a 3.x install left behind: HTML, never copied."""
    from app.database import SessionLocal
    with SessionLocal() as db:
        db.execute(text("UPDATE bugs SET description = :d, description_legacy_html = NULL WHERE id = :i"),
                   {"d": LEGACY_HTML, "i": bug_id})
        db.commit()


def test_boot_copies_legacy_html_and_saves_never_lose_it(admin_client):
    from app.database import init_db

    bug = _bug(admin_client, _project(admin_client, "Legacy desc"), "placeholder")
    _make_legacy(bug["id"])

    init_db()  # the first 4.0 boot
    description, legacy = _row(bug["id"])
    assert description == LEGACY_HTML, "the migration itself must not rewrite the description"
    assert legacy == LEGACY_HTML

    # The API serves plain text, and a save stores plain text...
    shown = admin_client.get(f"/api/bugs/{bug['id']}").json()
    assert "<" not in shown["description"] and "Reports" in shown["description"]
    r = admin_client.put(f"/api/bugs/{bug['id']}",
                         json={"description": shown["description"] + "\nEdited in 4.0"})
    assert r.status_code == 200, r.text
    description, legacy = _row(bug["id"])
    assert "<" not in description and description.endswith("Edited in 4.0")
    # ...but the original formatting is still recoverable.
    assert legacy == LEGACY_HTML

    init_db()  # later boots change nothing
    assert _row(bug["id"])[1] == LEGACY_HTML


def test_plain_text_rows_are_never_copied(admin_client):
    from app.database import init_db

    pid = _project(admin_client, "Plain desc")
    plain = _bug(admin_client, pid, "Line one\n\nCompare a < b and c > d")
    empty = _bug(admin_client, pid, "")
    init_db()
    assert _row(plain["id"])[1] is None
    assert _row(empty["id"])[1] is None
