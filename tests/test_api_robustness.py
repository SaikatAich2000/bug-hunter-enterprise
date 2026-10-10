"""Regressions found by property-based API fuzzing (Schemathesis).

Integers wider than the database column used to escape as HTTP 500
(SQLite raises OverflowError while binding; PostgreSQL raises DataError).
They are client input errors and must come back as 422.
"""
import pytest

HUGE = 10**25          # past any 64-bit column
NEG_HUGE = -(10**20)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", f"/api/bugs/{HUGE}"),
        ("get", f"/api/agile/boards?project_id={NEG_HUGE}"),
        ("get", f"/api/agile/boards/{NEG_HUGE}/planning"),
        ("get", f"/api/agile/reports/burnup?sprint_id={NEG_HUGE}"),
        ("get", f"/api/agile/reports/workload?sprint_id={HUGE}"),
        ("post", f"/api/agile/components/{HUGE}/archive"),
        ("post", f"/api/agile/projects/{NEG_HUGE}/disable"),
    ],
)
def test_out_of_range_ids_are_rejected_not_crashed(admin_client, method, path):
    r = getattr(admin_client, method)(path)
    assert r.status_code == 422, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail[0]["msg"] == "A value in the request is out of range."


def test_out_of_range_id_in_json_body_is_rejected(admin_client):
    r = admin_client.post("/api/bugs", json={
        "title": "Overflow probe", "project_id": HUGE, "item_type": "Bug",
    })
    assert r.status_code in (400, 404, 422), (r.status_code, r.text)


def test_in_range_missing_id_is_still_404(admin_client):
    assert admin_client.get("/api/bugs/987654").status_code == 404


def test_timestamps_carry_a_utc_offset(admin_client):
    """SQLite drops the offset; the API must still emit RFC 3339 date-times.

    Browsers parse an offset-less ISO string as *local* time, which shifted
    every displayed timestamp by the viewer's UTC offset.
    """
    project = admin_client.post("/api/projects", json={"name": "TZ probe"}).json()
    for stamp in (project["created_at"], admin_client.get("/api/projects").json()[0]["created_at"]):
        assert stamp.endswith(("+00:00", "Z")), stamp


def _agile_project(client, name):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    board = client.post(f"/api/agile/projects/{pid}/enable", json={}).json()["board_id"]
    return pid, board


@pytest.mark.parametrize("kind", ["sprint", "release", "component"])
def test_duplicate_names_are_409_on_create_and_rename(admin_client, kind):
    """Unique (scope, name) indexes must surface as 409, never as a 500."""
    pid, board = _agile_project(admin_client, f"Dup {kind}")
    if kind == "sprint":
        create = lambda n: admin_client.post(f"/api/agile/sprints?board_id={board}", json={"name": n})  # noqa: E731
        update = lambda obj, n: admin_client.put(  # noqa: E731
            f"/api/agile/sprints/{obj['id']}", json={"name": n, "version": obj["version"]})
    else:
        base = {"release": "/api/agile/releases", "component": "/api/agile/components"}[kind]
        create = lambda n: admin_client.post(base, json={"project_id": pid, "name": n})  # noqa: E731
        update = lambda obj, n: admin_client.put(  # noqa: E731
            f"{base}/{obj['id']}", json={"name": n, "version": obj["version"]})
    first = create("Alpha")
    assert first.status_code == 201, first.text
    assert create("alpha ").status_code == 409  # normalised duplicate
    second = create("Beta")
    assert second.status_code == 201, second.text
    renamed = update(second.json(), "ALPHA")
    assert renamed.status_code == 409, (renamed.status_code, renamed.text)


DAILY_BAD_DATES = ["0000-00-00", "2026-02-30", "2026-13-01"]


@pytest.mark.parametrize("bad", DAILY_BAD_DATES)
def test_daily_report_rejects_impossible_dates_cleanly(admin_client, bad):
    pid, board = _agile_project(admin_client, f"Daily {bad}")
    sprint = admin_client.post(f"/api/agile/sprints?board_id={board}", json={"name": "Sprint A"}).json()
    r = admin_client.get(f"/api/agile/reports/daily?sprint_id={sprint['id']}&report_date={bad}")
    assert r.status_code == 422
    assert "strptime" not in r.text and "does not match format" not in r.text


def test_validation_errors_never_echo_the_submitted_value(client):
    secret = "hunter2-NOT-ECHOED"
    res = client.post("/api/auth/login", json={"email": "not-an-email", "password": [secret]})
    assert res.status_code == 422
    assert secret not in res.text
    for err in res.json()["detail"]:
        assert set(err) == {"type", "loc", "msg"}


def test_validation_messages_read_naturally(admin_client):
    pid = admin_client.get("/api/projects").json()[0]["id"]
    res = admin_client.post("/api/bugs", json={"project_id": pid, "title": "x" * 1000})
    assert res.status_code == 422
    msg = res.json()["detail"][0]["msg"]
    assert msg.startswith("Title: ") and "Value error" not in msg
    res = admin_client.post("/api/users", json={
        "name": "Weak", "email": "weak@test.local", "role": "user", "password": "short",
    })
    assert res.status_code == 422
    assert not res.json()["detail"][0]["msg"].startswith("Value error")


def test_bcrypt_test_override_is_ignored_in_production(monkeypatch):
    from app import auth
    from app.config import Settings, get_settings

    monkeypatch.setenv("BCRYPT_TEST_ROUNDS", "4")
    get_settings.cache_clear()
    assert auth._bcrypt_rounds() == 4
    monkeypatch.setattr(Settings, "is_production", property(lambda self: True))
    get_settings.cache_clear()
    assert auth._bcrypt_rounds() == 12
    get_settings.cache_clear()


def test_explicit_nulls_on_required_fields_are_a_422_not_a_500(admin_client):
    pid = admin_client.get("/api/projects").json()[0]["id"]
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Null target"}).json()
    for field in ("project_id", "title", "status", "priority", "environment", "item_type"):
        res = admin_client.put(f"/api/bugs/{bug['id']}", json={field: None})
        assert res.status_code == 422, (field, res.status_code, res.text)
        assert f"{field} cannot be null" in res.json()["detail"][0]["msg"]
    event = admin_client.post("/api/events", json={"name": "Null event"}).json()
    res = admin_client.put(f"/api/events/{event['id']}", json={"name": None})
    assert res.status_code == 422


def test_names_and_titles_are_single_line(admin_client):
    res = admin_client.post("/api/projects", json={"name": "Ops\x00\x1b[31m Board\r\nTwo"})
    assert res.status_code == 201, res.text
    assert res.json()["name"] == "Ops [31m Board Two"
    pid = res.json()["id"]
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Line\x0bbreak\x07title"}).json()
    assert bug["title"] == "Line break title"
    # And the spreadsheet export of such data no longer fails.
    res = admin_client.post("/api/reports/export.xlsx", json={"report_key": "item_detail", "filters": {}})
    assert res.status_code == 200, res.text


def test_plain_text_fields_keep_what_was_typed(admin_client):
    pid = admin_client.get("/api/projects").json()[0]["id"]
    text = "Steps:\n1. Open <Settings>\n2. Check a < b & c > d\x07"
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Verbatim", "description": text})
    assert bug.status_code == 201, bug.text
    assert bug.json()["description"] == text.replace("\x07", "")


def test_email_headers_survive_every_unicode_line_break():
    from app.config import get_settings
    from app.email_service import _build

    nasty = "Release Bcc: victim@example.com\x85\x0bnow\x1c done"
    msg = _build(nasty, ["to@example.com"], "body", get_settings())
    assert msg["Subject"] == "Release Bcc: victim@example.com now done"
    assert "victim" not in (msg["Bcc"] or "")
    msg.as_bytes()  # serializes without a header error


def test_export_survives_control_characters_already_in_the_data(admin_client):
    from sqlalchemy import text

    from app.database import SessionLocal

    pid = admin_client.get("/api/projects").json()[0]["id"]
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Clean title"}).json()
    with SessionLocal() as db:  # rows written before titles were single-line
        db.execute(text("UPDATE bugs SET title = :t, description = :d WHERE id = :i"),
                   {"t": "Old\x01title\x1f", "d": "=cmd\x0b|calc", "i": bug["id"]})
        db.commit()
    res = admin_client.post("/api/reports/export.xlsx", json={"report_key": "item_detail", "filters": {}})
    assert res.status_code == 200, res.text


def test_push_subscribe_is_an_idempotent_upsert(admin_client):
    for _ in range(3):
        res = admin_client.post("/api/push/subscribe", json={"token": "tok-" + "a" * 40, "platform": "web"})
        assert res.status_code == 200, res.text
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models import PushSubscription
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PushSubscription)) == 1


def test_board_timezone_must_be_a_real_zone(admin_client):
    pid = admin_client.get("/api/projects").json()[0]["id"]
    assert admin_client.post(f"/api/agile/projects/{pid}/enable", json={}).status_code == 200
    board_id = admin_client.get(f"/api/agile/projects/{pid}/settings").json()["board_id"]
    board = admin_client.get(f"/api/agile/boards/{board_id}").json()
    bad = admin_client.put(f"/api/agile/boards/{board_id}", json={"timezone": "Mars/Olympus", "version": board["version"]})
    assert bad.status_code == 422
    assert "not a known timezone" in bad.json()["detail"][0]["msg"]
    good = admin_client.put(f"/api/agile/boards/{board_id}", json={"timezone": "Asia/Kolkata", "version": board["version"]})
    assert good.status_code == 200, good.text
    assert good.json()["timezone"] == "Asia/Kolkata"


def test_quick_filter_json_is_capped(admin_client):
    pid = admin_client.get("/api/projects").json()[0]["id"]
    assert admin_client.post(f"/api/agile/projects/{pid}/enable", json={}).status_code == 200
    board_id = admin_client.get(f"/api/agile/projects/{pid}/settings").json()["board_id"]
    huge = {"labels": ["x" * 100] * 200}
    res = admin_client.post(f"/api/agile/boards/{board_id}/quick-filters", json={"name": "Huge", "filter_json": huge})
    assert res.status_code == 422
    assert "at most 16 KB" in res.json()["detail"][0]["msg"]


def test_database_refuses_values_the_app_could_never_read(admin_client):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal

    pid = admin_client.get("/api/projects").json()[0]["id"]
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Guarded"}).json()
    for statement in ("UPDATE bugs SET priority = 'Urgent!!' WHERE id = :i",
                      "UPDATE bugs SET story_points = -3 WHERE id = :i",
                      "UPDATE users SET role = 'root' WHERE id = 1"):
        with SessionLocal() as db, pytest.raises(IntegrityError):
            db.execute(text(statement), {"i": bug["id"]})
            db.commit()


def test_app_name_cannot_inject_markup_or_script(client, monkeypatch):
    import json

    import app.main as main

    monkeypatch.setattr(main.settings, "APP_NAME", 'R&D </title><script>x()</script> "Q\'s"')
    main._html_cache.clear()
    page = client.get("/login.html").text
    assert "<script>x()</script>" not in page and "R&amp;D &lt;/title&gt;" in page
    monkeypatch.setattr(main.settings, "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr(main.settings, "FIREBASE_API_KEY", "k")
    sw = client.get("/firebase-messaging-sw.js").text
    assert "n.title || " + json.dumps(main.settings.APP_NAME) + "," in sw
    main._html_cache.clear()
